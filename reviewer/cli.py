"""Command line interface. Subcommands only, no clever flag parsing."""

from __future__ import annotations

import textwrap
from pathlib import Path

import typer

from reviewer.index.store import Relation, Store
from reviewer.ingest.sources import SourceError, locate
from reviewer.pipeline import (
    context_for_file,
    db_path_for_spec,
    generate_reports,
    index_repository,
    review_repository,
    runs_db_path_for,
    write_report_json,
)
from reviewer.report.build import ScanReport
from reviewer.retrieval.budget import DEFAULT_BUDGET
from reviewer.retrieval.context import render_bundle
from reviewer.runs.tracing import RunStore
from reviewer.signals.models import SEVERITY_RANK
from reviewer.verify.apply_patch import PatchError, parse_patch
from reviewer.verify.checks import Verifier
from reviewer.verify.sandbox import DockerSandbox

app = typer.Typer(help="AI code review tool.", no_args_is_help=True, add_completion=False)


@app.command()
def index(target: str = typer.Argument(..., help="Local path or git URL of a repository.")) -> None:
    """Ingest a repository and build its code graph."""
    try:
        report = index_repository(target)
    except SourceError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err

    pin = report.commit_sha[:12] + (" (dirty working tree)" if report.dirty else "")
    typer.echo(f"Indexed {report.root}")
    typer.echo(f"  pinned to   {pin}")
    typer.echo(
        f"  files       {report.files_total} "
        f"({report.files_changed} parsed, {report.files_removed} removed)"
    )
    if report.files_with_syntax_errors:
        typer.echo(f"  syntax errs {report.files_with_syntax_errors} file(s) parsed with errors")
    c = report.counts
    typer.echo(f"  symbols     {c['symbols']}")
    typer.echo(f"  chunks      {c['chunks']}")
    typer.echo(f"  embeddings  {report.chunks_embedded} new ({report.embedder})")
    for tool, status in report.signal_status.items():
        typer.echo(f"  signals     {tool}: {status}")
    typer.echo(f"  static hits {c['signals']}")
    typer.echo(f"  call sites  {c['calls']} ({c['edges']} resolved edges)")
    typer.echo(f"  database    {report.db_path}")


@app.command()
def query(
    symbol: str = typer.Argument(..., help="Function, method or class name (or Class.method)."),
    repo: str = typer.Option(".", help="Repository path or URL that was indexed."),
) -> None:
    """Show callers and callees of a symbol."""
    try:
        db_path = db_path_for_spec(repo)
    except SourceError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err
    if not db_path.exists():
        typer.echo(f"error: no index for {repo}; run `reviewer index {repo}` first", err=True)
        raise typer.Exit(1)

    with Store(db_path) as store:
        matches = store.find_symbols(symbol)
        if not matches:
            typer.echo(f"No symbol matching '{symbol}'.")
            raise typer.Exit(1)
        for sym in matches:
            typer.echo(f"{sym.qualname}  [{sym.kind}]  {sym.path}:{sym.start_line}-{sym.end_line}")
            typer.echo(f"  {sym.signature}")
            callers = store.callers(sym.id)
            typer.echo(f"  Callers ({len(callers)}):")
            _print_relations(callers, callers_side=True)
            callees = store.callees(sym.id)
            typer.echo(f"  Callees ({len(callees)}):")
            _print_relations(callees, callers_side=False)
            external = store.unresolved_calls(sym.id)
            if external:
                typer.echo(f"  Unresolved/external calls: {', '.join(external)}")
            typer.echo("")


@app.command()
def context(
    file: str = typer.Argument(..., help="Source file, relative to the repo or absolute."),
    repo: str = typer.Option(".", help="Repository path or URL that was indexed."),
    symbol: str = typer.Option("", help="Only this function/method (e.g. Gateway.charge)."),
    budget: int = typer.Option(DEFAULT_BUDGET, help="Token budget per bundle."),
    as_json: bool = typer.Option(False, "--json", help="Print bundles as JSON."),
) -> None:
    """Show the retrieval context built for each function in a file."""
    try:
        bundles = context_for_file(repo, file, symbol or None, budget)
    except SourceError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err
    if as_json:
        typer.echo("[" + ",".join(b.model_dump_json() for b in bundles) + "]")
        return
    for bundle in bundles:
        typer.echo(render_bundle(bundle))
        typer.echo("")


