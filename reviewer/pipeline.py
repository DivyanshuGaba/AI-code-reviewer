"""Orchestrates ingest -> parse -> store -> graph. Phase 1 stops after the code graph."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from reviewer.analysis.deep_review import deep_review
from reviewer.analysis.fixgen import generate_fix
from reviewer.analysis.repo_level import build_facts, repo_level_review, summarize_modules
from reviewer.analysis.triage import should_deep_review, should_escalate, triage_chunk
from reviewer.index.graph import resolve_edges
from reviewer.index.languages import parse_file
from reviewer.index.lexical import LexicalIndex
from reviewer.index.store import Store
from reviewer.index.vectors import VectorIndex
from reviewer.ingest.manifest import build_manifest
from reviewer.ingest.sources import SourceError, home_dir, locate, open_source
from reviewer.llm.cache import LLMCache
from reviewer.llm.embeddings import Embedder, default_embedder
from reviewer.llm.gateway import LLM, AnthropicGateway, GatewayAuthError
from reviewer.llm.schemas import SEVERITY_RANK, Finding
from reviewer.report import write_reports
from reviewer.report.build import ScanReport, build_scan_report
from reviewer.retrieval.budget import DEFAULT_BUDGET
from reviewer.retrieval.context import ContextBundle, build_context
from reviewer.retrieval.strategies import is_test_path
from reviewer.runs.models import RunRecord, RunReport
from reviewer.runs.tracing import RunStore, RunTracer
from reviewer.settings import Settings, load_settings
from reviewer.signals.collect import collect_signals
from reviewer.signals.runners import tool_fingerprint
from reviewer.verify.checks import Verifier
from reviewer.verify.models import VerificationResult
from reviewer.verify.sandbox import DockerSandbox, Sandbox


@dataclass(frozen=True)
class IndexReport:
    root: Path
    db_path: Path
    commit_sha: str
    dirty: bool
    files_total: int
    files_changed: int
    files_removed: int
    files_with_syntax_errors: int
    counts: dict[str, int]
    chunks_embedded: int
    embedder: str
    signal_status: dict[str, str]


def db_path_for(root: Path) -> Path:
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
    return home_dir() / "indexes" / f"{root.resolve().name}-{digest}.db"


def vector_path_for(db_path: Path) -> Path:
    return db_path.with_suffix(".chroma")


def db_path_for_spec(spec: str) -> Path:
    return db_path_for(locate(spec))


def default_signal_tools() -> tuple[str, ...]:
    return tuple(load_settings().signals.tools)


def index_repository(
    spec: str,
    embedder: Embedder | None = None,
    signal_tools: Sequence[str] | None = None,
    languages: Sequence[str] | None = None,
) -> IndexReport:
    """Index a repository. Files whose content hash is unchanged are not re-parsed."""
    source = open_source(spec)
    manifest = build_manifest(source, tuple(languages or load_settings().languages))
    db_path = db_path_for(source.root)

    with Store(db_path) as store:
        stored = store.stored_hashes()
        current = {f.path: f for f in manifest.files}

        removed = sorted(set(stored) - set(current))
        store.remove_files(removed)

        changed = 0
        changed_paths: list[str] = []
        syntax_errors = 0
        for entry in manifest.files:
            if stored.get(entry.path) == entry.sha256:
                continue
            parsed = parse_file(entry.path, (source.root / entry.path).read_bytes(), entry.language)
            syntax_errors += parsed.has_syntax_errors
            store.add_file(entry, parsed)
            changed += 1
            changed_paths.append(entry.path)

        store.replace_edges(
            resolve_edges(store.load_symbols(), store.load_imports(), store.load_calls())
        )
        signal_status = _refresh_signals(
            store,
            source.root,
            [f.path for f in manifest.files],
            changed_paths,
            default_signal_tools() if signal_tools is None else tuple(signal_tools),
        )
        chunks = store.load_chunks()
        LexicalIndex(store.conn).sync(chunks)
        embedder = embedder or default_embedder()
        embedded, _ = VectorIndex(vector_path_for(db_path), embedder).sync(chunks)

        store.set_meta("commit_sha", manifest.commit_sha)
        store.set_meta("dirty", "1" if manifest.dirty else "0")
        store.set_meta("root", str(source.root))
        store.set_meta("tree_hash", manifest.tree_hash)
        store.set_meta("indexed_at", datetime.now(UTC).isoformat())

        return IndexReport(
            root=source.root,
            db_path=db_path,
            commit_sha=manifest.commit_sha,
            dirty=manifest.dirty,
            files_total=len(manifest.files),
            files_changed=changed,
            files_removed=len(removed),
            files_with_syntax_errors=syntax_errors,
            counts=store.counts(),
            chunks_embedded=embedded,
            embedder=embedder.name,
            signal_status=signal_status,
        )


def _refresh_signals(
    store: Store,
    root: Path,
    all_paths: list[str],
    changed_paths: list[str],
    tools: tuple[str, ...],
) -> dict[str, str]:
    """Re-run static tools on changed files, or on everything if tools or rules changed.

    The tools are Python-only, so other languages get no static signals.
    """
    if not tools:
        return {}
    all_paths = [p for p in all_paths if p.endswith(".py")]
    changed_paths = [p for p in changed_paths if p.endswith(".py")]
    if not all_paths:
        return {tool: "skipped (no Python files)" for tool in tools}
    fingerprint = tool_fingerprint(tools)
    stale = store.get_meta("signals_fingerprint") != fingerprint
    targets = all_paths if stale else changed_paths
    if not targets:
        return {tool: "cached" for tool in tools}
    collection = collect_signals(root, targets, tools)
    store.replace_signals(targets, collection.signals)
    # A tool that was missing or failed is retried on the next index.
    store.set_meta("signals_fingerprint", fingerprint if collection.complete else "")
    return collection.status


def context_for_file(
    spec: str,
    file: str,
    symbol: str | None = None,
    budget: int = DEFAULT_BUDGET,
    embedder: Embedder | None = None,
) -> list[ContextBundle]:
    """ContextBundles for every chunk in `file` (or just the chunk named `symbol`)."""
    root = locate(spec)
    db_path = db_path_for(root)
    if not db_path.exists():
        raise SourceError(f"no index for {spec}; run `reviewer index {spec}` first")

    candidate = Path(file)
    abs_path = candidate if candidate.is_absolute() else (Path.cwd() / candidate)
    try:
        rel = abs_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = candidate.as_posix()  # already repo-relative

    with Store(db_path) as store:
        chunks = store.load_chunks(rel)
        if symbol:
            chunks = [c for c in chunks if c.symbol == symbol or c.symbol.endswith(f".{symbol}")]
        if not chunks:
            raise SourceError(
                f"no indexed functions found in {rel}" + (f" matching {symbol}" if symbol else "")
            )
        lexical = LexicalIndex(store.conn)
        vectors = VectorIndex(vector_path_for(db_path), embedder or default_embedder())
        return [build_context(c, store, lexical, vectors, budget) for c in chunks]


def runs_db_path_for(db_path: Path) -> Path:
    return db_path.with_suffix(".runs.db")


def review_repository(
    spec: str,
    *,
    file: str | None = None,
    max_chunks: int | None = None,
    fix: bool = False,
    repo_level: bool = False,
    sandbox: Sandbox | None = None,
    llm: LLM | None = None,
    settings: Settings | None = None,
    embedder: Embedder | None = None,
    languages: Sequence[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> RunReport:
    """Index, then triage and deep-review functions; every call is traced and cached.

    Test files are skipped (their code is used as context, not reviewed). The run stops
    cleanly when `settings.review.max_cost_usd` is reached.
    """
    settings = settings or load_settings()
    say = progress or (lambda _msg: None)
    source = open_source(spec)
    index_report = index_repository(spec, embedder, languages=languages)
    llm = llm or AnthropicGateway()
    emb = embedder or default_embedder()

    run = RunRecord(
        id=f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{index_report.commit_sha[:8]}-{uuid.uuid4().hex[:4]}",
        repo_root=str(index_report.root),
        commit_sha=index_report.commit_sha,
        dirty=index_report.dirty,
        config_version=settings.config_version,
        models=settings.models.model_dump(),
        prompt_versions=settings.prompts.model_dump(),
        started_at=datetime.now(UTC).isoformat(),
    )
    findings: list[Finding] = []
    bundles: dict[str, ContextBundle] = {}
    verifications: list[VerificationResult] = []

    with (
        Store(index_report.db_path) as store,
        RunStore(runs_db_path_for(index_report.db_path)) as run_store,
    ):
        tracer = RunTracer(run_store, run)
        cache = LLMCache(run_store.conn)
        lexical = LexicalIndex(store.conn)
        vectors = VectorIndex(vector_path_for(index_report.db_path), emb)

        chunks = [
            c
            for c in store.load_chunks()
            if not is_test_path(c.path) and (file is None or c.path == file)
        ]
        if max_chunks is not None:
            chunks = chunks[:max_chunks]
        run.chunks_total = len(chunks)

        status, error = "completed", None
        try:
            for i, chunk in enumerate(chunks, 1):
                if tracer.cost_usd >= settings.review.max_cost_usd:
                    status = "stopped_budget"
                    say(f"stopping: cost limit ${settings.review.max_cost_usd:.2f} reached")
                    break
                say(f"[{i}/{len(chunks)}] {chunk.path}::{chunk.symbol}")
                triage = triage_chunk(llm, cache, tracer, settings, chunk)
                run.chunks_triaged += 1
                if triage is None:
                    continue  # failure already recorded; one bad chunk must not stop the run
                wanted = should_deep_review(triage, settings.review.triage_threshold)
                escalated = not wanted and should_escalate(
                    store.signals_in(chunk.path, chunk.start_line, chunk.end_line),
                    settings.review.escalate_min_severity,
                )
                if not wanted and not escalated:
                    continue
                run.chunks_escalated_by_signals += int(escalated)
                bundle = build_context(
                    chunk, store, lexical, vectors, settings.retrieval.token_budget
                )
                outcome = deep_review(llm, cache, tracer, settings, bundle, triage)
                run.chunks_deep_reviewed += 1
                run.findings_dropped_low_confidence += outcome.dropped_low_confidence
                run.findings_dropped_out_of_range += outcome.dropped_out_of_range
                findings.extend(outcome.findings)
                bundles.update({f.id: bundle for f in outcome.findings})
        except GatewayAuthError as err:
            status, error = "failed", str(err)
        except Exception as err:
            tracer.finish("failed", f"{type(err).__name__}: {err}")
            raise

        if repo_level and file is None and status == "completed":
            facts, _deps = build_facts(store, settings.repo_level.max_modules)
            if len(facts.modules) >= settings.repo_level.min_modules:
                say("repo-level pass: summarising modules and reviewing architecture")
                try:
                    summarize_modules(llm, cache, tracer, settings, source, facts)
                    findings.extend(
                        repo_level_review(
                            llm, cache, tracer, settings, facts, store.stored_hashes(), findings
                        )
                    )
                except GatewayAuthError as err:
                    status, error = "failed", str(err)
                run_store.save_architecture(run.id, facts)

        if fix and findings and status != "failed":
            verifications = _fix_findings(
                findings, bundles, index_report.root, llm, cache, tracer, settings,
                sandbox or DockerSandbox(), say,
            )  # fmt: skip
            run_store.save_verifications(run.id, verifications)

        run.findings_count = len(findings)
        run_store.save_findings(run.id, findings)
        tracer.finish(status, error)
        report = run_store.load_report(run.id)
        assert report is not None
    return report


def write_report_json(report: RunReport) -> Path:
    """The JSON file is the source of truth for a run: findings plus every traced call."""
    path = home_dir() / "runs" / f"{report.run.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2))
    return path


def _fix_findings(
    findings: list[Finding],
    bundles: dict[str, ContextBundle],
    root: Path,
    llm: LLM,
    cache: LLMCache,
    tracer: RunTracer,
    settings: Settings,
    sandbox: Sandbox,
    say: Callable[[str], None],
) -> list[VerificationResult]:
    """Generate and verify a fix for each eligible finding, most severe first."""
    run = tracer.run
    floor = SEVERITY_RANK[settings.fix.min_severity]
    eligible = sorted(
        (f for f in findings if SEVERITY_RANK[f.severity] >= floor and f.id in bundles),
        key=lambda f: (-SEVERITY_RANK[f.severity], -f.confidence, f.id),
    )[: settings.fix.max_fixes]
    results: list[VerificationResult] = []
    if not eligible:
        return results

    try:
        with Verifier(sandbox, root) as verifier:
            for n, finding in enumerate(eligible, 1):
                if tracer.cost_usd >= settings.review.max_cost_usd:
                    say("stopping fixes: cost limit reached")
                    run.status = "stopped_budget"
                    break
                say(
                    f"[fix {n}/{len(eligible)}] {finding.file}:{finding.line_start} {finding.title}"
                )
                run.fixes_attempted += 1
                file_text = (root / finding.file).read_text(encoding="utf-8")
                outcome = generate_fix(
                    llm, cache, tracer, settings, finding, bundles[finding.id], file_text
                )
                if outcome.diff is None:
                    run.fixes_not_generated += 1
                    say(f"    no fix: {outcome.failure}")
                    continue
                finding.suggested_diff = outcome.diff
                result = verifier.verify(finding.id, outcome.diff, {finding.file})
                finding.verification_status = result.status
                results.append(result)
                if result.status == "verified_fix":
                    run.fixes_verified += 1
                elif result.status == "fix_failed":
                    run.fixes_failed += 1
                else:
                    run.fixes_unverified += 1
                say(f"    {result.status}")
    except GatewayAuthError as err:
        run.status, run.error = "failed", str(err)
    return results


def generate_reports(
    spec: str,
    *,
    run_id: str | None = None,
    out_dir: Path | None = None,
    report: RunReport | None = None,
) -> tuple[ScanReport, dict[str, Path]]:
    """Rank a run's findings and write report.json/.md/.sarif/.html. No LLM calls.

    Uses `report` if given, otherwise loads `run_id` (default: the latest run) from the store.
    """
    root = locate(spec)
    db_path = db_path_for(root)
    if not db_path.exists():
        raise SourceError(f"no index for {spec}; run `reviewer index {spec}` first")
    with RunStore(runs_db_path_for(db_path)) as run_store, Store(db_path) as store:
        if report is None:
            runs = run_store.list_runs()
            if not runs:
                raise SourceError("no runs yet; run `reviewer scan` or `reviewer review` first")
            wanted = run_id or runs[0].id
            report = run_store.load_report(wanted)
            if report is None:
                raise SourceError(f"no such run: {wanted}")
        trace = write_report_json(report)
        scan = build_scan_report(report, store, trace_file=trace)
    target = out_dir or home_dir() / "reports" / report.run.id
    return scan, write_reports(scan, target)
