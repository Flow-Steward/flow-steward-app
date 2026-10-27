"""Opt-in, real-Docker acceptance gate for the reusable CLI runtime."""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

import pytest

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
LAUNCHER: Final = REPOSITORY_ROOT / "flow-steward"
STATE_ENV: Final = REPOSITORY_ROOT / ".flow-steward" / "compose.env"
PERFORMANCE_OPT_IN: Final = "FS_RUN_CLI_PERF"
SAMPLE_COUNT: Final = 5
COMMAND_TIMEOUT_SECONDS: Final = 120.0
REPAIR_TIMEOUT_SECONDS: Final = 30.0
MAX_WARM_LATENCY_SECONDS: Final = 2.5
MAX_REPAIR_LATENCY_SECONDS: Final = 4.0
MINIMUM_IMPROVEMENT_FRACTION: Final = 0.50
BASELINE_MEDIANS_SECONDS: Final = {
    "version": 4.956964,
    "help": 4.961919,
}
READ_ONLY_COMMANDS: Final = {
    "version": ("--version",),
    "help": ("--help",),
    "database_status": ("database", "status"),
}
_SAFE_COMPOSE_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


pytestmark = pytest.mark.skipif(
    os.environ.get(PERFORMANCE_OPT_IN) != "1",
    reason=f"real-Docker CLI performance gate requires {PERFORMANCE_OPT_IN}=1",
)


@dataclass(frozen=True)
class ContainerIdentity:
    """Stable evidence that no container create or start occurred."""

    container_id: str
    created_at: str
    started_at: str
    running: bool


@dataclass(frozen=True)
class TimedCommand:
    """One launcher invocation and its elapsed wall time."""

    elapsed_seconds: float
    returncode: int


