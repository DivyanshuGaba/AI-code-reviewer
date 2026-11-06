"""Open a repository from a local path or a git URL and pin it to a commit."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_URL_RE = re.compile(r"^(https?://|git@|ssh://|git://)")


class SourceError(RuntimeError):
    """Raised when a repository cannot be opened."""


@dataclass(frozen=True)
class Source:
    """A repository on disk.

    `commit_sha` is None when the directory is not a git repository root; callers
    then derive a pin from the manifest instead. `dirty` means the working tree has
    uncommitted changes, so file contents may differ from `commit_sha`.
    """

    root: Path
    origin: str
    commit_sha: str | None
    dirty: bool


def is_url(spec: str) -> bool:
    return bool(_URL_RE.match(spec))


def home_dir() -> Path:
    """Where clones and indexes live. Override with REVIEWER_HOME."""
    override = os.environ.get("REVIEWER_HOME")
    return Path(override) if override else Path.home() / ".cache" / "reviewer"


def clone_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"))
    return home_dir() / "repos" / f"{name}-{digest}"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SourceError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def is_git_root(root: Path) -> bool:
    """True only if `root` itself is the top level of a git work tree.

    A subdirectory of some larger repo is deliberately treated as a plain directory,
    so we never pin to (or read ignore rules from) an unrelated parent repository.
    """
    try:
        top = _git(root, "rev-parse", "--show-toplevel").strip()
    except (SourceError, FileNotFoundError):
        return False
    return Path(top).resolve() == root.resolve()


def _clone(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--quiet", url, str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SourceError(f"git clone failed: {result.stderr.strip()}")


def locate(spec: str) -> Path:
    """Resolve a spec to a directory without touching the network."""
    if is_url(spec):
        return clone_path(spec)
    path = Path(spec).expanduser()
    if not path.is_dir():
        raise SourceError(f"not a directory: {spec}")
    return path.resolve()


def open_source(spec: str) -> Source:
    """Open a local directory, or clone/update a URL into the cache."""
    if is_url(spec):
        dest = clone_path(spec)
        if (dest / ".git").is_dir():
            # Best effort: an offline machine can still index the cached clone.
            subprocess.run(
                ["git", "-C", str(dest), "pull", "--ff-only", "--quiet"],
                capture_output=True,
                check=False,
            )
        else:
            _clone(spec, dest)
        root = dest
    else:
        root = locate(spec)

    if not is_git_root(root):
        return Source(root=root, origin=spec, commit_sha=None, dirty=False)

    try:
        sha = _git(root, "rev-parse", "HEAD").strip()
    except SourceError:
        # A repository with no commits yet.
        return Source(root=root, origin=spec, commit_sha=None, dirty=True)
    dirty = bool(_git(root, "status", "--porcelain").strip())
    return Source(root=root, origin=spec, commit_sha=sha, dirty=dirty)


def list_files(source: Source) -> list[str]:
    """Repo-relative POSIX paths of candidate files, sorted.

    In a git repo this honours .gitignore (tracked plus untracked-but-not-ignored).
    Otherwise it walks the directory and prunes excluded directories.
    """
    from reviewer.ingest.filters import EXCLUDED_DIRS

    if is_git_root(source.root):
        out = _git(source.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        paths = {p for p in out.split("\0") if p}
        return sorted(p for p in paths if (source.root / p).is_file())

    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(source.root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            full = Path(dirpath) / name
            found.append(full.relative_to(source.root).as_posix())
    return sorted(found)
