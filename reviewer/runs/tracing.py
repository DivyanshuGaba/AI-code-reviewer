"""Run store (SQLite) and tracer.

Every model call is logged with its full prompt and output (secrets scrubbed), the model
version, token counts, cost and latency, so a run can be inspected and replayed.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from pydantic import BaseModel

from reviewer.analysis.models import ArchitectureFacts
from reviewer.llm.gateway import StructuredOutcome
from reviewer.llm.prompts import Prompt
from reviewer.llm.schemas import Finding
from reviewer.runs.models import CallTrace, RunRecord, RunReport
from reviewer.verify.models import VerificationResult

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]
_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    ((?:api[_-]?key|secret|token|passwd|password|auth)[\w-]*\s*[=:]\s*)
    (["'])([^"'\n]{6,})\2
    """
)


def scrub_secrets(text: str) -> str:
    """Redact credential-looking strings before anything is written to the run store."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]{m.group(2)}", text)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_run ON calls(run_id);
CREATE TABLE IF NOT EXISTS findings (
    id TEXT NOT NULL, run_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (run_id, id)
);
CREATE TABLE IF NOT EXISTS verifications (
    finding_id TEXT NOT NULL, run_id TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (run_id, finding_id)
);
CREATE TABLE IF NOT EXISTS architecture (run_id TEXT PRIMARY KEY, data TEXT NOT NULL);
"""


class RunStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)

    def __enter__(self) -> RunStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.conn.close()

    def save_run(self, run: RunRecord) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?, ?, ?)",
            (run.id, run.started_at, run.model_dump_json()),
        )
        self.conn.commit()

    def add_call(self, call: CallTrace) -> None:
        self.conn.execute(
            "INSERT INTO calls(run_id, data) VALUES (?, ?)", (call.run_id, call.model_dump_json())
        )
        self.conn.commit()

    def save_findings(self, run_id: str, findings: list[Finding]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO findings VALUES (?, ?, ?)",
            [(f.id, run_id, f.model_dump_json()) for f in findings],
        )
        self.conn.commit()

    def save_verifications(self, run_id: str, results: list[VerificationResult]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO verifications VALUES (?, ?, ?)",
            [(v.finding_id, run_id, v.model_dump_json()) for v in results],
        )
        self.conn.commit()

    def save_architecture(self, run_id: str, facts: ArchitectureFacts) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO architecture VALUES (?, ?)", (run_id, facts.model_dump_json())
        )
        self.conn.commit()

    def list_runs(self) -> list[RunRecord]:
        rows = self.conn.execute("SELECT data FROM runs ORDER BY started_at DESC").fetchall()
        return [RunRecord.model_validate_json(r[0]) for r in rows]

    def load_report(self, run_id: str) -> RunReport | None:
        row = self.conn.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        calls = self.conn.execute(
            "SELECT data FROM calls WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        findings = self.conn.execute(
            "SELECT data FROM findings WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        verifications = self.conn.execute(
            "SELECT data FROM verifications WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        arch = self.conn.execute(
            "SELECT data FROM architecture WHERE run_id = ?", (run_id,)
        ).fetchone()
        return RunReport(
            run=RunRecord.model_validate_json(row[0]),
            architecture=ArchitectureFacts.model_validate_json(arch[0]) if arch else None,
            verifications=[VerificationResult.model_validate_json(v[0]) for v in verifications],
            calls=[CallTrace.model_validate_json(c[0]) for c in calls],
            findings=[Finding.model_validate_json(f[0]) for f in findings],
        )


class RunTracer:
    """Collects call traces and the running cost for one run."""

    def __init__(self, store: RunStore, run: RunRecord):
        self.store = store
        self.run = run
        self.store.save_run(run)

    @property
    def cost_usd(self) -> float:
        return self.run.cost_usd

    def record_outcome[T: BaseModel](
        self, stage: str, target: str, prompt: Prompt, outcome: StructuredOutcome[T]
    ) -> None:
        for call in outcome.calls:
            resp = call.response
            self.store.add_call(
                CallTrace(
                    run_id=self.run.id,
                    stage=stage,
                    target=target,
                    prompt_id=prompt.id,
                    prompt_version=prompt.version,
                    model=call.request.model,
                    attempt=call.attempt,
                    cached=call.cached,
                    valid=call.valid,
                    error=scrub_secrets(call.error) if call.error else None,
                    system=scrub_secrets(call.request.system),
                    user=scrub_secrets(call.request.user),
                    output=scrub_secrets(resp.text) if resp else "",
                    input_tokens=resp.input_tokens if resp else 0,
                    output_tokens=resp.output_tokens if resp else 0,
                    cache_read_tokens=resp.cache_read_tokens if resp else 0,
                    cost_usd=call.cost_usd,
                    latency_ms=resp.latency_ms if resp else 0,
                    request_id=resp.request_id if resp else None,
                )
            )
            self.run.cost_usd += call.cost_usd
        if outcome.value is None:
            self.run.stage_failures += 1

    def finish(self, status: str, error: str | None = None) -> RunRecord:
        self.run.status = status  # type: ignore[assignment]
        self.run.error = error
        self.run.finished_at = datetime.now(UTC).isoformat()
        self.store.save_run(self.run)
        return self.run
