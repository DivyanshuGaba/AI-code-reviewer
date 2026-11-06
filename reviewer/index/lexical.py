"""Lexical index: SQLite FTS5 over chunks, ranked with BM25.

Identifiers are split into sub-words at index and query time, so a search for
`user name` finds `get_user_name` and `UserName`. The index syncs itself from a
list of chunks by content hash, independent of how the chunks were stored.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from reviewer.index.models import Chunk
from reviewer.textutil import tokenize

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED, content_hash UNINDEXED, symbol, body,
    tokenize = "unicode61 remove_diacritics 2"
);
"""
_SYMBOL_WEIGHT = 5.0
_MAX_QUERY_TERMS = 30


@dataclass(frozen=True)
class LexicalHit:
    chunk_id: str
    score: float  # higher is better


class LexicalIndex:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.executescript(_SCHEMA)

    def sync(self, chunks: Iterable[Chunk]) -> tuple[int, int]:
        """Bring the index in line with `chunks`. Returns (upserted, removed)."""
        wanted = {c.id: c for c in chunks}
        rows = self.conn.execute("SELECT chunk_id, content_hash FROM chunks_fts").fetchall()
        existing = {r[0]: r[1] for r in rows}

        stale = {
            cid for cid, h in existing.items() if cid not in wanted or wanted[cid].content_hash != h
        }
        self.conn.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", [(c,) for c in stale])
        fresh = [c for cid, c in wanted.items() if cid not in existing or cid in stale]
        self.conn.executemany(
            "INSERT INTO chunks_fts(chunk_id, content_hash, symbol, body) VALUES (?, ?, ?, ?)",
            [
                (
                    c.id,
                    c.content_hash,
                    " ".join(tokenize(c.symbol)),
                    " ".join(tokenize(f"{c.header}\n{c.text}")),
                )
                for c in fresh
            ],
        )
        self.conn.commit()
        removed = len([cid for cid in stale if cid not in wanted])
        return len(fresh), removed

    def search(self, text: str, k: int = 20, exclude: Iterable[str] = ()) -> list[LexicalHit]:
        """BM25 search for any of the identifiers in `text`."""
        terms = list(dict.fromkeys(tokenize(text)))[:_MAX_QUERY_TERMS]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        skip = set(exclude)
        rows = self.conn.execute(
            """
            SELECT chunk_id, bm25(chunks_fts, 0.0, 0.0, ?, 1.0) AS rank
            FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?
            """,
            (_SYMBOL_WEIGHT, match, k + len(skip)),
        ).fetchall()
        hits = [
            LexicalHit(r["chunk_id"], -float(r["rank"])) for r in rows if r["chunk_id"] not in skip
        ]
        return hits[:k]
