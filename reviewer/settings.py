"""Typed pipeline settings, loaded from config/settings.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

CONFIG_DIR = Path(__file__).parent / "config"


class Models(BaseModel):
    triage: str
    deep_review: str
    fixgen: str


class Effort(BaseModel):
    triage: str
    deep_review: str
    fixgen: str


class PromptVersions(BaseModel):
    triage: str
    deep_review: str
    fixgen: str


class MaxTokens(BaseModel):
    triage: int
    deep_review: int
    fixgen: int


class Retrieval(BaseModel):
    token_budget: int


class FixSettings(BaseModel):
    min_severity: str
    max_changed_lines: int
    max_fixes: int


class Signals(BaseModel):
    tools: list[str]


class ReviewSettings(BaseModel):
    triage_threshold: float
    escalate_min_severity: str
    min_confidence: float
    max_findings_per_chunk: int
    max_cost_usd: float


class Settings(BaseModel):
    config_version: str
    models: Models
    effort: Effort
    prompts: PromptVersions
    max_tokens: MaxTokens
    retrieval: Retrieval
    signals: Signals
    review: ReviewSettings
    fix: FixSettings


def load_settings(path: Path | None = None) -> Settings:
    data = yaml.safe_load((path or CONFIG_DIR / "settings.yaml").read_text())
    return Settings.model_validate(data)
