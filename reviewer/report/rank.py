"""Dedupe and rank findings by severity x confidence x blast radius."""

from __future__ import annotations

import math
import re
from collections import defaultdict, deque

from pydantic import BaseModel

from reviewer.analysis import depgraph
from reviewer.index.graph import file_dependencies
from reviewer.index.store import Store
from reviewer.llm.schemas import SEVERITY_RANK, Finding
from reviewer.retrieval.strategies import is_test_path
from reviewer.verify.models import VerificationResult

BLAST_DEPTH = 3
_WORD_RE = re.compile(r"[a-z0-9]+")


class RankedFinding(BaseModel):
    rank: int
    score: float
    blast_radius: int  # distinct non-test functions (or modules) affected within 3 hops
    finding: Finding
    verification: VerificationResult | None = None
    duplicates: list[str] = []  # ids of overlapping findings merged into this one


def score(severity: str, confidence: float, blast_radius: int) -> float:
    """severity (1-5) x confidence (0-1) x blast factor; a lone function has factor 1."""
    return round(SEVERITY_RANK[severity] * confidence * (1 + math.log2(1 + blast_radius)), 4)


def _callers_index(store: Store) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = defaultdict(set)
    for edge in store.load_edges():
        reverse[edge.callee_id].add(edge.caller_id)
    return reverse


def function_blast_radius(reverse: dict[str, set[str]], symbol_id: str) -> int:
    seen: set[str] = {symbol_id}
    queue: deque[tuple[str, int]] = deque([(symbol_id, 0)])
    while queue:
        node, dist = queue.popleft()
        if dist == BLAST_DEPTH:
            continue
        for caller in reverse.get(node, ()):
            if caller not in seen:
                seen.add(caller)
                queue.append((caller, dist + 1))
    seen.discard(symbol_id)
    return sum(1 for s in seen if not is_test_path(s.split("::", 1)[0]))


def _similar(a: str, b: str) -> float:
    ta, tb = set(_WORD_RE.findall(a.lower())), set(_WORD_RE.findall(b.lower()))
    return len(ta & tb) / len(ta | tb) if ta | tb else 0.0


def _duplicates(a: Finding, b: Finding) -> bool:
    if a.file != b.file or a.category != b.category:
        return False
    if a.category == "architecture":  # all architecture findings share line 1
        return _similar(a.title, b.title) >= 0.6
    return a.line_start <= b.line_end and b.line_start <= a.line_end


def rank_findings(
    findings: list[Finding], verifications: list[VerificationResult], store: Store
) -> list[RankedFinding]:
    reverse_calls = _callers_index(store)
    paths = {p for p in store.file_lines() if not is_test_path(p)}
    rdeps = depgraph.reverse(file_dependencies(store.load_imports(), paths))
    verification_by_id = {v.finding_id: v for v in verifications}

    scored: list[RankedFinding] = []
    for f in findings:
        if f.symbol == "<repo>":
            blast = len(depgraph.transitive_dependents(rdeps, f.file, BLAST_DEPTH))
        else:
            blast = function_blast_radius(reverse_calls, f.chunk_id) if f.chunk_id else 0
        scored.append(
            RankedFinding(
                rank=0,
                score=score(f.severity, f.confidence, blast),
                blast_radius=blast,
                finding=f,
                verification=verification_by_id.get(f.id),
            )
        )
    scored.sort(key=lambda r: (-r.score, r.finding.id))

    kept: list[RankedFinding] = []
    for candidate in scored:
        original = next((k for k in kept if _duplicates(k.finding, candidate.finding)), None)
        if original is None:
            kept.append(candidate)
            continue
        original.duplicates.append(candidate.finding.id)
        merged = dict.fromkeys(
            [*original.finding.related_signals, *candidate.finding.related_signals]
        )
        original.finding = original.finding.model_copy(update={"related_signals": list(merged)})
    for i, ranked in enumerate(kept, 1):
        ranked.rank = i
    return kept
