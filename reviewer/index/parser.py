"""Tree-sitter parsing of Python source into symbols, imports and call sites.

This is the only module that touches Tree-sitter. Supporting another language means
adding a sibling adapter that returns the same `ParsedFile`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from reviewer.index.chunker import make_chunk
from reviewer.index.models import CallSite, ImportRef, ParsedFile, Symbol

_LANGUAGE = Language(tspython.language())
_DOTTED_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")


@dataclass(frozen=True)
class _Scope:
    symbol_id: str
    qualname: str
    kind: str


def _text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", errors="replace")


def _collapse(text: str) -> str:
    return " ".join(text.split())


def parse_python(path: str, source: bytes) -> ParsedFile:
    parser = Parser(_LANGUAGE)
    tree = parser.parse(source)
    lines = source.decode("utf-8", errors="replace").splitlines()

    module_id = f"{path}::<module>:1"
    module_symbol = Symbol(
        id=module_id,
        path=path,
        name="<module>",
        qualname="<module>",
        kind="module",
        parent=None,
        start_line=1,
        end_line=max(len(lines), 1),
        signature=f"<module {path}>",
    )
    result = ParsedFile(
        path=path,
        symbols=[module_symbol],
        imports=[],
        calls=[],
        chunks=[],
        has_syntax_errors=tree.root_node.has_error,
    )
    _visit(tree.root_node, [_Scope(module_id, "<module>", "module")], result, lines)
    return result


def _visit(node: Node, scopes: list[_Scope], out: ParsedFile, lines: list[str]) -> None:
    for child in node.children:
        kind = child.type
        if kind == "decorated_definition":
            for part in child.children:
                if part.type == "decorator":
                    _visit(part, scopes, out, lines)  # decorator calls belong to the outer scope
            definition = child.child_by_field_name("definition")
            if definition is not None:
                _define(definition, child, scopes, out, lines)
        elif kind in ("function_definition", "class_definition"):
            _define(child, child, scopes, out, lines)
        elif kind in ("import_statement", "import_from_statement"):
            _record_import(child, out)
        elif kind == "call":
            _record_call(child, scopes[-1], out)
            _visit(child, scopes, out, lines)
        else:
            _visit(child, scopes, out, lines)


def _define(
    node: Node, outer: Node, scopes: list[_Scope], out: ParsedFile, lines: list[str]
) -> None:
    """Record a def/class. `outer` includes decorators and sets the start line."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node)
    parent = scopes[-1]
    qualname = name if parent.kind == "module" else f"{parent.qualname}.{name}"
    start_line = outer.start_point.row + 1
    end_line = node.end_point.row + 1

    if node.type == "class_definition":
        kind = "class"
    else:
        kind = "method" if parent.kind == "class" else "function"

    body = node.child_by_field_name("body")
    header_bytes = (node.text or b"")[: (body.start_byte - node.start_byte)] if body else b""
    signature = _collapse(header_bytes.decode("utf-8", errors="replace")).rstrip(":").strip()

    symbol_id = f"{out.path}::{qualname}:{start_line}"
    out.symbols.append(
        Symbol(
            id=symbol_id,
            path=out.path,
            name=name,
            qualname=qualname,
            kind=kind,  # type: ignore[arg-type]
            parent=None if parent.kind == "module" else parent.qualname,
            start_line=start_line,
            end_line=end_line,
            signature=signature,
        )
    )

    # One chunk per function/method. Nested functions stay inside their parent's chunk.
    in_function = any(s.kind in ("function", "method") for s in scopes)
    if kind in ("function", "method") and not in_function:
        class_name = parent.qualname if parent.kind == "class" else None
        out.chunks.append(
            make_chunk(
                chunk_id=symbol_id,
                path=out.path,
                start_line=start_line,
                end_line=end_line,
                qualname=qualname,
                class_name=class_name,
                signature=signature,
                lines=lines,
            )
        )

    if body is not None:
        _visit(body, [*scopes, _Scope(symbol_id, qualname, kind)], out, lines)


def _record_import(node: Node, out: ParsedFile) -> None:
    line = node.start_point.row + 1
    if node.type == "import_statement":
        for item in node.children_by_field_name("name"):
            if item.type == "aliased_import":
                mod = item.child_by_field_name("name")
                alias = item.child_by_field_name("alias")
                if mod is not None:
                    out.imports.append(
                        ImportRef(
                            path=out.path,
                            module=_text(mod),
                            name=None,
                            alias=_text(alias) if alias else None,
                            line=line,
                        )
                    )
            else:
                out.imports.append(
                    ImportRef(path=out.path, module=_text(item), name=None, alias=None, line=line)
                )
        return

    module_node = node.child_by_field_name("module_name")
    module = _text(module_node) if module_node is not None else ""
    names = node.children_by_field_name("name")
    if not names and any(c.type == "wildcard_import" for c in node.children):
        out.imports.append(ImportRef(path=out.path, module=module, name="*", alias=None, line=line))
        return
    for item in names:
        if item.type == "aliased_import":
            inner = item.child_by_field_name("name")
            alias = item.child_by_field_name("alias")
            if inner is not None:
                out.imports.append(
                    ImportRef(
                        path=out.path,
                        module=module,
                        name=_text(inner),
                        alias=_text(alias) if alias else None,
                        line=line,
                    )
                )
        else:
            out.imports.append(
                ImportRef(path=out.path, module=module, name=_text(item), alias=None, line=line)
            )


def _record_call(node: Node, scope: _Scope, out: ParsedFile) -> None:
    func = node.child_by_field_name("function")
    if func is None:
        return
    line = node.start_point.row + 1
    if func.type == "identifier":
        callee, qualifier = _text(func), None
    elif func.type == "attribute":
        attr = func.child_by_field_name("attribute")
        obj = func.child_by_field_name("object")
        if attr is None:
            return
        callee = _text(attr)
        obj_text = _text(obj) if obj is not None else ""
        qualifier = obj_text if _DOTTED_RE.match(obj_text) else None
    else:
        return
    out.calls.append(
        CallSite(
            path=out.path,
            caller_id=scope.symbol_id,
            callee_name=callee,
            qualifier=qualifier,
            line=line,
        )
    )
