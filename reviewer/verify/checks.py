"""Verify a proposed fix: apply it to a throwaway copy, then run checks in the sandbox.

Every check compares the patched copy with an unpatched baseline, so problems that
already exist in the repository are never blamed on the fix.

Outcome labels:
  fix_failed      the patch does not apply, breaks syntax, adds lint or type errors, or makes
                  a previously passing test fail.
  verified_fix    everything above passed AND the repo's tests actually ran (at least one
                  passed before the patch) with no regressions.
  fix_unverified  nothing failed, but the tests could not be run (no tests, missing
                  dependencies in the sandbox) or the sandbox itself was unavailable.

"Verified" means the fix applies cleanly and breaks nothing the sandbox could check. It does
not prove that the reported problem is gone.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from types import TracebackType

from reviewer.index.languages import syntax_error
from reviewer.ingest.filters import EXCLUDED_DIRS, language_of
from reviewer.llm.schemas import VerificationStatus
from reviewer.signals.runners import RUFF_SELECT
from reviewer.verify.apply_patch import PatchError, apply_patch
from reviewer.verify.models import CheckResult, VerificationResult
from reviewer.verify.sandbox import ExecResult, Sandbox

_TEST_LINE_RE = re.compile(r"^(PASSED|FAILED|ERROR)\s+(\S+)", re.M)
_MYPY_RE = re.compile(
    r"^(?P<path>[^:\n]+):\d+(?::\d+)?: error: .*?(?:\[(?P<code>[\w-]+)\])?$", re.M
)

LINT_TIMEOUT = 120
TYPE_TIMEOUT = 300
TEST_TIMEOUT = 600


def _copy_repo(src: Path, dest: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {n for n in names if n in EXCLUDED_DIRS or n.endswith(".egg-info")}

    shutil.copytree(src, dest, ignore=ignore, symlinks=True)


# -- parsing tool output ----------------------------------------------------------------------


def parse_ruff(stdout: str) -> Counter[str] | None:
    try:
        return Counter(item["code"] for item in json.loads(stdout or "[]"))
    except (ValueError, KeyError, TypeError):
        return None


def parse_mypy(stdout: str, files: list[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    wanted = {f.removeprefix("./") for f in files}
    for m in _MYPY_RE.finditer(stdout):
        if m.group("path").removeprefix("./") in wanted:
            counts[m.group("code") or "error"] += 1
    return counts


def parse_pytest(stdout: str) -> dict[str, str]:
    return {m.group(2): m.group(1) for m in _TEST_LINE_RE.finditer(stdout)}


# -- the verifier -----------------------------------------------------------------------------


class Verifier:
    """Holds one baseline copy of the repo and verifies fixes against it. Use as a context."""

    def __init__(self, sandbox: Sandbox, repo_root: Path):
        self.sandbox = sandbox
        self.repo_root = repo_root
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._base: Path | None = None
        self._unavailable: str | None = None
        self._baseline: dict[tuple[str, ...], ExecResult] = {}
        self._counter = 0

    def __enter__(self) -> Verifier:
        self._tmp = tempfile.TemporaryDirectory(prefix="reviewer-verify-")
        self._base = Path(self._tmp.name) / "base"
        _copy_repo(self.repo_root, self._base)
        self._unavailable = self.sandbox.check_available()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()

    # -- helpers --

    @property
    def base(self) -> Path:
        assert self._base is not None, "use Verifier as a context manager"
        return self._base

    def _baseline_run(self, key: tuple[str, ...], argv: list[str], timeout: int) -> ExecResult:
        """Run a command on the unpatched copy once and reuse the result."""
        if key not in self._baseline:
            self._baseline[key] = self.sandbox.run(self.base, argv, timeout)
        return self._baseline[key]

    def _run(self, workdir: Path, argv: list[str], timeout: int) -> ExecResult:
        return self.sandbox.run(workdir, argv, timeout)

    # -- main entry point --

    def verify(self, finding_id: str, diff: str, allowed_files: set[str]) -> VerificationResult:
        assert self._tmp is not None, "use Verifier as a context manager"
        started = time.monotonic()
        result = VerificationResult(finding_id=finding_id, status="fix_unverified", sandbox="none")
        result.sandbox = self.sandbox.name if self._unavailable is None else "unavailable"

        def finish(status: VerificationStatus) -> VerificationResult:
            result.status = status
            result.duration_s = round(time.monotonic() - started, 2)
            return result

        def skip_rest(names: list[str], why: str) -> None:
            result.checks.extend(CheckResult(name=n, status="skipped", detail=why) for n in names)

        self._counter += 1
        patched = Path(self._tmp.name) / f"patched-{self._counter}"
        _copy_repo(self.base, patched)

        # 1. apply (pure Python, strict, path-safe)
        try:
            changed = apply_patch(patched, diff, allowed_files)
        except PatchError as err:
            result.checks.append(CheckResult(name="apply", status="failed", detail=str(err)))
            skip_rest(["syntax", "lint", "typecheck", "tests"], "patch did not apply")
            return finish("fix_failed")
        result.patched_files = changed
        result.checks.append(CheckResult(name="apply", status="passed", detail=", ".join(changed)))

        # 2. syntax (parsing only; nothing is executed)
        languages = {rel: language_of(PurePosixPath(rel)) or "python" for rel in changed}
        for rel in changed:
            text = (patched / rel).read_text(encoding="utf-8")
            problem = syntax_error(rel, text, languages[rel])
            if problem:
                result.checks.append(
                    CheckResult(name="syntax", status="failed", detail=f"{rel}: {problem}")
                )
                skip_rest(["lint", "typecheck", "tests"], "syntax error")
                return finish("fix_failed")
        result.checks.append(CheckResult(name="syntax", status="passed"))

        if self._unavailable is not None:
            skip_rest(["lint", "typecheck", "tests"], f"sandbox unavailable: {self._unavailable}")
            return finish("fix_unverified")

        foreign = sorted({lang for lang in languages.values() if lang != "python"})
        if foreign:  # the sandbox image only carries Python tooling
            skip_rest(
                ["lint", "typecheck", "tests"],
                f"no sandbox checks for {', '.join(foreign)}; only apply and syntax were checked",
            )
            return finish("fix_unverified")

        # 3-5. sandboxed checks, cheapest first; stop at the first failure
        key = tuple(changed)
        steps: list[tuple[str, Callable[[], CheckResult]]] = [
            ("lint", lambda: self._lint(patched, changed, key)),
            ("typecheck", lambda: self._typecheck(patched, changed, key)),
            ("tests", lambda: self._tests(patched)),
        ]
        for name, check in steps:
            outcome = check()
            result.checks.append(outcome)
            if outcome.status == "failed":
                remaining = ["lint", "typecheck", "tests"]
                skip_rest(remaining[remaining.index(name) + 1 :], f"{name} failed")
                return finish("fix_failed")

        tests = result.checks[-1]
        return finish("verified_fix" if tests.status == "passed" else "fix_unverified")

    # -- individual checks --

    def _lint(self, patched: Path, files: list[str], key: tuple[str, ...]) -> CheckResult:
        argv = [
            "ruff", "check", "--isolated", "--no-cache", "--output-format", "json",
            "--select", RUFF_SELECT, *files,
        ]  # fmt: skip
        base_run = self._baseline_run(("lint", *key), argv, LINT_TIMEOUT)
        new_run = self._run(patched, argv, LINT_TIMEOUT)
        before = parse_ruff(base_run.stdout) if base_run.exit_code in (0, 1) else None
        after = parse_ruff(new_run.stdout) if new_run.exit_code in (0, 1) else None
        if before is None or after is None:
            return CheckResult(name="lint", status="skipped", detail="ruff did not run in sandbox")
        added = after - before
        if added:
            summary = ", ".join(f"{code} x{n}" for code, n in sorted(added.items()))
            return CheckResult(name="lint", status="failed", detail=f"new lint errors: {summary}")
        return CheckResult(name="lint", status="passed")

    def _typecheck(self, patched: Path, files: list[str], key: tuple[str, ...]) -> CheckResult:
        argv = [
            "mypy", "--config-file=", "--ignore-missing-imports", "--no-incremental",
            "--cache-dir=/dev/null", "--show-error-codes", "--no-error-summary", "--no-pretty",
            *files,
        ]  # fmt: skip
        base_run = self._baseline_run(("types", *key), argv, TYPE_TIMEOUT)
        new_run = self._run(patched, argv, TYPE_TIMEOUT)
        if base_run.exit_code not in (0, 1) or new_run.exit_code not in (0, 1):
            return CheckResult(name="typecheck", status="skipped", detail="mypy did not run")
        added = parse_mypy(new_run.stdout, files) - parse_mypy(base_run.stdout, files)
        if added:
            summary = ", ".join(f"{code} x{n}" for code, n in sorted(added.items()))
            return CheckResult(
                name="typecheck", status="failed", detail=f"new type errors: {summary}"
            )
        return CheckResult(name="typecheck", status="passed")

    def _tests(self, patched: Path) -> CheckResult:
        argv = ["python", "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider", "-rA"]
        base_run = self._baseline_run(("tests",), argv, TEST_TIMEOUT)
        before = parse_pytest(base_run.stdout)
        passing = {t for t, s in before.items() if s == "PASSED"}
        if base_run.timed_out or not passing:
            return CheckResult(
                name="tests",
                status="skipped",
                detail="no tests could run in the sandbox before the patch "
                "(none exist, or the repo's dependencies are not installed there)",
            )
        new_run = self._run(patched, argv, TEST_TIMEOUT)
        if new_run.timed_out:
            return CheckResult(name="tests", status="failed", detail="tests timed out after patch")
        now_passing = {t for t, s in parse_pytest(new_run.stdout).items() if s == "PASSED"}
        broken = sorted(passing - now_passing)
        if broken:
            shown = ", ".join(broken[:5]) + (
                f" (+{len(broken) - 5} more)" if len(broken) > 5 else ""
            )
            return CheckResult(name="tests", status="failed", detail=f"tests now failing: {shown}")
        detail = f"{len(passing)} tests still pass"
        return CheckResult(name="tests", status="passed", detail=detail)
