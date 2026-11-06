"""Run a set of tools over some files and return normalised signals plus tool status."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from reviewer.signals.models import Signal
from reviewer.signals.normalize import dedupe, normalize
from reviewer.signals.runners import RUNNERS


@dataclass
class SignalCollection:
    signals: list[Signal] = field(default_factory=list)
    status: dict[str, str] = field(default_factory=dict)  # tool -> "ok" | "missing" | "failed: ..."

    @property
    def complete(self) -> bool:
        return all(s == "ok" for s in self.status.values())


def collect_signals(root: Path, paths: Sequence[str], tools: Sequence[str]) -> SignalCollection:
    result = SignalCollection()
    found: list[Signal] = []
    for tool in tools:
        if tool not in RUNNERS:
            result.status[tool] = "failed: unknown tool"
            continue
        run = RUNNERS[tool](root, paths)
        if run.status == "ok":
            result.status[tool] = "ok"
            found.extend(normalize(tool, run.raw or [], root))
        elif run.status == "missing":
            result.status[tool] = "missing"
        else:
            result.status[tool] = f"failed: {run.error}"
    result.signals = dedupe(found, set(paths))
    return result
