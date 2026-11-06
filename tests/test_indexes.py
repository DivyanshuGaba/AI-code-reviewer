from __future__ import annotations

import sqlite3
from pathlib import Path

from reviewer.index.lexical import LexicalIndex
from reviewer.index.models import Chunk
from reviewer.index.vectors import VectorIndex
from reviewer.llm.embeddings import HashingEmbedder


def make_chunk(symbol: str, text: str, path: str = "m.py") -> Chunk:
    import hashlib

    return Chunk(
        id=f"{path}::{symbol}:1",
        path=path,
        start_line=1,
        end_line=2,
        language="python",
        symbol=symbol,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
        header=f"# file: {path}\n# signature: def {symbol}()",
    )


CHUNKS = [
    make_chunk("get_user_name", "def get_user_name(user):\n    return user.name"),
    make_chunk("charge_card", "def charge_card(card, amount):\n    return card.charge(amount)"),
    make_chunk("parse_config", "def parse_config(path):\n    return json.load(open(path))"),
]


def lexical() -> LexicalIndex:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return LexicalIndex(conn)


def test_lexical_matches_identifier_subwords() -> None:
    idx = lexical()
    idx.sync(CHUNKS)
    hits = idx.search("user name")
    assert hits and hits[0].chunk_id == CHUNKS[0].id
    assert idx.search("nonexistent_thing") == []


def test_lexical_symbol_matches_outrank_body_matches() -> None:
    idx = lexical()
    body_only = make_chunk("helper", "def helper():\n    charge_card()\n    charge_card()")
    idx.sync([*CHUNKS, body_only])
    assert idx.search("charge card")[0].chunk_id == CHUNKS[1].id


def test_lexical_sync_is_incremental() -> None:
    idx = lexical()
    assert idx.sync(CHUNKS) == (3, 0)
    assert idx.sync(CHUNKS) == (0, 0)

    changed = make_chunk("get_user_name", "def get_user_name(u):\n    return u.nickname")
    assert idx.sync([changed, CHUNKS[1]]) == (1, 1)  # one rewritten, parse_config removed
    assert idx.search("nickname")[0].chunk_id == changed.id
    assert idx.search("parse config") == []


def test_lexical_excludes_requested_ids() -> None:
    idx = lexical()
    idx.sync(CHUNKS)
    assert all(h.chunk_id != CHUNKS[0].id for h in idx.search("user name", exclude=[CHUNKS[0].id]))


def test_hashing_embedder_is_deterministic_and_normalised() -> None:
    e = HashingEmbedder()
    a, b = e.embed(["def charge_card(card)"]), e.embed(["def charge_card(card)"])
    assert a == b
    assert abs(sum(v * v for v in a[0]) - 1.0) < 1e-9
    assert len(e.embed([""])[0]) == e.dim  # empty text still yields a valid vector


class CountingEmbedder(HashingEmbedder):
    def __init__(self, dim: int = 384, name: str | None = None):
        super().__init__(dim)
        if name:
            self.name = name
        self.embedded = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded += len(texts)
        return super().embed(texts)


def test_vector_search_ranks_similar_code_first(tmp_path: Path) -> None:
    vi = VectorIndex(tmp_path / "v", HashingEmbedder())
    vi.sync(CHUNKS)
    hits = vi.search("def charge_card(card, amount): card.charge(amount)", k=3)
    assert hits[0].chunk_id == CHUNKS[1].id
    assert hits[0].score > hits[-1].score
    excluded = vi.search("charge card", k=3, exclude=[CHUNKS[1].id])
    assert CHUNKS[1].id not in [h.chunk_id for h in excluded]


def test_vector_sync_only_embeds_new_or_changed_and_drops_removed(tmp_path: Path) -> None:
    emb = CountingEmbedder()
    vi = VectorIndex(tmp_path / "v", emb)
    assert vi.sync(CHUNKS) == (3, 0) and emb.embedded == 3
    assert vi.sync(CHUNKS) == (0, 0) and emb.embedded == 3

    changed = make_chunk("charge_card", "def charge_card(c):\n    return 1")
    assert vi.sync([CHUNKS[0], changed]) == (1, 1)
    assert vi.count() == 2


def test_vector_index_rebuilds_when_embedder_changes(tmp_path: Path) -> None:
    VectorIndex(tmp_path / "v", CountingEmbedder(name="model-a")).sync(CHUNKS)
    other = CountingEmbedder(name="model-b")
    vi = VectorIndex(tmp_path / "v", other)
    assert vi.count() == 0
    assert vi.sync(CHUNKS) == (3, 0)
