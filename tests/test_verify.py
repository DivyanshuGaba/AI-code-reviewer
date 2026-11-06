from __future__ import annotations

import json
from pathlib import Path

import pytest

from reviewer.verify.apply_patch import make_diff
from reviewer.verify.checks import Verifier, parse_mypy, parse_pytest, parse_ruff
from reviewer.verify.sandbox import ExecResult, docker_run_command

SRC = "def add(a, b):\n    return a + b\n"
FIXED = "def add(a, b):\n    return b + a\n"
BROKEN_SYNTAX = "def add(a, b):\n    return a +\n"
LINT_BAD = "def add(a, b):\n    return a + b + undefined_name\n"
TYPE_BAD = "def add(a, b):\n    x: int = 'no'\n    return a + b\n"


class FakeSandbox:
    """Answers each tool from a script; tests decide what 'ruff on the patched copy' says."""

    name = "fake"

    def __init__(self, unavailable: str | None = None) -> None:
        self.unavailable = unavailable
        self.calls: list[tuple[str, str]] = []  # (workdir name, tool)
        self.ruff: dict[str, list[str]] = {}  # workdir name -> rule codes
        self.mypy: dict[str, str] = {}
        self.tests: dict[str, str] = {}
        self.timeout_tests: set[str] = set()

    def check_available(self) -> str | None:
        return self.unavailable

    def run(self, workdir: Path, argv: list[str], timeout: int) -> ExecResult:
        who = "base" if workdir.name == "base" else "patched"
        tool = argv[0] if argv[0] != "python" else "pytest"
        self.calls.append((who, tool))
        if tool == "ruff":
            return ExecResult(1, json.dumps([{"code": c} for c in self.ruff.get(who, [])]), "")
        if tool == "mypy":
            return ExecResult(1, self.mypy.get(who, ""), "")
        if who in self.timeout_tests:
            return ExecResult(124, "", "timeout", timed_out=True)
        return ExecResult(0, self.tests.get(who, ""), "")


PASSING = "PASSED tests/test_a.py::test_one\nPASSED tests/test_a.py::test_two\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "m.py").write_text(SRC)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.py").write_text("x = 1\n")
    return root


def verify(project: Path, sandbox: FakeSandbox, new_text: str = FIXED, **kw: object):  # type: ignore[no-untyped-def]
    with Verifier(sandbox, project) as v:
        return v.verify("F-1", make_diff("m.py", SRC, new_text), {"m.py"}), v


def statuses(result) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {c.name: c.status for c in result.checks}


def test_parsers() -> None:
    assert parse_ruff('[{"code": "F821"}, {"code": "F821"}, {"code": "B006"}]') == {
        "F821": 2, "B006": 1,
    }  # fmt: skip
    assert parse_ruff("not json") is None
    out = "m.py:2:5: error: Incompatible types [assignment]\nother.py:1: error: x [misc]\n"
    assert dict(parse_mypy(out, ["m.py"])) == {"assignment": 1}
    assert parse_pytest("PASSED a::t1\nFAILED a::t2 - boom\nERROR b\n") == {
        "a::t1": "PASSED", "a::t2": "FAILED", "b": "ERROR",
    }  # fmt: skip


def test_verified_when_everything_passes_and_tests_ran(project: Path) -> None:
    sb = FakeSandbox()
    sb.tests = {"base": PASSING, "patched": PASSING}
    result, _ = verify(project, sb)
    assert result.status == "verified_fix" and result.sandbox == "fake"
    assert statuses(result) == {
        "apply": "passed", "syntax": "passed", "lint": "passed",
        "typecheck": "passed", "tests": "passed",
    }  # fmt: skip
    assert result.patched_files == ["m.py"]


def test_unverified_when_no_tests_can_run(project: Path) -> None:
    result, _ = verify(project, FakeSandbox())  # pytest prints nothing -> nothing passed before
    assert result.status == "fix_unverified" and statuses(result)["tests"] == "skipped"
    assert "no tests could run" in result.checks[-1].detail


def test_failed_on_a_syntax_error_without_touching_the_sandbox(project: Path) -> None:
    sb = FakeSandbox()
    result, _ = verify(project, sb, BROKEN_SYNTAX)
    assert result.status == "fix_failed" and statuses(result)["syntax"] == "failed"
    assert sb.calls == [] and statuses(result)["tests"] == "skipped"


