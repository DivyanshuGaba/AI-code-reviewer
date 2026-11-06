from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from reviewer.analysis.fixgen import FixOutcome, apply_edits
from reviewer.cli import app
from reviewer.index.models import Chunk
from reviewer.llm.gateway import LLMRequest
from reviewer.llm.schemas import FixEdit, FixResult
from reviewer.pipeline import review_repository
from reviewer.retrieval.context import ContextBundle
from reviewer.settings import load_settings
from reviewer.verify.apply_patch import apply_patch
from tests.fakes import FakeLLM, finding_json, triage_json
from tests.test_verify import PASSING, FakeSandbox

SETTINGS = load_settings()
OLD_LINE = "        amount = round_money(amount)\n"
NEW_LINE = "        amount = round_money(max(amount, 0))\n"


def good_fix() -> dict[str, Any]:
    return {
        "edits": [{"old_text": OLD_LINE, "new_text": NEW_LINE}],
        "explanation": "Clamp negative amounts to zero before charging.",
    }


def make_handler(fix: dict[str, Any] | None = None, severity: str = "high"):  # type: ignore[no-untyped-def]
    def handler(request: LLMRequest) -> str:
        if "fix generation stage" in request.system:
            return json.dumps(fix if fix is not None else good_fix())
        if request.model == SETTINGS.models.triage:
            return triage_json("Function: Gateway.charge" in request.user)
        return json.dumps({"findings": [finding_json(severity=severity)]})

    return handler


def passing_sandbox() -> FakeSandbox:
    sb = FakeSandbox()
    sb.tests = {"base": PASSING, "patched": PASSING}
    return sb


def run_fix(repo: Path, handler=None, sandbox=None, settings=None):  # type: ignore[no-untyped-def]
    llm = FakeLLM(handler or make_handler())
    report = review_repository(
        str(repo),
        llm=llm,
        fix=True,
        sandbox=sandbox or passing_sandbox(),
        file="pkg/payments.py",
        settings=settings,
    )
    return report, llm


def test_fix_is_generated_as_a_valid_diff_and_verified(repo: Path) -> None:
    before = (repo / "pkg" / "payments.py").read_text()
    report, llm = run_fix(repo)
    (finding,) = report.findings

    assert finding.verification_status == "verified_fix"
    assert finding.suggested_diff is not None
    assert f"-{OLD_LINE.rstrip()}" in finding.suggested_diff
    assert f"+{NEW_LINE.rstrip()}" in finding.suggested_diff
    (verification,) = report.verifications
    assert verification.finding_id == finding.id and verification.status == "verified_fix"
    assert [c.name for c in verification.checks] == [
        "apply", "syntax", "lint", "typecheck", "tests",
    ]  # fmt: skip

    run = report.run
    assert (run.fixes_attempted, run.fixes_verified, run.fixes_failed) == (1, 1, 0)
    assert any(c.stage == "fixgen" and c.prompt_version == "v1" for c in report.calls)
    assert llm.count(SETTINGS.models.fixgen) >= 1

    # the user's repository is untouched; the diff is a real patch for it
    assert (repo / "pkg" / "payments.py").read_text() == before
    apply_patch(repo, finding.suggested_diff)
    assert NEW_LINE in (repo / "pkg" / "payments.py").read_text()


def test_failing_checks_mark_the_fix_failed_but_keep_the_diff(repo: Path) -> None:
    sb = passing_sandbox()
    sb.ruff = {"base": [], "patched": ["F821"]}
    report, _ = run_fix(repo, sandbox=sb)
    (finding,) = report.findings
    assert finding.verification_status == "fix_failed" and finding.suggested_diff
    assert report.run.fixes_failed == 1 and report.verifications[0].checks[2].status == "failed"


def test_unavailable_sandbox_leaves_the_fix_unverified(repo: Path) -> None:
    report, _ = run_fix(repo, sandbox=FakeSandbox(unavailable="the Docker daemon is not running"))
    assert report.findings[0].verification_status == "fix_unverified"
    assert report.run.fixes_unverified == 1


