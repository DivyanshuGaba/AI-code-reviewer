"""Run against a real Docker daemon and the sandbox image; skipped when they are absent.

Start Docker, then `reviewer sandbox-build`, to enable these.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reviewer.verify.apply_patch import make_diff
from reviewer.verify.checks import Verifier
from reviewer.verify.sandbox import DockerSandbox

SANDBOX = DockerSandbox()
REASON = SANDBOX.check_available()
pytestmark = pytest.mark.skipif(REASON is not None, reason=f"sandbox unavailable: {REASON}")


def test_the_container_has_no_network_and_no_writable_root(tmp_path: Path) -> None:
    net = SANDBOX.run(
        tmp_path,
        ["python", "-c", "import urllib.request as u; u.urlopen('http://1.1.1.1', timeout=3)"],
        timeout=60,
    )
    assert net.exit_code != 0
    root = SANDBOX.run(tmp_path, ["python", "-c", "open('/etc/x', 'w')"], timeout=60)
    assert root.exit_code != 0
    work = SANDBOX.run(tmp_path, ["python", "-c", "open('/work/ok', 'w').write('1')"], timeout=60)
    assert work.exit_code == 0 and (tmp_path / "ok").exists()


def test_host_environment_does_not_leak_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-appear")
    out = SANDBOX.run(tmp_path, ["python", "-c", "import os; print(dict(os.environ))"], 60)
    assert "sk-ant-should-not-appear" not in out.stdout


def test_timeouts_kill_the_container(tmp_path: Path) -> None:
    out = SANDBOX.run(tmp_path, ["python", "-c", "import time; time.sleep(60)"], timeout=3)
    assert out.timed_out


def test_full_verification_in_a_real_container(tmp_path: Path) -> None:
    src = "def add(a, b):\n    return a + b\n"
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "m.py").write_text(src)
    (root / "tests" / "test_m.py").write_text(
        "import sys\nsys.path.insert(0, '/work')\nfrom m import add\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n"
    )
    with Verifier(SANDBOX, root) as v:
        good = v.verify(
            "ok", make_diff("m.py", src, "def add(a, b):\n    return b + a\n"), {"m.py"}
        )
        bad = v.verify(
            "bad", make_diff("m.py", src, "def add(a, b):\n    return a - b\n"), {"m.py"}
        )
    assert good.status == "verified_fix", good.checks
    assert bad.status == "fix_failed" and "test_add" in bad.checks[-1].detail
