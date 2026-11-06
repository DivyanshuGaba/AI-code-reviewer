from __future__ import annotations

import json
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from reviewer.cli import app
from reviewer.llm.gateway import (
    GatewayAuthError,
    GatewayError,
    LLMRequest,
    complete_structured,
)
from reviewer.llm.ollama import OllamaGateway, normalise_host
from reviewer.llm.prompts import load_prompt
from reviewer.llm.providers import build_llm
from reviewer.pipeline import review_repository
from reviewer.settings import load_settings
from tests.fakes import finding_json, triage_json

SETTINGS = load_settings()
LOCAL = SETTINGS.ollama.models


class FakeOllama:
    """A local HTTP server that answers /api/chat from a function, and records requests."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.respond: Callable[[dict[str, Any]], tuple[int, Any]] = lambda body: (
            200,
            {"model": body["model"], "message": {"role": "assistant", "content": "{}"}},
        )
        self.delay = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, **body})
                time.sleep(outer.delay)
                status, payload = outer.respond(body)
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: object) -> None:
                pass

        class QuickServer(ThreadingHTTPServer):
            def server_bind(self) -> None:
                # HTTPServer.server_bind does a reverse-DNS lookup that can take many seconds
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

        self.server = QuickServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True
        ).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama()
    yield server
    server.close()


REQ = LLMRequest(model="m:7b", system="sys", user="usr", max_tokens=123, effort="high")


def test_request_shape_and_response_mapping(ollama: FakeOllama) -> None:
    ollama.respond = lambda body: (
        200,
        {
            "model": "m:7b", "message": {"content": '{"ok": true}'}, "done_reason": "stop",
            "prompt_eval_count": 42, "eval_count": 7,
        },
    )  # fmt: skip
    resp = OllamaGateway(ollama.url, num_ctx=8192).complete(REQ)
    assert (resp.text, resp.input_tokens, resp.output_tokens) == ('{"ok": true}', 42, 7)
    assert resp.stop_reason == "end_turn" and resp.cache_read_tokens == 0

    (sent,) = ollama.requests
    assert sent["path"] == "/api/chat" and sent["model"] == "m:7b"
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    assert sent["stream"] is False and sent["format"] == "json"
    assert sent["options"] == {"num_ctx": 8192, "num_predict": 123, "temperature": 0}


def test_truncated_output_is_reported_as_max_tokens(ollama: FakeOllama) -> None:
    ollama.respond = lambda body: (
        200, {"message": {"content": "{"}, "done_reason": "length"}
    )  # fmt: skip
    assert OllamaGateway(ollama.url).complete(REQ).stop_reason == "max_tokens"


def test_missing_model_and_server_errors_are_ordinary_failures(ollama: FakeOllama) -> None:
    gw = OllamaGateway(ollama.url)
    ollama.respond = lambda body: (404, {"error": "model 'm:7b' not found"})
    with pytest.raises(GatewayError, match="ollama pull m:7b") as missing:
        gw.complete(REQ)
    assert not isinstance(missing.value, GatewayAuthError)  # other stages may use other models
    ollama.respond = lambda body: (500, {"error": "boom"})
    with pytest.raises(GatewayError, match="HTTP 500"):
        gw.complete(REQ)
    ollama.respond = lambda body: (200, b"<html>not json</html>")
    with pytest.raises(GatewayError, match="unexpected response"):
        gw.complete(REQ)
    ollama.respond = lambda body: (200, {"no": "message"})
    with pytest.raises(GatewayError, match="unexpected response"):
        gw.complete(REQ)


def test_an_unreachable_server_aborts_the_run_but_a_slow_one_does_not() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    with pytest.raises(GatewayAuthError, match="ollama serve"):
        OllamaGateway(f"http://127.0.0.1:{closed}").complete(REQ)


def test_a_slow_response_times_out_as_a_normal_failure(ollama: FakeOllama) -> None:
    ollama.delay = 1.0
    with pytest.raises(GatewayError, match="timed out") as slow:
        OllamaGateway(ollama.url, timeout=0.2).complete(REQ)
    assert not isinstance(slow.value, GatewayAuthError)


def test_host_normalisation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert normalise_host("localhost:11434") == "http://localhost:11434"
    assert normalise_host("https://gpu.internal:11434/") == "https://gpu.internal:11434"
    assert normalise_host(None) == "http://localhost:11434"
    monkeypatch.setenv("OLLAMA_HOST", "10.0.0.5:9999")
    assert normalise_host(None) == "http://10.0.0.5:9999"
    for bad in ("ftp://x", "file:///etc/passwd", "http://"):
        with pytest.raises(ValueError):
            normalise_host(bad)


def test_structured_output_retries_when_a_small_model_returns_bad_json(
    ollama: FakeOllama,
) -> None:
    class Answer(BaseModel):
        value: int

    replies = iter(["Sure! here you go", '{"value": 5}'])
    ollama.respond = lambda body: (200, {"message": {"content": next(replies)}})
    out = complete_structured(OllamaGateway(ollama.url), REQ, Answer, load_prompt("triage", "v1"))
    assert out.value == Answer(value=5) and len(ollama.requests) == 2
    assert "not valid" in ollama.requests[1]["messages"][1]["content"]
    assert out.cost_usd == 0.0  # local models cost nothing


# -- provider selection ------------------------------------------------------------------------


def test_build_llm_swaps_in_the_local_models() -> None:
    llm, settings = build_llm(SETTINGS, "ollama")
    assert isinstance(llm, OllamaGateway) and llm.num_ctx == SETTINGS.ollama.num_ctx
    assert settings.models == LOCAL and settings.models != SETTINGS.models
    assert SETTINGS.models.triage == "claude-haiku-5-5"  # the original settings are untouched

    sentinel = object()
    client, same = build_llm(SETTINGS, "anthropic", anthropic_factory=lambda: sentinel)  # type: ignore[arg-type, return-value]
    assert client is sentinel and same is SETTINGS
    with pytest.raises(ValueError, match="unknown provider"):
        build_llm(SETTINGS, "gemini")


def local_handler(body: dict[str, Any]) -> tuple[int, Any]:
    """A pretend local model: flags Gateway.charge, reports one finding, answers a fix."""
    system, user = body["messages"][0]["content"], body["messages"][1]["content"]
    if "triage stage" in system:
        content = triage_json("Function: Gateway.charge" in user)
    elif "deep review stage" in system:
        content = json.dumps({"findings": [finding_json()]})
    else:
        raise AssertionError(system[:60])
    return 200, {"model": body["model"], "message": {"content": content},
                 "prompt_eval_count": 500, "eval_count": 50}  # fmt: skip


def test_review_runs_end_to_end_on_a_local_model(
    repo: Path, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    ollama.respond = local_handler
    monkeypatch.setenv("OLLAMA_HOST", ollama.url)
    settings = SETTINGS.model_copy(deep=True)
    settings.ollama.host = ""  # fall through to OLLAMA_HOST
    report = review_repository(
        str(repo), provider="ollama", settings=settings, file="pkg/payments.py"
    )

    assert report.run.status == "completed" and report.run.cost_usd == 0.0
    assert [f.symbol for f in report.findings] == ["Gateway.charge"]
    assert report.run.models["triage"] == LOCAL.triage != SETTINGS.models.triage
    used = {r["model"] for r in ollama.requests}
    assert used == {LOCAL.triage, LOCAL.deep_review}
    assert all(r["options"]["num_ctx"] == SETTINGS.ollama.num_ctx for r in ollama.requests)


def test_a_missing_ollama_server_fails_the_run_cleanly(repo: Path) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    settings = SETTINGS.model_copy(deep=True)
    settings.ollama.host = f"http://127.0.0.1:{closed}"
    report = review_repository(str(repo), provider="ollama", settings=settings, max_chunks=1)
    assert report.run.status == "failed" and "ollama serve" in (report.run.error or "")


def test_cli_provider_option(
    repo: Path, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    ollama.respond = local_handler
    monkeypatch.setenv("OLLAMA_HOST", ollama.url)
    monkeypatch.setattr(
        "reviewer.pipeline.load_settings",
        lambda: SETTINGS.model_copy(
            update={"ollama": SETTINGS.ollama.model_copy(update={"host": ""})}
        ),
    )
    runner = CliRunner()
    ok = runner.invoke(
        app, ["review", str(repo), "--provider", "ollama", "--file", "pkg/payments.py"]
    )
    assert ok.exit_code == 0, ok.output
    assert "cost $0.0000" in ok.output and "Amount is not validated" in ok.output
    bad = runner.invoke(app, ["review", str(repo), "--provider", "nope"])
    assert bad.exit_code == 1 and "unknown provider" in bad.output
