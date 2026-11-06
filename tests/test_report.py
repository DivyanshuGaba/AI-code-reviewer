from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from reviewer.analysis.depgraph import find_cycles, hubs, reverse, transitive_dependents
from reviewer.cli import app
from reviewer.index.graph import file_dependencies
from reviewer.index.parser import parse_python
from reviewer.index.store import Store
from reviewer.llm.gateway import LLMRequest
from reviewer.llm.schemas import Finding
from reviewer.pipeline import generate_reports, index_repository, review_repository
from reviewer.report.build import ScanReport, build_scan_report
from reviewer.report.rank import function_blast_radius, rank_findings, score
from reviewer.report.render_html import render_html
from reviewer.report.render_md import md_code, render_markdown
from reviewer.report.render_sarif import render_sarif
from reviewer.settings import load_settings
from reviewer.verify.apply_patch import apply_patch, parse_patch
from tests.fakes import FakeLLM, finding_json, triage_json
from tests.test_fix import good_fix, passing_sandbox

SETTINGS = load_settings()


# -- dependency graph --------------------------------------------------------------------


def test_file_dependencies_resolve_imports_between_indexed_files(repo: Path) -> None:
    report = index_repository(str(repo), signal_tools=())
    with Store(report.db_path) as store:
        deps = file_dependencies(store.load_imports(), set(store.file_lines()))
    assert deps["pkg/payments.py"] == {"pkg/utils.py"}
    assert deps["pkg/orders.py"] == {"pkg/payments.py"}
    assert deps["pkg/__init__.py"] == {"pkg/orders.py"}
    assert deps["app.py"] == {"pkg/__init__.py", "pkg/orders.py"}
    assert deps["pkg/utils.py"] == set()


def test_cycles_hubs_and_transitive_dependents() -> None:
    deps = {"a": {"b"}, "b": {"a", "c"}, "c": set(), "d": {"c"}, "e": {"c"}, "f": {"c", "d"}}
    assert find_cycles(deps) == [["a", "b"]]
    assert find_cycles({"x": {"y"}, "y": {"z"}, "z": set()}) == []
    assert find_cycles({"x": {"y"}, "y": {"z"}, "z": {"x"}}) == [["x", "y", "z"]]
    rdeps = reverse(deps)
    assert hubs(rdeps, min_dependents=3) == ["c"]
    assert transitive_dependents(rdeps, "c", depth=1) == {"b", "d", "e", "f"}
    assert transitive_dependents(rdeps, "c", depth=2) == {"a", "b", "d", "e", "f"}


# -- ranking -----------------------------------------------------------------------------


def make_finding(store: Store, symbol: str, **kw: Any) -> Finding:
    (sym,) = store.find_symbols(symbol)
    base: dict[str, Any] = dict(
        id=f"F-{symbol}-{kw.get('line_start', 1)}-{kw.get('category', 'bug')}",
        file=sym.path, line_start=sym.start_line, line_end=sym.start_line, symbol=sym.qualname,
        category="bug", severity="high", confidence=0.9, title=f"Problem in {symbol}",
        explanation="e", evidence="x", chunk_content_hash="h", chunk_id=sym.id,
    )  # fmt: skip
    base.update(kw)
    return Finding(**base)


def test_score_formula() -> None:
    assert score("high", 0.9, 0) == pytest.approx(3.6)
    assert score("high", 0.9, 3) == pytest.approx(4 * 0.9 * (1 + math.log2(4)))
    assert score("critical", 1.0, 0) > score("high", 1.0, 0) > score("low", 1.0, 0)


def test_blast_radius_counts_transitive_non_test_callers(repo: Path) -> None:
    report = index_repository(str(repo), signal_tools=())
    with Store(report.db_path) as store:
        from reviewer.report.rank import _callers_index

        reverse_calls = _callers_index(store)
        round_money = store.find_symbols("round_money")[0].id
        total = store.find_symbols("OrderBook.total")[0].id
        charge = store.find_symbols("Gateway.charge")[0].id
        # callers of round_money: charge, refund -> their callers: create_order, cancel_order,
        # test_charge_rounds (a test: not counted) -> main ...
        assert function_blast_radius(reverse_calls, round_money) >= 5
        assert function_blast_radius(reverse_calls, total) < function_blast_radius(
            reverse_calls, round_money
        )
        callers_of_charge = function_blast_radius(reverse_calls, charge)
        assert callers_of_charge >= 2  # create_order, main (+ module level), no tests


