"""Local models through Ollama's HTTP API (stdlib only; no extra dependency).

Implements the same `LLM` protocol as the Claude gateway, so triage, deep review, fixes and
the repo-level pass run unchanged on a local model. Cost is zero. Expect lower quality and
less reliable JSON from small models: the structured-output retry covers some of that, and
the evals are how you find out how much.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from reviewer.llm.gateway import GatewayAuthError, GatewayError, LLMRequest, LLMResponse

DEFAULT_HOST = "http://localhost:11434"


def normalise_host(host: str | None) -> str:
    """Accept `localhost:11434` (Ollama's own OLLAMA_HOST style) as well as full URLs."""
    host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).strip()
    if "://" not in host:
        host = f"http://{host}"
    parsed = urlparse(host)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"invalid Ollama host: {host!r}")
    return host.rstrip("/")


class OllamaGateway:
    def __init__(
        self, host: str | None = None, num_ctx: int = 16384, timeout: float = 900.0
    ) -> None:
        self.host = normalise_host(host)
        self.num_ctx = num_ctx  # Ollama's default (2048) silently truncates review prompts
        self.timeout = timeout

    def complete(self, request: LLMRequest) -> LLMResponse:
        body = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "stream": False,
            "format": "json",
            "options": {
                "num_ctx": self.num_ctx,
                "num_predict": request.max_tokens,
                "temperature": 0,
            },
        }
        http_request = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")[:300]
            if err.code == 404:
                raise GatewayError(
                    f"Ollama has no model {request.model!r}; run `ollama pull {request.model}` "
                    f"({detail})"
                ) from err
            raise GatewayError(f"Ollama returned HTTP {err.code}: {detail}") from err
        except urllib.error.URLError as err:
            if isinstance(err.reason, TimeoutError | socket.timeout):
                raise GatewayError(f"Ollama timed out after {self.timeout:.0f}s") from err
            raise GatewayAuthError(
                f"cannot reach Ollama at {self.host} ({err.reason}); is `ollama serve` running?"
            ) from err
        except TimeoutError as err:
            raise GatewayError(f"Ollama timed out after {self.timeout:.0f}s") from err
        except ConnectionError as err:
            raise GatewayAuthError(f"cannot reach Ollama at {self.host} ({err})") from err

        try:
            data = json.loads(raw)
            content = data["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content is not text")
        except (ValueError, KeyError, TypeError) as err:
            raise GatewayError(f"unexpected response from Ollama: {raw[:200]!r}") from err
        return LLMResponse(
            text=content,
            model=str(data.get("model", request.model)),
            stop_reason="max_tokens" if data.get("done_reason") == "length" else "end_turn",
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
