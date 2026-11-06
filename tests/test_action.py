from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from reviewer.cli import app

ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.safe_load((ROOT / "action.yml").read_text())
WORKFLOW = yaml.safe_load((ROOT / "examples" / "github-actions" / "reviewer.yml").read_text())
STEPS: list[dict[str, Any]] = ACTION["runs"]["steps"]


def test_action_is_a_composite_action_with_documented_inputs() -> None:
    assert ACTION["runs"]["using"] == "composite"
    inputs = ACTION["inputs"]
    assert inputs["anthropic-api-key"]["required"] is True
    for name in ("diff-base", "languages", "max-cost", "fix", "upload-sarif"):
        assert "description" in inputs[name] and "default" in inputs[name]
    assert inputs["fix"]["default"] == "false"  # the expensive, Docker-needing path is opt-in


def test_inputs_never_reach_shell_scripts_by_interpolation() -> None:
    """`${{ inputs.x }}` inside `run:` is a script-injection hole; use env vars instead."""
    scripts = [s["run"] for s in STEPS if "run" in s]
    assert scripts
    for script in scripts:
        assert "${{" not in script, script
    assert all(s.get("shell") == "bash" for s in STEPS if "run" in s)


def test_the_api_key_goes_only_to_the_review_step_environment() -> None:
    for step in STEPS:
        mentions = "anthropic-api-key" in yaml.safe_dump(step)
        assert mentions == (step.get("id") == "scan"), step.get("name")
    scan = next(s for s in STEPS if s.get("id") == "scan")
    assert "ANTHROPIC_API_KEY" in scan["env"] and "echo $ANTHROPIC" not in scan["run"]
    assert "set -x" not in scan["run"] and "set -euo pipefail" in scan["run"]


def test_third_party_actions_are_version_pinned() -> None:
    uses = [s["uses"] for s in STEPS if "uses" in s]
    assert uses and all(re.search(r"@(v\d+|[0-9a-f]{40})$", u) for u in uses), uses


def test_every_flag_the_action_passes_exists_on_the_cli() -> None:
    script = next(s["run"] for s in STEPS if s.get("id") == "scan")
    flags = set(re.findall(r"--[a-z-]+", script))
    help_text = CliRunner().invoke(app, ["scan", "--help"]).output
    assert flags >= {"--out", "--max-cost", "--diff-base", "--lang", "--no-fix"}
    for flag in flags:
        assert flag in help_text, flag
    assert "sandbox-build" in next(s for s in STEPS if "sandbox-build" in s.get("run", ""))["run"]


def test_sarif_and_summary_steps_run_even_when_the_review_reports_findings() -> None:
    upload = next(s for s in STEPS if "upload-sarif" in s.get("uses", ""))
    assert "always()" in upload["if"] and upload["with"]["category"] == "reviewer"
    assert upload["with"]["sarif_file"].endswith("/report.sarif")


def test_the_example_workflow_follows_the_safe_pattern() -> None:
    triggers = WORKFLOW[True] if True in WORKFLOW else WORKFLOW["on"]  # YAML parses `on` as True
    assert list(triggers) == ["pull_request"]  # never pull_request_target / workflow_run
    assert WORKFLOW["permissions"] == {"contents": "read", "security-events": "write"}
    job = WORKFLOW["jobs"]["review"]
    assert "head.repo.full_name == github.repository" in job["if"]  # no secrets for forks
    checkout = next(s for s in job["steps"] if "actions/checkout" in s.get("uses", ""))
    assert checkout["with"]["fetch-depth"] == 0 and checkout["with"]["persist-credentials"] is False
    review = job["steps"][-1]
    assert review["with"]["anthropic-api-key"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    assert review["with"]["diff-base"] == "origin/${{ github.base_ref }}"
    assert job["timeout-minutes"] <= 60


@pytest.mark.parametrize(
    "path", [ROOT / "action.yml", ROOT / "examples" / "github-actions" / "reviewer.yml"]
)
def test_no_secret_values_are_committed(path: Path) -> None:
    text = path.read_text()
    assert not re.search(r"sk-ant-|ghp_|AKIA[0-9A-Z]{16}", text)
