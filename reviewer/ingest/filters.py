"""Decide which files in a repository are worth analyzing."""

from __future__ import annotations

from pathlib import PurePosixPath

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".eggs",
        "build",
        "dist",
        "site-packages",
        ".reviewer",
    }
)

PYTHON_SUFFIXES = frozenset({".py"})

# Generated or vendored files this large are not worth indexing.
MAX_FILE_BYTES = 1_000_000


def is_excluded_path(rel_path: PurePosixPath) -> bool:
    """True if any directory component of the path is on the exclusion list."""
    return any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in rel_path.parts[:-1])


def is_python_file(rel_path: PurePosixPath) -> bool:
    return rel_path.suffix in PYTHON_SUFFIXES


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def should_index(rel_path: PurePosixPath, size: int) -> bool:
    """Path-level checks; content-level checks (binary) happen after reading."""
    return is_python_file(rel_path) and not is_excluded_path(rel_path) and size <= MAX_FILE_BYTES
