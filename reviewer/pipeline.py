"""Orchestrates ingest -> parse -> store -> graph. Phase 1 stops after the code graph."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from reviewer.index.graph import resolve_edges
from reviewer.index.lexical import LexicalIndex
from reviewer.index.parser import parse_python
from reviewer.index.store import Store
from reviewer.index.vectors import VectorIndex
from reviewer.ingest.manifest import build_manifest
from reviewer.ingest.sources import SourceError, home_dir, locate, open_source
from reviewer.llm.embeddings import Embedder, default_embedder
from reviewer.retrieval.budget import DEFAULT_BUDGET
from reviewer.retrieval.context import ContextBundle, build_context


@dataclass(frozen=True)
class IndexReport:
    root: Path
    db_path: Path
    commit_sha: str
    dirty: bool
    files_total: int
    files_changed: int
    files_removed: int
    files_with_syntax_errors: int
    counts: dict[str, int]
    chunks_embedded: int
    embedder: str


def db_path_for(root: Path) -> Path:
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
    return home_dir() / "indexes" / f"{root.resolve().name}-{digest}.db"


def vector_path_for(db_path: Path) -> Path:
    return db_path.with_suffix(".chroma")


def db_path_for_spec(spec: str) -> Path:
    return db_path_for(locate(spec))


def index_repository(spec: str, embedder: Embedder | None = None) -> IndexReport:
    """Index a repository. Files whose content hash is unchanged are not re-parsed."""
    source = open_source(spec)
    manifest = build_manifest(source)
    db_path = db_path_for(source.root)

    with Store(db_path) as store:
        stored = store.stored_hashes()
        current = {f.path: f for f in manifest.files}

        removed = sorted(set(stored) - set(current))
        store.remove_files(removed)

        changed = 0
        syntax_errors = 0
        for entry in manifest.files:
            if stored.get(entry.path) == entry.sha256:
                continue
            parsed = parse_python(entry.path, (source.root / entry.path).read_bytes())
            syntax_errors += parsed.has_syntax_errors
            store.add_file(entry, parsed)
            changed += 1

        store.replace_edges(
            resolve_edges(store.load_symbols(), store.load_imports(), store.load_calls())
        )
        chunks = store.load_chunks()
        LexicalIndex(store.conn).sync(chunks)
        embedder = embedder or default_embedder()
        embedded, _ = VectorIndex(vector_path_for(db_path), embedder).sync(chunks)

        store.set_meta("commit_sha", manifest.commit_sha)
        store.set_meta("dirty", "1" if manifest.dirty else "0")
        store.set_meta("root", str(source.root))
        store.set_meta("tree_hash", manifest.tree_hash)
        store.set_meta("indexed_at", datetime.now(UTC).isoformat())

        return IndexReport(
            root=source.root,
            db_path=db_path,
            commit_sha=manifest.commit_sha,
            dirty=manifest.dirty,
            files_total=len(manifest.files),
            files_changed=changed,
            files_removed=len(removed),
            files_with_syntax_errors=syntax_errors,
            counts=store.counts(),
            chunks_embedded=embedded,
            embedder=embedder.name,
        )


def context_for_file(
    spec: str,
    file: str,
    symbol: str | None = None,
    budget: int = DEFAULT_BUDGET,
    embedder: Embedder | None = None,
) -> list[ContextBundle]:
    """ContextBundles for every chunk in `file` (or just the chunk named `symbol`)."""
    root = locate(spec)
    db_path = db_path_for(root)
    if not db_path.exists():
        raise SourceError(f"no index for {spec}; run `reviewer index {spec}` first")

    candidate = Path(file)
    abs_path = candidate if candidate.is_absolute() else (Path.cwd() / candidate)
    try:
        rel = abs_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = candidate.as_posix()  # already repo-relative

    with Store(db_path) as store:
        chunks = store.load_chunks(rel)
        if symbol:
            chunks = [c for c in chunks if c.symbol == symbol or c.symbol.endswith(f".{symbol}")]
        if not chunks:
            raise SourceError(
                f"no indexed functions found in {rel}" + (f" matching {symbol}" if symbol else "")
            )
        lexical = LexicalIndex(store.conn)
        vectors = VectorIndex(vector_path_for(db_path), embedder or default_embedder())
        return [build_context(c, store, lexical, vectors, budget) for c in chunks]
