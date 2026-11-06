"""Records produced by the repo-level pass."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ModuleInfo(BaseModel):
    path: str
    lines: int
    symbols: list[str] = Field(default_factory=list)  # signatures of top-level defs
    imports: list[str] = Field(default_factory=list)  # files this module depends on
    imported_by: int = 0  # direct dependents
    summary: str = ""  # one-sentence description (LLM, cached); empty if not summarised


class ArchitectureFacts(BaseModel):
    """Deterministic structure facts plus module summaries, used by the architecture pass."""

    modules: list[ModuleInfo] = Field(default_factory=list)
    cycles: list[list[str]] = Field(default_factory=list)  # strongly connected components
    hubs: list[str] = Field(default_factory=list)  # most depended-on modules
    edges: int = 0
