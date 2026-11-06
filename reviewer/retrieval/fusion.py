"""Reciprocal rank fusion across retrieval strategies."""

from __future__ import annotations

from dataclasses import dataclass, field

from reviewer.retrieval.strategies import (
    GRAPH_CALLEE,
    GRAPH_CALLER,
    GRAPH_TEST,
    LEXICAL,
    VECTOR,
    Candidate,
)

RRF_K = 60

# Exact graph signals outrank fuzzy ones; the default embedder is a weak signal.
DEFAULT_WEIGHTS: dict[str, float] = {
    GRAPH_CALLEE: 1.5,
    GRAPH_CALLER: 1.5,
    GRAPH_TEST: 1.2,
    LEXICAL: 1.0,
    VECTOR: 0.8,
}


@dataclass
class FusedItem:
    chunk_id: str
    score: float = 0.0
    contributions: list[Candidate] = field(default_factory=list)


def reciprocal_rank_fusion(
    rankings: dict[str, list[Candidate]],
    weights: dict[str, float] | None = None,
    k: int = RRF_K,
) -> list[FusedItem]:
    """score(chunk) = sum over strategies of weight / (k + rank). Deterministic order."""
    w = DEFAULT_WEIGHTS if weights is None else weights
    fused: dict[str, FusedItem] = {}
    for source, candidates in rankings.items():
        weight = w.get(source, 1.0)
        for cand in candidates:
            item = fused.setdefault(cand.chunk_id, FusedItem(cand.chunk_id))
            item.score += weight / (k + cand.rank)
            item.contributions.append(cand)
    return sorted(fused.values(), key=lambda i: (-i.score, i.chunk_id))
