from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from reviewer.cli import app
from reviewer.llm.schemas import Finding
from reviewer.runs.models import RunRecord, RunReport
from reviewer.vcs import (
    FixBranch,
    VcsError,
    commit_message,
    create_fix_branch,
    eligible_findings,
    pr_body,
    pr_commands,
)
from reviewer.verify.apply_patch import make_diff
from tests.test_fix import NEW_LINE, OLD_LINE, run_fix
from tests.test_verify import FakeSandbox


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


@pytest.fixture
def reviewed(git_repo: Path):  # type: ignore[no-untyped-def]
    """A git repo reviewed with one verified fix for Gateway.charge."""
    report, _ = run_fix(git_repo)
    assert report.findings[0].verification_status == "verified_fix"
    return git_repo, report


def test_a_verified_fix_becomes_one_commit_on_a_new_branch(reviewed) -> None:  # type: ignore[no-untyped-def]
    repo, report = reviewed
    before = git(repo, "rev-parse", "HEAD")
    fb = create_fix_branch(repo, report)

    assert fb.branch == f"reviewer/fixes-{report.run.id}" and fb.base_branch == "main"
    assert len(fb.applied) == 1 and not fb.skipped
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"  # the checkout is untouched
    assert git(repo, "rev-parse", "HEAD") == before and git(repo, "status", "--porcelain") == ""
    assert OLD_LINE in (repo / "pkg" / "payments.py").read_text()  # main still has the bug

    assert git(repo, "rev-list", "--count", f"main..{fb.branch}") == "1"
    assert NEW_LINE in git(repo, "show", f"{fb.branch}:pkg/payments.py") + "\n"
    message = git(repo, "log", "-1", "--format=%B", fb.branch)
    finding = report.findings[0]
    assert message.startswith("fix: Amount is not validated")
    assert f"Finding: {finding.id}" in message and "Fix status: verified_fix" in message
    assert "co-authored" not in message.lower() and "claude" not in message.lower()


def test_only_verified_fixes_by_default(git_repo: Path) -> None:
    report, _ = run_fix(git_repo, sandbox=FakeSandbox(unavailable="Docker is not running"))
    assert report.findings[0].verification_status == "fix_unverified"
    with pytest.raises(VcsError, match="no verified fixes"):
        create_fix_branch(git_repo, report)
    fb = create_fix_branch(git_repo, report, include_unverified=True)
    assert len(fb.applied) == 1


