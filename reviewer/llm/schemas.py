"""Pydantic schemas for everything the LLM returns, plus the persisted Finding."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["bug", "security", "performance", "readability", "architecture"]
Severity = Literal["critical", "high", "medium", "low", "info"]
VerificationStatus = Literal["unverified", "verified_fix", "fix_unverified", "fix_failed"]


class Concern(BaseModel):
    category: Category
    line: int = Field(ge=1)
    note: str = Field(max_length=500)


class TriageResult(BaseModel):
    needs_review: bool
    suspicion: float = Field(ge=0.0, le=1.0)
    concerns: list[Concern] = Field(default_factory=list, max_length=8)


class RawFinding(BaseModel):
    """A finding as the model reports it, before validation against the code."""

    category: Category
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    title: str = Field(min_length=3, max_length=140)
    explanation: str = Field(min_length=3, max_length=2000)
    evidence: str = Field(min_length=1, max_length=1000)


class DeepReviewResult(BaseModel):
    findings: list[RawFinding] = Field(default_factory=list, max_length=20)


class Finding(BaseModel):
    id: str
    file: str
    line_start: int
    line_end: int
    symbol: str
    category: Category
    severity: Severity
    confidence: float
    title: str
    explanation: str
    evidence: str
    suggested_diff: str | None = None  # filled by fix generation (Phase 5)
    verification_status: VerificationStatus = "unverified"
    chunk_content_hash: str


def finding_id(file: str, chunk_hash: str, category: str, line_start: int, title: str) -> str:
    """Stable across runs: same code, same issue -> same id (eval-friendly)."""
    raw = f"{file}\0{chunk_hash}\0{category}\0{line_start}\0{title.strip().lower()}"
    return "F-" + hashlib.sha256(raw.encode()).hexdigest()[:12]
