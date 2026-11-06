"""Build the ContextBundle the analysis stages will read.

A bundle records what was included and why, and what was dropped, so retrieval can be
inspected by hand and scored on its own later.
"""

from __future__ import annotations

from pydantic import BaseModel

from reviewer.index.lexical import LexicalIndex
from reviewer.index.models import Chunk
from reviewer.index.store import Store
from reviewer.index.vectors import VectorIndex
from reviewer.retrieval.budget import DEFAULT_BUDGET, chunk_tokens
from reviewer.retrieval.fusion import reciprocal_rank_fusion
from reviewer.retrieval.strategies import (
    Candidate,
    graph_candidates,
    lexical_candidates,
    vector_candidates,
)
from reviewer.signals.models import Signal


class SourceHit(BaseModel):
    source: str
    rank: int
    score: float
    detail: str


class ContextItem(BaseModel):
    chunk: Chunk
    fused_score: float
    sources: list[SourceHit]
    tokens: int


class ExcludedItem(BaseModel):
    chunk_id: str
    fused_score: float
    tokens: int
    reason: str


class ContextBundle(BaseModel):
    target: Chunk
    target_tokens: int
    items: list[ContextItem]
    excluded: list[ExcludedItem]
    token_count: int  # target plus included items
    token_budget: int
    signals: list[Signal] = []  # static hints inside the target; not verified


def build_context(
    target: Chunk,
    store: Store,
    lexical: LexicalIndex,
    vectors: VectorIndex,
    budget: int = DEFAULT_BUDGET,
    per_strategy_k: int = 10,
) -> ContextBundle:
    rankings: dict[str, list[Candidate]] = dict(graph_candidates(store, target, per_strategy_k))
    rankings["lexical"] = lexical_candidates(lexical, target, per_strategy_k)
    rankings["vector"] = vector_candidates(vectors, target, per_strategy_k)
    rankings = {k: v for k, v in rankings.items() if v}

    target_tokens = chunk_tokens(target)
    used = target_tokens
    items: list[ContextItem] = []
    excluded: list[ExcludedItem] = []

    for fused in reciprocal_rank_fusion(rankings):
        chunk = store.get_chunk(fused.chunk_id)
        if chunk is None:
            continue
        tokens = chunk_tokens(chunk)
        if used + tokens > budget:
            excluded.append(
                ExcludedItem(
                    chunk_id=chunk.id,
                    fused_score=fused.score,
                    tokens=tokens,
                    reason=f"over budget ({used}+{tokens} > {budget})",
                )
            )
            continue
        used += tokens
        items.append(
            ContextItem(
                chunk=chunk,
                fused_score=fused.score,
                sources=[
                    SourceHit(source=c.source, rank=c.rank, score=c.score, detail=c.detail)
                    for c in fused.contributions
                ],
                tokens=tokens,
            )
        )

    return ContextBundle(
        target=target,
        target_tokens=target_tokens,
        items=items,
        excluded=excluded,
        token_count=used,
        token_budget=budget,
        signals=store.signals_in(target.path, target.start_line, target.end_line),
    )


def render_bundle(bundle: ContextBundle) -> str:
    """Human-readable summary for `reviewer context`."""
    t = bundle.target
    lines = [
        f"== {t.symbol}  {t.path}:{t.start_line}-{t.end_line}  "
        f"[{bundle.token_count}/{bundle.token_budget} tokens, target {bundle.target_tokens}]"
    ]
    if not bundle.items:
        lines.append("   (no related context found)")
    for item in bundle.items:
        c = item.chunk
        lines.append(
            f"   {item.fused_score:.4f}  {c.symbol}  {c.path}:{c.start_line}-{c.end_line}  "
            f"({item.tokens} tok)"
        )
        for hit in item.sources:
            lines.append(f"            - {hit.source} #{hit.rank}: {hit.detail}")
    for sig in bundle.signals:
        lines.append(f"   signal {sig.ref} [{sig.severity}] line {sig.line_start}: {sig.message}")
    for ex in bundle.excluded:
        lines.append(f"   excluded {ex.chunk_id}: {ex.reason}")
    return "\n".join(lines)