@pytest.mark.parametrize(
    "fix, reason",
    [
        ({"edits": [], "explanation": "Cannot be fixed locally."}, "declined"),
        ({"edits": [{"old_text": "not in the file", "new_text": "x"}], "explanation": "e"}, ""),
        ({"edits": [{"old_text": "amount", "new_text": "value"}], "explanation": "e"}, ""),
        ({"edits": [{"old_text": OLD_LINE, "new_text": OLD_LINE}], "explanation": "e"}, ""),
        # exists elsewhere in the file but not inside the target function
        (
            {"edits": [{"old_text": 'log(f"charged', "new_text": 'log(f"paid'}], "explanation": ""},
            "",
        ),
        ("garbage", ""),
    ],
)
def test_bad_or_declined_fixes_produce_no_diff_and_no_verification(
    repo: Path, fix: Any, reason: str
) -> None:
    handler = make_handler(fix) if isinstance(fix, dict) else (lambda r: "not json")
    if fix == "garbage":
        base = make_handler()
        handler = lambda r: "not json" if "fix generation stage" in r.system else base(r)  # noqa: E731
    report, _ = run_fix(repo, handler=handler)
    (finding,) = report.findings
    assert finding.verification_status == "no_fix" and finding.suggested_diff is None
    assert report.verifications == [] and report.run.fixes_not_generated == 1


def test_oversized_fixes_are_rejected(repo: Path) -> None:
    settings = SETTINGS.model_copy(deep=True)
    settings.fix.max_changed_lines = 1
    report, _ = run_fix(repo, settings=settings)
    assert report.findings[0].suggested_diff is None and report.run.fixes_not_generated == 1


def test_only_findings_at_or_above_min_severity_get_fixes(repo: Path) -> None:
    report, llm = run_fix(repo, handler=make_handler(severity="low"))
    assert report.run.fixes_attempted == 0 and report.findings[0].verification_status == "no_fix"
    assert not any("fix generation stage" in r.system for r in llm.requests)


def test_fixes_are_opt_in(repo: Path) -> None:
    llm = FakeLLM(make_handler())
    report = review_repository(str(repo), llm=llm, file="pkg/payments.py")
    assert report.run.fixes_attempted == 0
    assert not any("fix generation stage" in r.system for r in llm.requests)


def test_rerunning_with_fixes_reuses_cached_model_output(repo: Path) -> None:
    run_fix(repo)
    again, llm = run_fix(repo)
    assert llm.requests == [] and again.run.cost_usd == 0
    assert again.findings[0].verification_status == "verified_fix"


def test_apply_edits_refuses_when_the_file_changed_since_indexing() -> None:
    text = "def f():\n    return 1\n"
    chunk = Chunk(
        id="a.py::f:1", path="a.py", start_line=1, end_line=2, language="python", symbol="f",
        content_hash="h", text="def f():\n    return 2", header="# file: a.py",
    )  # fmt: skip
    bundle = ContextBundle(
        target=chunk, target_tokens=1, items=[], excluded=[], token_count=1, token_budget=9
    )
    result = apply_edits(
        text, bundle, FixResult(edits=[FixEdit(old_text="1", new_text="3")], explanation="")
    )
    assert isinstance(result, FixOutcome) and "changed since" in (result.failure or "")


def test_cli_review_fix_shows_status_checks_and_diff(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("reviewer.pipeline.AnthropicGateway", lambda: FakeLLM(make_handler()))
    monkeypatch.setattr("reviewer.pipeline.DockerSandbox", passing_sandbox)
    result = CliRunner().invoke(app, ["review", str(repo), "--file", "pkg/payments.py", "--fix"])
    assert result.exit_code == 0, result.output
    assert "fix: verified_fix" in result.output and "tests: ok" in result.output
    assert "+        amount = round_money(max(amount, 0))" in result.output
    assert "fixes: 1 verified, 0 unverified, 0 failed, 0 not generated" in result.output


def test_cli_verify_checks_a_patch_without_any_llm(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reviewer.verify.apply_patch import make_diff

    text = (repo / "pkg" / "utils.py").read_text()
    patch = tmp_path / "fix.diff"
    patch.write_text(
        make_diff("pkg/utils.py", text, text.replace("round(value, 2)", "round(value, 3)"))
    )
    monkeypatch.setattr("reviewer.cli.DockerSandbox", passing_sandbox)
    runner = CliRunner()
    ok = runner.invoke(app, ["verify", str(repo), str(patch)])
    assert ok.exit_code == 0 and "apply: passed" in ok.output and "verified_fix" in ok.output

    patch.write_text("--- a/pkg/utils.py\n+++ b/pkg/utils.py\n@@ -1 +1 @@\n-nope\n+yes\n")
    bad = runner.invoke(app, ["verify", str(repo), str(patch)])
    assert bad.exit_code == 1 and "fix_failed" in bad.output
