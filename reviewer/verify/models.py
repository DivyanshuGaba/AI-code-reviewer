"""Result records for fix verification."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from reviewer.llm.schemas import VerificationStatus

CheckStatus = Literal["passed", "failed", "skipped"]


class CheckResult(BaseModel):
    name: str  # apply | syntax | lint | typecheck | tests
    status: CheckStatus
    detail: str = ""


class VerificationResult(BaseModel):
    finding_id: str
    status: VerificationStatus
    sandbox: str
    checks: list[CheckResult] = Field(default_factory=list)
    patched_files: list[str] = Field(default_factory=list)
    duration_s: float = 0.0