def test_failed_when_the_patch_does_not_apply(project: Path) -> None:
    with Verifier(FakeSandbox(), project) as v:
        stale = make_diff("m.py", "def add(a, b):\n    return a * b\n", FIXED)
        result = v.verify("F-1", stale, {"m.py"})
    assert result.status == "fix_failed" and statuses(result)["apply"] == "failed"


def test_failed_on_new_lint_errors_but_not_on_preexisting_ones(project: Path) -> None:
    sb = FakeSandbox()
    sb.ruff = {"base": ["B006"], "patched": ["B006", "F821"]}  # one pre-existing, one new
    result, _ = verify(project, sb, LINT_BAD)
    assert result.status == "fix_failed" and "F821" in result.checks[2].detail
    assert "B006" not in result.checks[2].detail

    sb = FakeSandbox()
    sb.ruff = {"base": ["B006"], "patched": ["B006"]}
    result, _ = verify(project, sb)
    assert statuses(result)["lint"] == "passed"


def test_failed_on_new_type_errors(project: Path) -> None:
    sb = FakeSandbox()
    sb.mypy = {"patched": "m.py:2:5: error: Incompatible types [assignment]\n"}
    result, _ = verify(project, sb, TYPE_BAD)
    assert result.status == "fix_failed" and "assignment" in result.checks[3].detail


def test_failed_when_a_previously_passing_test_now_fails(project: Path) -> None:
    sb = FakeSandbox()
    sb.tests = {
        "base": PASSING,
        "patched": "PASSED tests/test_a.py::test_one\nFAILED tests/test_a.py::test_two\n",
    }
    result, _ = verify(project, sb)
    assert result.status == "fix_failed" and "test_two" in result.checks[-1].detail


def test_tests_that_already_failed_are_not_blamed_on_the_fix(project: Path) -> None:
    sb = FakeSandbox()
    both = "PASSED t::ok\nFAILED t::flaky\n"
    sb.tests = {"base": both, "patched": both}
    result, _ = verify(project, sb)
    assert result.status == "verified_fix"


def test_a_timeout_after_the_patch_is_a_failure(project: Path) -> None:
    sb = FakeSandbox()
    sb.tests = {"base": PASSING}
    sb.timeout_tests = {"patched"}
    result, _ = verify(project, sb)
    assert result.status == "fix_failed" and "timed out" in result.checks[-1].detail


def test_baseline_is_computed_once_and_reused(project: Path) -> None:
    sb = FakeSandbox()
    sb.tests = {"base": PASSING, "patched": PASSING}
    with Verifier(sb, project) as v:
        diff = make_diff("m.py", SRC, FIXED)
        v.verify("F-1", diff, {"m.py"})
        v.verify("F-2", diff, {"m.py"})
    assert sb.calls.count(("base", "pytest")) == 1 and sb.calls.count(("base", "ruff")) == 1
    assert sb.calls.count(("patched", "pytest")) == 2


def test_without_a_sandbox_only_safe_host_checks_run(project: Path) -> None:
    sb = FakeSandbox(unavailable="the Docker daemon is not running")
    result, _ = verify(project, sb)
    assert result.status == "fix_unverified" and result.sandbox == "unavailable"
    assert statuses(result)["lint"] == "skipped" and sb.calls == []
    assert "daemon is not running" in result.checks[-1].detail


def test_the_original_repository_is_never_modified(project: Path) -> None:
    sb = FakeSandbox()
    sb.tests = {"base": PASSING, "patched": PASSING}
    verify(project, sb)
    assert (project / "m.py").read_text() == SRC


def test_excluded_directories_are_not_copied(project: Path) -> None:
    with Verifier(FakeSandbox(), project) as v:
        assert (v.base / "m.py").exists() and not (v.base / "node_modules").exists()


def test_docker_command_is_locked_down(tmp_path: Path) -> None:
    cmd = docker_run_command(tmp_path, ["pytest", "-q"], "reviewer-abc")
    joined = " ".join(cmd)
    for flag in (
        "--network none", "--cap-drop ALL", "--security-opt no-new-privileges",
        "--read-only", "--pids-limit", "--memory", "--user 10001:10001",
    ):  # fmt: skip
        assert flag in joined, flag
    assert cmd[-2:] == ["pytest", "-q"] and "reviewer-sandbox" in joined
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert mounts == [f"{tmp_path.resolve()}:/work:rw"]  # only the throwaway copy
    assert not any(a == "-e" and "KEY" in cmd[i + 1].upper() for i, a in enumerate(cmd))
