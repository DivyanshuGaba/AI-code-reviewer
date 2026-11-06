from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from reviewer.cli import app
from reviewer.evals.cases import (
    DEFAULT_CASES_DIR,
    BugSpec,
    CaseError,
    ResolvedBug,
    load_cases,
    materialize,
)
from reviewer.evals.models import BugOutcome, CaseResult, EvalResult, FalsePositive, FixEval
from reviewer.evals.render import render_compare, render_eval, render_retrieval
from reviewer.evals.retrieval_eval import run_retrieval_eval
from reviewer.evals.runner import estimate_eval, resolves_by_tests, run_eval
from reviewer.evals.scoring import (
    compute_totals,
    fix_is_exact,
    function_dump,
    match_findings,
)
from reviewer.llm.gateway import LLMRequest
from reviewer.llm.schemas import Finding
from reviewer.settings import load_settings
from reviewer.verify.sandbox import ExecResult
from tests.test_fix import passing_sandbox
from tests.test_verify import FakeSandbox

SETTINGS = load_settings()
CASES = load_cases(DEFAULT_CASES_DIR)


# -- the shipped cases are valid ---------------------------------------------------------------


def run_case_tests(root: Path) -> bool:
    """Run a case's own tests on the host. Only for the cases shipped in this repository."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x"],
        cwd=root, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    return proc.returncode == 0


def test_the_shipped_suite_has_the_promised_shape() -> None:
    bugs = [b for _, spec in CASES for b in spec.bugs]
    assert len(CASES) == 8 and len(bugs) == 17
    assert {b.category for b in bugs} >= {"bug", "security", "performance"}
    assert {b.severity for b in bugs} >= {"critical", "high", "medium", "low"}
    assert any(not spec.bugs for _, spec in CASES)  # a control case with no bugs
    assert len({b.id for _, spec in CASES for b in spec.bugs}) == len(bugs)


@pytest.mark.parametrize("case_dir, spec", CASES, ids=[s.id for _, s in CASES])
def test_clean_variant_passes_its_own_tests(tmp_path: Path, case_dir: Path, spec: Any) -> None:
    mat = materialize(case_dir, spec, tmp_path / "clean", "clean")
    assert mat.seeded == [] and run_case_tests(mat.root)


@pytest.mark.parametrize("case_dir, spec", CASES, ids=[s.id for _, s in CASES])
def test_tests_detect_exactly_the_bugs_flagged_as_test_detectable(
    tmp_path: Path, case_dir: Path, spec: Any
) -> None:
    for bug in spec.bugs:
        single = spec.model_copy(update={"bugs": [bug]})
        mat = materialize(case_dir, single, tmp_path / bug.id, "buggy")
        assert run_case_tests(mat.root) is (not bug.tests_detect), bug.id


def test_ground_truth_is_exact(tmp_path: Path) -> None:
    case_dir, spec = next((d, s) for d, s in CASES if s.id == "pricing")
    mat = materialize(case_dir, spec, tmp_path / "x", "buggy")
    sign = next(b for b in mat.seeded if b.id == "sign")
    line = (mat.root / "pricing.py").read_text().splitlines()[sign.line_start - 1]
    assert "1 + pct / 100" in line and sign.symbol == "apply_discount"
    assert (case_dir / "repo" / "pricing.py").read_text().count("1 - pct / 100") == 1  # untouched


def test_materialize_rejects_ambiguous_or_missing_mutations(tmp_path: Path) -> None:
    case_dir, spec = next((d, s) for d, s in CASES if s.id == "pricing")
    bad = BugSpec(id="x", file="pricing.py", old="return", new="yield", category="bug",
                  severity="low", title="t")  # fmt: skip
    with pytest.raises(CaseError, match="exactly once"):
        materialize(case_dir, spec.model_copy(update={"bugs": [bad]}), tmp_path / "a", "buggy")
    gone = bad.model_copy(update={"file": "nope.py"})
    with pytest.raises(CaseError, match="no such file"):
        materialize(case_dir, spec.model_copy(update={"bugs": [gone]}), tmp_path / "b", "buggy")
    with pytest.raises(CaseError, match="unknown case"):
        load_cases(DEFAULT_CASES_DIR, ["nope"])


# -- scoring -----------------------------------------------------------------------------------


def rb(**kw: Any) -> ResolvedBug:
    base: dict[str, Any] = dict(
        id="b1", case_id="c", file="a.py", category="bug", severity="high", title="t",
        tests_detect=False, relevant=[], line_start=10, line_end=10, symbol="f",
    )  # fmt: skip
    base.update(kw)
    return ResolvedBug(**base)


def fnd(fid: str, start: int, end: int | None = None, **kw: Any) -> Finding:
    base: dict[str, Any] = dict(
        id=fid, file="a.py", line_start=start, line_end=end or start, symbol="f",
        category="bug", severity="high", confidence=0.8, title="t", explanation="e",
        evidence="x", chunk_content_hash="h",
    )  # fmt: skip
    base.update(kw)
    return Finding(**base)


def test_matching_by_line_proximity_then_same_function() -> None:
    bugs = [rb()]
    assert match_findings([fnd("F1", 12)], bugs, []).matched["b1"].id == "F1"  # within slack
    assert match_findings([fnd("F1", 30, symbol="g")], bugs, []).matched == {}  # far, elsewhere
    far_same_function = fnd("F1", 30, symbol="f", category="bug")
    assert match_findings([far_same_function], bugs, []).matched["b1"].id == "F1"
    wrong_category = fnd("F1", 30, category="security")
    assert match_findings([wrong_category], bugs, []).matched == {}
    assert match_findings([fnd("F1", 10, file="other.py")], bugs, []).matched == {}


def test_matching_is_one_to_one_and_prefers_the_closest_finding() -> None:
    bugs = [rb(id="b1", line_start=10, line_end=10), rb(id="b2", line_start=20, line_end=20)]
    result = match_findings([fnd("near", 10), fnd("dup", 11), fnd("other", 20)], bugs, [])
    assert result.matched["b1"].id == "near" and result.matched["b2"].id == "other"
    assert [f.id for f in result.false_positives] == ["dup"]  # a second finding is not a free hit


def test_acceptable_patterns_and_architecture_findings_are_not_false_positives() -> None:
    from reviewer.evals.cases import AcceptableRange

    acceptable = [AcceptableRange(file="a.py", line_start=40, line_end=42)]
    findings = [fnd("deliberate", 41), fnd("arch", 1, symbol="<repo>", category="architecture"),
                fnd("real-fp", 99)]  # fmt: skip
    result = match_findings(findings, [], acceptable)
    assert [f.id for f in result.ignored] == ["deliberate"]
    assert [f.id for f in result.architecture] == ["arch"]
    assert [f.id for f in result.false_positives] == ["real-fp"]


def test_function_dump_ignores_formatting_and_comments() -> None:
    a = "def f(x):\n    return x+1  # inc\n"
    b = "def f( x ):\n\n    return x + 1\n"
    assert function_dump(a, "f") == function_dump(b, "f")
    assert fix_is_exact(a, b, "f") and not fix_is_exact("def f(x):\n    return x+2\n", b, "f")
    cls = "class C:\n    def m(self):\n        return 1\n"
    assert function_dump(cls, "C.m") is not None and function_dump(cls, "C.zz") is None
    assert function_dump("def (:", "f") is None


def outcome(bug_id: str, found: bool, *, reviewed: bool = True, category: str = "bug",
            severity: str = "high", confidence: float = 0.9, correct: bool = True,
            fix: FixEval | None = None) -> BugOutcome:  # fmt: skip
    return BugOutcome(
        case_id="c", bug_id=bug_id, category=category, severity=severity,
        function_reviewed=reviewed, found=found, finding_confidence=confidence if found else None,
        category_correct=correct if found else None, fix=fix or FixEval(),
    )  # fmt: skip


def test_totals_arithmetic() -> None:
    fp = FalsePositive(finding_id="F", file="a.py", line_start=1, title="t", severity="low",
                       confidence=0.5)  # fmt: skip
    case = CaseResult(
        case_id="c", variant="buggy", run_id="r", run_status="completed", functions=10,
        deep_reviewed=4, stage_failures=0, cost_usd=0.5, false_positives=[fp],
        bugs=[
            outcome("1", True, fix=FixEval(attempted=True, exact=True, status="verified_fix",
                                           resolves=True)),
            outcome("2", True, category="security", correct=False,
                    fix=FixEval(attempted=True, exact=False, status="fix_failed", resolves=False)),
            outcome("3", False, reviewed=True, severity="low"),
            outcome("4", False, reviewed=False, severity="low"),
        ],
    )  # fmt: skip
    clean = case.model_copy(update={"variant": "clean", "bugs": [], "false_positives": [fp, fp],
                                    "functions": 20, "cost_usd": 0.25})  # fmt: skip
    t = compute_totals([case, clean])
    assert (t.bugs, t.tp, t.fp, t.fn) == (4, 2, 1, 2)
    assert t.precision == pytest.approx(2 / 3) and t.recall == pytest.approx(0.5)
    assert t.f1 == pytest.approx(2 * (2 / 3) * 0.5 / (2 / 3 + 0.5))
    assert (t.triage_selected.hit, t.triage_selected.total) == (3, 4)
    assert (t.deep_found_given_selected.hit, t.deep_found_given_selected.total) == (2, 3)
    assert (t.category_accuracy.hit, t.category_accuracy.total) == (1, 2)
    assert (t.recall_by_severity["low"].hit, t.recall_by_severity["low"].total) == (0, 2)
    assert (t.fixes_exact.hit, t.fixes_exact.total) == (1, 2)
    assert (t.fixes_verified.hit, t.fixes_verified.total) == (1, 2)
    assert (t.fixes_resolve_tests.hit, t.fixes_resolve_tests.total) == (1, 2)
    assert t.clean_false_positives == 2 and t.fp_per_100_clean_functions == pytest.approx(10.0)
    assert t.cost_usd == pytest.approx(0.75)
    assert t.mean_confidence_tp == pytest.approx(0.9) and t.mean_confidence_fp == pytest.approx(0.5)


def test_totals_with_nothing_to_score_do_not_divide_by_zero() -> None:
    t = compute_totals([])
    assert t.precision is None and t.recall is None and t.f1 is None


# -- an oracle model that answers from the ground truth ----------------------------------------


class Oracle:
    """Acts like a model that has read the answer key, optionally with planted mistakes."""

    def __init__(self, tmp: Path, miss: set[str] = frozenset(), spurious: str | None = None,  # type: ignore[assignment]
                 wrong_category: dict[str, str] | None = None) -> None:  # fmt: skip
        self.miss, self.spurious = miss, spurious
        self.wrong_category = wrong_category or {}
        self.bugs: dict[tuple[str, str], tuple[BugSpec, ResolvedBug]] = {}
        for case_dir, spec in CASES:
            mat = materialize(case_dir, spec, tmp / f"oracle-{spec.id}", "buggy")
            by_id = {b.id: b for b in spec.bugs}
            for resolved in mat.seeded:
                self.bugs[(resolved.file, resolved.symbol)] = (by_id[resolved.id], resolved)

    @staticmethod
    def _code(user: str) -> str:
        return "\n".join(line[7:] for line in user.splitlines() if re.match(r"^\s*\d+\| ", line))

    def _planted(self, user: str, file: str, symbol: str) -> tuple[BugSpec, ResolvedBug] | None:
        entry = self.bugs.get((file, symbol))
        if entry and entry[0].new.strip("\n") in self._code(user):
            return entry
        return None

    def __call__(self, request: LLMRequest) -> str:
        user = request.user
        if "triage stage" in request.system:
            m = re.search(r"File: (\S+)\nFunction: (\S+)", user)
            assert m
            hit = self._planted(user, m.group(1), m.group(2))
            flag = hit is not None or (self.spurious == m.group(2))
            return json.dumps(
                {"needs_review": flag, "suspicion": 0.9 if flag else 0.0, "concerns": []}
            )
        m = re.search(r"File: (\S+)\nTarget: (\S+)  \(lines (\d+)-(\d+)\)", user)
        assert m, request.system[:60]
        file, symbol, start = m.group(1), m.group(2), int(m.group(3))
        hit = self._planted(user, file, symbol)
        if "deep review stage" in request.system:
            if hit and hit[1].id not in self.miss:
                spec, resolved = hit
                category = self.wrong_category.get(resolved.id, resolved.category)
                return json.dumps({"findings": [{
                    "category": category, "severity": resolved.severity, "confidence": 0.9,
                    "line_start": resolved.line_start, "line_end": resolved.line_end,
                    "title": resolved.title, "explanation": "Planted bug.",
                    "evidence": spec.new.strip().splitlines()[0],
                }]})  # fmt: skip
            if not hit and self.spurious == symbol:
                return json.dumps({"findings": [{
                    "category": "bug", "severity": "medium", "confidence": 0.8,
                    "line_start": start, "line_end": start, "title": "Looks odd",
                    "explanation": "Not actually a problem.", "evidence": "x",
                }]})  # fmt: skip
            return json.dumps({"findings": []})
        assert "fix generation stage" in request.system
        if not hit:  # nothing really wrong here: a sensible model declines
            return json.dumps({"edits": [], "explanation": "Nothing to fix."})
        spec = hit[0]
        return json.dumps({"edits": [{"old_text": spec.new, "new_text": spec.old}],
                           "explanation": "Reverts the change."})  # fmt: skip


def run(tmp_path: Path, oracle: Oracle, **kw: Any) -> EvalResult:
    from tests.fakes import FakeLLM

    sandbox = FakeSandbox()
    sandbox.tests = {
        "base": "PASSED tests/test_a.py::t\n", "patched": "PASSED tests/test_a.py::t\n",
    }  # fmt: skip
    return run_eval(
        DEFAULT_CASES_DIR, llm=FakeLLM(oracle), sandbox=sandbox, settings=SETTINGS,
        max_cost_usd=kw.pop("max_cost_usd", 50.0), **kw,
    )  # fmt: skip


def test_a_perfect_oracle_scores_perfectly(tmp_path: Path) -> None:
    result = run(tmp_path, Oracle(tmp_path), include_clean=True)
    t = result.totals
    assert result.status == "completed" and t.bugs == 17
    assert (t.tp, t.fp, t.fn) == (17, 0, 0)
    assert t.precision == t.recall == t.f1 == 1.0
    assert t.triage_selected.hit == 17 and t.deep_found_given_selected.hit == 17
    assert t.category_accuracy.hit == 17
    assert t.fixes_attempted.hit == 17 and t.fixes_exact.hit == 17  # reversing the change
    assert t.fixes_verified.hit == 17  # the fake sandbox reports a passing test suite
    assert t.clean_false_positives == 0 and t.clean_functions > 0
    assert t.cost_usd > 0
    assert {c.variant for c in result.cases} == {"buggy", "clean"}


def test_planted_mistakes_show_up_in_exactly_the_right_metrics(tmp_path: Path) -> None:
    oracle = Oracle(
        tmp_path,
        miss={"sign"},  # never reported
        spurious="slugify",  # flagged in the control case, which has no bugs
        wrong_category={"last-unit": "performance"},
    )
    t = run(tmp_path, oracle).totals
    assert (t.tp, t.fp, t.fn) == (16, 1, 1)
    assert t.precision == pytest.approx(16 / 17) and t.recall == pytest.approx(16 / 17)
    assert t.triage_selected.hit == 17  # the missed bug reached deep review: a deep-review miss
    assert (t.deep_found_given_selected.hit, t.deep_found_given_selected.total) == (16, 17)
    assert (t.category_accuracy.hit, t.category_accuracy.total) == (15, 16)
    assert t.recall_by_severity["high"].hit < t.recall_by_severity["high"].total


def test_a_triage_miss_is_distinguished_from_a_deep_review_miss(tmp_path: Path) -> None:
    class NeverFlags(Oracle):
        def __call__(self, request: LLMRequest) -> str:
            if "triage stage" in request.system:
                return json.dumps({"needs_review": False, "suspicion": 0.0, "concerns": []})
            return super().__call__(request)

    t = run(tmp_path, NeverFlags(tmp_path), fix=False).totals
    # Triage forwards nothing, but a medium Ruff signal (B006, mutable default) still escalates
    # one function to deep review: that is the only bug found.
    assert t.tp == 1 and t.triage_selected.hit == 1 and t.deep_found_given_selected.hit == 1
    assert t.recall == pytest.approx(1 / 17) and t.fn == 16


def test_findings_on_clean_code_are_counted_per_100_functions(tmp_path: Path) -> None:
    from tests.fakes import FakeLLM

    def flag_everything(request: LLMRequest) -> str:
        if "triage stage" in request.system:
            return json.dumps({"needs_review": True, "suspicion": 1.0, "concerns": []})
        m = re.search(r"Target: (\S+)  \(lines (\d+)-", request.user)
        assert m
        line = int(m.group(2))
        return json.dumps({"findings": [{
            "category": "bug", "severity": "low", "confidence": 0.9, "line_start": line,
            "line_end": line, "title": "Noise", "explanation": "Noise.", "evidence": "x",
        }]})  # fmt: skip

    result = run_eval(
        DEFAULT_CASES_DIR, llm=FakeLLM(flag_everything), sandbox=FakeSandbox(), settings=SETTINGS,
        max_cost_usd=50.0, include_clean=True, fix=False, only=["control"],
    )  # fmt: skip
    t = result.totals
    assert t.clean_functions > 0 and t.clean_false_positives == t.clean_functions
    assert t.fp_per_100_clean_functions == pytest.approx(100.0)


def test_the_cost_cap_stops_the_eval_early(tmp_path: Path) -> None:
    result = run(tmp_path, Oracle(tmp_path), max_cost_usd=0.0001, fix=False)
    assert result.status == "stopped_budget" and 0 < len(result.cases) < 8
    assert any("cost cap" in n for n in result.notes)


def test_a_missing_sandbox_is_reported_not_hidden(tmp_path: Path) -> None:
    from tests.fakes import FakeLLM

    result = run_eval(
        DEFAULT_CASES_DIR, llm=FakeLLM(Oracle(tmp_path)),
        sandbox=FakeSandbox(unavailable="the Docker daemon is not running"),
        settings=SETTINGS, max_cost_usd=50.0, only=["pricing"],
    )  # fmt: skip
    assert any("Sandbox unavailable" in n for n in result.notes)
    assert result.totals.fixes_resolve_tests.total == 0
    assert result.totals.fixes_verified.hit == 0  # unverified, never silently "verified"


def test_resolves_by_tests_needs_failing_before_and_passing_after(tmp_path: Path) -> None:
    case_dir, spec = next((d, s) for d, s in CASES if s.id == "pricing")
    mat = materialize(case_dir, spec, tmp_path / "w", "buggy")
    from reviewer.verify.apply_patch import make_diff

    text = (mat.root / "pricing.py").read_text()
    diff = make_diff("pricing.py", text, text.replace("1 + pct", "1 - pct"))

    sb = FakeSandbox()
    sb.tests = {"base": "FAILED t::a\nPASSED t::b\n", "patched": "PASSED t::a\nPASSED t::b\n"}
    assert resolves_by_tests(sb, mat, "pricing.py", diff) is True
    sb.tests = {"base": "FAILED t::a\nPASSED t::b\n", "patched": "PASSED t::a\nFAILED t::b\n"}
    assert resolves_by_tests(sb, mat, "pricing.py", diff) is False  # fixed one, broke another
    sb.tests = {"base": "PASSED t::a\n", "patched": "PASSED t::a\n"}
    assert resolves_by_tests(sb, mat, "pricing.py", diff) is None  # nothing was failing


# -- retrieval eval, rendering, CLI ------------------------------------------------------------


def test_retrieval_eval_reports_every_strategy_and_the_known_ranking() -> None:
    report = run_retrieval_eval(DEFAULT_CASES_DIR)
    assert report.bugs_evaluated == 8 and set(report.configs) >= {"graph", "lexical", "vector"}
    fused, lexical = report.configs["fused (all)"], report.configs["lexical"]
    assert fused["mrr"] >= lexical["mrr"] and fused["recall@3"] >= lexical["recall@3"]
    assert 0.0 <= report.bundle_recall <= 1.0
    assert "| fused (all) |" in render_retrieval(report)


def test_render_eval_and_compare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = run(tmp_path, Oracle(tmp_path), include_clean=True)
    # a fresh home, otherwise the response cache would replay the perfect run's answers
    monkeypatch.setenv("REVIEWER_HOME", str(tmp_path / "second-home"))
    bad = run(tmp_path, Oracle(tmp_path, miss={"sign"}, spurious="slugify"))
    text = render_eval(bad)
    assert "Precision 94%" in text and "Recall 94%" in text and "Missed bugs" in text
    assert "`pricing/sign`" in text and "Looks odd" in text and "## Fixes" in text
    cmp = render_compare(bad, good)
    assert "▲" in cmp and "| precision |" in cmp
    assert "▼" in render_compare(good, bad)
    assert EvalResult.model_validate_json(good.model_dump_json()).totals == good.totals


def test_dry_run_estimates_without_calling_a_model() -> None:
    est = estimate_eval(DEFAULT_CASES_DIR, SETTINGS, include_clean=False, fix=True)
    assert est["bugs"] == 17 and est["functions"] > 30 and 0 < est["estimated_cost_usd"] < 5


def test_cli_eval_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.fakes import FakeLLM

    runner = CliRunner()
    retrieval = runner.invoke(app, ["eval", "retrieval", "--case", "pricing"])
    assert retrieval.exit_code == 0 and "| graph |" in retrieval.output

    dry = runner.invoke(app, ["eval", "run", "--dry-run", "--case", "pricing"])
    assert dry.exit_code == 0 and "No API calls were made" in dry.output

    oracle = Oracle(tmp_path)
    monkeypatch.setattr("reviewer.cli.AnthropicGateway", lambda: FakeLLM(oracle))
    monkeypatch.setattr("reviewer.cli.DockerSandbox", passing_sandbox)
    out = tmp_path / "res"
    done = runner.invoke(app, ["eval", "run", "--case", "pricing", "--case", "inventory",
                               "--out", str(out)])  # fmt: skip
    assert done.exit_code == 0, done.output
    assert "Precision 100%" in done.output and (out / "summary.md").exists()
    saved = EvalResult.model_validate_json((out / "results.json").read_text())
    assert saved.totals.bugs == 4 and saved.totals.tp == 4

    same = runner.invoke(
        app, ["eval", "compare", str(out / "results.json"), str(out / "results.json")]
    )
    assert same.exit_code == 0 and "| precision | 1.00 | 1.00 | = |" in same.output
    unknown = runner.invoke(app, ["eval", "run", "--case", "nope"])
    assert unknown.exit_code == 1 and "unknown case" in unknown.output
    assert runner.invoke(app, ["eval", "compare", "a.json", "b.json"]).exit_code == 1


# -- the tests-flip metric, measured with real pytest on the shipped cases -----------------------


class HostPytestSandbox:
    """Runs commands on the host. ONLY for the trusted fixtures shipped in this repository."""

    name = "host-for-tests-only"

    def check_available(self) -> str | None:
        return None

    def run(self, workdir: Path, argv: list[str], timeout: int) -> ExecResult:
        cmd = [sys.executable, *argv[1:]] if argv[0] == "python" else [
            sys.executable, "-m", argv[0], *argv[1:]
        ]  # fmt: skip
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout,
                              check=False)  # fmt: skip
        return ExecResult(proc.returncode, proc.stdout, proc.stderr)


def seeded(case_id: str, tmp: Path):  # type: ignore[no-untyped-def]
    case_dir, spec = next((d, s) for d, s in CASES if s.id == case_id)
    return spec, materialize(case_dir, spec, tmp / case_id, "buggy")


def fix_diff(mat, file: str, old: str, new: str) -> str:  # type: ignore[no-untyped-def]
    from reviewer.verify.apply_patch import make_diff

    text = (mat.root / file).read_text()
    assert text.count(old) == 1, old
    return make_diff(file, text, text.replace(old, new))


def test_each_fix_is_judged_with_the_other_seeded_bugs_reverted(tmp_path: Path) -> None:
    spec, mat = seeded("pricing", tmp_path)  # two bugs: sign and threshold
    box = HostPytestSandbox()
    sign = next(b for b in spec.bugs if b.id == "sign")
    threshold = next(b for b in spec.bugs if b.id == "threshold")

    sign_fix = fix_diff(mat, "pricing.py", sign.new, sign.old)
    threshold_fix = fix_diff(mat, "pricing.py", threshold.new, threshold.old)
    # Correct fixes resolve their own bug even though the other bug still fails other tests.
    assert resolves_by_tests(box, mat, "pricing.py", sign_fix, [threshold]) is True
    assert resolves_by_tests(box, mat, "pricing.py", threshold_fix, [sign]) is True
    # Without isolating, the same correct fix looks like a failure: the original flaw.
    assert resolves_by_tests(box, mat, "pricing.py", sign_fix, []) is False


def test_a_fix_that_does_not_fix_the_bug_is_not_counted(tmp_path: Path) -> None:
    spec, mat = seeded("pricing", tmp_path)
    box = HostPytestSandbox()
    sign = next(b for b in spec.bugs if b.id == "sign")
    others = [b for b in spec.bugs if b.id != "sign"]
    harmless = fix_diff(mat, "pricing.py", "round(price", "round( price")  # reformat only
    wrong = fix_diff(mat, "pricing.py", sign.new, "price * (1 - pct / 10)")  # a different bug
    assert resolves_by_tests(box, mat, "pricing.py", harmless, others) is False
    assert resolves_by_tests(box, mat, "pricing.py", wrong, others) is False


def test_bugs_the_tests_cannot_see_are_not_measured(tmp_path: Path) -> None:
    spec, mat = seeded("userdb", tmp_path)
    timing = next(b for b in spec.bugs if b.id == "timing")
    assert not timing.tests_detect
    fix = fix_diff(mat, "users.py", timing.new, timing.old)
    others = [b for b in spec.bugs if b.id != "timing"]
    assert resolves_by_tests(HostPytestSandbox(), mat, "users.py", fix, others) is None


def test_a_perfect_oracle_resolves_every_test_detectable_bug(tmp_path: Path) -> None:
    from tests.fakes import FakeLLM

    result = run_eval(
        DEFAULT_CASES_DIR, llm=FakeLLM(Oracle(tmp_path)), sandbox=HostPytestSandbox(),
        settings=SETTINGS, max_cost_usd=50.0, only=["pricing", "config", "inventory", "cache"],
    )  # fmt: skip
    detectable = sum(
        b.tests_detect
        for _, s in CASES
        if s.id in {"pricing", "config", "inventory", "cache"}
        for b in s.bugs
    )
    t = result.totals
    assert t.fixes_resolve_tests.total == detectable == 9
    assert t.fixes_resolve_tests.hit == detectable  # was 1 of N before bugs were isolated