def test_ranking_orders_by_score_and_merges_overlapping_duplicates(repo: Path) -> None:
    report = index_repository(str(repo), signal_tools=())
    with Store(report.db_path) as store:
        hub = make_finding(store, "round_money", confidence=0.8)
        leaf = make_finding(store, "OrderBook.total", severity="critical", confidence=0.9)
        dup_a = make_finding(store, "Gateway.charge", title="Negative amount charged",
                             related_signals=["ruff:F1"])  # fmt: skip
        dup_b = make_finding(store, "Gateway.charge", title="No validation of amount",
                             confidence=0.7, related_signals=["bandit:B1"],
                             id="F-dup-b")  # fmt: skip
        other_category = make_finding(store, "Gateway.charge", category="security",
                                      id="F-sec", title="Different category")  # fmt: skip
        ranked = rank_findings([hub, leaf, dup_a, dup_b, other_category], [], store)

    assert [r.rank for r in ranked] == list(range(1, len(ranked) + 1))
    assert [r.score for r in ranked] == sorted((r.score for r in ranked), reverse=True)
    ids = [r.finding.id for r in ranked]
    assert "F-dup-b" not in ids and "F-sec" in ids  # same lines + category merge; others stay
    merged = next(r for r in ranked if r.finding.id == dup_a.id)
    assert merged.duplicates == ["F-dup-b"]
    assert set(merged.finding.related_signals) == {"ruff:F1", "bandit:B1"}
    assert ranked[0].blast_radius >= 0
    by_id = {r.finding.id: r for r in ranked}
    # a modest finding in a widely used helper can outrank its raw severity
    assert by_id[hub.id].blast_radius > by_id[leaf.id].blast_radius


def test_architecture_findings_on_one_file_are_only_merged_when_titles_match(
    repo: Path,
) -> None:
    report = index_repository(str(repo), signal_tools=())
    with Store(report.db_path) as store:

        def arch(title: str, fid: str) -> Finding:
            return Finding(
                id=fid, file="pkg/orders.py", line_start=1, line_end=1, symbol="<repo>",
                category="architecture", severity="medium", confidence=0.8, title=title,
                explanation="e", evidence="x", chunk_content_hash="h",
            )  # fmt: skip

        ranked = rank_findings(
            [
                arch("Circular import between orders and payments", "A1"),
                arch("Circular import between orders and payments modules", "A2"),
                arch("God module with too many responsibilities", "A3"),
            ],
            [],
            store,
        )
    kept = sorted(r.finding.id for r in ranked)
    assert len(kept) == 2 and "A3" in kept and ("A1" in kept) != ("A2" in kept)


# -- repo-level pass and the full scan ----------------------------------------------------


def handler(request: LLMRequest) -> str:
    system = request.system
    if "one-sentence summaries" in system:
        return json.dumps({"summary": "Handles a slice of the order flow."})
    if "repo-level stage" in system:
        return json.dumps(
            {
                "findings": [
                    {
                        "severity": "medium", "confidence": 0.8,
                        "title": "Orders reach into payments internals",
                        "explanation": "orders imports Gateway directly instead of a narrow API.",
                        "evidence": "pkg/orders.py -> pkg/payments.py",
                        "files": ["pkg/orders.py", "pkg/payments.py", "pkg/not_a_file.py"],
                    },
                    {  # only unknown files: dropped
                        "severity": "high", "confidence": 0.9, "title": "Ghost problem",
                        "explanation": "no such module", "evidence": "x", "files": ["nope.py"],
                    },
                    {  # below the confidence bar: dropped
                        "severity": "low", "confidence": 0.2, "title": "Maybe",
                        "explanation": "not sure about it", "evidence": "x", "files": ["app.py"],
                    },
                ]
            }
        )  # fmt: skip
    if "fix generation stage" in system:
        return json.dumps(good_fix())
    if request.model == SETTINGS.models.triage:
        return triage_json("Function: Gateway.charge" in request.user)
    return json.dumps({"findings": [finding_json()]})


