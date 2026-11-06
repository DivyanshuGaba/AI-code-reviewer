"""SQLite store for the manifest, symbols, imports, calls, chunks and call graph."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from reviewer.index.models import CallSite, Chunk, Edge, ImportRef, ParsedFile, Symbol
from reviewer.ingest.manifest import FileEntry

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    lines INTEGER NOT NULL, language TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    id TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL, qualname TEXT NOT NULL,
    kind TEXT NOT NULL, parent TEXT, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
    signature TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, module TEXT NOT NULL,
    name TEXT, alias TEXT, line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imports_path ON imports(path);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, caller_id TEXT NOT NULL,
    callee_name TEXT NOT NULL, qualifier TEXT, line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_path ON calls(path);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY, path TEXT NOT NULL, start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL, language TEXT NOT NULL, symbol TEXT NOT NULL,
    content_hash TEXT NOT NULL, text TEXT NOT NULL, header TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
CREATE TABLE IF NOT EXISTS edges (
    caller_id TEXT NOT NULL, callee_id TEXT NOT NULL, line INTEGER NOT NULL,
    resolution TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_caller ON edges(caller_id);
CREATE INDEX IF NOT EXISTS idx_edges_callee ON edges(callee_id);
"""

_PER_FILE_TABLES = ("symbols", "imports", "calls", "chunks")


@dataclass(frozen=True)
class Relation:
    """A symbol on the other end of a call edge."""

    symbol: Symbol
    line: int
    resolution: str


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.conn.close()

    # -- meta -------------------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    # -- incremental file sync ---------------------------------------------------------

    def stored_hashes(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT path, sha256 FROM files").fetchall()
        return {r["path"]: r["sha256"] for r in rows}

    def remove_files(self, paths: Iterable[str]) -> None:
        for path in paths:
            for table in (*_PER_FILE_TABLES, "files"):
                self.conn.execute(f"DELETE FROM {table} WHERE path = ?", (path,))
        self.conn.commit()

    def add_file(self, entry: FileEntry, parsed: ParsedFile) -> None:
        self.remove_files([entry.path])
        c = self.conn
        c.execute(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?)",
            (entry.path, entry.sha256, entry.size, entry.lines, entry.language),
        )
        c.executemany(
            "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    s.id,
                    s.path,
                    s.name,
                    s.qualname,
                    s.kind,
                    s.parent,
                    s.start_line,
                    s.end_line,
                    s.signature,
                )
                for s in parsed.symbols
            ],
        )
        c.executemany(
            "INSERT INTO imports(path, module, name, alias, line) VALUES (?, ?, ?, ?, ?)",
            [(i.path, i.module, i.name, i.alias, i.line) for i in parsed.imports],
        )
        c.executemany(
            "INSERT INTO calls(path, caller_id, callee_name, qualifier, line) VALUES (?,?,?,?,?)",
            [(k.path, k.caller_id, k.callee_name, k.qualifier, k.line) for k in parsed.calls],
        )
        c.executemany(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    k.id,
                    k.path,
                    k.start_line,
                    k.end_line,
                    k.language,
                    k.symbol,
                    k.content_hash,
                    k.text,
                    k.header,
                )
                for k in parsed.chunks
            ],
        )
        c.commit()

    def replace_edges(self, edges: Iterable[Edge]) -> None:
        self.conn.execute("DELETE FROM edges")
        self.conn.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?)",
            [(e.caller_id, e.callee_id, e.line, e.resolution) for e in edges],
        )
        self.conn.commit()

    # -- loading ------------------------------------------------------------------------

    def load_symbols(self) -> list[Symbol]:
        rows = self.conn.execute("SELECT * FROM symbols").fetchall()
        return [Symbol(**dict(r)) for r in rows]

    def load_imports(self) -> list[ImportRef]:
        rows = self.conn.execute(
            "SELECT path, module, name, alias, line FROM imports ORDER BY id"
        ).fetchall()
        return [ImportRef(**dict(r)) for r in rows]

    def load_calls(self) -> list[CallSite]:
        rows = self.conn.execute(
            "SELECT path, caller_id, callee_name, qualifier, line FROM calls ORDER BY id"
        ).fetchall()
        return [CallSite(**dict(r)) for r in rows]

    def load_chunks(self, path: str | None = None) -> list[Chunk]:
        if path is None:
            rows = self.conn.execute("SELECT * FROM chunks ORDER BY path, start_line").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM chunks WHERE path = ? ORDER BY start_line", (path,)
            ).fetchall()
        return [Chunk(**dict(r)) for r in rows]

    # -- graph queries --------------------------------------------------------------------

    def find_symbols(self, query: str) -> list[Symbol]:
        """Match by id, qualname, bare name, or trailing qualname (`Class.method`)."""
        rows = self.conn.execute(
            """
            SELECT * FROM symbols
            WHERE kind != 'module'
              AND (id = ? OR qualname = ? OR name = ? OR qualname LIKE ? ESCAPE '\\')
            ORDER BY path, start_line
            """,
            (query, query, query, "%." + query.replace("_", "\\_").replace("%", "\\%")),
        ).fetchall()
        return [Symbol(**dict(r)) for r in rows]

    def callers(self, symbol_id: str) -> list[Relation]:
        rows = self.conn.execute(
            """
            SELECT s.*, e.line AS call_line, e.resolution AS resolution
            FROM edges e JOIN symbols s ON s.id = e.caller_id
            WHERE e.callee_id = ? ORDER BY s.path, e.line
            """,
            (symbol_id,),
        ).fetchall()
        return [_relation(r) for r in rows]

    def callees(self, symbol_id: str) -> list[Relation]:
        rows = self.conn.execute(
            """
            SELECT s.*, e.line AS call_line, e.resolution AS resolution
            FROM edges e JOIN symbols s ON s.id = e.callee_id
            WHERE e.caller_id = ? ORDER BY e.line
            """,
            (symbol_id,),
        ).fetchall()
        return [_relation(r) for r in rows]

    def unresolved_calls(self, symbol_id: str) -> list[str]:
        """Names called from a symbol that produced no edge (builtins, third-party...)."""
        rows = self.conn.execute(
            """
            SELECT DISTINCT
                CASE WHEN c.qualifier IS NULL THEN c.callee_name
                     ELSE c.qualifier || '.' || c.callee_name END AS display
            FROM calls c
            WHERE c.caller_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM edges e
                  WHERE e.caller_id = c.caller_id AND e.line = c.line
              )
            ORDER BY display
            """,
            (symbol_id,),
        ).fetchall()
        return [str(r["display"]) for r in rows]

    def counts(self) -> dict[str, int]:
        return {
            table: int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("files", "symbols", "chunks", "calls", "edges")
        }


def _relation(row: sqlite3.Row) -> Relation:
    data = dict(row)
    line = data.pop("call_line")
    resolution = data.pop("resolution")
    return Relation(symbol=Symbol(**data), line=line, resolution=resolution)
