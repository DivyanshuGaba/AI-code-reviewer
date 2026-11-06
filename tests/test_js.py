from __future__ import annotations

import json
import shutil
from pathlib import Path
from pathlib import PurePosixPath as P

import pytest
from typer.testing import CliRunner

from reviewer.cli import app
from reviewer.index.graph import file_dependencies
from reviewer.index.languages import parse_file, syntax_error
from reviewer.index.store import Store
from reviewer.ingest.filters import language_of, should_index
from reviewer.llm.gateway import LLMRequest
from reviewer.pipeline import index_repository, review_repository
from reviewer.retrieval.strategies import is_test_path
from reviewer.settings import load_settings
from reviewer.verify.apply_patch import make_diff
from reviewer.verify.checks import Verifier
from tests.fakes import FakeLLM, finding_json, triage_json
from tests.test_verify import FakeSandbox

FIXTURE = Path(__file__).parent / "fixtures" / "ts_repo"
SETTINGS = load_settings()
JS_LANGS = ("javascript", "typescript")


@pytest.fixture
def ts_repo(tmp_path: Path) -> Path:
    dest = tmp_path / "ts_repo"
    shutil.copytree(FIXTURE, dest)
    return dest


SRC = b"""\
import def, { a as b, c } from './x';
import * as ns from '../lib/y';
const fs = require('./z');
const { p, q: r } = require('./w');
export { re1, re2 as re3 } from './re';
export * from './all';

export default function main(a) { return helper(a) + this.m(); }

export class K extends Base {
  static s() { return 1; }
  method(x) { return ns.go(x) + new Other(x).run(); }
  field = (y) => y + 1;
}

const arrow = (x) => x + 1;
export const f2 = async function (y) { return arrow(y); };

function outer() { function inner() { return nested(); } return inner; }
"""


def test_js_symbols_kinds_and_chunks() -> None:
    parsed = parse_file("m.js", SRC, "javascript")
    kinds = {s.qualname: s.kind for s in parsed.symbols}
    assert kinds["main"] == "function" and kinds["K"] == "class"
    assert (
        kinds["K.s"] == "method" and kinds["K.method"] == "method" and kinds["K.field"] == "method"
    )
    assert kinds["arrow"] == "function" and kinds["f2"] == "function"
    assert kinds["outer.inner"] == "function"
    assert sorted(c.symbol for c in parsed.chunks) == sorted(
        ["main", "K.s", "K.method", "K.field", "arrow", "f2", "outer"]
    )
    main = next(c for c in parsed.chunks if c.symbol == "main")
    assert main.text.startswith("export default function main") and main.language == "javascript"
    assert "# class: K" in next(c for c in parsed.chunks if c.symbol == "K.method").header
    arrow = next(s for s in parsed.symbols if s.qualname == "arrow")
    assert arrow.signature.startswith("const arrow = (x) =>")


def test_js_imports_requires_and_reexports() -> None:
    got = {(i.module, i.name, i.alias) for i in parse_file("m.js", SRC, "javascript").imports}
    assert ("./x", "default", "def") in got
    assert ("./x", "a", "b") in got and ("./x", "c", None) in got
    assert ("../lib/y", None, "ns") in got
    assert ("./z", None, "fs") in got
    assert ("./w", "p", None) in got and ("./w", "q", "r") in got
    assert ("./re", "re1", None) in got and ("./re", "re2", "re3") in got
    assert ("./all", "*", None) in got


def test_js_calls_are_attributed_with_qualifiers() -> None:
    calls = {
        (c.caller_id.split("::")[1].rsplit(":", 1)[0], c.qualifier, c.callee_name)
        for c in parse_file("m.js", SRC, "javascript").calls
    }
    assert ("main", None, "helper") in calls and ("main", "this", "m") in calls
    assert ("K.method", "ns", "go") in calls and ("K.method", None, "Other") in calls  # new X()
    assert ("outer.inner", None, "nested") in calls
    assert not any(callee == "require" for _, _, callee in calls)


