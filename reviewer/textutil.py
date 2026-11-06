"""Shared identifier tokenization for the lexical index and the hashing embedder."""

from __future__ import annotations

import keyword
import re

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")

_STOP = frozenset(keyword.kwlist) | {"self", "cls", "str", "int", "len", "none"}


def split_identifier(ident: str) -> list[str]:
    """`getUserName` / `get_user_name` -> ['get', 'user', 'name']."""
    return [p.lower() for p in _CAMEL_RE.findall(ident)]


def tokenize(text: str) -> list[str]:
    """Lowercased identifiers plus their sub-words, stop words removed, order kept."""
    tokens: list[str] = []
    for ident in _IDENT_RE.findall(text):
        low = ident.lower().strip("_")
        if not low or low in _STOP:
            continue
        parts = split_identifier(ident)
        if len(parts) > 1:
            tokens.append(low)
        tokens.extend(p for p in parts if p not in _STOP and len(p) > 1)
        if len(parts) <= 1 and len(low) > 1 and low not in tokens[-1:]:
            tokens.append(low)
    return tokens
