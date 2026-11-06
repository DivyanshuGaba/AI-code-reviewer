"""Embedding models behind one small interface.

Only this module knows how text becomes vectors, so the vector index and retrieval
code never depend on a provider. `HashingEmbedder` is the default: deterministic,
offline, no model download. It captures identifier overlap (a bag of sub-words), not
semantics. A neural embedder can be added by implementing `Embedder`.
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

from reviewer.textutil import tokenize


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Signed feature hashing of identifier sub-words, L2-normalised."""

    def __init__(self, dim: int = 384):
        self.dim = dim
        self.name = f"hashing-{dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        counts: dict[str, int] = {}
        for tok in tokenize(text):
            counts[tok] = counts.get(tok, 0) + 1
        for tok, n in counts.items():
            digest = hashlib.blake2b(tok.encode(), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign * (1.0 + math.log(n))
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            vec[0] = 1.0  # keep the vector valid for cosine distance
            return vec
        return [v / norm for v in vec]


def default_embedder() -> Embedder:
    return HashingEmbedder()
