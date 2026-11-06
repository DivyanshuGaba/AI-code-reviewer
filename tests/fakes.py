"""A scriptable stand-in for the model, so no test touches the network."""

from __future__ import annotations

import json
from collections.abc import Callable

from reviewer.llm.gateway import LLMRequest, LLMResponse

Handler = Callable[[LLMRequest], str]


class FakeLLM:
    def __init__(
        self, handler: Handler, input_tokens: int = 1000, output_tokens: int = 200
    ) -> None:
        self.handler = handler
        self.requests: list[LLMRequest] = []
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            text=self.handler(request),
            model=request.model,
            stop_reason="end_turn",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=5,
            request_id="req_fake",
        )

    def count(self, model: str) -> int:
        return sum(1 for r in self.requests if r.model == model)


def triage_json(needs_review: bool, suspicion: float = 0.1) -> str:
    return json.dumps({"needs_review": needs_review, "suspicion": suspicion, "concerns": []})


def finding_json(
    line_start: int = 6,
    line_end: int = 6,
    confidence: float = 0.9,
    title: str = "Amount is not validated",
    severity: str = "high",
) -> dict[str, object]:
    return {
        "category": "bug",
        "severity": severity,
        "confidence": confidence,
        "line_start": line_start,
        "line_end": line_end,
        "title": title,
        "explanation": "A negative amount would be charged without any check.",
        "evidence": "amount = round_money(amount)",
    }
