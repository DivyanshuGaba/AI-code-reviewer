"""Which lines changed relative to a base ref, so a review can be limited to a pull request."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from reviewer.ingest.sources import SourceError

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@^~{}-]*$")


class DiffError(SourceError):
    """The diff against the base could not be computed."""


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotepath=off", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise DiffError(proc.stderr.strip() or f"git {args[0]} failed")
    return proc.stdout


def parse_zero_context_diff(text: str) -> dict[str, list[tuple[int, int]]]:
    """New-file line ranges per path from `git diff -U0`. A pure deletion is a one-line point."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].split("\t")[0]
            current = target[2:] if target.startswith("b/") else None  # None for /dev/null
            if current is not None and current.startswith('"'):
                current = None  # quoted (unusual) paths are skipped rather than mis-parsed
        elif current is not None and (m := _HUNK_RE.match(line)):
            start = int(m.group(1))
            length = int(m.group(2)) if m.group(2) is not None else 1
            ranges.setdefault(current, []).append((start, start + max(length, 1) - 1))
    return ranges


def changed_ranges(root: Path, base: str) -> dict[str, list[tuple[int, int]]]:
    """Lines changed between the merge-base of `base` and HEAD, plus uncommitted edits."""
    if not _REF_RE.match(base):
        raise DiffError(f"not a valid ref name: {base!r}")
    _git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    try:
        merge_base = _git(root, "merge-base", "--end-of-options", base, "HEAD").strip()
    except DiffError as err:
        raise DiffError(
            f"no common history with {base!r} (a shallow clone? fetch the full history, e.g. "
            "actions/checkout with fetch-depth: 0)"
        ) from err
    diff = _git(
        root, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", merge_base, "--"
    )
    return parse_zero_context_diff(diff)


def overlaps(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= hi and lo <= end for lo, hi in ranges)
