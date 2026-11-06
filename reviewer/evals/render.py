"""Plain-text and Markdown views of eval results, and a comparison between two runs."""

from __future__ import annotations

from reviewer.evals.models import EvalResult, Rate
from reviewer.evals.retrieval_eval import RetrievalReport


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def rate(r: Rate) -> str:
    return f"{r.hit}/{r.total} ({pct(r.value)})" if r.total else "n/a"


def render_eval(result: EvalResult) -> str:
    t = result.totals
    lines = [
        f"# Eval {result.eval_id}",
        "",
        f"- Config version `{result.config_version}`; models: "
        + ", ".join(f"{k}=`{v}`" for k, v in result.models.items()),
        "- Prompts: " + ", ".join(f"{k}=`{v}`" for k, v in result.prompt_versions.items()),
        f"- Status: {result.status}; cost ${t.cost_usd:.4f}",
        "",
        "## Detection (function-level review)",
        "",
        f"- Seeded bugs: {t.bugs}; found {t.tp}, missed {t.fn}; false positives {t.fp}",
        f"- **Precision {pct(t.precision)} · Recall {pct(t.recall)} · F1 {num(t.f1)}**",
        f"- Where recall is lost: the buggy function reached deep review (via triage or a static "
        f"signal) in "
        f"{rate(t.triage_selected)}; deep review found the bug in "
        f"{rate(t.deep_found_given_selected)} of those",
        f"- Category correct on found bugs: {rate(t.category_accuracy)}",
        f"- Mean confidence: true positives {num(t.mean_confidence_tp)}, "
        f"false positives {num(t.mean_confidence_fp)}",
        "",
        "| By category | Found |",
        "|---|---|",
        *[f"| {k} | {rate(v)} |" for k, v in sorted(t.recall_by_category.items())],
        "",
        "| By severity | Found |",
        "|---|---|",
        *[f"| {k} | {rate(v)} |" for k, v in sorted(t.recall_by_severity.items())],
        "",
    ]
    if t.clean_functions:
        lines += [
            "## False positives on clean code",
            "",
            f"- {t.clean_false_positives} finding(s) across {t.clean_functions} functions "
            f"({num(t.fp_per_100_clean_functions, 1)} per 100 functions)",
            "",
        ]
    if result.fix_enabled:
        lines += [
            "## Fixes (for bugs that were found)",
            "",
            f"- Fix produced: {rate(t.fixes_attempted)}",
            f"- Exactly matches the original code (strict): {rate(t.fixes_exact)}",
            f"- Reached `verified_fix` in the sandbox: {rate(t.fixes_verified)}",
            f"- Case tests go from failing to passing: {rate(t.fixes_resolve_tests)}",
            "",
        ]
    lines += ["## Per case", "", "| Case | Variant | Found | Missed | FP | Functions | Cost |"]
    lines.append("|---|---|---|---|---|---|---|")
    for c in result.cases:
        found = sum(b.found for b in c.bugs)
        lines.append(
            f"| {c.case_id} | {c.variant} | {found} | {len(c.bugs) - found} | "
            f"{len(c.false_positives)} | {c.functions} | ${c.cost_usd:.4f} |"
        )
    missed = [(c.case_id, b) for c in result.cases for b in c.bugs if not b.found]
    if missed:
        lines += ["", "## Missed bugs", ""]
        lines += [
            f"- `{cid}/{b.bug_id}` ({b.severity} {b.category}): "
            + ("reviewed but not reported" if b.function_reviewed else "never reached deep review")
            for cid, b in missed
        ]
    fps = [(c.case_id, f) for c in result.cases for f in c.false_positives]
    if fps:
        lines += ["", "## False positives", ""]
        lines += [
            f"- `{cid}` {f.file}:{f.line_start} [{f.severity}, {f.confidence:.0%}] {f.title}"
            for cid, f in fps
        ]
    if result.notes:
        lines += ["", "## Notes", "", *[f"- {n}" for n in result.notes]]
    return "\n".join(lines) + "\n"


def render_retrieval(report: RetrievalReport) -> str:
    cols = [f"recall@{k}" for k in report.ks] + ["mrr"]
    lines = [
        f"# Retrieval eval ({report.bugs_evaluated} seeded bugs)",
        "",
        "| Strategy | " + " | ".join(cols) + " |",
        "|---|" + "---|" * len(cols),
    ]
    for name, metrics in report.configs.items():
        lines.append(f"| {name} | " + " | ".join(num(metrics[c]) for c in cols) + " |")
    lines += [
        "",
        f"Relevant context inside the final token-budgeted bundle: {pct(report.bundle_recall)}",
        "",
        "Cases are small, so high-k recall saturates; recall@1, recall@3 and MRR are the "
        "metrics that discriminate between strategies.",
    ]
    return "\n".join(lines) + "\n"


def _delta(a: float | None, b: float | None, higher_is_better: bool = True) -> str:
    if a is None or b is None:
        return "n/a"
    diff = b - a
    if abs(diff) < 0.005:
        return "="
    mark = "▲" if (diff > 0) == higher_is_better else "▼"
    return f"{mark} {diff:+.2f}"


def render_compare(a: EvalResult, b: EvalResult) -> str:
    ta, tb = a.totals, b.totals
    rows = [
        ("precision", ta.precision, tb.precision, True),
        ("recall", ta.recall, tb.recall, True),
        ("f1", ta.f1, tb.f1, True),
        ("triage selected", ta.triage_selected.value, tb.triage_selected.value, True),
        ("deep found | selected", ta.deep_found_given_selected.value,
         tb.deep_found_given_selected.value, True),
        ("clean FP / 100 fn", ta.fp_per_100_clean_functions, tb.fp_per_100_clean_functions, False),
        ("fixes exact", ta.fixes_exact.value, tb.fixes_exact.value, True),
        ("fixes verified", ta.fixes_verified.value, tb.fixes_verified.value, True),
        ("cost (USD)", ta.cost_usd, tb.cost_usd, False),
    ]  # fmt: skip
    lines = [
        f"# Compare {a.eval_id} → {b.eval_id}",
        "",
        f"- A: config `{a.config_version}`, prompts {a.prompt_versions}",
        f"- B: config `{b.config_version}`, prompts {b.prompt_versions}",
        "",
        "| Metric | A | B | Change |",
        "|---|---|---|---|",
    ]
    lines += [f"| {n} | {num(x)} | {num(y)} | {_delta(x, y, hib)} |" for n, x, y, hib in rows]
    return "\n".join(lines) + "\n"
