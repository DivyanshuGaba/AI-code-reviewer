"""Tree-sitter adapter for JavaScript, TypeScript and TSX.

Returns the same `ParsedFile` as the Python adapter, so indexing, the call graph, retrieval
and review work unchanged. Covered: function and class declarations, methods, arrow
functions and function expressions bound to a name, ES imports and re-exports, simple
CommonJS `require`, and call sites (`this.x()`, `obj.x()`, `new X()`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from reviewer.index.chunker import make_chunk
from reviewer.index.models import CallSite, ImportRef, ParsedFile, Symbol

_LANGUAGES = {
    "javascript": Language(tsjs.language()),
    "typescript": Language(tsts.language_typescript()),
    "tsx": Language(tsts.language_tsx()),
}
_DOTTED_RE = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$")
_FUNC_DECLS = {"function_declaration", "generator_function_declaration"}
_CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}
_FUNC_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_FIELD_DEFS = {"public_field_definition", "field_definition"}


@dataclass(frozen=True)
class _Scope:
    symbol_id: str
    qualname: str
    kind: str


def _text(node: Node | None) -> str:
    return (node.text or b"").decode("utf-8", errors="replace") if node is not None else ""


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _unquote(node: Node | None) -> str:
    return _text(node)[1:-1] if node is not None and len(_text(node)) >= 2 else ""


def _outer(node: Node) -> Node:
    """Include a wrapping `export` / `export default` in the node's range."""
    parent = node.parent
    return parent if parent is not None and parent.type == "export_statement" else node


def parse_js(path: str, source: bytes, language: str) -> ParsedFile:
    tree = Parser(_LANGUAGES[language]).parse(source)
    lines = source.decode("utf-8", errors="replace").splitlines()
    module_id = f"{path}::<module>:1"
    out = ParsedFile(
        path=path,
        symbols=[
            Symbol(
                id=module_id,
                path=path,
                name="<module>",
                qualname="<module>",
                kind="module",
                parent=None,
                start_line=1,
                end_line=max(len(lines), 1),
                signature=f"<module {path}>",
            )  # fmt: skip
        ],
        imports=[],
        calls=[],
        chunks=[],
        has_syntax_errors=tree.root_node.has_error,
    )
    _Walker(path, source, lines, language, out).visit(
        tree.root_node, [_Scope(module_id, "<module>", "module")]
    )
    return out


