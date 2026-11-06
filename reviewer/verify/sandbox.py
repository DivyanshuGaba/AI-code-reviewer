"""Run commands against a copy of a repository inside a locked-down Docker container.

Repository code is untrusted: its tests, conftest files and mypy plugins can run
arbitrary code. Nothing from the repository is ever executed on the host. The container
gets no network, no capabilities, no host environment variables and no mounts except the
throwaway working copy, and it is killed on timeout.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

IMAGE = "reviewer-sandbox:1"
DOCKERFILE = Path(__file__).with_name("sandbox.Dockerfile")


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class SandboxUnavailable(RuntimeError):
    """Docker or the sandbox image is not usable."""


class Sandbox(Protocol):
    name: str

    def check_available(self) -> str | None:
        """None if usable, otherwise a human-readable reason it is not."""
        ...

    def run(self, workdir: Path, argv: list[str], timeout: int) -> ExecResult: ...


def docker_run_command(
    workdir: Path,
    argv: list[str],
    container: str,
    image: str = IMAGE,
    memory: str = "1g",
    cpus: str = "2",
) -> list[str]:
    """The exact `docker run` invocation; kept pure so the isolation flags are testable."""
    return [
        "docker", "run", "--rm", "--name", container,
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
        "--pids-limit", "256",
        "--memory", memory,
        "--cpus", cpus,
        "--user", "10001:10001",
        "-e", "HOME=/tmp",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "PYTHONUNBUFFERED=1",
        "-v", f"{workdir.resolve()}:/work:rw",
        "-w", "/work",
        image, *argv,
    ]  # fmt: skip


class DockerSandbox:
    name = "docker"

    def __init__(self, image: str = IMAGE):
        self.image = image

    def check_available(self) -> str | None:
        try:
            info = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=20, check=False,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as err:
            return f"docker is not usable: {err}"
        if info.returncode != 0:
            return "the Docker daemon is not running"
        image = subprocess.run(
            ["docker", "image", "inspect", self.image],
            capture_output=True, text=True, timeout=20, check=False,
        )  # fmt: skip
        if image.returncode != 0:
            return f"sandbox image {self.image} is missing; run `reviewer sandbox-build`"
        return None

    def run(self, workdir: Path, argv: list[str], timeout: int) -> ExecResult:
        # The container user must be able to write the throwaway copy.
        _make_world_writable(workdir)
        container = f"reviewer-{uuid.uuid4().hex[:12]}"
        cmd = docker_run_command(workdir, argv, container, self.image)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False,
                errors="replace",
            )  # fmt: skip
        except subprocess.TimeoutExpired as err:
            subprocess.run(["docker", "kill", container], capture_output=True, check=False)
            out = err.stdout.decode("utf-8", "replace") if isinstance(err.stdout, bytes) else ""
            return ExecResult(124, out, f"timed out after {timeout}s", timed_out=True)
        return ExecResult(proc.returncode, proc.stdout, proc.stderr)

    def build_image(self) -> subprocess.CompletedProcess[str]:
        """Build the sandbox image. This is the only step that needs network access."""
        return subprocess.run(
            ["docker", "build", "-t", self.image, "-f", str(DOCKERFILE), str(DOCKERFILE.parent)],
            capture_output=True, text=True, check=False,
        )  # fmt: skip


def _make_world_writable(path: Path) -> None:
    for p in [path, *path.rglob("*")]:
        if p.is_symlink():
            continue  # never chmod through a link planted by the repository
        p.chmod(p.stat().st_mode | 0o777 if p.is_dir() else p.stat().st_mode | 0o666)
