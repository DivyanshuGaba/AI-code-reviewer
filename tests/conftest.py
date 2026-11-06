from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture(autouse=True)
def reviewer_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep clones and indexes out of the real cache."""
    home = tmp_path / "home"
    monkeypatch.setenv("REVIEWER_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def light_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only Ruff by default; tests that exercise other tools ask for them explicitly."""
    monkeypatch.setattr("reviewer.pipeline.default_signal_tools", lambda: ("ruff",))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A writable copy of the fixture repo (a plain directory, not a git repo)."""
    dest = tmp_path / "sample_repo"
    shutil.copytree(FIXTURE, dest)
    return dest


@pytest.fixture
def git_repo(repo: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("ignored.py\n")
    (repo / "ignored.py").write_text("def nope(): pass\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    return repo