@app.command()
def signals(
    repo: str = typer.Option(".", help="Repository path or URL that was indexed."),
    file: str = typer.Option("", help="Only this file (repo-relative path)."),
    min_severity: str = typer.Option("low", help="low, medium or high."),
) -> None:
    """List the normalised static-analysis signals stored for a repository."""
    if min_severity not in SEVERITY_RANK:
        typer.echo("error: --min-severity must be low, medium or high", err=True)
        raise typer.Exit(1)
    try:
        db_path = db_path_for_spec(repo)
    except SourceError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err
    if not db_path.exists():
        typer.echo(f"error: no index for {repo}; run `reviewer index {repo}` first", err=True)
        raise typer.Exit(1)
    with Store(db_path) as store:
        found = [
            s
            for s in store.load_signals(file or None)
            if SEVERITY_RANK[s.severity] >= SEVERITY_RANK[min_severity]
        ]
    for s in found:
        typer.echo(f"{s.path}:{s.line_start}  [{s.severity}] {s.ref} ({s.category})  {s.message}")
    typer.echo(f"{len(found)} signal(s). These are hints, not verified findings.")


@app.command()
def review(
    target: str = typer.Argument(..., help="Local path or git URL of a repository."),
    file: str = typer.Option("", help="Only review this file (repo-relative path)."),
    max_chunks: int = typer.Option(0, help="Review at most this many functions (0 = all)."),
    fix: bool = typer.Option(False, "--fix", help="Also generate and sandbox-verify fixes."),
) -> None:
    """Triage and deep-review functions with Claude. Calls the API and costs money."""
    try:
        report = review_repository(
            target,
            file=file or None,
            max_chunks=max_chunks or None,
            fix=fix,
            progress=lambda msg: typer.echo(msg, err=True),
        )
    except SourceError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err

    run = report.run
    out_path = write_report_json(report)
    verification_by_id = {v.finding_id: v.checks for v in report.verifications}
    typer.echo("")
    for f in report.findings:
        typer.echo(
            f"[{f.severity.upper()}] {f.title}  ({f.category}, confidence {f.confidence:.2f})"
        )
        typer.echo(f"    {f.file}:{f.line_start}-{f.line_end}  in {f.symbol}")
        typer.echo(f"    {f.explanation}")
        typer.echo(f"    evidence: {f.evidence}")
        if f.verification_status != "no_fix":
            typer.echo(f"    fix: {f.verification_status}")
            for check in verification_by_id.get(f.id, []):
                mark = {"passed": "ok", "failed": "FAILED", "skipped": "skipped"}[check.status]
                typer.echo(f"      {check.name}: {mark} {check.detail}".rstrip())
            typer.echo("")
            typer.echo(textwrap.indent(f.suggested_diff or "", "    "))
        typer.echo("")
    typer.echo(
        f"Run {run.id}: {run.status}; {run.findings_count} finding(s) from "
        f"{run.chunks_deep_reviewed}/{run.chunks_total} functions deep-reviewed; "
        f"cost ${run.cost_usd:.4f}"
    )
    if run.fixes_attempted:
        typer.echo(
            f"  fixes: {run.fixes_verified} verified, {run.fixes_unverified} unverified, "
            f"{run.fixes_failed} failed, {run.fixes_not_generated} not generated"
        )
    dropped = run.findings_dropped_low_confidence + run.findings_dropped_out_of_range
    if dropped or run.stage_failures:
        typer.echo(
            f"  dropped {dropped} low-confidence/out-of-range finding(s); "
            f"{run.stage_failures} stage failure(s)"
        )
    typer.echo(f"  full trace: {out_path}")
    if run.status == "failed":
        typer.echo(f"error: {run.error}", err=True)
        raise typer.Exit(1)


def _print_report_summary(scan: ScanReport, paths: dict[str, Path]) -> None:
    run, s = scan.run, scan.summary
    typer.echo("")
    for r in scan.findings[:10]:
        f = r.finding
        where = f.file if f.symbol == "<repo>" else f"{f.file}:{f.line_start}"
        fix = "" if f.verification_status == "no_fix" else f"  fix: {f.verification_status}"
        typer.echo(f"{r.rank:>2}. [{f.severity.upper():<8}] {f.title}  ({where}){fix}")
    if len(scan.findings) > 10:
        typer.echo(f"    ... and {len(scan.findings) - 10} more in the report")
    typer.echo(
        f"\nRun {run.id}: {run.status}; {s.total} finding(s)"
        + (f" ({s.duplicates_merged} duplicate(s) merged)" if s.duplicates_merged else "")
        + f"; cost ${run.cost_usd:.4f}"
    )
    for fmt, path in paths.items():
        typer.echo(f"  {fmt:<8} {path}")


