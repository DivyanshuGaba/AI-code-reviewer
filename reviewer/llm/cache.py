"""Response cache keyed by (model, prompt id + version, exact rendered request).

The rendered request already contains the code hash and all retrieved context, so any
change to the code, the context, the prompt version or the model is a miss, and
unchanged code costs nothing on a re-scan. Only validated outputs are stored.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY, model TEXT NOT NULL, prompt TEXT NOT NULL,
    text TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def cache_key(model: str, prompt_id: str, prompt_version: str, system: str, user: str) -> str:
    digest = hashlib.sha256()
    for part in (model, prompt_id, prompt_version, system, user):
        digest.update(part.encode())
        digest.update(b"\0")
    return digest.hexdigest()


class LLMCache:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.executescript(_SCHEMA)

    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT text FROM llm_cache WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def put(self, key: str, model: str, prompt: str, text: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_cache VALUES (?, ?, ?, ?, ?)",
            (key, model, prompt, text, datetime.now(UTC).isoformat()),
        )
        self.conn.commit()
