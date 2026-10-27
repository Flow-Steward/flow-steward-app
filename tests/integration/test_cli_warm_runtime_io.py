"""Opt-in real-Docker I/O checks for the persistent fs-cli service."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
COMPOSE_FILE = ROOT / "infra/docker/docker-compose.yaml"
RUN_DOCKER_INTEGRATION = os.environ.get("FS_RUN_CLI_DOCKER_INTEGRATION") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_DOCKER_INTEGRATION,
    reason="set FS_RUN_CLI_DOCKER_INTEGRATION=1 to run real-Docker warm CLI I/O checks",
)


def _compose(project_name: str, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        project_name,
        "-f",
        str(COMPOSE_FILE),
        "--ansi",
        "never",
        "--progress",
        "quiet",
        "--profile",
        "cli",
        *args,
    ]


@pytest.fixture
def warm_cli_project() -> Iterator[tuple[str, dict[str, str]]]:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")
    daemon = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if daemon.returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    cli_image = f"flow-steward-cli:{version}"
    image = subprocess.run(
        ["docker", "image", "inspect", cli_image],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if image.returncode != 0:
        pytest.skip(f"required prebuilt CLI image is unavailable: {cli_image}")

    user = re.sub(r"[^a-z0-9_-]+", "-", os.environ.get("USER", "user").lower()).strip("-")
    project_name = f"flow-steward-cli-warm-io-{user or 'user'}-{os.getpid()}"
    socket_path = Path("/var/run/docker.sock")
    docker_gid = socket_path.stat().st_gid if socket_path.exists() else 0
    env = os.environ.copy()
    env.update(
        {
            "FS_CLI_IMAGE": cli_image,
            "FS_CLI_RUNTIME_REVISION": "1" * 64,
            "FS_DOCKER_GID": str(docker_gid),
            "FS_DOCKER_HOST_PROJECT_ROOT": str(ROOT),
            "FS_HOST_GID": str(os.getgid()),
            "FS_HOST_UID": str(os.getuid()),
            "FS_INSTALLER_STATE_DIR": str(ROOT / ".flow-steward"),
        }
    )
    start = subprocess.run(
        _compose(
            project_name,
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "fs-cli",
        ),
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if start.returncode != 0:
        pytest.fail(f"could not start disposable fs-cli project: {start.stderr}")
    try:
        yield project_name, env
    finally:
        subprocess.run(
            _compose(project_name, "down", "--remove-orphans"),
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


def test_real_docker_exec_preserves_piped_bytes_and_keeps_container_on_sigint(
    warm_cli_project: tuple[str, dict[str, str]],
) -> None:
    project_name, env = warm_cli_project
    payload = "stdin λ Ω \\x00 ansi=\\x1b[32mgreen\\x1b[0m\n".encode()
    echo = subprocess.run(
        _compose(
            project_name,
            "exec",
            "-T",
            "fs-cli",
            "python",
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ),
        cwd=ROOT,
        env=env,
        input=payload,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert echo.returncode == 0, echo.stderr.decode(errors="replace")
    assert echo.stdout == payload

    sleeper = subprocess.Popen(
        _compose(
            project_name,
            "exec",
            "-T",
            "fs-cli",
            "python",
            "-c",
            "import time; time.sleep(60)",
        ),
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(1)
        sleeper.send_signal(signal.SIGINT)
        sleeper.communicate(timeout=10)
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)

    assert sleeper.returncode in (130, -signal.SIGINT)
    running = subprocess.run(
        _compose(project_name, "ps", "--status", "running", "-q", "fs-cli"),
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert running.returncode == 0
    assert running.stdout.strip()
