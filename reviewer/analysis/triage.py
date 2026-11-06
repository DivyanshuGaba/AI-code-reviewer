"""Stage 1: cheap, high-recall triage of one function."""

from __future__ import annotations

from pydantic import BaseModel

from reviewer.index.models import Chunk
from reviewer.llm.cache import LLMCache
from reviewer.llm.gateway import LLM, LLMRequest, complete_structured
from reviewer.llm.prompts import load_prompt, neutralise_tags, number_lines
from reviewer.llm.schemas import TriageResult
from reviewer.runs.tracing import RunTracer
from reviewer.settings import Settings
from reviewer.signals.models import SEVERITY_RANK, Signal


def schema_json(model: type[BaseModel]) -> str:
    import json

    return json.dumps(model.model_json_schema(), separators=(",", ":"))


def triage_chunk(
    llm: LLM, cache: LLMCache | None, tracer: RunTracer, settings: Settings, chunk: Chunk
) -> TriageResult | None:
    prompt = load_prompt("triage", settings.prompts.triage)
    system, user = prompt.render(
        schema=schema_json(TriageResult),
        path=chunk.path,
        symbol=chunk.symbol,
        start_line=str(chunk.start_line),
        end_line=str(chunk.end_line),
        code=neutralise_tags(number_lines(chunk.text, chunk.start_line)),
    )
    request = LLMRequest(
        model=settings.models.triage,
        system=system,
        user=user,
        max_tokens=settings.max_tokens.triage,
        effort=settings.effort.triage,
    )
    outcome = complete_structured(llm, request, TriageResult, prompt, cache)
    tracer.record_outcome("triage", chunk.id, prompt, outcome)
    return outcome.value


def should_escalate(signals: list[Signal], min_severity: str) -> bool:
    """A sufficiently severe static signal forces a deep review, whatever triage said."""
    floor = SEVERITY_RANK[min_severity]
    return any(SEVERITY_RANK[s.severity] >= floor for s in signals)


def should_deep_review(result: TriageResult, threshold: float) -> bool:
    return result.needs_review or result.suspicion >= threshold
