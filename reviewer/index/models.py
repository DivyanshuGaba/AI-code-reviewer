"""Typed records produced by the indexer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

SymbolKind = Literal["module", "class", "function", "method"]
Resolution = Literal["self", "local", "import", "name", "ambiguous"]


class Symbol(BaseModel):
    id: str
    path: str
    name: str
    qualname: str
    kind: SymbolKind
    parent: str | None  # qualname of the enclosing class/function, None at module level
    start_line: int
    end_line: int
    signature: str


class ImportRef(BaseModel):
    """One name bound by an import statement.

    `import a.b as c`     -> module="a.b", name=None, alias="c"
    `from a import b as c` -> module="a",   name="b",  alias="c"
    `from . import b`      -> module=".",   name="b"
    `from a import *`      -> module="a",   name="*"
    """

    path: str
    module: str
    name: str | None
    alias: str | None
    line: int

    @property
    def bound_name(self) -> str:
        if self.alias:
            return self.alias
        if self.name:
            return self.name
        return self.module.split(".")[0]


class CallSite(BaseModel):
    path: str
    caller_id: str
    callee_name: str  # `foo` in foo(), `bar` in a.b.bar()
    qualifier: str | None  # `a.b` in a.b.bar(); None if absent or not a plain dotted name
    line: int


class Chunk(BaseModel):
    """One function or method, with a contextual header for retrieval."""

    id: str
    path: str
    start_line: int
    end_line: int
    language: str
    symbol: str  # qualname
    content_hash: str
    text: str
    header: str


class ParsedFile(BaseModel):
    path: str
    symbols: list[Symbol]
    imports: list[ImportRef]
    calls: list[CallSite]
    chunks: list[Chunk]
    has_syntax_errors: bool = False


class Edge(BaseModel):
    caller_id: str
    callee_id: str
    line: int
    resolution: Resolution
