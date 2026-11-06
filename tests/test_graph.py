from __future__ import annotations

from pathlib import Path

from reviewer.index.store import Store
from reviewer.pipeline import index_repository


def _names(relations) -> set[str]:  # type: ignore[no-untyped-def]
    return {r.symbol.qualname for r in relations}


def _sym(store: Store, query: str):  # type: ignore[no-untyped-def]
    matches = store.find_symbols(query)
    assert len(matches) == 1, matches
    return matches[0]


def test_who_calls_a_function_across_files(repo: Path) -> None:
    report = index_repository(str(repo))
    with Store(report.db_path) as store:
        callers = store.callers(_sym(store, "round_money").id)
        assert _names(callers) == {"Gateway.charge", "refund"}
        assert {r.resolution for r in callers} == {"import"}


def test_self_method_calls_resolve_within_the_class(repo: Path) -> None:
    report = index_repository(str(repo))
    with Store(report.db_path) as store:
        audit = _sym(store, "Gateway._audit")
        callers = store.callers(audit.id)
        assert _names(callers) == {"Gateway.charge"}
        assert callers[0].resolution == "self"


def test_module_level_calls_are_attributed_to_the_module(repo: Path) -> None:
    report = index_repository(str(repo))
    with Store(report.db_path) as store:
        main = _sym(store, "main")
        assert _names(store.callers(main.id)) == {"helper", "<module>"}


def test_dotted_module_calls_and_reexports_resolve(repo: Path) -> None:
    report = index_repository(str(repo))
    with Store(report.db_path) as store:
        # `pkg.orders.cancel_order(...)` via `import pkg.orders`
        assert _names(store.callers(_sym(store, "cancel_order").id)) == {"main"}
        # `from pkg import create_order` is re-exported by pkg/__init__.py
        create = _sym(store, "create_order")
        assert _names(store.callers(create.id)) == {"main"}
        # `payments.refund(...)` via `from pkg import payments`
        assert "cancel_order" in _names(store.callers(_sym(store, "refund").id))


def test_callees_and_unresolved_external_calls(repo: Path) -> None:
    report = index_repository(str(repo))
    with Store(report.db_path) as store:
        create = _sym(store, "create_order")
        assert {"Gateway", "Gateway.charge", "OrderBook.add"} <= _names(store.callees(create.id))
        assert "json.dumps" in store.unresolved_calls(create.id)


def test_calls_into_external_modules_never_match_repo_symbols(repo: Path) -> None:
    (repo / "pkg" / "ext.py").write_text(
        "import json\n\ndef dumps(x):\n    return x\n\ndef use():\n    return json.dumps(1)\n"
    )
    report = index_repository(str(repo))
    with Store(report.db_path) as store:
        assert store.callers(_sym(store, "dumps").id) == []


def test_ambiguous_method_names_are_labelled(repo: Path) -> None:
    (repo / "pkg" / "dup.py").write_text(
        "class A:\n    def go(self): pass\n\nclass B:\n    def go(self): pass\n\n"
        "def run(x):\n    x.go()\n"
    )
    report = index_repository(str(repo))
    with Store(report.db_path) as store:
        run = _sym(store, "run")
        callees = store.callees(run.id)
        assert _names(callees) == {"A.go", "B.go"}
        assert {r.resolution for r in callees} == {"ambiguous"}


def test_find_symbols_by_trailing_qualname(repo: Path) -> None:
    report = index_repository(str(repo))
    with Store(report.db_path) as store:
        assert [s.qualname for s in store.find_symbols("OrderBook.add")] == ["OrderBook.add"]
        assert store.find_symbols("does_not_exist") == []
