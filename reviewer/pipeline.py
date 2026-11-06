"""Orchestrates ingest -> parse -> store -> graph. Phase 1 stops after the code graph."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from reviewer.index.graph import resolve_edges
from reviewer.index.parser import parse_python
from reviewer.index.store import Store
from reviewer.ingest.manifest import build_manifest
from reviewer.ingest.sources import home_dir, locate, open_source


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


def db_path_for(root: Path) -> Path:
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
    return home_dir() / "indexes" / f"{root.resolve().name}-{digest}.db"


def db_path_for_spec(spec: str) -> Path:
    return db_path_for(locate(spec))


def index_repository(spec: str) -> IndexReport:
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
        )