class _Walker:
    def __init__(
        self, path: str, source: bytes, lines: list[str], language: str, out: ParsedFile
    ) -> None:
        self.path, self.source, self.lines, self.language, self.out = (
            path, source, lines, language, out,
        )  # fmt: skip

    # -- traversal -------------------------------------------------------------------------

    def visit(self, node: Node, scopes: list[_Scope]) -> None:
        for child in node.children:
            t = child.type
            if t in _FUNC_DECLS:
                self.define_function(
                    child, _outer(child), self._name(child), child, scopes, "function"
                )
            elif t in _CLASS_DECLS:
                self.define_class(child, scopes)
            elif t == "method_definition":
                self.define_function(child, child, self._name(child), child, scopes, "method")
            elif t in _FIELD_DEFS:
                value = child.child_by_field_name("value")
                name = child.child_by_field_name("name") or child.child_by_field_name("property")
                if value is not None and value.type in _FUNC_VALUES and name is not None:
                    self.define_function(value, child, name, value, scopes, "method")
                else:
                    self.visit(child, scopes)
            elif t in ("lexical_declaration", "variable_declaration"):
                self.declaration(child, scopes)
            elif t == "import_statement":
                self.import_statement(child)
            elif t == "export_statement" and child.child_by_field_name("source") is not None:
                self.reexport(child)
            elif t in ("call_expression", "new_expression"):
                self.call(child, scopes[-1])
                self.visit(child, scopes)
            else:
                self.visit(child, scopes)

    @staticmethod
    def _name(node: Node) -> Node | None:
        return node.child_by_field_name("name")

    # -- definitions -----------------------------------------------------------------------

    def _register(
        self, name: str, kind: str, outer: Node, header_end: int, scopes: list[_Scope]
    ) -> tuple[Symbol, _Scope]:
        parent = scopes[-1]
        qualname = name if parent.kind == "module" else f"{parent.qualname}.{name}"
        start_line = outer.start_point.row + 1
        signature = _collapse(
            self.source[outer.start_byte : header_end].decode("utf-8", errors="replace")
        )
        symbol = Symbol(
            id=f"{self.path}::{qualname}:{start_line}",
            path=self.path,
            name=name,
            qualname=qualname,
            kind=kind,  # type: ignore[arg-type]
            parent=None if parent.kind == "module" else parent.qualname,
            start_line=start_line,
            end_line=outer.end_point.row + 1,
            signature=signature,
        )
        self.out.symbols.append(symbol)
        return symbol, _Scope(symbol.id, qualname, kind)

    def define_function(
        self,
        node: Node,
        outer: Node,
        name_node: Node | None,
        body_owner: Node,
        scopes: list[_Scope],
        default_kind: str,
    ) -> None:
        body = body_owner.child_by_field_name("body")
        name = _text(name_node)
        if name_node is None or not name or body is None:
            # anonymous (`export default () => ...`) or a bodiless signature: not a symbol
            if body is not None:
                self.visit(body, scopes)
            return
        kind = "method" if scopes[-1].kind == "class" else "function"
        if default_kind == "method" and scopes[-1].kind != "class":
            kind = "function"
        symbol, scope = self._register(name, kind, outer, body.start_byte, scopes)

        in_function = any(s.kind in ("function", "method") for s in scopes)
        if kind in ("function", "method") and not in_function:
            class_name = scopes[-1].qualname if scopes[-1].kind == "class" else None
            self.out.chunks.append(
                make_chunk(
                    chunk_id=symbol.id,
                    path=self.path,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    qualname=symbol.qualname,
                    class_name=class_name,
                    signature=symbol.signature,
                    lines=self.lines,
                    language=self.language,
                )  # fmt: skip
            )
        self.visit(body, [*scopes, scope])

    def define_class(self, node: Node, scopes: list[_Scope]) -> None:
        name_node, body = self._name(node), node.child_by_field_name("body")
        name = _text(name_node)
        if not name or body is None:
            return
        _, scope = self._register(name, "class", _outer(node), body.start_byte, scopes)
        self.visit(body, [*scopes, scope])

    def declaration(self, node: Node, scopes: list[_Scope]) -> None:
        for declarator in (c for c in node.children if c.type == "variable_declarator"):
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if value is None or name_node is None:
                self.visit(declarator, scopes)
            elif value.type in _FUNC_VALUES and name_node.type == "identifier":
                self.define_function(value, _outer(node), name_node, value, scopes, "function")
            elif self._require_target(value) is not None:
                self.require(name_node, self._require_target(value) or "")
            else:
                self.visit(declarator, scopes)

    # -- imports ---------------------------------------------------------------------------

    def _imp(self, module: str, name: str | None, alias: str | None, node: Node) -> None:
        self.out.imports.append(
            ImportRef(
                path=self.path,
                module=module,
                name=name,
                alias=alias or None,
                line=node.start_point.row + 1,
            )  # fmt: skip
        )

    def import_statement(self, node: Node) -> None:
        module = _unquote(node.child_by_field_name("source"))
        clause = next((c for c in node.children if c.type == "import_clause"), None)
        if clause is None:
            self._imp(module, None, None, node)  # side-effect import: still a dependency
            return
        for c in clause.children:
            if c.type == "identifier":
                self._imp(module, "default", _text(c), node)
            elif c.type == "named_imports":
                for spec in (s for s in c.children if s.type == "import_specifier"):
                    name = _text(spec.child_by_field_name("name"))
                    self._imp(module, name, _text(spec.child_by_field_name("alias")), node)
            elif c.type == "namespace_import":
                ident = next((x for x in c.children if x.type == "identifier"), None)
                self._imp(module, None, _text(ident), node)

    def reexport(self, node: Node) -> None:
        """`export { a as b } from './x'` and `export * from './x'` forward names."""
        module = _unquote(node.child_by_field_name("source"))
        found = False
        for c in node.children:
            if c.type == "export_clause":
                for spec in (s for s in c.children if s.type == "export_specifier"):
                    name = _text(spec.child_by_field_name("name"))
                    self._imp(module, name, _text(spec.child_by_field_name("alias")), node)
                    found = True
            elif c.type == "namespace_export":
                ident = next((x for x in c.children if x.type == "identifier"), None)
                self._imp(module, None, _text(ident), node)
                found = True
            elif c.type == "*" and not found:
                self._imp(module, "*", None, node)
                found = True
        if not found:
            self._imp(module, None, None, node)

    @staticmethod
    def _require_target(value: Node) -> str | None:
        if value.type != "call_expression":
            return None
        func, args = value.child_by_field_name("function"), value.child_by_field_name("arguments")
        if func is None or _text(func) != "require" or args is None:
            return None
        strings = [c for c in args.children if c.type == "string"]
        return _unquote(strings[0]) if len(strings) == 1 else None

    def require(self, name_node: Node, module: str) -> None:
        if name_node.type == "identifier":
            self._imp(module, None, _text(name_node), name_node)
        elif name_node.type == "object_pattern":
            for c in name_node.children:
                if c.type == "shorthand_property_identifier_pattern":
                    self._imp(module, _text(c), None, name_node)
                elif c.type == "pair_pattern":
                    key, value = c.child_by_field_name("key"), c.child_by_field_name("value")
                    self._imp(module, _text(key), _text(value), name_node)

    # -- calls -----------------------------------------------------------------------------

    def call(self, node: Node, scope: _Scope) -> None:
        func = node.child_by_field_name(
            "function" if node.type == "call_expression" else "constructor"
        )
        if func is None:
            return
        qualifier: str | None = None
        if func.type == "identifier":
            callee = _text(func)
            if callee == "require" and node.type == "call_expression":
                return
        elif func.type == "member_expression":
            prop, obj = func.child_by_field_name("property"), func.child_by_field_name("object")
            if prop is None:
                return
            callee = _text(prop)
            obj_text = _text(obj)
            qualifier = obj_text if _DOTTED_RE.match(obj_text) else None
        else:
            return
        self.out.calls.append(
            CallSite(
                path=self.path,
                caller_id=scope.symbol_id,
                callee_name=callee,
                qualifier=qualifier,
                line=node.start_point.row + 1,
            )  # fmt: skip
        )
