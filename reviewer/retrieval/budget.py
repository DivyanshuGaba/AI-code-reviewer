"""Token budgeting. Uses a chars/4 estimate: cheap, provider-independent, good enough."""

from __future__ import annotations

import math

from reviewer.index.models import Chunk

DEFAULT_BUDGET = 6000


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def chunk_tokens(chunk: Chunk) -> int:
    return estimate_tokens(f"{chunk.header}\n{chunk.text}")