def test_typescript_and_tsx_parse_types_generics_and_interfaces() -> None:
    ts = b"""
export class Box<T> implements I {
  private x: number = 1;
  constructor(a: string) {}
  async get(b: T): Promise<void> { this.x++; foo.bar(b); }
}
interface I { get(): void }
const g = <T,>(x: T): T => x;
"""
    parsed = parse_file("a.ts", ts, "typescript")
    assert not parsed.has_syntax_errors
    assert {"Box", "Box.constructor", "Box.get", "g"} <= {s.qualname for s in parsed.symbols}
    tsx = b"export const View = (p: {n: number}) => <div>{fmt(p.n)}</div>;"
    view = parse_file("v.tsx", tsx, "tsx")
    assert not view.has_syntax_errors and any(c.callee_name == "fmt" for c in view.calls)


def test_language_detection_and_selection() -> None:
    assert language_of(P("a/b.ts")) == "typescript" and language_of(P("a.tsx")) == "tsx"
    assert language_of(P("a.js")) == "javascript" and language_of(P("a.py")) == "python"
    assert language_of(P("types.d.ts")) is None and language_of(P("app.min.js")) is None
    assert language_of(P("README.md")) is None
    assert should_index(P("a.ts"), 10, ("typescript",)) and not should_index(P("a.ts"), 10)
    assert should_index(P("a.tsx"), 10, ("typescript",))  # tsx belongs to the typescript group
    assert not should_index(P("a.js"), 10, ("typescript",))
    assert is_test_path("src/cart.test.ts") and is_test_path("src/__tests__/a.ts")
    assert is_test_path("a.spec.js") and not is_test_path("src/contest.ts")


def test_syntax_errors_are_detected_without_running_anything() -> None:
    assert syntax_error("a.ts", "const x: number = 1;", "typescript") is None
    assert "line" in (syntax_error("a.ts", "function f( {", "typescript") or "")
    assert syntax_error("a.py", "x = 1", "python") is None
    assert "line 1" in (syntax_error("a.py", "def f(:", "python") or "")


# -- indexing and the call graph ----------------------------------------------------------------


def callers(store: Store, symbol: str) -> dict[str, str]:
    (sym,) = store.find_symbols(symbol)
    return {r.symbol.qualname: r.resolution for r in store.callers(sym.id)}


def test_python_only_is_the_default_and_languages_are_opt_in(ts_repo: Path) -> None:
    assert index_repository(str(ts_repo), signal_tools=()).files_total == 0
    only_ts = index_repository(str(ts_repo), signal_tools=(), languages=["typescript"])
    assert only_ts.files_total == 6  # the test file is indexed too; .d.ts and .js are not
    both = index_repository(str(ts_repo), signal_tools=(), languages=JS_LANGS)
    assert both.files_total == 7 and both.files_removed == 0


def test_cross_file_resolution_in_typescript_and_commonjs(ts_repo: Path) -> None:
    report = index_repository(str(ts_repo), signal_tools=(), languages=JS_LANGS)
    with Store(report.db_path) as store:
        # relative import, re-export (index.ts -> pricing.ts), CommonJS require, same-file call
        assert callers(store, "applyDiscount") == {
            "discounted": "import", "main": "import", "bulkUnitPrice": "local",
        }  # fmt: skip
        assert callers(store, "bulkUnitPrice") == {"Cart.total": "import"}
        assert callers(store, "money") == {"Cart.label": "import"}  # namespace import
        total = callers(store, "Cart.total")
        assert total["Cart.label"] == "self"  # this.total()
        assert total["<module>"] == "name"  # the test file's c.total() (tests are in the graph)
        # `new Cart()` through a default import resolves to the class
        (cart,) = store.find_symbols("Cart")
        assert "main" in {r.symbol.qualname for r in store.callers(cart.id)}
        assert "node_modules" not in {f for f in store.file_lines() if "node_modules" in f}


