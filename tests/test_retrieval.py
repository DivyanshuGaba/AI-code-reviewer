from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from reviewer.cli import app
from reviewer.index.lexical import LexicalIndex
from reviewer.index.store import Store
from reviewer.index.vectors import VectorIndex
from reviewer.llm.embeddings import HashingEmbedder
from reviewer.pipeline import context_for_file, index_repository, vector_path_for
from reviewer.retrieval.context import build_context, render_bundle
from reviewer.retrieval.fusion import reciprocal_rank_fusion
from reviewer.retrieval.strategies import Candidate, is_test_path


def cand(chunk: str, source: str, rank: int) -> Candidate:
    return Candidate(chunk, source, rank, 1.0, "")


def test_rrf_rewards_agreement_between_strategies() -> None:
    fused = reciprocal_rank_fusion(
        {
            "lexical": [cand("a", "lexical", 1), cand("b", "lexical", 2)],
            "vector": [cand("b", "vector", 1), cand("c", "vector", 2)],
        },
        weights={"lexical": 1.0, "vector": 1.0},
    )
    assert [f.chunk_id for f in fused] == ["b", "a", "c"]
    assert len(fused[0].contributions) == 2
    assert abs(fused[0].score - (1 / 62 + 1 / 61)) < 1e-12


def test_rrf_weights_favour_exact_signals_and_order_is_deterministic() -> None:
    rankings = {
        "graph:callee": [cand("x", "graph:callee", 1)],
        "vector": [cand("y", "vector", 1)],
    }
    assert [f.chunk_id for f in reciprocal_rank_fusion(rankings)] == ["x", "y"]
    tie = reciprocal_rank_fusion(
        {"lexical": [cand("b", "lexical", 1)], "vector": [cand("a", "vector", 1)]},
        weights={"lexical": 1.0, "vector": 1.0},
    )
    assert [f.chunk_id for f in tie] == ["a", "b"]


def test_is_test_path() -> None:
    assert is_test_path("tests/foo.py") and is_test_path("pkg/test_x.py")
    assert is_test_path("pkg/x_test.py") and is_test_path("conftest.py")
    assert not is_test_path("pkg/contest.py") and not is_test_path("pkg/latest.py")


def bundle_for(repo: Path, symbol: str, budget: int = 6000):  # type: ignore[no-untyped-def]
    report = index_repository(str(repo))
    with Store(report.db_path) as store:
        target = next(c for c in store.load_chunks() if c.symbol == symbol)
        return build_context(
            target,
            store,
            LexicalIndex(store.conn),
            VectorIndex(vector_path_for(report.db_path), HashingEmbedder()),
            budget=budget,
        )


def sources_by_symbol(bundle) -> dict[str, set[str]]:  # type: ignore[no-untyped-def]
    return {i.chunk.symbol: {s.source for s in i.sources} for i in bundle.items}


def test_bundle_includes_callers_callees_and_tests_with_reasons(repo: Path) -> None:
    bundle = bundle_for(repo, "Gateway.charge")
    got = sources_by_symbol(bundle)
    assert "graph:callee" in got["round_money"]
    assert "graph:callee" in got["Gateway._audit"]
    assert "graph:caller" in got["create_order"]
    assert "graph:test" in got["test_charge_rounds"]
    assert all(i.chunk.id != bundle.target.id for i in bundle.items)
    # exact graph neighbours come before purely fuzzy matches
    order = [i.chunk.symbol for i in bundle.items]
    assert order.index("create_order") < order.index("refund")


def test_bundle_respects_the_token_budget_and_records_exclusions(repo: Path) -> None:
    full = bundle_for(repo, "Gateway.charge")
    budget = full.target_tokens + 70
    small = bundle_for(repo, "Gateway.charge", budget=budget)
    assert small.token_count <= budget
    assert len(small.items) < len(full.items)
    assert small.excluded and "over budget" in small.excluded[0].reason
    assert small.token_count == small.target_tokens + sum(i.tokens for i in small.items)


def test_bundle_is_json_serialisable_and_renders(repo: Path) -> None:
    bundle = bundle_for(repo, "create_order")
    assert type(bundle).model_validate_json(bundle.model_dump_json()) == bundle
    text = render_bundle(bundle)
    assert "create_order" in text and "graph:caller" in text


def test_context_for_file_and_cli(repo: Path) -> None:
    index_repository(str(repo))
    bundles = context_for_file(str(repo), "pkg/payments.py")
    assert [b.target.symbol for b in bundles] == ["Gateway.charge", "Gateway._audit", "refund"]

    runner = CliRunner()
    result = runner.invoke(
        app, ["context", "pkg/orders.py", "--repo", str(repo), "--symbol", "create_order"]
    )
    assert result.exit_code == 0, result.output
    assert "== create_order" in result.output and "graph:callee" in result.output

    missing = runner.invoke(app, ["context", "nope.py", "--repo", str(repo)])
    assert missing.exit_code == 1