def test_refuses_unsafe_repository_states(reviewed, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    repo, report = reviewed
    (repo / "scratch.txt").write_text("uncommitted")
    with pytest.raises(VcsError, match="uncommitted"):
        create_fix_branch(repo, report)
    (repo / "scratch.txt").unlink()

    with pytest.raises(VcsError, match="already exists"):
        create_fix_branch(repo, report, branch="main")
    for bad in ("--evil", "a..b", "with space", "bad~name"):
        with pytest.raises(VcsError):
            create_fix_branch(repo, report, branch=bad)
    assert git(repo, "branch", "--list").split() == ["*", "main"]  # nothing was left behind

    git(repo, "commit", "-q", "--allow-empty", "-m", "someone pushed more work")
    with pytest.raises(VcsError, match="reviewed"):
        create_fix_branch(repo, report)

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(VcsError, match="not the top level of a git repository"):
        create_fix_branch(plain, report)


def test_detached_head_is_refused(reviewed) -> None:  # type: ignore[no-untyped-def]
    repo, report = reviewed
    git(repo, "checkout", "-q", "--detach")
    with pytest.raises(VcsError, match="detached HEAD"):
        create_fix_branch(repo, report)


def test_repository_hooks_are_never_run(reviewed, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    repo, report = reviewed
    marker = tmp_path / "hook-ran"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    fb = create_fix_branch(repo, report)
    assert len(fb.applied) == 1 and not marker.exists()


def finding(fid: str, diff: str, **kw: object) -> Finding:
    base = dict(
        id=fid, file="pkg/utils.py", line_start=2, line_end=2, symbol="round_money",
        category="bug", severity="high", confidence=0.9, title=f"Problem {fid}",
        explanation="Because.", evidence="x", chunk_content_hash="h", suggested_diff=diff,
        verification_status="verified_fix",
    )  # fmt: skip
    base.update(kw)
    return Finding(**base)  # type: ignore[arg-type]


def synthetic_report(repo: Path, findings: list[Finding]) -> RunReport:
    run = RunRecord(
        id="run-1", repo_root=str(repo), commit_sha=git(repo, "rev-parse", "HEAD"), dirty=False,
        config_version="x", models={}, prompt_versions={}, started_at="now",
    )  # fmt: skip
    return RunReport(run=run, findings=findings)


def test_fixes_that_conflict_are_skipped_not_forced(git_repo: Path) -> None:
    text = (git_repo / "pkg" / "utils.py").read_text()
    first = make_diff("pkg/utils.py", text, text.replace("round(value, 2)", "round(value, 3)"))
    second = make_diff("pkg/utils.py", text, text.replace("round(value, 2)", "round(value, 4)"))
    report = synthetic_report(
        git_repo, [finding("F-a", first), finding("F-b", second, confidence=0.5)]
    )
    fb = create_fix_branch(git_repo, report)
    assert [a.finding.id for a in fb.applied] == ["F-a"]
    assert [s.finding.id for s in fb.skipped] == ["F-b"]
    assert "does not apply" in fb.skipped[0].reason
    assert git(git_repo, "rev-list", "--count", f"main..{fb.branch}") == "1"


def test_when_nothing_applies_no_branch_is_left_behind(git_repo: Path) -> None:
    stale = make_diff("pkg/utils.py", "def other():\n    pass\n", "def other():\n    return 1\n")
    report = synthetic_report(git_repo, [finding("F-x", stale)])
    with pytest.raises(VcsError, match="none of the fixes could be applied"):
        create_fix_branch(git_repo, report)
    assert git(git_repo, "branch", "--list").split() == ["*", "main"]


def test_eligibility_and_commit_message() -> None:
    fixes = [
        finding("F-low", "d", severity="low", verification_status="verified_fix"),
        finding("F-high", "d", severity="high", verification_status="verified_fix"),
        finding("F-unv", "d", verification_status="fix_unverified"),
        finding("F-bad", "d", verification_status="fix_failed"),
        finding("F-none", "", verification_status="no_fix", suggested_diff=None),
    ]
    run = RunRecord(id="r", repo_root="/", commit_sha="a", dirty=False, config_version="x",
                    models={}, prompt_versions={}, started_at="n")  # fmt: skip
    report = RunReport(run=run, findings=fixes)
    assert [f.id for f in eligible_findings(report)] == ["F-high", "F-low"]  # most severe first
    assert {f.id for f in eligible_findings(report, True)} == {"F-high", "F-low", "F-unv"}
    long_title = finding("F-long", "d", title="x" * 200)
    assert len(commit_message(long_title).splitlines()[0]) <= 72


def test_pr_body_escapes_repository_derived_text(git_repo: Path) -> None:
    f = finding("F-1", "d", title="<script>alert(1)</script> [click](http://evil)")
    report = synthetic_report(git_repo, [f])
    fb = FixBranch(branch="b", base_branch="main")
    from reviewer.vcs import AppliedFix

    fb.applied.append(AppliedFix(f, "abcdef1234567890"))
    body = pr_body(report, fb, [])
    assert "<script>" not in body and "\\<script\\>" in body  # tags are escaped
    assert "[click](" not in body and "\\[click\\]" in body  # so it cannot render as a link
    assert "Please review each commit" in body and "not been tested together" in body
    cmds = pr_commands(git_repo, fb, "main", "T", Path("/tmp/body.md"))
    assert cmds[0][-3:] == ["--set-upstream", "origin", "b"][-3:] and cmds[1][:3] == [
        "gh",
        "pr",
        "create",
    ]
    assert "--head" in cmds[1] and "--body-file" in cmds[1]


# -- commands ---------------------------------------------------------------------------------


def test_cli_fix_branch(reviewed) -> None:  # type: ignore[no-untyped-def]
    repo, _ = reviewed
    result = CliRunner().invoke(app, ["fix-branch", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "nothing was pushed" in result.output and "Amount is not validated" in result.output
    again = CliRunner().invoke(app, ["fix-branch", "--repo", str(repo)])
    assert again.exit_code == 1 and "already exists" in again.output


def test_pr_prints_commands_and_pushes_nothing_by_default(reviewed, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, _ = reviewed
    ran: list[list[str]] = []
    monkeypatch.setattr("reviewer.vcs.run_command", lambda argv: ran.append(argv) or 0)
    result = CliRunner().invoke(app, ["pr", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "git -C" in result.output and "push --set-upstream origin" in result.output
    assert "gh pr create" in result.output and "Not pushed" in result.output
    assert ran == []


def test_pr_create_needs_an_explicit_yes(reviewed, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, report = reviewed
    ran: list[list[str]] = []
    monkeypatch.setattr("reviewer.vcs.run_command", lambda argv: ran.append(argv) or 0)
    runner = CliRunner()

    declined = runner.invoke(
        app, ["pr", "--repo", str(repo), "--create", "--branch", "b1"], input="n\n"
    )
    assert declined.exit_code == 0 and "Not pushed." in declined.output and ran == []
    default = runner.invoke(
        app, ["pr", "--repo", str(repo), "--create", "--branch", "b2"], input="\n"
    )
    assert ran == [] and default.exit_code == 0  # Enter alone is not consent

    accepted = runner.invoke(
        app, ["pr", "--repo", str(repo), "--create", "--branch", "b3"], input="y\n"
    )
    assert accepted.exit_code == 0, accepted.output
    push, create = ran
    assert push[3:] == ["push", "--set-upstream", "origin", "b3"]
    assert create[:3] == ["gh", "pr", "create"] and create[create.index("--base") + 1] == "main"
    assert (
        Path(create[create.index("--body-file") + 1]).read_text().startswith("Fixes for problems")
    )


def test_a_failing_push_is_reported(reviewed, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, _ = reviewed
    monkeypatch.setattr("reviewer.vcs.run_command", lambda argv: 1)
    result = CliRunner().invoke(app, ["pr", "--repo", str(repo), "--create"], input="y\n")
    assert result.exit_code == 1 and "command failed" in result.output
