"""Dispatch from a file's language to its parser, and per-language syntax checks."""

from __future__ import annotations

import ast

from reviewer.index.models import ParsedFile
from reviewer.index.parse_js import _LANGUAGES, parse_js
from reviewer.index.parser import parse_python


def parse_file(path: str, source: bytes, language: str) -> ParsedFile:
    if language == "python":
        return parse_python(path, source)
    if language in ("javascript", "typescript", "tsx"):
        return parse_js(path, source, language)
    raise ValueError(f"unsupported language: {language}")


def syntax_error(path: str, text: str, language: str) -> str | None:
    """A short description of the first syntax error, or None. Parsing only: nothing runs."""
    if language == "python":
        try:
            ast.parse(text, filename=path)
        except SyntaxError as err:
            return f"line {err.lineno}: {err.msg}"
        return None
    from tree_sitter import Node, Parser

    tree = Parser(_LANGUAGES[language]).parse(text.encode("utf-8"))
    if not tree.root_node.has_error:
        return None

    def first_error(node: Node) -> Node | None:
        if node.is_error or node.is_missing:
            return node
        return next((e for c in node.children if (e := first_error(c)) is not None), None)

    bad = first_error(tree.root_node)
    return f"line {bad.start_point.row + 1}: syntax error" if bad else "syntax error"
