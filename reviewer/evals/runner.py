"""Run the review pipeline over the eval cases and score the result."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from reviewer.evals.cases import BugSpec, CaseSpec, Materialized, Variant, load_cases, materialize
from reviewer.evals.models import (
    BugOutcome,
    CaseResult,
    EvalResult,
    FalsePositive,
    FixEval,
    Totals,
)
from reviewer.evals.scoring import (
    bug_outcome_for,
    compute_totals,
    fix_is_exact,
    match_findings,
)
from reviewer.index.store import Store
from reviewer.ingest.sources import home_dir
from reviewer.llm.gateway import LLM, PRICING
from reviewer.llm.schemas import Finding
from reviewer.pipeline import db_path_for, index_repository, review_repository
from reviewer.retrieval.budget import chunk_tokens
from reviewer.settings import Settings
from reviewer.verify.apply_patch import PatchError, apply_to_text, parse_patch
from reviewer.verify.checks import parse_pytest
from reviewer.verify.sandbox import Sandbox

PYTEST = ["python", "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider", "-rA"]


def work_dir(case_id: str, variant: Variant) -> Path:
    """Stable per-case path, so repeated eval runs share the response cache."""
    return home_dir() / "evals" / "work" / f"{case_id}-{variant}"


def fix_is_exact_for(mat: Materialized, file: str, qualname: str, diff: str) -> bool:
    try:
        (patch,) = parse_patch(diff)
        patched = apply_to_text((mat.root / file).read_text(), patch)
    except (PatchError, ValueError):
        return False
    return fix_is_exact(patched, (mat.clean_dir / file).read_text(), qualname)


def _revert_bugs(root: Path, bugs: list[BugSpec]) -> None:
    """Put the original code back for the given seeded bugs."""
    for bug in bugs:
        path = root / bug.file
        text = path.read_text()
        if text.count(bug.new) == 1:
            path.write_text(text.replace(bug.new, bug.old, 1))


def resolves_by_tests(
    sandbox: Sandbox,
    mat: Materialized,
    file: str,
    diff: str,
    other_bugs: list[BugSpec] | None = None,
) -> bool | None:
    """Did the case's own tests go from failing to passing because of this fix, and nothing else?

    A case can seed several bugs, and a fix for one cannot make tests that fail because of
    another one pass. So the other seeded bugs are reverted in both copies (after the diff is
    applied, since the diff was written against the fully seeded code), leaving exactly one
    bug between the baseline and the patched copy.

    None when it cannot be measured (the bug is not test-detectable or tests did not run).
    """
    from reviewer.verify.apply_patch import apply_patch

    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache")
    with tempfile.TemporaryDirectory(prefix="reviewer-eval-") as tmp:
        base, patched = Path(tmp) / "base", Path(tmp) / "patched"
        shutil.copytree(mat.root, base, ignore=ignore)
        shutil.copytree(mat.root, patched, ignore=ignore)
        try:
            apply_patch(patched, diff, {file})
        except PatchError:
            return False
        _revert_bugs(base, other_bugs or [])
        _revert_bugs(patched, other_bugs or [])
        before = parse_pytest(sandbox.run(base, PYTEST, 300).stdout)
        after = parse_pytest(sandbox.run(patched, PYTEST, 300).stdout)
    failing = {t for t, s in before.items() if s != "PASSED"}
    if not failing:
        return None
    return all(after.get(t) == "PASSED" for t in before)


def score_case(
    mat: Materialized,
    spec: CaseSpec,
    findings: list[Finding],
    deep_targets: set[str],
    store: Store,
    sandbox: Sandbox | None,
    sandbox_ok: bool,
) -> tuple[list[BugOutcome], list[FalsePositive], int, int]:
    matches = match_findings(findings, mat.seeded, mat.acceptable)
    outcomes = []
    for bug in mat.seeded:
        chunk = store.chunk_at(bug.file, bug.line_start)
        finding = matches.matched.get(bug.id)
        outcome = bug_outcome_for(bug, finding, bool(chunk and chunk.id in deep_targets))
        if finding is not None and finding.suggested_diff:
            outcome.fix = FixEval(
                attempted=True,
                exact=fix_is_exact_for(mat, bug.file, bug.symbol, finding.suggested_diff),
                status=finding.verification_status,
            )
            if sandbox_ok and sandbox is not None and bug.tests_detect and mat.tests_dir_present:
                outcome.fix.resolves = resolves_by_tests(
                    sandbox,
                    mat,
                    bug.file,
                    finding.suggested_diff,
                    [b for b in spec.bugs if b.id != bug.id],
                )
        outcomes.append(outcome)
    false_positives = [
        FalsePositive(
            finding_id=f.id,
            file=f.file,
            line_start=f.line_start,
            title=f.title,
            severity=f.severity,
            confidence=f.confidence,
        )  # fmt: skip
        for f in matches.false_positives
    ]
    return outcomes, false_positives, len(matches.ignored), len(matches.architecture)


def run_eval(
    cases_dir: Path,
    *,
    llm: LLM,
    sandbox: Sandbox,
    settings: Settings,
    max_cost_usd: float,
    include_clean: bool = False,
    fix: bool = True,
    only: list[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> EvalResult:
    say = progress or (lambda _m: None)
    cases = load_cases(cases_dir, only)
    sandbox_ok = fix and sandbox.check_available() is None
    result = EvalResult(
        eval_id=f"{datetime.now(UTC):%Y%m%dT%H%M%S}",
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        config_version=settings.config_version,
        models=settings.models.model_dump(),
        prompt_versions=settings.prompts.model_dump(),
        status="completed",
        fix_enabled=fix,
        cases=[],
        totals=Totals(),
    )
    if fix and not sandbox_ok:
        result.notes.append(
            f"Sandbox unavailable ({sandbox.check_available()}): fix correctness by tests was not "
            "measured and fixes cannot reach verified_fix."
        )

    spent = 0.0
    for case_dir, spec in cases:
        variants: list[Variant] = ["buggy", "clean"] if include_clean else ["buggy"]
        for variant in variants:
            if spent >= max_cost_usd:
                result.status = "stopped_budget"
                result.notes.append(f"Stopped before {spec.id}/{variant}: cost cap reached.")
                break
            say(f"{spec.id} ({variant})")
            mat = materialize(case_dir, spec, work_dir(spec.id, variant), variant)
            case_settings = settings.model_copy(deep=True)
            case_settings.review.max_cost_usd = min(
                settings.review.max_cost_usd, max_cost_usd - spent
            )
            # Measure fix quality on every found bug, not just those above the production cut-off.
            case_settings.fix.min_severity = "info"
            report = review_repository(
                str(mat.root), llm=llm, fix=fix and variant == "buggy",
                sandbox=sandbox, settings=case_settings,
            )  # fmt: skip
            deep_targets = {c.target for c in report.calls if c.stage == "deep_review"}
            with Store(db_path_for(mat.root)) as store:
                bugs, fps, ignored, arch = score_case(
                    mat, spec, report.findings, deep_targets, store, sandbox, sandbox_ok
                )
            spent += report.run.cost_usd
            result.cases.append(
                CaseResult(
                    case_id=spec.id,
                    variant=variant,
                    run_id=report.run.id,
                    run_status=report.run.status,
                    functions=report.run.chunks_total,
                    deep_reviewed=report.run.chunks_deep_reviewed,
                    stage_failures=report.run.stage_failures,
                    cost_usd=report.run.cost_usd,
                    bugs=bugs,
                    false_positives=fps,
                    ignored_acceptable=ignored,
                    architecture_findings=arch,
                )  # fmt: skip
            )
            if report.run.status == "failed":
                result.status = "failed"
                result.notes.append(f"{spec.id}/{variant}: {report.run.error}")
                break
        else:
            continue
        break
    result.totals = compute_totals(result.cases)
    return result


# -- dry run ---------------------------------------------------------------------------------


def estimate_eval(
    cases_dir: Path,
    settings: Settings,
    include_clean: bool,
    fix: bool,
    only: list[str] | None = None,
) -> dict[str, float]:
    """A rough cost estimate without calling the model. Indexing is free and local."""
    tri_in, tri_out = PRICING.get(settings.models.triage, (0.0, 0.0))
    deep_in, deep_out = PRICING.get(settings.models.deep_review, (0.0, 0.0))
    functions = bugs = 0
    cost = 0.0
    for case_dir, spec in load_cases(cases_dir, only):
        for variant in ["buggy"] + (["clean"] if include_clean else []):
            mat = materialize(case_dir, spec, work_dir(spec.id, variant), variant)  # type: ignore[arg-type]
            report = index_repository(str(mat.root), signal_tools=())
            with Store(report.db_path) as store:
                chunks = [c for c in store.load_chunks() if not c.path.startswith("tests/")
                          and c.path != "conftest.py"]  # fmt: skip
            functions += len(chunks)
            bugs += len(mat.seeded)
            tokens = sum(chunk_tokens(c) for c in chunks)
            # triage on every function; deep review on ~40% (the bugs plus triage's over-flagging)
            cost += (tokens + 700 * len(chunks)) * tri_in / 1e6 + 150 * len(chunks) * tri_out / 1e6
            deep_calls = max(len(mat.seeded), round(0.4 * len(chunks)))
            cost += deep_calls * (3000 * deep_in + 700 * deep_out) / 1e6
            if fix and variant == "buggy":
                cost += len(mat.seeded) * (2800 * deep_in + 400 * deep_out) / 1e6
    return {"functions": functions, "bugs": bugs, "estimated_cost_usd": round(cost, 2)}
