"""Turn a function or method into a retrieval chunk with a contextual header."""

from __future__ import annotations

import hashlib

from reviewer.index.models import Chunk


def build_header(path: str, class_name: str | None, signature: str) -> str:
    parts = [f"# file: {path}"]
    if class_name:
        parts.append(f"# class: {class_name}")
    parts.append(f"# signature: {signature}")
    return "\n".join(parts)


def make_chunk(
    *,
    chunk_id: str,
    path: str,
    start_line: int,
    end_line: int,
    qualname: str,
    class_name: str | None,
    signature: str,
    lines: list[str],
) -> Chunk:
    text = "\n".join(lines[start_line - 1 : end_line])
    return Chunk(
        id=chunk_id,
        path=path,
        start_line=start_line,
        end_line=end_line,
        language="python",
        symbol=qualname,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
        header=build_header(path, class_name, signature),
    )
