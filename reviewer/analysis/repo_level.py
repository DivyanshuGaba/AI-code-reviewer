"""Repo-level pass: module summaries, dependency facts, and an architecture review."""

from __future__ import annotations

from collections import Counter

from reviewer.analysis import depgraph
from reviewer.analysis.models import ArchitectureFacts, ModuleInfo
from reviewer.analysis.triage import schema_json
from reviewer.index.graph import file_dependencies
from reviewer.index.store import Store
from reviewer.ingest.sources import Source
from reviewer.llm.cache import LLMCache
from reviewer.llm.gateway import LLM, LLMRequest, complete_structured
from reviewer.llm.prompts import load_prompt, neutralise_tags
from reviewer.llm.schemas import (
    Finding,
    ModuleSummaryResult,
    RepoLevelResult,
    finding_id,
)
from reviewer.retrieval.strategies import is_test_path
from reviewer.runs.tracing import RunTracer
from reviewer.settings import Settings

HEAD_LINES = 40
MAX_SYMBOLS = 30


def build_facts(store: Store, max_modules: int) -> tuple[ArchitectureFacts, dict[str, set[str]]]:
    """Deterministic structure facts for non-test modules. Returns the dependency map too."""
    lines = {p: n for p, n in store.file_lines().items() if not is_test_path(p)}
    deps = file_dependencies(store.load_imports(), set(lines))
    rdeps = depgraph.reverse(deps)

    defs: dict[str, list[str]] = {p: [] for p in lines}
    for sym in store.load_symbols():
        if sym.parent is None and sym.kind in ("class", "function") and sym.path in defs:
            defs[sym.path].append(sym.signature)

    modules = [
        ModuleInfo(
            path=p,
            lines=lines[p],
            symbols=defs[p][:MAX_SYMBOLS],
            imports=sorted(deps[p]),
            imported_by=len(rdeps.get(p, ())),
        )
        for p in sorted(lines)
    ]
    # Keep the most connected, then largest, modules when the repo is big.
    modules.sort(key=lambda m: (-(m.imported_by + len(m.imports)), -m.lines, m.path))
    kept = modules[:max_modules]
    facts = ArchitectureFacts(
        modules=kept,
        cycles=depgraph.find_cycles(deps),
        hubs=depgraph.hubs(rdeps),
        edges=sum(len(t) for t in deps.values()),
    )
    return facts, deps


def summarize_modules(
    llm: LLM,
    cache: LLMCache | None,
    tracer: RunTracer,
    settings: Settings,
    source: Source,
    facts: ArchitectureFacts,
) -> None:
    prompt = load_prompt("module_summary", settings.prompts.module_summary)
    for module in facts.modules:
        if not module.symbols:
            continue
        try:
            text = (source.root / module.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        head = "\n".join(text.splitlines()[:HEAD_LINES])
        system, user = prompt.render(
            schema=schema_json(ModuleSummaryResult),
            path=module.path,
            lines=str(module.lines),
            symbols=neutralise_tags("\n".join(f"- {s}" for s in module.symbols)),
            head=neutralise_tags(head),
        )
        request = LLMRequest(
            model=settings.models.summary,
            system=system,
            user=user,
            max_tokens=settings.max_tokens.summary,
            effort=settings.effort.summary,
        )
        outcome = complete_structured(llm, request, ModuleSummaryResult, prompt, cache)
        tracer.record_outcome("module_summary", module.path, prompt, outcome)
        if outcome.value is not None:
            module.summary = outcome.value.summary


def render_facts(facts: ArchitectureFacts) -> str:
    lines = [f"modules shown: {len(facts.modules)}; dependency edges: {facts.edges}"]
    if facts.cycles:
        for cycle in facts.cycles:
            lines.append("import cycle: " + " <-> ".join(cycle))
    else:
        lines.append("import cycles: none")
    lines.append("most depended-on: " + (", ".join(facts.hubs) if facts.hubs else "none notable"))
    return "\n".join(lines)


def render_modules(facts: ArchitectureFacts) -> str:
    rows = []
    for m in facts.modules:
        deps = ", ".join(m.imports) if m.imports else "-"
        summary = m.summary or "(no summary)"
        rows.append(
            f"{m.path} ({m.lines} lines, imported by {m.imported_by}; imports: {deps}): {summary}"
        )
    return "\n".join(rows)


def render_reviewed(findings: list[Finding]) -> str:
    if not findings:
        return "(none)"
    return "\n".join(f"- {f.file}: [{f.severity}] {f.title}" for f in findings[:30])


def repo_level_review(
    llm: LLM,
    cache: LLMCache | None,
    tracer: RunTracer,
    settings: Settings,
    facts: ArchitectureFacts,
    hashes: dict[str, str],
    reviewed: list[Finding],
) -> list[Finding]:
    prompt = load_prompt("repo_level", settings.prompts.repo_level)
    system, user = prompt.render(
        schema=schema_json(RepoLevelResult),
        facts=neutralise_tags(render_facts(facts)),
        modules=neutralise_tags(render_modules(facts)),
        findings=neutralise_tags(render_reviewed(reviewed)),
    )
    request = LLMRequest(
        model=settings.models.repo_level,
        system=system,
        user=user,
        max_tokens=settings.max_tokens.repo_level,
        effort=settings.effort.repo_level,
    )
    outcome = complete_structured(llm, request, RepoLevelResult, prompt, cache)
    tracer.record_outcome("repo_level", "<repo>", prompt, outcome)
    if outcome.value is None:
        return []

    known = {m.path for m in facts.modules}
    findings: list[Finding] = []
    for raw in outcome.value.findings:
        files = [f for f in dict.fromkeys(raw.files) if f in known]
        if not files or raw.confidence < settings.review.min_confidence:
            continue  # unknown modules are hallucinations; low confidence is noise
        primary = files[0]
        findings.append(
            Finding(
                id=finding_id(primary, hashes.get(primary, ""), "architecture", 1, raw.title),
                file=primary,
                line_start=1,
                line_end=1,
                symbol="<repo>",
                category="architecture",
                severity=raw.severity,
                confidence=raw.confidence,
                title=raw.title,
                explanation=raw.explanation,
                evidence=raw.evidence,
                chunk_content_hash=hashes.get(primary, ""),
                related_files=files[1:],
            )
        )
    findings.sort(key=lambda f: (-f.confidence, f.id))
    return findings[: settings.repo_level.max_findings]


def finding_counts(findings: list[Finding]) -> Counter[str]:
    return Counter(f.file for f in findings)
