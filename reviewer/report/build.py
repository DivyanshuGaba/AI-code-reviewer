"""The scan report: ranked findings plus everything the renderers need. JSON is the source of
truth; Markdown, SARIF and HTML are views of this one model."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from reviewer.analysis.models import ArchitectureFacts
from reviewer.index.store import Store
from reviewer.report.rank import RankedFinding, rank_findings
from reviewer.runs.models import RunRecord, RunReport


class ScanSummary(BaseModel):
    total: int
    by_severity: dict[str, int]
    by_category: dict[str, int]
    by_fix_status: dict[str, int]
    duplicates_merged: int


class ScanReport(BaseModel):
    report_version: Literal["1"] = "1"
    generated_at: str
    run: RunRecord
    summary: ScanSummary
    findings: list[RankedFinding]
    architecture: ArchitectureFacts | None = None
    trace_file: str | None = None  # full prompt/output trace of every model call


SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]


def build_scan_report(
    report: RunReport, store: Store, trace_file: Path | None = None
) -> ScanReport:
    ranked = rank_findings(report.findings, report.verifications, store)
    severity: Counter[str] = Counter(r.finding.severity for r in ranked)
    return ScanReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        run=report.run,
        summary=ScanSummary(
            total=len(ranked),
            by_severity={s: severity[s] for s in SEVERITY_ORDER if severity[s]},
            by_category=dict(Counter(r.finding.category for r in ranked)),
            by_fix_status=dict(Counter(r.finding.verification_status for r in ranked)),
            duplicates_merged=sum(len(r.duplicates) for r in ranked),
        ),
        findings=ranked,
        architecture=report.architecture,
        trace_file=str(trace_file) if trace_file else None,
    )