def scan_run(repo: Path, **kw: Any):  # type: ignore[no-untyped-def]
    llm = FakeLLM(handler)
    report = review_repository(
        str(repo), llm=llm, repo_level=True, fix=True, sandbox=passing_sandbox(), **kw
    )
    return report, llm


def test_repo_level_pass_summarises_modules_and_reports_architecture_findings(repo: Path) -> None:
    report, llm = scan_run(repo)
    arch = [f for f in report.findings if f.symbol == "<repo>"]
    assert [f.title for f in arch] == ["Orders reach into payments internals"]
    f = arch[0]
    assert (f.category, f.file, f.related_files) == (
        "architecture", "pkg/orders.py", ["pkg/payments.py"],  # unknown file discarded
    )  # fmt: skip
    assert f.chunk_content_hash and f.chunk_id == ""

    facts = report.architecture
    assert facts is not None and facts.cycles == [] and facts.edges == 5
    by_path = {m.path: m for m in facts.modules}
    assert by_path["pkg/utils.py"].summary and by_path["pkg/utils.py"].imported_by == 1
    assert by_path["pkg/__init__.py"].summary == ""  # no top-level defs: not summarised
    assert "tests/check_orders.py" not in by_path

    stages = {c.stage for c in report.calls}
    assert {"module_summary", "repo_level", "triage", "deep_review", "fixgen"} <= stages
    # fixes are only for function-level findings
    assert report.run.fixes_attempted == 1


def test_repo_level_pass_is_opt_in_and_skipped_for_partial_scans(repo: Path) -> None:
    plain = review_repository(str(repo), llm=FakeLLM(handler))
    assert plain.architecture is None and not any(c.stage == "repo_level" for c in plain.calls)
    partial = review_repository(
        str(repo), llm=FakeLLM(handler), repo_level=True, file="pkg/payments.py"
    )
    assert partial.architecture is None