def _run(
    command: tuple[str, ...],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env={**os.environ, "NO_COLOR": "1"},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("real-Docker CLI performance gate requires the Docker CLI")
    for label, command in (
        ("Docker daemon", ("docker", "info")),
        ("Docker Compose v2", ("docker", "compose", "version")),
    ):
        try:
            result = _run(command, timeout=15.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            pytest.skip(f"{label} is unavailable: {type(exc).__name__}")
        if result.returncode != 0:
            pytest.skip(f"{label} is unavailable (exit {result.returncode})")


def _configured_compose_project() -> str:
    project = os.environ.get("FS_COMPOSE_PROJECT_NAME", "flow-steward").strip()
    if STATE_ENV.is_file():
        for raw_line in STATE_ENV.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line.removeprefix("export ").strip()
            key, separator, raw_value = line.partition("=")
            if separator and key.strip() == "FS_COMPOSE_PROJECT_NAME":
                value = raw_value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                project = value
    if not _SAFE_COMPOSE_PROJECT.fullmatch(project):
        pytest.fail("configured Compose project identity is missing or unsafe")
    return project


def _time_launcher(arguments: tuple[str, ...], *, timeout: float) -> TimedCommand:
    started = time.perf_counter()
    result = _run((str(LAUNCHER), *arguments), timeout=timeout)
    return TimedCommand(
        elapsed_seconds=time.perf_counter() - started,
        returncode=result.returncode,
    )


def _assert_command_succeeded(command_name: str, result: TimedCommand) -> None:
    assert result.returncode == 0, (
        f"read-only {command_name} command exited {result.returncode}; "
        "rerun it directly to inspect its protected diagnostics"
    )


def _docker(command: tuple[str, ...], *, timeout: float = 15.0) -> str:
    result = _run(("docker", *command), timeout=timeout)
    assert result.returncode == 0, f"Docker evidence command exited {result.returncode}"
    return result.stdout.strip()


def _cli_container_id(compose_project: str) -> str:
    output = _docker(
        (
            "container",
            "ls",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={compose_project}",
            "--filter",
            "label=com.docker.compose.service=fs-cli",
            "--filter",
            "label=com.flow-steward.cli-runtime=true",
            "--format",
            "{{.ID}}",
        )
    )
    container_ids = tuple(line.strip() for line in output.splitlines() if line.strip())
    assert len(container_ids) == 1, (
        "configured Compose identity must resolve exactly one reusable fs-cli container"
    )
    return container_ids[0]


def _inspect_container(container_id: str) -> ContainerIdentity:
    output = _docker(
        (
            "container",
            "inspect",
            "--format",
            "{{.Id}}|{{.Created}}|{{.State.StartedAt}}|{{.State.Running}}",
            container_id,
        )
    )
    fields = output.split("|")
    assert len(fields) == 4, "Docker returned an invalid fs-cli identity record"
    return ContainerIdentity(
        container_id=fields[0],
        created_at=fields[1],
        started_at=fields[2],
        running=fields[3] == "true",
    )


def _warm_measurements() -> dict[str, list[float]]:
    samples: dict[str, list[float]] = {}
    for name, arguments in READ_ONLY_COMMANDS.items():
        command_samples: list[float] = []
        for _sample in range(SAMPLE_COUNT):
            result = _time_launcher(arguments, timeout=COMMAND_TIMEOUT_SECONDS)
            _assert_command_succeeded(name, result)
            command_samples.append(result.elapsed_seconds)
        samples[name] = command_samples
    return samples


def test_warm_cli_runtime_meets_real_docker_performance_contract() -> None:
    """Measure the public launcher without modifying product data."""
    _require_docker()
    assert LAUNCHER.is_file(), "root ./flow-steward launcher is missing"
    compose_project = _configured_compose_project()

    warmup = _time_launcher(READ_ONLY_COMMANDS["version"], timeout=COMMAND_TIMEOUT_SECONDS)
    _assert_command_succeeded("warmup --version", warmup)
    before = _inspect_container(_cli_container_id(compose_project))
    assert before.running is True

    samples = _warm_measurements()
    after = _inspect_container(_cli_container_id(compose_project))
    medians = {name: statistics.median(values) for name, values in samples.items()}

    assert after.container_id == before.container_id, (
        "repeated warm commands must not create or replace the fs-cli container"
    )
    assert after.created_at == before.created_at, (
        "repeated warm commands must preserve the fs-cli creation timestamp"
    )
    assert after.started_at == before.started_at, (
        "repeated warm commands must not produce a container start event"
    )
    assert after.running is True

    stopped_id = _docker(("stop", "--timeout", "10", before.container_id), timeout=20.0)
    assert stopped_id in {before.container_id, before.container_id[:12]}
    stopped = _inspect_container(before.container_id)
    assert stopped.container_id == before.container_id
    assert stopped.created_at == before.created_at
    assert stopped.running is False

    repair = _time_launcher(READ_ONLY_COMMANDS["help"], timeout=REPAIR_TIMEOUT_SECONDS)
    _assert_command_succeeded("stopped-container repair --help", repair)
    repaired = _inspect_container(_cli_container_id(compose_project))
    assert repaired.running is True

    evidence = {
        "baseline_medians_seconds": BASELINE_MEDIANS_SECONDS,
        "warm_samples_seconds": samples,
        "warm_medians_seconds": medians,
        "warm_container_before": asdict(before),
        "warm_container_after": asdict(after),
        "repaired_container": asdict(repaired),
        "repair_seconds": repair.elapsed_seconds,
        "sample_count": SAMPLE_COUNT,
    }
    print(f"CLI_WARM_PERF_EVIDENCE={json.dumps(evidence, sort_keys=True)}")

    for name in ("version", "help"):
        assert medians[name] <= MAX_WARM_LATENCY_SECONDS, (
            f"warm {name} median {medians[name]:.6f}s exceeds {MAX_WARM_LATENCY_SECONDS:.1f}s"
        )
        improvement_fraction = 1.0 - (medians[name] / BASELINE_MEDIANS_SECONDS[name])
        assert improvement_fraction >= MINIMUM_IMPROVEMENT_FRACTION, (
            f"warm {name} improved only {improvement_fraction:.1%}; at least "
            f"{MINIMUM_IMPROVEMENT_FRACTION:.0%} is required"
        )
    assert repair.elapsed_seconds < MAX_REPAIR_LATENCY_SECONDS, (
        f"stopped-container repair took {repair.elapsed_seconds:.6f}s; "
        f"must be below {MAX_REPAIR_LATENCY_SECONDS:.1f}s"
    )
