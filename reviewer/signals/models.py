"""The one schema every static-analysis tool is normalised into."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel

from reviewer.llm.schemas import Category

SignalSeverity = Literal["low", "medium", "high"]
SEVERITY_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3}


class Signal(BaseModel):
    """A hint from a static tool. Hints can be wrong; the review stages must verify them."""

    id: str
    tool: str
    rule_id: str
    path: str
    line_start: int
    line_end: int
    category: Category
    severity: SignalSeverity
    confidence: SignalSeverity | None = None  # the tool's own confidence, if it reports one
    message: str
    url: str | None = None
    cwe: int | None = None

    @property
    def ref(self) -> str:
        return f"{self.tool}:{self.rule_id}"


def signal_id(tool: str, rule_id: str, path: str, line: int) -> str:
    raw = f"{tool}\0{rule_id}\0{path}\0{line}"
    return "S-" + hashlib.sha256(raw.encode()).hexdigest()[:12]
