"""Vector index over chunks, stored in Chroma.

Used for "similar code" and "same bug elsewhere" lookups; exact questions go to the
code graph and the lexical index first. Syncs by content hash, so unchanged chunks
are never re-embedded.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import chromadb
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings

from reviewer.index.models import Chunk
from reviewer.llm.embeddings import Embedder

_BATCH = 128
_COLLECTION = "chunks"


@dataclass(frozen=True)
class VectorHit:
    chunk_id: str
    score: float  # cosine similarity, higher is better


def _vectors(vecs: list[list[float]]) -> list[Sequence[float]]:
    """Chroma's stubs want Sequence[float] rows rather than list[list[float]]."""
    return [list(v) for v in vecs]


def embedding_text(chunk: Chunk) -> str:
    return f"{chunk.header}\n{chunk.text}"


class VectorIndex:
    def __init__(self, path: Path, embedder: Embedder):
        path.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self.client = chromadb.PersistentClient(
            path=str(path), settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self._open_collection()

    def _open_collection(self) -> Collection:
        meta = {"hnsw:space": "cosine", "embedder": self.embedder.name}
        collection = self.client.get_or_create_collection(_COLLECTION, metadata=meta)
        if (collection.metadata or {}).get("embedder") != self.embedder.name:
            # Vectors from a different model are not comparable: start over.
            self.client.delete_collection(_COLLECTION)
            collection = self.client.create_collection(_COLLECTION, metadata=meta)
        return collection

    def sync(self, chunks: Iterable[Chunk]) -> tuple[int, int]:
        """Embed new/changed chunks, drop removed ones. Returns (upserted, removed)."""
        wanted = {c.id: c for c in chunks}
        got = self.collection.get(include=["metadatas"])
        existing = {
            cid: (meta or {}).get("content_hash")
            for cid, meta in zip(got["ids"], got["metadatas"] or [], strict=False)
        }

        stale = [cid for cid in existing if cid not in wanted]
        for i in range(0, len(stale), _BATCH):
            self.collection.delete(ids=stale[i : i + _BATCH])

        fresh = [c for cid, c in wanted.items() if existing.get(cid) != c.content_hash]
        for i in range(0, len(fresh), _BATCH):
            batch = fresh[i : i + _BATCH]
            self.collection.upsert(
                ids=[c.id for c in batch],
                embeddings=_vectors(self.embedder.embed([embedding_text(c) for c in batch])),
                metadatas=[{"content_hash": c.content_hash, "path": c.path} for c in batch],
            )
        return len(fresh), len(stale)

    def count(self) -> int:
        return int(self.collection.count())

    def search(self, text: str, k: int = 20, exclude: Iterable[str] = ()) -> list[VectorHit]:
        total = self.count()
        if total == 0:
            return []
        skip = set(exclude)
        res = self.collection.query(
            query_embeddings=_vectors(self.embedder.embed([text])),
            n_results=min(total, k + len(skip)),
            include=["distances"],
        )
        ids = res["ids"][0]
        distances = (res["distances"] or [[]])[0]
        hits = [
            VectorHit(i, 1.0 - float(d))
            for i, d in zip(ids, distances, strict=True)
            if i not in skip
        ]
        return hits[:k]
