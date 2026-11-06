"""The only module that imports the provider SDK.

Everything else talks to the small `LLM` protocol, so tests use a fake and another
provider (or a local model) can be added without touching the pipeline.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel, ValidationError

from reviewer.llm.cache import LLMCache, cache_key
from reviewer.llm.prompts import Prompt


class GatewayError(RuntimeError):
    """A provider call failed (after the SDK's own retries)."""


class GatewayAuthError(GatewayError):
    """The provider is unusable (no credentials, or it cannot be reached). Aborts the run."""


class GatewayRefusal(GatewayError):
    """The model declined the request (safety classifier)."""


@dataclass(frozen=True)
class LLMRequest:
    model: str
    system: str
    user: str
    max_tokens: int
    effort: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: int
    request_id: str | None = None


class LLM(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...


# USD per million tokens (input, output). Cache reads cost 0.1x input, writes 1.25x.
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def estimate_cost(response: LLMResponse) -> float:
    in_price, out_price = PRICING.get(response.model, (0.0, 0.0))
    cost = (
        response.input_tokens * in_price
        + response.cache_read_tokens * in_price * 0.1
        + response.cache_write_tokens * in_price * 1.25
        + response.output_tokens * out_price
    )
    return cost / 1_000_000


class AnthropicGateway:
    def __init__(self, client: anthropic.Anthropic | None = None):
        # Credentials come from the environment (ANTHROPIC_API_KEY) or an `ant auth` profile.
        self.client = client or anthropic.Anthropic()

    def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {}
        if request.effort:
            kwargs["output_config"] = {"effort": request.effort}
        started = time.monotonic()
        try:
            message = self.client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
                **kwargs,
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as err:
            raise GatewayAuthError(str(err)) from err
        except TypeError as err:  # the SDK raises this when no credentials can be resolved
            if "authentication" in str(err).lower():
                raise GatewayAuthError(
                    "No Anthropic credentials found. Set ANTHROPIC_API_KEY."
                ) from err
            raise
        except anthropic.APIError as err:
            raise GatewayError(f"{type(err).__name__}: {err}") from err

        if message.stop_reason == "refusal":
            raise GatewayRefusal("the model declined this request")
        usage = message.usage
        return LLMResponse(
            text="".join(b.text for b in message.content if b.type == "text"),
            model=message.model,
            stop_reason=message.stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=getattr(message, "_request_id", None),
        )


# -- structured output -------------------------------------------------------------------


@dataclass
class CallRecord:
    """One attempt, successful or not, for the run trace."""

    request: LLMRequest
    response: LLMResponse | None
    cached: bool
    error: str | None
    valid: bool
    cost_usd: float
    attempt: int


@dataclass
class StructuredOutcome[T: BaseModel]:
    value: T | None
    calls: list[CallRecord] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def extract_json(text: str) -> str:
    """Pull the JSON object out of a reply that may be wrapped in fences or prose."""
    stripped = _FENCE_RE.sub("", text.strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    return stripped[start : end + 1] if start != -1 and end > start else stripped


def _parse[T: BaseModel](text: str, schema: type[T]) -> T:
    return schema.model_validate_json(extract_json(text))


def _summarise(err: Exception) -> str:
    if isinstance(err, ValidationError):
        parts = [
            f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in err.errors()[:5]
        ]
        return "; ".join(parts)
    return str(err)[:300]


def complete_structured[T: BaseModel](
    llm: LLM,
    request: LLMRequest,
    schema: type[T],
    prompt: Prompt,
    cache: LLMCache | None = None,
    max_attempts: int = 2,
) -> StructuredOutcome[T]:
    """Call the model, validate the reply against `schema`, retry once on a bad reply.

    Never raises for a bad reply, a refusal or a provider error: the outcome has
    `value=None` and every attempt recorded. Authentication errors do propagate, since
    nothing else in the run can succeed either.
    """
    outcome: StructuredOutcome[T] = StructuredOutcome(value=None)
    key = cache_key(request.model, prompt.id, prompt.version, request.system, request.user)

    if cache is not None and (cached := cache.get(key)) is not None:
        try:
            outcome.value = _parse(cached, schema)
            outcome.calls.append(CallRecord(request, None, True, None, True, 0.0, 0))
            return outcome
        except ValidationError:
            pass  # a stale or corrupt entry is just a miss

    current = request
    for attempt in range(1, max_attempts + 1):
        try:
            response = llm.complete(current)
        except GatewayAuthError:
            raise
        except GatewayError as err:
            outcome.calls.append(CallRecord(current, None, False, str(err), False, 0.0, attempt))
            return outcome

        cost = estimate_cost(response)
        try:
            outcome.value = _parse(response.text, schema)
        except (ValidationError, json.JSONDecodeError, ValueError) as err:
            problem = _summarise(err)
            outcome.calls.append(
                CallRecord(current, response, False, problem, False, cost, attempt)
            )
            hint = (
                "Your previous reply was cut off; keep it shorter. "
                if response.stop_reason == "max_tokens"
                else ""
            )
            current = LLMRequest(
                model=request.model,
                system=request.system,
                user=(
                    f"{request.user}\n\n{hint}Your previous reply was not valid ({problem}). "
                    "Reply again with only a JSON object that matches the schema."
                ),
                max_tokens=request.max_tokens,
                effort=request.effort,
            )
            continue

        outcome.calls.append(CallRecord(current, response, False, None, True, cost, attempt))
        if cache is not None:
            cache.put(key, request.model, f"{prompt.id}.{prompt.version}", response.text)
        return outcome
    return outcome
