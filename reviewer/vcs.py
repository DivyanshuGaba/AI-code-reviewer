"""Turn verified fixes into a local branch of commits, and optionally a pull request.

Everything here is local by default. Pushing and opening a pull request happen only when the
caller asks for the exact commands to be run, after showing them. The repository being
changed is the user's own, but its git hooks are still repository-controlled code, so every
git command that could run one is invoked with hooks disabled.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from reviewer.ingest.sources import is_git_root
from reviewer.llm.schemas import SEVERITY_RANK, Finding
from reviewer.report.render_md import md_code, md_text
from reviewer.runs.models import RunReport
from reviewer.verify.apply_patch import PatchError, apply_patch
from reviewer.verify.models import VerificationResult

NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]
ELIGIBLE = ("verified_fix",)
ELIGIBLE_WITH_UNVERIFIED = ("verified_fix", "fix_unverified")


class VcsError(RuntimeError):
    """The repository is not in a state where fixes can be safely turned into commits."""


@dataclass
class AppliedFix:
    finding: Finding
    commit: str


@dataclass
class SkippedFix:
    finding: Finding
    reason: str


@dataclass
class FixBranch:
    branch: str
    base_branch: str
    applied: list[AppliedFix] = field(default_factory=list)
    skipped: list[SkippedFix] = field(default_factory=list)


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *NO_HOOKS, *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise VcsError((proc.stderr or proc.stdout).strip() or f"git {args[0]} failed")
    return proc.stdout.strip()


def eligible_findings(report: RunReport, include_unverified: bool = False) -> list[Finding]:
    allowed = ELIGIBLE_WITH_UNVERIFIED if include_unverified else ELIGIBLE
    chosen = [f for f in report.findings if f.suggested_diff and f.verification_status in allowed]
    return sorted(chosen, key=lambda f: (-SEVERITY_RANK[f.severity], -f.confidence, f.id))


def commit_message(f: Finding) -> str:
    title = " ".join(f.title.split())
    first = f"fix: {title}"
    if len(first) > 72:
        first = first[:69].rstrip() + "..."
    lines = (
        f"{f.file}:{f.line_start}-{f.line_end}"
        if f.line_end != f.line_start
        else (f"{f.file}:{f.line_start}")
    )
    return (
        f"{first}\n\n{' '.join(f.explanation.split())}\n\n"
        f"Finding: {f.id} ({f.severity} {f.category}, confidence {f.confidence:.0%})\n"
        f"Location: {lines} in {f.symbol}\n"
        f"Fix status: {f.verification_status}\n"
    )


def create_fix_branch(
    root: Path,
    report: RunReport,
    *,
    branch: str | None = None,
    include_unverified: bool = False,
) -> FixBranch:
    """Apply each eligible fix as its own commit on a new branch; leave the checkout untouched."""
    root = root.resolve()
    if not is_git_root(root):
        raise VcsError(f"{root} is not the top level of a git repository")
    if _git(root, "status", "--porcelain"):
        raise VcsError("the working tree has uncommitted changes; commit or stash them first")
    head = _git(root, "rev-parse", "HEAD")
    if report.run.commit_sha != head:
        raise VcsError(
            f"HEAD is {head[:10]} but run {report.run.id} reviewed {report.run.commit_sha[:10]}; "
            "check out the reviewed commit first (fixes are diffs against that code)"
        )
    fixes = eligible_findings(report, include_unverified)
    if not fixes:
        wanted = "verified or unverified" if include_unverified else "verified"
        raise VcsError(f"no {wanted} fixes in run {report.run.id}")

    name = branch or f"reviewer/fixes-{report.run.id}"
    _git(root, "check-ref-format", "--branch", name)
    if (
        subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    ):
        raise VcsError(f"branch {name!r} already exists")

    original = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if original == "HEAD":
        raise VcsError(
            "detached HEAD: check out a branch first, so there is something to open a PR against"
        )
    result = FixBranch(branch=name, base_branch=original)
    _git(root, "switch", "--create", name)
    try:
        for finding in fixes:
            try:
                apply_patch(root, finding.suggested_diff or "", {finding.file})
            except PatchError as err:
                result.skipped.append(
                    SkippedFix(finding, f"does not apply on top of earlier fixes: {err}")
                )
                continue
            _git(root, "add", "--", finding.file)
            _git(root, "commit", "--no-verify", "--quiet", "-m", commit_message(finding))
            result.applied.append(AppliedFix(finding, _git(root, "rev-parse", "HEAD")))
    finally:
        _git(root, "switch", original)
    if not result.applied:
        _git(root, "branch", "-D", name)
        raise VcsError(
            "none of the fixes could be applied: " + "; ".join(s.reason for s in result.skipped)
        )
    return result


def pr_title(fb: FixBranch) -> str:
    n = len(fb.applied)
    return f"Reviewer: {n} verified fix{'es' if n != 1 else ''}"


def pr_body(report: RunReport, fb: FixBranch, verifications: list[VerificationResult]) -> str:
    by_id = {v.finding_id: v for v in verifications}
    lines = [
        f"Fixes for problems found by reviewer in run {md_code(report.run.id)} "
        f"(commit {md_code(report.run.commit_sha[:12])}). One commit per fix.",
        "",
    ]
    for item in fb.applied:
        f = item.finding
        v = by_id.get(f.id)
        checks = ", ".join(f"{c.name} {c.status}" for c in v.checks) if v else "no checks recorded"
        lines += [
            f"### {md_text(f.title)}",
            f"- {f.severity} {f.category}, confidence {f.confidence:.0%} · "
            f"{md_code(f'{f.file}:{f.line_start}')} · commit {md_code(item.commit[:10])}",
            f"- Fix status `{f.verification_status}`; checks: {md_text(checks)}",
            "",
            md_text(f.explanation),
            "",
        ]
    if fb.skipped:
        lines += ["### Not included", ""]
        lines += [f"- {md_text(s.finding.title)}: {md_text(s.reason)}" for s in fb.skipped]
        lines.append("")
    lines += [
        "**Please review each commit.** Each fix was checked on its own against the reviewed "
        "commit (it applies, parses, adds no lint or type errors, and breaks no test the sandbox "
        "could run). That does not prove the underlying problem is fixed, and the fixes have not "
        "been tested together.",
    ]
    return "\n".join(lines) + "\n"


def pr_commands(
    root: Path, fb: FixBranch, base: str, title: str, body_file: Path
) -> list[list[str]]:
    return [
        ["git", "-C", str(root), "push", "--set-upstream", "origin", fb.branch],
        [
            "gh", "pr", "create", "--title", title, "--body-file", str(body_file),
            "--base", base, "--head", fb.branch,
        ],
    ]  # fmt: skip


def run_command(argv: list[str]) -> int:
    """Run one command without a shell. Separated out so tests never touch the network."""
    return subprocess.run(argv, check=False).returncode


def execute(commands: list[list[str]], runner: Callable[[list[str]], int] | None = None) -> None:
    run = runner or run_command
    for argv in commands:
        if run(argv) != 0:
            raise VcsError(f"command failed: {' '.join(argv[:4])} ...")
