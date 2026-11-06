"""SARIF 2.1.0 view of a ScanReport, for GitHub code scanning and other SARIF consumers."""

from __future__ import annotations

from typing import Any

from reviewer import __version__
from reviewer.report.build import ScanReport
from reviewer.report.rank import RankedFinding
from reviewer.verify.apply_patch import PatchError, parse_patch

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}
_RULES = {
    "bug": "Likely defect: behaviour that is wrong for some input.",
    "security": "Security weakness.",
    "performance": "Performance problem with a concrete cost.",
    "readability": "Readability problem with a concrete cost.",
    "architecture": "Structural problem spanning modules.",
}


def _fixes(r: RankedFinding) -> list[dict[str, Any]]:
    f = r.finding
    if not f.suggested_diff or f.verification_status in ("no_fix", "fix_failed"):
        return []
    try:
        patches = parse_patch(f.suggested_diff)
    except PatchError:
        return []
    changes = []
    for patch in patches:
        replacements = []
        for hunk in patch.hunks:
            new_text = "".join(text + "\n" for op, text in hunk.lines if op in " +")
            if hunk.old_len:
                region = {"startLine": hunk.old_start, "endLine": hunk.old_start + hunk.old_len - 1}
            else:
                region = {"startLine": hunk.old_start + 1, "startColumn": 1, "endColumn": 1}
            replacements.append({"deletedRegion": region, "insertedContent": {"text": new_text}})
        changes.append(
            {
                "artifactLocation": {"uri": patch.path, "uriBaseId": "%SRCROOT%"},
                "replacements": replacements,
            }
        )
    return [
        {
            "description": {
                "text": f"Suggested fix ({f.verification_status}). Review before applying."
            },
            "artifactChanges": changes,
        }
    ]


def _result(r: RankedFinding, rule_index: dict[str, int]) -> dict[str, Any]:
    f = r.finding
    result: dict[str, Any] = {
        "ruleId": f"reviewer/{f.category}",
        "ruleIndex": rule_index[f.category],
        "level": _LEVEL[f.severity],
        "message": {"text": f"{f.title}. {f.explanation}"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": f.file, "uriBaseId": "%SRCROOT%"},
                    "region": {"startLine": f.line_start, "endLine": max(f.line_start, f.line_end)},
                }
            }
        ],
        "partialFingerprints": {"reviewerFindingId/v1": f.id},
        "properties": {
            "severity": f.severity,
            "confidence": f.confidence,
            "score": r.score,
            "blastRadius": r.blast_radius,
            "verificationStatus": f.verification_status,
            "evidence": f.evidence,
            "symbol": f.symbol,
            "relatedSignals": f.related_signals,
        },
    }
    if f.related_files:
        result["relatedLocations"] = [
            {
                "id": i,
                "physicalLocation": {
                    "artifactLocation": {"uri": path, "uriBaseId": "%SRCROOT%"},
                },
            }
            for i, path in enumerate(f.related_files)
        ]
    if fixes := _fixes(r):
        result["fixes"] = fixes
    return result


def render_sarif(scan: ScanReport) -> dict[str, Any]:
    categories = sorted({r.finding.category for r in scan.findings} | set(_RULES))
    rule_index = {c: i for i, c in enumerate(categories)}
    run = scan.run
    sarif_run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "reviewer",
                "version": __version__,
                "rules": [
                    {
                        "id": f"reviewer/{c}",
                        "name": c,
                        "shortDescription": {"text": _RULES[c]},
                        "defaultConfiguration": {"level": "warning"},
                    }
                    for c in categories
                ],
            }
        },
        "invocations": [
            {
                "executionSuccessful": run.status in ("completed", "stopped_budget"),
                "properties": {"runId": run.id, "configVersion": run.config_version},
            }
        ],
        "results": [_result(r, rule_index) for r in scan.findings],
    }
    # Not `versionControlProvenance`: SARIF requires a repositoryUri there, which we do not
    # reliably know (a local checkout may have no remote). GitHub rejects the file without it.
    if not run.commit_sha.startswith("dir-"):
        sarif_run["properties"] = {"commitSha": run.commit_sha}
    return {"$schema": SARIF_SCHEMA, "version": "2.1.0", "runs": [sarif_run]}
