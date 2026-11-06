"""Stage 2: careful review of one function with its retrieved context."""

from __future__ import annotations

from dataclasses import dataclass, field

from reviewer.analysis.triage import schema_json
from reviewer.llm.cache import LLMCache
from reviewer.llm.gateway import LLM, LLMRequest, complete_structured
from reviewer.llm.prompts import load_prompt, neutralise_tags, number_lines
from reviewer.llm.schemas import DeepReviewResult, Finding, RawFinding, TriageResult, finding_id
from reviewer.retrieval.context import ContextBundle
from reviewer.runs.tracing import RunTracer
from reviewer.settings import Settings

_SEVERITY_WEIGHT = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}


@dataclass
class DeepReviewOutcome:
    findings: list[Finding] = field(default_factory=list)
    failed: bool = False
    dropped_low_confidence: int = 0
    dropped_out_of_range: int = 0


def render_context(bundle: ContextBundle) -> str:
    if not bundle.items:
        return "(none)"
    blocks = []
    for item in bundle.items:
        c = item.chunk
        why = "; ".join(dict.fromkeys(h.detail for h in item.sources))
        blocks.append(
            f'<context_chunk path="{c.path}" symbol="{c.symbol}" why="{why}">\n'
            f"{neutralise_tags(number_lines(c.text, c.start_line))}\n</context_chunk>"
        )
    return "\n\n".join(blocks)


def render_signals(bundle: ContextBundle) -> str:
    if not bundle.signals:
        return "(none)"
    return "\n".join(
        f"- {s.ref} ({s.category}, severity {s.severity}) lines {s.line_start}-{s.line_end}: "
        f"{s.message}"
        for s in bundle.signals
    )


def render_hints(triage: TriageResult | None) -> str:
    if triage is None or not triage.concerns:
        return "(none)"
    return "\n".join(f"- line {c.line} [{c.category}]: {c.note}" for c in triage.concerns)


def deep_review(
    llm: LLM,
    cache: LLMCache | None,
    tracer: RunTracer,
    settings: Settings,
    bundle: ContextBundle,
    triage: TriageResult | None,
) -> DeepReviewOutcome:
    target = bundle.target
    prompt = load_prompt("deep_review", settings.prompts.deep_review)
    system, user = prompt.render(
        schema=schema_json(DeepReviewResult),
        path=target.path,
        symbol=target.symbol,
        start_line=str(target.start_line),
        end_line=str(target.end_line),
        code=neutralise_tags(number_lines(target.text, target.start_line)),
        hints=neutralise_tags(render_hints(triage)),
        signals=neutralise_tags(render_signals(bundle)),
        context=render_context(bundle),
    )
    request = LLMRequest(
        model=settings.models.deep_review,
        system=system,
        user=user,
        max_tokens=settings.max_tokens.deep_review,
        effort=settings.effort.deep_review,
    )
    outcome = complete_structured(llm, request, DeepReviewResult, prompt, cache)
    tracer.record_outcome("deep_review", target.id, prompt, outcome)
    result = DeepReviewOutcome(failed=outcome.value is None)
    if outcome.value is None:
        return result

    known_refs = {s.ref for s in bundle.signals}
    kept: list[tuple[RawFinding, Finding]] = []
    for raw in outcome.value.findings:
        in_range = target.start_line <= raw.line_start <= raw.line_end <= target.end_line
        if not in_range:
            result.dropped_out_of_range += 1
            continue
        if raw.confidence < settings.review.min_confidence:
            result.dropped_low_confidence += 1
            continue
        kept.append(
            (
                raw,
                Finding(
                    id=finding_id(
                        target.path, target.content_hash, raw.category, raw.line_start, raw.title
                    ),
                    file=target.path,
                    line_start=raw.line_start,
                    line_end=raw.line_end,
                    symbol=target.symbol,
                    category=raw.category,
                    severity=raw.severity,
                    confidence=raw.confidence,
                    title=raw.title,
                    explanation=raw.explanation,
                    evidence=raw.evidence,
                    related_signals=[r for r in raw.related_signals if r in known_refs],
                    chunk_content_hash=target.content_hash,
                ),
            )
        )
    kept.sort(key=lambda p: (-_SEVERITY_WEIGHT[p[0].severity] * p[0].confidence, p[1].id))
    result.findings = [f for _, f in kept[: settings.review.max_findings_per_chunk]]
    return result
