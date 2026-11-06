from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from reviewer.cli import app
from reviewer.index.store import Store
from reviewer.pipeline import index_repository

runner = CliRunner()


def test_reindex_skips_unchanged_files(repo: Path) -> None:
    first = index_repository(str(repo))
    assert first.files_changed == first.files_total

    second = index_repository(str(repo))
    assert second.files_changed == 0
    assert second.counts == first.counts


def test_reindex_picks_up_edits_and_deletions(repo: Path) -> None:
    index_repository(str(repo))
    (repo / "pkg" / "utils.py").write_text("def round_money(v):\n    return v\n")
    (repo / "pkg" / "payments.py").unlink()
    report = index_repository(str(repo))
    assert report.files_changed == 1 and report.files_removed == 1
    with Store(report.db_path) as store:
        assert store.find_symbols("Gateway") == []
        assert store.find_symbols("log") == []
        assert store.callers(store.find_symbols("round_money")[0].id) == []


def test_chunks_are_stored_with_headers(repo: Path) -> None:
    report = index_repository(str(repo))
    with Store(report.db_path) as store:
        chunks = store.load_chunks("pkg/payments.py")
        assert [c.symbol for c in chunks] == ["Gateway.charge", "Gateway._audit", "refund"]
        assert chunks[0].header.startswith("# file: pkg/payments.py")
        assert chunks[0].text.lstrip().startswith("def charge")


def test_cli_index_then_query(repo: Path) -> None:
    result = runner.invoke(app, ["index", str(repo)])
    assert result.exit_code == 0, result.output
    assert "symbols" in result.output

    result = runner.invoke(app, ["query", "round_money", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "Gateway.charge" in result.output and "refund" in result.output


def test_cli_query_errors_cleanly(repo: Path) -> None:
    result = runner.invoke(app, ["query", "x", "--repo", str(repo)])
    assert result.exit_code == 1  # not indexed yet

    runner.invoke(app, ["index", str(repo)])
    result = runner.invoke(app, ["query", "nope_nope", "--repo", str(repo)])
    assert result.exit_code == 1 and "No symbol" in result.output
