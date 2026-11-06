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
        "coverage",
        ".next",
        "bower_components",
        "site-packages",
        ".reviewer",
    }
)

PYTHON_SUFFIXES = frozenset({".py"})

# file suffix -> parser language
LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
}  # fmt: skip
# user-facing language names -> parser languages
LANGUAGE_GROUPS: dict[str, frozenset[str]] = {
    "python": frozenset({"python"}),
    "javascript": frozenset({"javascript"}),
    "typescript": frozenset({"typescript", "tsx"}),
}

# Generated or vendored files this large are not worth indexing.
MAX_FILE_BYTES = 1_000_000


def is_excluded_path(rel_path: PurePosixPath) -> bool:
    """True if any directory component of the path is on the exclusion list."""
    return any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in rel_path.parts[:-1])


def is_python_file(rel_path: PurePosixPath) -> bool:
    return rel_path.suffix in PYTHON_SUFFIXES


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def language_of(rel_path: PurePosixPath) -> str | None:
    """Parser language for a source file, or None. Declaration and minified files are skipped."""
    name = rel_path.name
    if name.endswith((".d.ts", ".d.mts", ".d.cts", ".min.js", ".min.mjs", ".bundle.js")):
        return None
    return LANGUAGE_BY_SUFFIX.get(rel_path.suffix)


def should_index(
    rel_path: PurePosixPath, size: int, languages: tuple[str, ...] = ("python",)
) -> bool:
    """Path-level checks; content-level checks (binary) happen after reading."""
    language = language_of(rel_path)
    wanted = {p for group in languages for p in LANGUAGE_GROUPS[group]}
    return language in wanted and not is_excluded_path(rel_path) and size <= MAX_FILE_BYTES
