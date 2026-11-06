"""Turn each tool's raw JSON into `Signal`s.

Severity and category are mapped here, in one place, so the rest of the pipeline never
needs to know which tool a hint came from.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from reviewer.signals.models import Signal, SignalSeverity, signal_id

_BANDIT_LEVEL: dict[str, SignalSeverity] = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}
_SEMGREP_LEVEL: dict[str, SignalSeverity] = {"INFO": "low", "WARNING": "medium", "ERROR": "high"}
_RUFF_HIGH = {"F821", "F822", "F823", "F63", "F7", "E9"}
_VALID_CATEGORIES = {"bug", "security", "performance", "readability", "architecture"}


def _relative(filename: str, root: Path) -> str:
    path = Path(filename)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()
    return PurePosixPath(filename.replace("\\", "/")).as_posix().removeprefix("./")


def normalize_ruff(documents: list[Any], root: Path) -> list[Signal]:
    signals = []
    for doc in documents:
        for item in doc or []:
            code = item.get("code") or "unknown"
            path = _relative(item["filename"], root)
            line = item["location"]["row"]
            end = item.get("end_location", {}).get("row", line)
            if any(code.startswith(p) for p in _RUFF_HIGH):
                severity: SignalSeverity = "high"
            elif code.startswith("B") or code.startswith("PLE"):
                severity = "medium"
            else:
                severity = "low"
            signals.append(
                Signal(
                    id=signal_id("ruff", code, path, line),
                    tool="ruff",
                    rule_id=code,
                    path=path,
                    line_start=line,
                    line_end=max(line, end),
                    category="bug",
                    severity=severity,
                    confidence="high" if code.startswith(("F", "E9")) else "medium",
                    message=item.get("message", ""),
                    url=item.get("url"),
                )
            )
    return signals


def normalize_bandit(documents: list[Any], root: Path) -> list[Signal]:
    signals = []
    for doc in documents:
        for item in (doc or {}).get("results", []):
            path = _relative(item["filename"], root)
            lines = item.get("line_range") or [item["line_number"]]
            rule = item.get("test_id", "unknown")
            cwe = (item.get("issue_cwe") or {}).get("id")
            signals.append(
                Signal(
                    id=signal_id("bandit", rule, path, min(lines)),
                    tool="bandit",
                    rule_id=rule,
                    path=path,
                    line_start=min(lines),
                    line_end=max(lines),
                    category="security",
                    severity=_BANDIT_LEVEL.get(item.get("issue_severity", ""), "low"),
                    confidence=_BANDIT_LEVEL.get(item.get("issue_confidence", "")),
                    message=item.get("issue_text", ""),
                    url=item.get("more_info"),
                    cwe=cwe if isinstance(cwe, int) else None,
                )
            )
    return signals


def normalize_semgrep(documents: list[Any], root: Path) -> list[Signal]:
    signals = []
    for doc in documents:
        for item in (doc or {}).get("results", []):
            path = _relative(item["path"], root)
            extra = item.get("extra", {})
            # Semgrep prefixes rule ids with the config's path; the last segment is the rule.
            rule = item["check_id"].rsplit(".", 1)[-1]
            category = (extra.get("metadata") or {}).get("category", "bug")
            line = item["start"]["line"]
            signals.append(
                Signal(
                    id=signal_id("semgrep", rule, path, line),
                    tool="semgrep",
                    rule_id=rule,
                    path=path,
                    line_start=line,
                    line_end=max(line, item["end"]["line"]),
                    category=category if category in _VALID_CATEGORIES else "bug",
                    severity=_SEMGREP_LEVEL.get(extra.get("severity", ""), "low"),
                    message=extra.get("message", ""),
                )
            )
    return signals


_NORMALIZERS = {
    "ruff": normalize_ruff,
    "bandit": normalize_bandit,
    "semgrep": normalize_semgrep,
}


def normalize(tool: str, documents: list[Any], root: Path) -> list[Signal]:
    return _NORMALIZERS[tool](documents, root)


def dedupe(signals: list[Signal], allowed_paths: set[str] | None = None) -> list[Signal]:
    """Drop exact repeats and anything outside the indexed files; stable order."""
    seen: set[str] = set()
    result = []
    for s in sorted(signals, key=lambda s: (s.path, s.line_start, s.tool, s.rule_id)):
        if s.id in seen or (allowed_paths is not None and s.path not in allowed_paths):
            continue
        seen.add(s.id)
        result.append(s)
    return result