def test_tiny_repos_skip_the_architecture_pass(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    report = review_repository(str(tmp_path), llm=FakeLLM(handler), repo_level=True)
    assert not any(c.stage == "repo_level" for c in report.calls)


def build_scan(repo: Path) -> tuple[ScanReport, Path]:
    report, _ = scan_run(repo)
    report_db = index_repository(str(repo), signal_tools=()).db_path
    with Store(report_db) as store:
        scan = build_scan_report(report, store, trace_file=Path("/tmp/trace.json"))
    return scan, report_db


def test_scan_report_summary_and_json_round_trip(repo: Path) -> None:
    scan, _ = build_scan(repo)
    assert scan.summary.total == 2 and scan.summary.by_severity == {"high": 1, "medium": 1}
    assert scan.summary.by_fix_status == {"verified_fix": 1, "no_fix": 1}
    assert [r.rank for r in scan.findings] == [1, 2]
    assert ScanReport.model_validate_json(scan.model_dump_json()) == scan
    top = scan.findings[0]
    assert top.finding.symbol == "Gateway.charge" and top.verification is not None


def test_markdown_report(repo: Path) -> None:
    scan, _ = build_scan(repo)
    md = render_markdown(scan)
    assert md.startswith("# Code review report")
    assert "### 1. [HIGH] Amount is not validated" in md
    assert "```diff" in md and "+        amount = round_money(max(amount, 0))" in md
    assert "lint: passed" in md and "tests: passed" in md
    assert "## Architecture" in md and "No import cycles." in md
    assert "Orders reach into payments internals" in md


def test_markdown_escapes_untrusted_text_and_cannot_be_fenced_out() -> None:
    from reviewer.report.render_md import _block, md_text

    assert "<script>" not in md_text("<script>alert(1)</script> [x](http://e.vil) *b*")
    assert md_text("a\nb   c") == "a b c"
    tricky = "x = '```'; y = ````"
    span = md_code(tricky)
    assert span.startswith("`````") and span.endswith("`````")  # longer than any run inside
    assert md_code("plain") == "`plain`"
    block = _block("```\nbreak out\n```", "diff")
    assert block.startswith("````diff") and block.endswith("````")


def test_html_report_is_static_escaped_and_self_contained(repo: Path) -> None:
    scan, _ = build_scan(repo)
    evil = scan.findings[0].model_copy(deep=True)
    evil.finding = evil.finding.model_copy(
        update={
            "title": "<script>alert(1)</script><img src=x onerror=alert(2)>",
            "evidence": '"><svg onload=alert(3)>',
            "suggested_diff": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-<b>old</b>\n+<i>new</i>\n",
        }
    )
    scan = scan.model_copy(update={"findings": [evil]})
    page = render_html(scan)

    assert "Content-Security-Policy" in page and "default-src 'none'" in page
    assert "<script" not in page.lower() and "&lt;script&gt;" in page
    # injected markup is inert text: the tags are escaped, so no element can be created
    assert "<img" not in page and "<svg" not in page
    assert "&lt;img src=x onerror=alert(2)&gt;" in page and "&lt;svg onload=alert(3)&gt;" in page
    assert 'class="add"' in page and 'class="del"' in page and "<details>" in page
    import re

    # no real element loads or links anything external
    assert not re.search(r"<[^>]*\b(src|href|action)\s*=", page)
    assert "<h1>Code review report</h1>" in page


def apply_sarif_fix(original: str, result: dict[str, Any]) -> str:
    (fix,) = result["fixes"]
    (change,) = fix["artifactChanges"]
    lines = original.splitlines(keepends=True)
    for rep in sorted(change["replacements"], key=lambda r: -r["deletedRegion"]["startLine"]):
        region = rep["deletedRegion"]
        start = region["startLine"] - 1
        end = region.get("endLine", region["startLine"] - 1)
        count = 0 if "startColumn" in region else end - start
        lines[start : start + count] = [rep["insertedContent"]["text"]]
    return "".join(lines)


def test_sarif_structure_and_fix_replacements(repo: Path) -> None:
    scan, _ = build_scan(repo)
    sarif = render_sarif(scan)
    json.dumps(sarif)  # serialisable
    assert sarif["version"] == "2.1.0" and sarif["$schema"].endswith("sarif-2.1.0.json")
    (run,) = sarif["runs"]
    assert run["tool"]["driver"]["name"] == "reviewer"
    rules = run["tool"]["driver"]["rules"]
    assert len(run["results"]) == 2
    for result in run["results"]:
        assert rules[result["ruleIndex"]]["id"] == result["ruleId"]
        assert result["level"] in {"error", "warning", "note"}
        region = result["locations"][0]["physicalLocation"]["region"]
        assert isinstance(region["startLine"], int) and region["endLine"] >= region["startLine"]
        assert result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == (
            result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"].lstrip("/")
        )
        assert result["partialFingerprints"]["reviewerFindingId/v1"].startswith("F-")
    bug = next(r for r in run["results"] if r["ruleId"] == "reviewer/bug")
    arch = next(r for r in run["results"] if r["ruleId"] == "reviewer/architecture")
    assert bug["level"] == "error" and arch["level"] == "warning"
    assert arch["relatedLocations"][0]["physicalLocation"]["artifactLocation"]["uri"] == (
        "pkg/payments.py"
    )
    assert "fixes" not in arch

    # the SARIF replacement produces exactly the file the diff produces
    original = (repo / "pkg" / "payments.py").read_text()
    via_sarif = apply_sarif_fix(original, bug)
    apply_patch(repo, scan.findings[0].finding.suggested_diff or "")
    assert (repo / "pkg" / "payments.py").read_text() == via_sarif
    assert run["invocations"][0]["properties"]["runId"] == scan.run.id
    assert "versionControlProvenance" not in run  # needs a repositoryUri GitHub insists on


def test_sarif_omits_fixes_for_failed_and_missing_fixes(repo: Path) -> None:
    scan, _ = build_scan(repo)
    failed = scan.findings[0].model_copy(deep=True)
    failed.finding = failed.finding.model_copy(update={"verification_status": "fix_failed"})
    result = render_sarif(scan.model_copy(update={"findings": [failed]}))["runs"][0]["results"][0]
    assert "fixes" not in result and result["properties"]["verificationStatus"] == "fix_failed"
    assert parse_patch(scan.findings[0].finding.suggested_diff or "")  # sanity


# -- commands ----------------------------------------------------------------------------


def test_cli_scan_writes_all_four_reports_and_report_rerenders(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("reviewer.pipeline.AnthropicGateway", lambda: FakeLLM(handler))
    monkeypatch.setattr("reviewer.pipeline.DockerSandbox", passing_sandbox)
    out = tmp_path / "out"
    runner = CliRunner()
    result = runner.invoke(app, ["scan", str(repo), "--out", str(out)])
    assert result.exit_code == 0, result.output
    for name in ("report.json", "report.md", "report.sarif", "report.html"):
        assert (out / name).stat().st_size > 0, name
    assert "Amount is not validated" in result.output and "fix: verified_fix" in result.output
    assert json.loads((out / "report.sarif").read_text())["version"] == "2.1.0"
    scan = ScanReport.model_validate_json((out / "report.json").read_text())
    assert scan.trace_file and Path(scan.trace_file).exists()

    again = tmp_path / "again"
    rerun = runner.invoke(app, ["report", "--repo", str(repo), "--out", str(again)])
    assert rerun.exit_code == 0, rerun.output
    assert (again / "report.md").read_text().splitlines()[0] == "# Code review report"
    assert (
        ScanReport.model_validate_json((again / "report.json").read_text()).summary == scan.summary
    )


def test_cli_scan_no_fix_and_report_errors(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    nothing = runner.invoke(app, ["report", "--repo", str(repo)])
    assert nothing.exit_code == 1 and "no index" in nothing.output

    index_repository(str(repo), signal_tools=())
    no_runs = runner.invoke(app, ["report", "--repo", str(repo)])
    assert no_runs.exit_code == 1 and "no runs yet" in no_runs.output

    monkeypatch.setattr("reviewer.pipeline.AnthropicGateway", lambda: FakeLLM(handler))
    done = runner.invoke(app, ["scan", str(repo), "--no-fix", "--out", str(tmp_path / "o")])
    assert done.exit_code == 0 and "fix:" not in done.output
    missing = runner.invoke(app, ["report", "--repo", str(repo), "--run", "nope"])
    assert missing.exit_code == 1 and "no such run" in missing.output


def test_generate_reports_defaults_to_the_home_reports_directory(
    repo: Path, reviewer_home: Path
) -> None:
    report, _ = scan_run(repo)
    scan, paths = generate_reports(str(repo), report=report)
    assert all(p.is_relative_to(reviewer_home / "reports" / report.run.id) for p in paths.values())
    assert scan.run.id == report.run.id


def test_parser_import_for_dependency_tests_is_available() -> None:
    assert parse_python("x.py", b"import os\n").imports  # guards the fixture helpers above


def test_sarif_validates_against_the_official_schema_when_available(repo: Path) -> None:
    """Set REVIEWER_SARIF_SCHEMA to the OASIS sarif-2.1.0 schema file (and install jsonschema)
    to run this; GitHub's validator rejected an earlier version that offline structural tests
    accepted."""
    import os

    schema_path = os.environ.get("REVIEWER_SARIF_SCHEMA")
    jsonschema = pytest.importorskip("jsonschema")
    if not schema_path:
        pytest.skip("REVIEWER_SARIF_SCHEMA not set")
    schema = json.loads(Path(schema_path).read_text())
    scan, _ = build_scan(repo)
    errors = list(jsonschema.Draft7Validator(schema).iter_errors(render_sarif(scan)))
    assert errors == [], [e.message for e in errors[:3]]
