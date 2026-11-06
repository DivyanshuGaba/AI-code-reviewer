"""Typed pipeline settings, loaded from config/settings.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

CONFIG_DIR = Path(__file__).parent / "config"


class Models(BaseModel):
    triage: str
    deep_review: str


class Effort(BaseModel):
    triage: str
    deep_review: str


class PromptVersions(BaseModel):
    triage: str
    deep_review: str


class MaxTokens(BaseModel):
    triage: int
    deep_review: int


class Retrieval(BaseModel):
    token_budget: int


class ReviewSettings(BaseModel):
    triage_threshold: float
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
    review: ReviewSettings


def load_settings(path: Path | None = None) -> Settings:
    data = yaml.safe_load((path or CONFIG_DIR / "settings.yaml").read_text())
    return Settings.model_validate(data)
