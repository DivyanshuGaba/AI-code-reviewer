from __future__ import annotations

from reviewer.index.parser import parse_python

SRC = b"""\
import os
import a.b as c
from x import y, z as w
from . import sibling
from .mod import thing
from pkg import *


@decorator(1)
def top(a, b: int = 2) -> str:
    helper()
    return os.path.join(a, "b")


class Box(Base):
    attr = make()

    def method(self):
        self.other()

        def inner():
            nested_call()

        return inner

    async def other(self):
        pass
"""


def test_symbols_and_kinds() -> None:
    parsed = parse_python("m.py", SRC)
    by_qual = {s.qualname: s for s in parsed.symbols}
    assert by_qual["top"].kind == "function"
    assert by_qual["Box"].kind == "class"
    assert by_qual["Box.method"].kind == "method"
    assert by_qual["Box.other"].kind == "method"
    assert by_qual["Box.method.inner"].kind == "function"
    assert by_qual["Box.method.inner"].parent == "Box.method"
    assert by_qual["<module>"].kind == "module"


def test_decorators_extend_the_start_line_and_signature_is_normalised() -> None:
    parsed = parse_python("m.py", SRC)
    top = next(s for s in parsed.symbols if s.qualname == "top")
    assert top.start_line == 9  # the decorator line
    assert top.signature == "def top(a, b: int = 2) -> str"


def test_one_chunk_per_function_or_method_not_nested() -> None:
    parsed = parse_python("m.py", SRC)
    assert sorted(c.symbol for c in parsed.chunks) == ["Box.method", "Box.other", "top"]
    method = next(c for c in parsed.chunks if c.symbol == "Box.method")
    assert "def inner" in method.text  # nested function stays inside its parent
    assert "# class: Box" in method.header and "# file: m.py" in method.header


def test_imports() -> None:
    parsed = parse_python("m.py", SRC)
    got = {(i.module, i.name, i.alias) for i in parsed.imports}
    assert ("os", None, None) in got
    assert ("a.b", None, "c") in got
    assert ("x", "y", None) in got
    assert ("x", "z", "w") in got
    assert (".", "sibling", None) in got
    assert (".mod", "thing", None) in got
    assert ("pkg", "*", None) in got


def test_calls_are_attributed_to_the_enclosing_scope() -> None:
    parsed = parse_python("m.py", SRC)
    calls = {
        (c.caller_id.split("::")[1].rsplit(":", 1)[0], c.qualifier, c.callee_name)
        for c in parsed.calls
    }
    assert ("top", None, "helper") in calls
    assert ("top", "os.path", "join") in calls
    assert ("Box", None, "make") in calls  # class body
    assert ("Box.method", "self", "other") in calls
    assert ("Box.method.inner", None, "nested_call") in calls
    assert ("<module>", None, "decorator") in calls  # decorator belongs to the outer scope


def test_syntax_errors_are_flagged_but_parsing_continues() -> None:
    parsed = parse_python("bad.py", b"def ok():\n    pass\n\ndef broken(:\n")
    assert parsed.has_syntax_errors
    assert any(s.qualname == "ok" for s in parsed.symbols)
