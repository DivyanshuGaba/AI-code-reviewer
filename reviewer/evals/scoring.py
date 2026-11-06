"""Score findings against seeded ground truth. Pure functions, no I/O."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from reviewer.evals.cases import AcceptableRange, ResolvedBug
from reviewer.evals.models import BugOutcome, CaseResult, EvalResult, Rate, Totals
from reviewer.llm.schemas import Finding

LINE_SLACK = 2  # a finding may point a couple of lines away from the planted change


@dataclass
class MatchResult:
    matched: dict[str, Finding] = field(default_factory=dict)  # bug id -> finding
    false_positives: list[Finding] = field(default_factory=list)
    ignored: list[Finding] = field(default_factory=list)  # on deliberate patterns
    architecture: list[Finding] = field(default_factory=list)  # repo-level; not scored


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int, slack: int = 0) -> bool:
    return a_start <= b_end + slack and b_start - slack <= a_end


def _affinity(finding: Finding, bug: ResolvedBug) -> int:
    """0 = unrelated, 2 = lines line up, 1 = same function and same category."""
    if finding.file != bug.file:
        return 0
    if _overlaps(finding.line_start, finding.line_end, bug.line_start, bug.line_end, LINE_SLACK):
        return 2
    if finding.symbol == bug.symbol and finding.category == bug.category:
        return 1
    return 0


def match_findings(
    findings: list[Finding], bugs: list[ResolvedBug], acceptable: list[AcceptableRange]
) -> MatchResult:
    """One-to-one: each bug takes the best-fitting unused finding; the rest are classified."""
    result = MatchResult()
    function_level = [f for f in findings if f.symbol != "<repo>"]
    result.architecture = [f for f in findings if f.symbol == "<repo>"]
    used: set[str] = set()

    for bug in sorted(bugs, key=lambda b: b.id):
        candidates = [
            (aff, f.confidence, f)
            for f in function_level
            if f.id not in used and (aff := _affinity(f, bug)) > 0
        ]
        if candidates:
            _, _, best = max(candidates, key=lambda c: (c[0], c[1], c[2].id))
            result.matched[bug.id] = best
            used.add(best.id)

    for f in function_level:
        if f.id in used:
            continue
        if any(
            f.file == a.file and _overlaps(f.line_start, f.line_end, a.line_start, a.line_end)
            for a in acceptable
        ):
            result.ignored.append(f)
        else:
            result.false_positives.append(f)
    return result


def function_dump(source: str, qualname: str) -> str | None:
    """Canonical AST dump of one function or method, ignoring formatting and comments."""
    parts = qualname.split(".")

    def find(body: list[ast.stmt], names: list[str]) -> ast.AST | None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                if node.name == names[0]:
                    return node if len(names) == 1 else find(node.body, names[1:])
        return None

    try:
        node = find(ast.parse(source).body, parts)
    except SyntaxError:
        return None
    return ast.dump(node) if node is not None else None


def fix_is_exact(patched_source: str, clean_source: str, qualname: str) -> bool:
    patched, clean = function_dump(patched_source, qualname), function_dump(clean_source, qualname)
    return patched is not None and patched == clean


def _rate_add(table: dict[str, Rate], key: str, hit: bool) -> None:
    rate = table.setdefault(key, Rate())
    rate.total += 1
    rate.hit += int(hit)


def compute_totals(cases: list[CaseResult]) -> Totals:
    t = Totals()
    tp_conf: list[float] = []
    fp_conf: list[float] = []

    for case in cases:
        t.cost_usd += case.cost_usd
        if case.variant == "clean":
            t.clean_functions += case.functions
            t.clean_false_positives += len(case.false_positives)
            continue
        t.fp += len(case.false_positives)
        fp_conf += [f.confidence for f in case.false_positives]
        for bug in case.bugs:
            t.bugs += 1
            t.tp += int(bug.found)
            _rate_add(t.recall_by_category, bug.category, bug.found)
            _rate_add(t.recall_by_severity, bug.severity, bug.found)
            t.triage_selected.total += 1
            t.triage_selected.hit += int(bug.function_reviewed)
            if bug.function_reviewed:
                t.deep_found_given_selected.total += 1
                t.deep_found_given_selected.hit += int(bug.found)
            if not bug.found:
                continue
            tp_conf.append(bug.finding_confidence or 0.0)
            t.category_accuracy.total += 1
            t.category_accuracy.hit += int(bool(bug.category_correct))
            t.fixes_attempted.total += 1
            t.fixes_attempted.hit += int(bug.fix.attempted)
            if bug.fix.attempted:
                t.fixes_exact.total += 1
                t.fixes_exact.hit += int(bool(bug.fix.exact))
                t.fixes_verified.total += 1
                t.fixes_verified.hit += int(bug.fix.status == "verified_fix")
            if bug.fix.resolves is not None:
                t.fixes_resolve_tests.total += 1
                t.fixes_resolve_tests.hit += int(bug.fix.resolves)

    t.fn = t.bugs - t.tp
    t.precision = t.tp / (t.tp + t.fp) if t.tp + t.fp else None
    t.recall = t.tp / t.bugs if t.bugs else None
    if t.precision is not None and t.recall is not None and t.precision + t.recall:
        t.f1 = 2 * t.precision * t.recall / (t.precision + t.recall)
    t.mean_confidence_tp = sum(tp_conf) / len(tp_conf) if tp_conf else None
    t.mean_confidence_fp = sum(fp_conf) / len(fp_conf) if fp_conf else None
    if t.clean_functions:
        t.fp_per_100_clean_functions = 100 * t.clean_false_positives / t.clean_functions
    return t


def bug_outcome_for(bug: ResolvedBug, finding: Finding | None, reviewed: bool) -> BugOutcome:
    return BugOutcome(
        case_id=bug.case_id, bug_id=bug.id, category=bug.category, severity=bug.severity,
        function_reviewed=reviewed, found=finding is not None,
        finding_id=finding.id if finding else None,
        finding_confidence=finding.confidence if finding else None,
        category_correct=(finding.category == bug.category) if finding else None,
    )  # fmt: skip


def with_totals(result: EvalResult) -> EvalResult:
    result.totals = compute_totals(result.cases)
    return result
