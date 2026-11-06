"""Build a manifest of indexable files with content hashes."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from pydantic import BaseModel

from reviewer.ingest.filters import looks_binary, should_index
from reviewer.ingest.sources import Source, list_files


class FileEntry(BaseModel):
    path: str
    sha256: str
    size: int
    lines: int
    language: str = "python"


class Manifest(BaseModel):
    root: str
    commit_sha: str
    dirty: bool
    files: list[FileEntry]

    @property
    def tree_hash(self) -> str:
        """Stable hash over (path, content hash) pairs."""
        digest = hashlib.sha256()
        for entry in self.files:
            digest.update(f"{entry.path}\0{entry.sha256}\n".encode())
        return digest.hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(source: Source) -> Manifest:
    entries: list[FileEntry] = []
    for rel in list_files(source):
        full = source.root / rel
        try:
            size = full.stat().st_size
        except OSError:
            continue
        if not should_index(PurePosixPath(rel), size):
            continue
        data = full.read_bytes()
        if looks_binary(data):
            continue
        entries.append(
            FileEntry(
                path=rel,
                sha256=sha256_hex(data),
                size=len(data),
                lines=data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1),
            )
        )

    manifest = Manifest(
        root=str(source.root), commit_sha=source.commit_sha or "", dirty=source.dirty, files=entries
    )
    if not manifest.commit_sha:
        # Not a git repo: pin to the content so a run is still reproducible.
        manifest.commit_sha = f"dir-{manifest.tree_hash[:12]}"
    return manifest
