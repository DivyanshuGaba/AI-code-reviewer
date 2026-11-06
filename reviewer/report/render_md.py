"""Markdown view of a ScanReport. All repository-derived text is escaped."""

from __future__ import annotations

import re

from reviewer.report.build import ScanReport
from reviewer.report.rank import RankedFinding

_ESCAPE_RE = re.compile(r"([\\`*_\[\]<>|#])")


def md_text(text: str) -> str:
    """Escape Markdown and HTML metacharacters in untrusted inline text."""
    return _ESCAPE_RE.sub(r"\\\1", " ".join(text.split()))


def _fence(content: str, minimum: int = 3) -> str:
    longest = max((len(m) for m in re.findall(r"`+", content)), default=0)
    return "`" * max(minimum, longest + 1)


def md_code(text: str) -> str:
    """Inline code span that cannot be broken out of."""
    text = " ".join(text.split())
    tick = "`" * (max((len(m) for m in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{tick}{pad}{text}{pad}{tick}"


def _block(content: str, lang: str = "") -> str:
    fence = _fence(content)
    return f"{fence}{lang}\n{content.rstrip()}\n{fence}"


def _finding(r: RankedFinding) -> list[str]:
    f = r.finding
    where = f.file if f.symbol == "<repo>" else f"{f.file}:{f.line_start}"
    if f.symbol != "<repo>" and f.line_end != f.line_start:
        where += f"-{f.line_end}"
    out = [
        f"### {r.rank}. [{f.severity.upper()}] {md_text(f.title)}",
        "",
        f"- **Location:** {md_code(where)}"
        + ("" if f.symbol == "<repo>" else f" in {md_code(f.symbol)}"),
        f"- **Category:** {f.category} · **Confidence:** {f.confidence:.0%} · "
        f"**Blast radius:** {r.blast_radius} · **Score:** {r.score:.2f}",
    ]
    if f.related_files:
        out.append("- **Also involves:** " + ", ".join(md_code(p) for p in f.related_files))
    if f.related_signals:
        out.append("- **Static signals:** " + ", ".join(md_code(s) for s in f.related_signals))
    if r.duplicates:
        out.append(f"- **Merged duplicates:** {len(r.duplicates)}")
    out += ["", md_text(f.explanation), "", f"**Evidence:** {md_code(f.evidence)}", ""]

    if f.verification_status != "no_fix" and f.suggested_diff:
        out.append(f"**Suggested fix** (`{f.verification_status}`):")
        if r.verification is not None:
            out.append("")
            for check in r.verification.checks:
                detail = f" — {md_text(check.detail)}" if check.detail else ""
                out.append(f"- {check.name}: {check.status}{detail}")
        out += ["", _block(f.suggested_diff, "diff"), ""]
    return out


def render_markdown(scan: ScanReport) -> str:
    run, s = scan.run, scan.summary
    sha = run.commit_sha[:12] + (" (uncommitted changes)" if run.dirty else "")
    lines = [
        "# Code review report",
        "",
        f"- **Repository:** {md_code(run.repo_root)}",
        f"- **Commit:** {md_code(sha)}",
        f"- **Run:** {md_code(run.id)} · {run.status} · generated {scan.generated_at}",
        "",
        "## Summary",
        "",
        f"{s.total} finding(s) from {run.chunks_deep_reviewed} of {run.chunks_total} functions "
        "deep-reviewed. Findings are ranked by severity × confidence × blast radius.",
        "",
    ]
    if s.by_severity:
        lines += ["| Severity | Count |", "|---|---|"]
        lines += [f"| {sev} | {n} |" for sev, n in s.by_severity.items()]
        lines.append("")
    if run.fixes_attempted:
        lines += [
            f"Fixes: {run.fixes_verified} verified, {run.fixes_unverified} unverified, "
            f"{run.fixes_failed} failed, {run.fixes_not_generated} not generated. "
            "*Verified* means the fix applies and breaks nothing the sandbox could check; "
            "it does not prove the problem is gone.",
            "",
        ]
    lines += ["## Findings", ""]
    if not scan.findings:
        lines += ["No findings. Review was precision-oriented: nothing met the confidence bar.", ""]
    for ranked in scan.findings:
        lines += _finding(ranked)

    arch = scan.architecture
    if arch is not None:
        lines += ["## Architecture", ""]
        if arch.cycles:
            lines.append("**Import cycles:**")
            lines += [f"- {' ↔ '.join(md_code(p) for p in cycle)}" for cycle in arch.cycles]
        else:
            lines.append("No import cycles.")
        if arch.hubs:
            lines += [
                "",
                "**Most depended-on modules:** " + ", ".join(md_code(p) for p in arch.hubs),
            ]
        lines += ["", "| Module | Lines | Imported by | Summary |", "|---|---|---|---|"]
        for m in arch.modules:
            lines.append(
                f"| {md_code(m.path)} | {m.lines} | {m.imported_by} | {md_text(m.summary)} |"
            )
        lines.append("")

    lines += [
        "## Run details",
        "",
        f"- Config version {md_code(run.config_version)}; models: "
        + ", ".join(f"{k}={md_code(v)}" for k, v in run.models.items()),
        "- Prompt versions: "
        + ", ".join(f"{k}={md_code(v)}" for k, v in run.prompt_versions.items()),
        f"- Cost: ${run.cost_usd:.4f}",
        f"- Dropped before ranking: {run.findings_dropped_low_confidence} below the confidence "
        f"bar, {run.findings_dropped_out_of_range} outside the reviewed function",
    ]
    if scan.trace_file:
        lines.append(f"- Full model trace: {md_code(scan.trace_file)}")
    return "\n".join(lines) + "\n"
