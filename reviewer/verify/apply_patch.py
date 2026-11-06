"""Strict unified-diff handling: build diffs from edits, parse them, apply them safely.

The applier is deliberately unforgiving: hunks must match the file exactly (no fuzz, no
offset search), only existing regular files inside the repository may be modified, and
creating, deleting, renaming or following symlinks is refused. A patch is untrusted input.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
NO_NEWLINE = "\\ No newline at end of file"


class PatchError(ValueError):
    """The patch is malformed, unsafe, or does not apply."""


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_len: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (" " | "-" | "+", text)
    no_newline_after: set[int] = field(default_factory=set)  # indexes into `lines`


@dataclass
class FilePatch:
    path: str
    hunks: list[Hunk]


# -- building ------------------------------------------------------------------------------


def make_diff(path: str, old: str, new: str, context: int = 3) -> str:
    """Unified diff turning `old` into `new` for the repo-relative `path` ('' if identical)."""
    old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
    out: list[str] = []
    for line in difflib.unified_diff(
        old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}", n=context
    ):
        if line.endswith("\n"):
            out.append(line)
        else:  # a body line with no trailing newline: git's marker convention
            out.append(line + "\n" + NO_NEWLINE + "\n")
    return "".join(out)


def count_changed_lines(diff: str) -> int:
    return sum(
        1 for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---"))
    )


# -- parsing -------------------------------------------------------------------------------


def _clean_path(raw: str) -> str:
    raw = raw.split("\t")[0].strip()
    if raw == "/dev/null":
        raise PatchError("creating or deleting files is not allowed")
    for prefix in ("a/", "b/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    path = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[0] == ".git"
    ):
        raise PatchError(f"unsafe path in patch: {raw!r}")
    return path.as_posix()


def parse_patch(text: str) -> list[FilePatch]:
    lines = text.splitlines()
    patches: list[FilePatch] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(
            ("rename ", "copy ", "new file", "deleted file", "GIT binary", "Binary")
        ):
            raise PatchError(f"unsupported patch operation: {line.split(' ', 2)[0]}")
        if line.startswith("--- "):
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise PatchError("'---' header without a '+++' header")
            old_path, new_path = _clean_path(line[4:]), _clean_path(lines[i + 1][4:])
            if old_path != new_path:
                raise PatchError("renames are not allowed")
            hunks, i = _parse_hunks(lines, i + 2)
            if not hunks:
                raise PatchError(f"no hunks for {old_path}")
            patches.append(FilePatch(old_path, hunks))
            continue
        i += 1
    if not patches:
        raise PatchError("no file changes found in patch")
    return patches


def _parse_hunks(lines: list[str], i: int) -> tuple[list[Hunk], int]:
    hunks: list[Hunk] = []
    while i < len(lines) and (m := _HUNK_RE.match(lines[i])):
        old_start, old_len = int(m.group(1)), int(m.group(2) or 1)
        new_len = int(m.group(4) or 1)
        hunk = Hunk(old_start, old_len, new_len)
        i += 1
        seen_old = seen_new = 0
        while i < len(lines) and (seen_old < old_len or seen_new < new_len):
            line = lines[i]
            op, text = (line[:1], line[1:]) if line else (" ", "")
            if op == "\\":
                hunk.no_newline_after.add(len(hunk.lines) - 1)
            elif op == " ":
                hunk.lines.append((" ", text))
                seen_old += 1
                seen_new += 1
            elif op == "-":
                hunk.lines.append(("-", text))
                seen_old += 1
            elif op == "+":
                hunk.lines.append(("+", text))
                seen_new += 1
            else:
                raise PatchError(f"unexpected line in hunk: {line[:40]!r}")
            i += 1
        while i < len(lines) and lines[i].startswith("\\"):  # marker after the final line
            hunk.no_newline_after.add(len(hunk.lines) - 1)
            i += 1
        if seen_old != old_len or seen_new != new_len:
            raise PatchError("hunk line counts do not match its header")
        hunks.append(hunk)
    return hunks, i


# -- applying ------------------------------------------------------------------------------


def apply_to_text(original: str, patch: FilePatch) -> str:
    """Apply hunks to `original` exactly; raise PatchError on any mismatch."""
    src = original.splitlines(keepends=True)
    out: list[str] = []
    pos = 0  # next unread index in src
    for hunk in patch.hunks:
        start = hunk.old_start - 1 if hunk.old_len else hunk.old_start
        if start < pos or start > len(src):
            raise PatchError(f"hunk @@ -{hunk.old_start} is out of order or out of range")
        out.extend(src[pos:start])
        pos = start
        for idx, (op, text) in enumerate(hunk.lines):
            if op in " -":
                if pos >= len(src) or src[pos].rstrip("\n") != text:
                    raise PatchError(f"context mismatch near line {pos + 1} of {patch.path}")
                if op == " ":
                    out.append(src[pos])
                pos += 1
            else:
                missing_newline = idx in hunk.no_newline_after
                out.append(text if missing_newline else text + "\n")
    out.extend(src[pos:])
    return "".join(out)


def apply_patch(root: Path, diff: str, allowed_paths: set[str] | None = None) -> list[str]:
    """Apply `diff` under `root`. Returns the changed paths. All-or-nothing.

    Raises PatchError for anything unsafe: paths outside `allowed_paths` or the repo,
    symlinks, missing files, or hunks that do not match.
    """
    root = root.resolve()
    pending: dict[Path, str] = {}
    for patch in parse_patch(diff):
        if allowed_paths is not None and patch.path not in allowed_paths:
            raise PatchError(f"patch touches a file outside the allowed set: {patch.path}")
        target = root / patch.path
        if target.is_symlink() or not target.is_file():
            raise PatchError(f"not an existing regular file: {patch.path}")
        if root not in target.resolve().parents:
            raise PatchError(f"path escapes the repository: {patch.path}")
        try:
            original = target.read_bytes().decode("utf-8")
        except UnicodeDecodeError as err:
            raise PatchError(f"{patch.path} is not valid UTF-8") from err
        pending[target] = apply_to_text(pending.get(target, original), patch)
    for target, content in pending.items():  # only write once every file applied cleanly
        target.write_bytes(content.encode("utf-8"))
    return sorted(p.relative_to(root).as_posix() for p in pending)