def test_file_dependency_graph_for_js(ts_repo: Path) -> None:
    report = index_repository(str(ts_repo), signal_tools=(), languages=JS_LANGS)
    with Store(report.db_path) as store:
        deps = file_dependencies(store.load_imports(), set(store.file_lines()))
    assert deps["src/cart.ts"] == {"src/pricing.ts", "src/format.ts"}
    assert deps["src/index.ts"] == {"src/pricing.ts", "src/cart.ts"}
    assert deps["lib/util.js"] == {"src/pricing.ts"}
    assert deps["src/app.tsx"] == {"src/cart.ts", "src/index.ts"}


def test_signals_are_skipped_for_non_python_files(ts_repo: Path) -> None:
    report = index_repository(str(ts_repo), signal_tools=("ruff",), languages=JS_LANGS)
    assert report.signal_status == {"ruff": "skipped (no Python files)"}


def test_mixed_repos_keep_python_signals(repo: Path, ts_repo: Path) -> None:
    shutil.copy(ts_repo / "src" / "format.ts", repo / "format.ts")
    report = index_repository(str(repo), signal_tools=("ruff",), languages=("python", "typescript"))
    assert report.signal_status == {"ruff": "ok"}
    with Store(report.db_path) as store:
        assert {f for f in store.file_lines() if f.endswith(".ts")} == {"format.ts"}
        assert {s.path for s in store.load_signals()} <= {
            p for p in store.file_lines() if p.endswith(".py")
        }


# -- review, prompts and verification ------------------------------------------------------------


def test_review_of_typescript_tells_the_model_the_language(ts_repo: Path) -> None:
    def handler(request: LLMRequest) -> str:
        if request.model == SETTINGS.models.triage:
            return triage_json("Function: Cart.total" in request.user)
        return json.dumps({"findings": [finding_json(line_start=18, line_end=18)]})

    llm = FakeLLM(handler)
    report = review_repository(str(ts_repo), llm=llm, languages=JS_LANGS, file="src/cart.ts")
    assert [f.symbol for f in report.findings] == ["Cart.total"]
    assert all("TypeScript, not Python" in r.system for r in llm.requests)
    deep = next(r for r in llm.requests if r.model == SETTINGS.models.deep_review)
    assert "Cart.label" in deep.user and "bulkUnitPrice" in deep.user  # graph context works


def test_python_prompts_are_unchanged(repo: Path) -> None:
    llm = FakeLLM(lambda r: triage_json(False))
    review_repository(str(repo), llm=llm, file="pkg/utils.py")
    assert llm.requests and not any("Language note" in r.system for r in llm.requests)


def test_typescript_fixes_get_syntax_checked_but_not_sandbox_checked(ts_repo: Path) -> None:
    text = (ts_repo / "src" / "format.ts").read_text()
    good = make_diff("src/format.ts", text, text.replace("toFixed(2)", "toFixed(1)"))
    bad = make_diff("src/format.ts", text, text.replace("return `", "return (`"))
    sb = FakeSandbox()
    with Verifier(sb, ts_repo) as v:
        ok = v.verify("F1", good, {"src/format.ts"})
        broken = v.verify("F2", bad, {"src/format.ts"})
    assert ok.status == "fix_unverified" and [c.status for c in ok.checks][:2] == ["passed"] * 2
    assert "no sandbox checks for typescript" in ok.checks[-1].detail and sb.calls == []
    assert broken.status == "fix_failed" and broken.checks[1].name == "syntax"


def test_cli_index_lang_option(ts_repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app, ["index", str(ts_repo), "--lang", "typescript", "--lang", "javascript"]
    )
    assert result.exit_code == 0, result.output
    assert "files       7" in result.output and "ruff: skipped (no Python files)" in result.output
    bad = runner.invoke(app, ["index", str(ts_repo), "--lang", "cobol"])
    assert bad.exit_code == 1 and "unknown language" in bad.output
