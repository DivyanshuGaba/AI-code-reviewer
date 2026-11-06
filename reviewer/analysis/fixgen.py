"""Stage 3: propose a minimal fix for one finding.

The model returns exact text replacements, not diff syntax: models are unreliable at
hunk headers and line numbers. We validate the edits against the real file and build the
unified diff ourselves, so a diff that comes out of here always applies.
"""

from __future__ import annotations

from dataclasses import dataclass

from reviewer.analysis.deep_review import render_context, render_signals
from reviewer.analysis.triage import schema_json
from reviewer.llm.cache import LLMCache
from reviewer.llm.gateway import LLM, LLMRequest, complete_structured
from reviewer.llm.prompts import load_prompt, neutralise_tags, number_lines
from reviewer.llm.schemas import Finding, FixResult
from reviewer.retrieval.context import ContextBundle
from reviewer.runs.tracing import RunTracer
from reviewer.settings import Settings
from reviewer.verify.apply_patch import count_changed_lines, make_diff


@dataclass
class FixOutcome:
    diff: str | None = None
    explanation: str = ""
    failure: str | None = None  # why no diff was produced


def apply_edits(file_text: str, bundle: ContextBundle, result: FixResult) -> str | FixOutcome:
    """Splice the edits into the target function and return the new file text."""
    target = bundle.target
    lines = file_text.splitlines(keepends=True)
    segment_lines = lines[target.start_line - 1 : target.end_line]
    if "\n".join(line.rstrip("\r\n") for line in segment_lines) != target.text:
        return FixOutcome(failure="the file changed since it was indexed")
    segment = "".join(segment_lines)

    for n, edit in enumerate(result.edits, 1):
        found = segment.count(edit.old_text)
        if found != 1:
            return FixOutcome(
                failure=f"edit {n}: old_text matched {found} times in the function (need exactly 1)"
            )
        if edit.old_text == edit.new_text:
            return FixOutcome(failure=f"edit {n} changes nothing")
        segment = segment.replace(edit.old_text, edit.new_text, 1)

    prefix = "".join(lines[: target.start_line - 1])
    suffix = "".join(lines[target.end_line :])
    return prefix + segment + suffix


def generate_fix(
    llm: LLM,
    cache: LLMCache | None,
    tracer: RunTracer,
    settings: Settings,
    finding: Finding,
    bundle: ContextBundle,
    file_text: str,
) -> FixOutcome:
    target = bundle.target
    prompt = load_prompt("fixgen", settings.prompts.fixgen)
    system, user = prompt.render(
        language=target.language,
        schema=schema_json(FixResult),
        path=target.path,
        symbol=target.symbol,
        start_line=str(target.start_line),
        end_line=str(target.end_line),
        code=neutralise_tags(number_lines(target.text, target.start_line)),
        severity=finding.severity,
        category=finding.category,
        title=neutralise_tags(finding.title),
        explanation=neutralise_tags(finding.explanation),
        evidence=neutralise_tags(finding.evidence),
        finding_start=str(finding.line_start),
        finding_end=str(finding.line_end),
        signals=neutralise_tags(render_signals(bundle)),
        context=render_context(bundle),
    )
    request = LLMRequest(
        model=settings.models.fixgen,
        system=system,
        user=user,
        max_tokens=settings.max_tokens.fixgen,
        effort=settings.effort.fixgen,
    )
    outcome = complete_structured(llm, request, FixResult, prompt, cache)
    tracer.record_outcome("fixgen", finding.id, prompt, outcome)
    if outcome.value is None:
        return FixOutcome(failure="the model did not return a valid fix")
    result = outcome.value
    if not result.edits:
        return FixOutcome(failure=f"the model declined to fix this: {result.explanation}")

    new_text = apply_edits(file_text, bundle, result)
    if isinstance(new_text, FixOutcome):
        return new_text
    diff = make_diff(target.path, file_text, new_text)
    if not diff:
        return FixOutcome(failure="the edits produce no change")
    if count_changed_lines(diff) > settings.fix.max_changed_lines:
        return FixOutcome(
            failure=f"fix too large ({count_changed_lines(diff)} changed lines; "
            f"limit {settings.fix.max_changed_lines})"
        )
    return FixOutcome(diff=diff, explanation=result.explanation)
