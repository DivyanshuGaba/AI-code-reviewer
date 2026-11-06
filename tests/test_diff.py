from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from reviewer.cli import app
from reviewer.ingest.diff import DiffError, changed_ranges, overlaps, parse_zero_context_diff
from reviewer.llm.gateway import LLMRequest
from reviewer.pipeline import review_repository
from reviewer.settings import load_settings
from tests.fakes import FakeLLM, finding_json, triage_json

SETTINGS = load_settings()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


def test_parse_zero_context_diff() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -3 +3 @@\n-x\n+y\n"
        "@@ -10,2 +10,3 @@\n-a\n-b\n+c\n+d\n+e\n"
        "@@ -20,2 +22,0 @@\n-gone\n-gone2\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-q\n"
        "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+1\n+2\n"
    )
    assert parse_zero_context_diff(diff) == {
        "a.py": [(3, 3), (10, 12), (22, 22)],  # a pure deletion marks the line where it happened
        "new.py": [(1, 2)],
    }


def test_overlaps() -> None:
    assert overlaps(5, 10, [(10, 12)]) and overlaps(5, 10, [(1, 5)]) and overlaps(5, 10, [(7, 7)])
    assert not overlaps(5, 10, [(11, 12), (1, 4)]) and not overlaps(5, 10, [])


@pytest.fixture
def feature(git_repo: Path) -> Path:
    """main has the fixture; 'feature' changes Gateway.charge (committed) and utils.log."""
    git(git_repo, "switch", "-q", "-c", "feature")
    payments = git_repo / "pkg" / "payments.py"
    payments.write_text(
        payments.read_text().replace("round_money(amount)", "round_money(abs(amount))", 1)
    )
    git(git_repo, "commit", "-q", "-am", "change charge")
    return git_repo


def test_changed_ranges_cover_committed_and_uncommitted_edits(feature: Path) -> None:
    ranges = changed_ranges(feature, "main")
    assert ranges == {"pkg/payments.py": [(6, 6)]}
    utils = feature / "pkg" / "utils.py"
    utils.write_text(utils.read_text().replace("print(message)", "print(message.upper())"))
    assert changed_ranges(feature, "main")["pkg/utils.py"] == [(6, 6)]  # working tree counts


def test_refs_are_validated(feature: Path) -> None:
    for bad in ("--output=/tmp/x", "-q", "a b", "x;y", ""):
        with pytest.raises(DiffError, match="not a valid ref"):
            changed_ranges(feature, bad)
    with pytest.raises(DiffError):
        changed_ranges(feature, "no-such-branch")


def test_shallow_clones_get_an_actionable_error(feature: Path, tmp_path: Path) -> None:
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--no-local", f"file://{feature}", str(clone)],
        check=True, capture_output=True,
    )  # fmt: skip
    git(clone, "fetch", "-q", "--depth", "1", "origin", "main")
    git(clone, "branch", "-q", "main", "FETCH_HEAD")
    with pytest.raises(DiffError, match="fetch-depth"):
        changed_ranges(clone, "main")


def handler(request: LLMRequest) -> str:
    if request.model == SETTINGS.models.triage:
        return triage_json(True)
    return json.dumps({"findings": [finding_json()]})


def test_review_only_touches_functions_that_changed(feature: Path) -> None:
    llm = FakeLLM(handler)
    report = review_repository(str(feature), llm=llm, diff_base="main")
    triaged = {
        line.split("Function: ")[1].split("  (")[0]
        for r in llm.requests
        for line in r.user.splitlines()
        if line.startswith("Function: ")
    }
    assert triaged == {"Gateway.charge"}  # not the other 11 functions in the repo
    assert report.run.diff_base == "main" and report.run.chunks_total == 1
    assert report.architecture is None


def test_a_diff_with_no_changed_functions_costs_nothing(git_repo: Path) -> None:
    git(git_repo, "switch", "-q", "-c", "docs")
    (git_repo / "NOTES.md").write_text("hello\n")
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-q", "-m", "docs")
    llm = FakeLLM(handler)
    report = review_repository(str(git_repo), llm=llm, diff_base="main", repo_level=True)
    assert llm.requests == [] and report.run.chunks_total == 0 and report.run.cost_usd == 0


def test_cli_diff_base(feature: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("reviewer.pipeline.AnthropicGateway", lambda: FakeLLM(handler))
    runner = CliRunner()
    ok = runner.invoke(app, ["review", str(feature), "--diff-base", "main"])
    assert ok.exit_code == 0, ok.output
    assert "1 function(s) changed since main" in ok.output
    bad = runner.invoke(app, ["review", str(feature), "--diff-base", "--output=x"])
    assert bad.exit_code != 0
    missing = runner.invoke(app, ["review", str(feature), "--diff-base", "nope"])
    assert missing.exit_code == 1 and "error:" in missing.output
