"""Run Ruff, Bandit and Semgrep as subprocesses and return their raw JSON.

The tools only read source; none of them imports or executes the analysed code. They run
with a timeout and with the repository's own tool configuration ignored, so results do not
depend on settings the repository controls. A missing or failing tool is reported, never
fatal: signals are optional hints.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reviewer.ingest.sources import home_dir
from reviewer.settings import CONFIG_DIR

TIMEOUT_SECONDS = 300
BATCH_SIZE = 200
SEMGREP_RULES = CONFIG_DIR / "semgrep" / "python.yaml"

# Correctness-oriented Ruff rules only: pyflakes, bugbear, pylint errors/warnings, syntax.
RUFF_SELECT = "E9,F,B,PLE,PLW"


@dataclass
class ToolRun:
    tool: str
    status: str  # "ok" | "missing" | "failed"
    version: str | None = None
    raw: list[Any] | None = None  # one parsed JSON document per batch
    error: str | None = None


def find_tool(name: str) -> Path | None:
    """Locate a tool: $REVIEWER_<NAME>, this venv, PATH, then ~/.cache/reviewer/tools/<name>/."""
    override = os.environ.get(f"REVIEWER_{name.upper()}")
    candidates = [Path(override)] if override else []
    candidates.append(Path(sys.executable).parent / name)
    which = shutil.which(name)
    if which:
        candidates.append(Path(which))
    candidates.append(home_dir() / "tools" / name / "bin" / name)
    return next((c for c in candidates if c.is_file() and os.access(c, os.X_OK)), None)


def _exec(cmd: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "SEMGREP_SEND_METRICS": "off",
        "SEMGREP_ENABLE_VERSION_CHECK": "0",
        "NO_COLOR": "1",
    }
    return subprocess.run(
        list(cmd),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=env,
        check=False,
    )


def tool_version(name: str) -> str | None:
    exe = find_tool(name)
    if exe is None:
        return None
    try:
        out = _exec([str(exe), "--version"], Path.cwd())
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (out.stdout or out.stderr).strip().splitlines()
    return lines[0].strip() if lines else "unknown"


def _batches(paths: Sequence[str]) -> Iterator[list[str]]:
    for i in range(0, len(paths), BATCH_SIZE):
        yield list(paths[i : i + BATCH_SIZE])


def _run_tool(
    tool: str,
    root: Path,
    paths: Sequence[str],
    build_cmd: Callable[[Path, list[str]], list[str]],
    ok_codes: tuple[int, ...],
) -> ToolRun:
    exe = find_tool(tool)
    if exe is None:
        return ToolRun(tool, "missing", error=f"{tool} not found")
    version = tool_version(tool)
    documents: list[Any] = []
    try:
        for batch in _batches(paths):
            proc = _exec(build_cmd(exe, batch), root)
            if proc.returncode not in ok_codes:
                detail = (proc.stderr or proc.stdout).strip()[:300]
                return ToolRun(tool, "failed", version, error=f"exit {proc.returncode}: {detail}")
            documents.append(json.loads(proc.stdout or "null"))
    except subprocess.TimeoutExpired:
        return ToolRun(tool, "failed", version, error=f"timed out after {TIMEOUT_SECONDS}s")
    except (OSError, json.JSONDecodeError) as err:
        return ToolRun(tool, "failed", version, error=f"{type(err).__name__}: {err}")
    return ToolRun(tool, "ok", version, documents)


def run_ruff(root: Path, paths: Sequence[str]) -> ToolRun:
    def cmd(exe: Path, batch: list[str]) -> list[str]:
        return [
            str(exe), "check", "--isolated", "--no-cache", "--no-fix",
            "--output-format", "json", "--select", RUFF_SELECT, "--", *batch,
        ]  # fmt: skip

    return _run_tool("ruff", root, paths, cmd, ok_codes=(0, 1))


def run_bandit(root: Path, paths: Sequence[str]) -> ToolRun:
    def cmd(exe: Path, batch: list[str]) -> list[str]:
        return [str(exe), "-f", "json", "-q", "--exit-zero", *batch]

    return _run_tool("bandit", root, paths, cmd, ok_codes=(0,))


def run_semgrep(root: Path, paths: Sequence[str]) -> ToolRun:
    def cmd(exe: Path, batch: list[str]) -> list[str]:
        return [
            str(exe), "scan", "--config", str(SEMGREP_RULES), "--json", "--metrics", "off",
            "--disable-version-check", "--quiet", "--max-target-bytes", "1000000", *batch,
        ]  # fmt: skip

    return _run_tool("semgrep", root, paths, cmd, ok_codes=(0, 1))


RUNNERS: dict[str, Callable[[Path, Sequence[str]], ToolRun]] = {
    "ruff": run_ruff,
    "bandit": run_bandit,
    "semgrep": run_semgrep,
}


def tool_fingerprint(tools: Sequence[str]) -> str:
    """Changes when a tool, its version or the rules change, which invalidates stored signals."""
    parts = []
    for tool in sorted(tools):
        parts.append(f"{tool}={tool_version(tool) or 'missing'}")
    parts.append(f"ruff_select={RUFF_SELECT}")
    parts.append("semgrep_rules=" + hashlib.sha256(SEMGREP_RULES.read_bytes()).hexdigest()[:12])
    return ";".join(parts)
