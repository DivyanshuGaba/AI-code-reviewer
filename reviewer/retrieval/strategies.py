"""Candidate generators. Each returns ranked chunk ids tagged with why they matched."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from reviewer.index.lexical import LexicalIndex
from reviewer.index.models import Chunk
from reviewer.index.store import Relation, Store
from reviewer.index.vectors import VectorIndex, embedding_text

GRAPH_CALLER = "graph:caller"
GRAPH_CALLEE = "graph:callee"
GRAPH_TEST = "graph:test"
LEXICAL = "lexical"
VECTOR = "vector"


@dataclass(frozen=True)
class Candidate:
    chunk_id: str
    source: str
    rank: int  # 1-based position within its own strategy
    score: float
    detail: str


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        any(part in ("tests", "test", "__tests__") for part in p.parts[:-1])
        or p.name.startswith("test_")
        or p.name.endswith("_test.py")
        or p.name == "conftest.py"
        or ".test." in p.name  # foo.test.ts, foo.test.jsx
        or ".spec." in p.name
    )


def graph_candidates(store: Store, target: Chunk, per_kind: int = 10) -> dict[str, list[Candidate]]:
    """Exact signals: who calls the target, whom it calls, which tests exercise it."""
    symbol = next((s for s in store.find_symbols(target.id) if s.id == target.id), None)
    if symbol is None:
        return {}

    callers: list[Candidate] = []
    tests: list[Candidate] = []
    seen: set[str] = {target.id}
    for rel in store.callers(symbol.id):
        chunk = store.chunk_at(rel.symbol.path, rel.line)
        if chunk is None or chunk.id in seen:
            continue
        seen.add(chunk.id)
        detail = f"calls {symbol.qualname} at {rel.symbol.path}:{rel.line} [{rel.resolution}]"
        bucket, source = (
            (tests, GRAPH_TEST) if is_test_path(chunk.path) else (callers, GRAPH_CALLER)
        )
        bucket.append(Candidate(chunk.id, source, len(bucket) + 1, 1.0, detail))

    callees: list[Candidate] = []
    for rel in store.callees(symbol.id):
        chunk = _callee_chunk(store, rel)
        if chunk is None or chunk.id in seen:
            continue
        seen.add(chunk.id)
        detail = f"called from line {rel.line} [{rel.resolution}]"
        callees.append(Candidate(chunk.id, GRAPH_CALLEE, len(callees) + 1, 1.0, detail))

    result = {
        GRAPH_CALLER: callers[:per_kind],
        GRAPH_CALLEE: callees[:per_kind],
        GRAPH_TEST: tests[:per_kind],
    }
    return {k: v for k, v in result.items() if v}


def _callee_chunk(store: Store, rel: Relation) -> Chunk | None:
    return store.chunk_for_symbol(rel.symbol)


def lexical_candidates(lexical: LexicalIndex, target: Chunk, k: int) -> list[Candidate]:
    query = f"{target.symbol}\n{target.text}"
    hits = lexical.search(query, k=k, exclude=[target.id])
    return [
        Candidate(h.chunk_id, LEXICAL, i, h.score, "shared identifiers (BM25)")
        for i, h in enumerate(hits, 1)
    ]


def vector_candidates(vectors: VectorIndex, target: Chunk, k: int) -> list[Candidate]:
    hits = vectors.search(embedding_text(target), k=k, exclude=[target.id])
    return [
        Candidate(h.chunk_id, VECTOR, i, h.score, f"similar code (cosine {h.score:.2f})")
        for i, h in enumerate(hits, 1)
    ]
