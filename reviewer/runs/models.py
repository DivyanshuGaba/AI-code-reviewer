"""Records that make every run reproducible and scoreable."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from reviewer.llm.schemas import Finding

RunStatus = Literal["running", "completed", "stopped_budget", "failed"]


class CallTrace(BaseModel):
    """One model call (or cache hit): the prompt, the output and what it cost."""

    run_id: str
    stage: str
    target: str  # chunk id
    prompt_id: str
    prompt_version: str
    model: str
    attempt: int
    cached: bool
    valid: bool
    error: str | None = None
    system: str
    user: str
    output: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    request_id: str | None = None


class RunRecord(BaseModel):
    id: str
    repo_root: str
    commit_sha: str
    dirty: bool
    config_version: str
    models: dict[str, str]
    prompt_versions: dict[str, str]
    started_at: str
    finished_at: str | None = None
    status: RunStatus = "running"
    error: str | None = None
    cost_usd: float = 0.0
    chunks_total: int = 0
    chunks_triaged: int = 0
    chunks_deep_reviewed: int = 0
    stage_failures: int = 0
    findings_dropped_low_confidence: int = 0
    findings_dropped_out_of_range: int = 0
    findings_count: int = 0


class RunReport(BaseModel):
    run: RunRecord
    findings: list[Finding] = Field(default_factory=list)
    calls: list[CallTrace] = Field(default_factory=list)
