"""Retrieval quality, measured on its own: no model calls, so it is free and deterministic.

For each seeded bug, the buggy function is the target and `relevant` lists the other
functions a reviewer needs in order to see the bug (its callers, the callee whose contract
it breaks). We ask how high the retrieval strategies rank those, alone and fused.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from reviewer.evals.cases import CaseError, load_cases, materialize
from reviewer.index.lexical import LexicalIndex
from reviewer.index.store import Store
from reviewer.index.vectors import VectorIndex
from reviewer.ingest.sources import home_dir
from reviewer.llm.embeddings import default_embedder
from reviewer.pipeline import db_path_for, index_repository, vector_path_for
from reviewer.retrieval.budget import DEFAULT_BUDGET
from reviewer.retrieval.context import build_context
from reviewer.retrieval.fusion import reciprocal_rank_fusion
from reviewer.retrieval.strategies import (
    Candidate,
    graph_candidates,
    lexical_candidates,
    vector_candidates,
)

PER_STRATEGY_K = 10
# strategy-name prefixes that make up each configuration
CONFIGS: dict[str, tuple[str, ...]] = {
    "graph": ("graph:",),
    "lexical": ("lexical",),
    "vector": ("vector",),
    "graph+lexical": ("graph:", "lexical"),
    "fused (all)": ("graph:", "lexical", "vector"),
}


class BugRetrieval(BaseModel):
    case_id: str
    bug_id: str
    target: str
    relevant: list[str]
    ranks: dict[str, list[int | None]]  # config -> 1-based rank of each relevant chunk
    bundle_recall: float  # share of relevant chunks inside the final token-budgeted bundle


class RetrievalReport(BaseModel):
    ks: list[int]
    bugs_evaluated: int
    configs: dict[str, dict[str, float]] = Field(default_factory=dict)  # config -> metric -> value
    bundle_recall: float = 0.0
    bugs: list[BugRetrieval] = Field(default_factory=list)


def _recall_at(ranks: list[int | None], k: int) -> float:
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks)


def _reciprocal_rank(ranks: list[int | None]) -> float:
    found = [r for r in ranks if r is not None]
    return 1 / min(found) if found else 0.0


def run_retrieval_eval(
    cases_dir: Path,
    only: list[str] | None = None,
    ks: tuple[int, ...] = (1, 3, 5, 10),
    budget: int = DEFAULT_BUDGET,
) -> RetrievalReport:
    bugs: list[BugRetrieval] = []
    work = home_dir() / "evals" / "work"
    for case_dir, spec in load_cases(cases_dir, only):
        wanted = [b for b in spec.bugs if b.relevant]
        if not wanted:
            continue
        mat = materialize(case_dir, spec, work / f"{spec.id}-buggy", "buggy")
        index_repository(str(mat.root), signal_tools=())
        db_path = db_path_for(mat.root)
        with Store(db_path) as store:
            lexical = LexicalIndex(store.conn)
            vectors = VectorIndex(vector_path_for(db_path), default_embedder())
            for bug in (b for b in mat.seeded if b.relevant):
                target = store.chunk_at(bug.file, bug.line_start)
                if target is None:
                    raise CaseError(f"{spec.id}/{bug.id}: no function chunk at the bug location")
                relevant_ids = []
                for ref in bug.relevant:
                    path, qualname = ref.split("::", 1)
                    chunk = next((c for c in store.load_chunks(path) if c.symbol == qualname), None)
                    if chunk is None:
                        raise CaseError(f"{spec.id}/{bug.id}: relevant {ref!r} is not a chunk")
                    relevant_ids.append(chunk.id)

                rankings: dict[str, list[Candidate]] = dict(
                    graph_candidates(store, target, PER_STRATEGY_K)
                )
                rankings["lexical"] = lexical_candidates(lexical, target, PER_STRATEGY_K)
                rankings["vector"] = vector_candidates(vectors, target, PER_STRATEGY_K)

                ranks: dict[str, list[int | None]] = {}
                for name, prefixes in CONFIGS.items():
                    subset = {k: v for k, v in rankings.items() if k.startswith(prefixes)}
                    order = [f.chunk_id for f in reciprocal_rank_fusion(subset)]
                    ranks[name] = [order.index(r) + 1 if r in order else None for r in relevant_ids]
                bundle = build_context(target, store, lexical, vectors, budget, PER_STRATEGY_K)
                included = {i.chunk.id for i in bundle.items}
                bugs.append(
                    BugRetrieval(
                        case_id=spec.id,
                        bug_id=bug.id,
                        target=target.id,
                        relevant=relevant_ids,
                        ranks=ranks,
                        bundle_recall=sum(r in included for r in relevant_ids) / len(relevant_ids),
                    )  # fmt: skip
                )

    report = RetrievalReport(ks=list(ks), bugs_evaluated=len(bugs), bugs=bugs)
    for name in CONFIGS:
        metrics = {
            f"recall@{k}": sum(_recall_at(b.ranks[name], k) for b in bugs) / len(bugs) for k in ks
        }
        metrics["mrr"] = sum(_reciprocal_rank(b.ranks[name]) for b in bugs) / len(bugs)
        report.configs[name] = metrics
    report.bundle_recall = sum(b.bundle_recall for b in bugs) / len(bugs)
    return report