@app.command()
def scan(
    target: str = typer.Argument(..., help="Local path or git URL of a repository."),
    out: Path | None = typer.Option(None, help="Directory for the reports."),
    max_chunks: int = typer.Option(0, help="Review at most this many functions (0 = all)."),
    no_fix: bool = typer.Option(False, "--no-fix", help="Skip fix generation and verification."),
) -> None:
    """Full pipeline: index, review, repo-level pass, fixes, ranking and reports.

    Calls the Claude API and costs money (see review.max_cost_usd in settings.yaml).
    """
    try:
        run_report = review_repository(
            target,
            max_chunks=max_chunks or None,
            fix=not no_fix,
            repo_level=True,
            progress=lambda msg: typer.echo(msg, err=True),
        )
        scan_report, paths = generate_reports(target, report=run_report, out_dir=out)
    except SourceError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err
    _print_report_summary(scan_report, paths)
    if run_report.run.status == "failed":
        typer.echo(f"error: {run_report.run.error}", err=True)
        raise typer.Exit(1)


@app.command()
def report(
    repo: str = typer.Option(".", help="Repository path or URL that was scanned."),
    run: str = typer.Option("", help="Run id (default: the latest run)."),
    out: Path | None = typer.Option(None, help="Directory for the reports."),
) -> None:
    """Re-render the reports for a stored run. No LLM calls."""
    try:
        scan_report, paths = generate_reports(repo, run_id=run or None, out_dir=out)
    except SourceError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err
    _print_report_summary(scan_report, paths)


@app.command()
def verify(
    repo: str = typer.Argument(..., help="Local path of the repository."),
    patch: Path = typer.Argument(..., help="Unified diff to apply and check."),
) -> None:
    """Apply a patch to a throwaway copy and check it in the sandbox. No LLM involved."""
    try:
        root = locate(repo)
        diff = patch.read_text(encoding="utf-8")
        files = {p.path for p in parse_patch(diff)}
    except (SourceError, OSError, PatchError) as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err
    with Verifier(DockerSandbox(), root) as verifier:
        result = verifier.verify("manual", diff, files)
    typer.echo(f"{result.status}  (sandbox: {result.sandbox}, {result.duration_s}s)")
    for check in result.checks:
        typer.echo(f"  {check.name}: {check.status} {check.detail}".rstrip())
    if result.status == "fix_failed":
        raise typer.Exit(1)


@app.command("sandbox-build")
def sandbox_build() -> None:
    """Build the Docker image used to verify fixes. Needs network access once."""
    sandbox = DockerSandbox()
    typer.echo(f"Building {sandbox.image} (downloads a Python base image and tools)...")
    try:
        proc = sandbox.build_image()
    except OSError as err:
        typer.echo(f"error: docker is not available: {err}", err=True)
        raise typer.Exit(1) from err
    if proc.returncode != 0:
        typer.echo(proc.stderr.strip()[-800:], err=True)
        raise typer.Exit(1)
    typer.echo("Done.")


@app.command()
def runs(
    repo: str = typer.Option(".", help="Repository path or URL that was indexed."),
) -> None:
    """List past review runs for a repository."""
    try:
        db = runs_db_path_for(db_path_for_spec(repo))
    except SourceError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err
    if not db.exists():
        typer.echo("No runs yet.")
        return
    with RunStore(db) as store:
        for r in store.list_runs():
            typer.echo(
                f"{r.id}  {r.status:<14} {r.findings_count} finding(s)  ${r.cost_usd:.4f}  "
                f"commit {r.commit_sha[:10]}  config v{r.config_version}"
            )


def _print_relations(relations: list[Relation], *, callers_side: bool) -> None:
    """Callers are located at the call site; callees at their definition."""
    if not relations:
        typer.echo("    (none)")
    for rel in relations:
        s = rel.symbol
        if callers_side:
            where = f"{s.path}:{rel.line}"
        else:
            where = f"{s.path}:{s.start_line}  (called at line {rel.line})"
        typer.echo(f"    {s.qualname:<28} {where}  [{rel.resolution}]")


if __name__ == "__main__":
    app()
