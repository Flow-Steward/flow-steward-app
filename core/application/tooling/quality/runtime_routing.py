"""Route quality and testing commands to a runtime that has their tools.

Remediation epic 2026-07-29 — `AUDIT-089` and `AUDIT-083`, resolved per the
owner's decision: run quality checks and UI validation in `fs-audit` rather
than adding test tooling to the shipped CLI image.

The shipped CLI image installs `requirements-cli.lock` only. `pytest` and
`playwright` live in `requirements-audit.txt`, which the `audit` build target
installs. Verified against a freshly built `flow-steward-cli:remediation-1acd32f`:
git is present (2.47.3), `python -m pytest` reports "No module named pytest", and
importing `playwright` raises. So the tools were never missing by accident — the
commands were simply pointed at the wrong runtime.

`lint.sh` already solves this by re-invoking itself inside an on-demand
`fs-audit`. This module gives the Python-side commands the same capability.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

#: Compose service that carries the quality toolchain (pytest, coverage, semgrep).
AUDIT_SERVICE = "fs-audit"

COMPOSE_FILE = "infra/docker/docker-compose.yaml"

#: Set inside a delegated run so the child never tries to delegate again.
NESTED_ENV_FLAG = "FS_QUALITY_RUNTIME_DELEGATED"


@dataclass(frozen=True)
class RuntimeRequirement:
    """A tool the command needs and the compose service that provides it."""

    module: str
    service: str

    def satisfied(self) -> bool:
        return importlib.util.find_spec(self.module) is not None


PYTEST_IN_AUDIT = RuntimeRequirement(module="pytest", service=AUDIT_SERVICE)


def already_delegated() -> bool:
    return str(_runtime_env.get(NESTED_ENV_FLAG, "")).strip() not in {"", "0", "false"}


def can_delegate() -> bool:
    """Whether a compose delegation is possible from here."""
    if already_delegated():
        return False
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    return probe.returncode == 0


def delegation_command(service: str, argv: list[str]) -> list[str]:
    """The compose invocation that runs *argv* inside *service*."""
    command = [
        "docker",
        "compose",
    ]
    host_project_root_value = str(_runtime_env.get("FS_DOCKER_HOST_PROJECT_ROOT", "")).strip()
    host_project_root = Path(host_project_root_value) if host_project_root_value else None
    compose_files = str(_runtime_env.get("FS_COMPOSE_FILES", COMPOSE_FILE)).split()
    for compose_file in compose_files:
        compose_path = Path(compose_file)
        if host_project_root is not None and not compose_path.is_absolute():
            compose_path = host_project_root / compose_path
        command.extend(["-f", str(compose_path)])
    project_name = str(_runtime_env.get("FS_COMPOSE_PROJECT_NAME", "")).strip()
    if project_name:
        command.extend(["-p", project_name])
    env_file = str(_runtime_env.get("FS_ENV_FILE", ".env")).strip()
    if env_file:
        env_path = Path(env_file)
        if host_project_root is not None and not env_path.is_absolute():
            env_path = host_project_root / env_path
        command.extend(["--env-file", str(env_path)])
    command.extend(
        [
            "run",
            "--no-deps",
            "--rm",
            "-T",
            "--pull",
            "never",
            "-e",
            f"{NESTED_ENV_FLAG}=1",
            service,
            *argv,
        ]
    )
    return command


def missing_tool_message(requirement: RuntimeRequirement) -> str:
    """Actionable diagnostic for the case where delegation is not available."""
    return (
        f"'{requirement.module}' is not available in this runtime, and it is not "
        f"shipped in the CLI image by design. This command runs in the "
        f"'{requirement.service}' service, which installs requirements-audit.txt. "
        f"Run it with:\n"
        f"  docker compose -f {COMPOSE_FILE} run --rm -T {requirement.service} <command>\n"
        f"or start Docker so the command can delegate there automatically."
    )


def run_in_required_runtime(
    requirement: RuntimeRequirement,
    argv: list[str],
) -> tuple[int, str]:
    """Run *argv* here if the tool is present, otherwise inside its service.

    Returns ``(exit_code, diagnostic)``. The diagnostic is empty on success and
    on a normal non-zero exit from the command itself; it is populated only when
    the command could not be run at all, so a caller can distinguish "the checks
    failed" from "the checks never ran" — which is the distinction `AUDIT-089`
    lost when the gate exited immediately.
    """
    if requirement.satisfied():
        return int(subprocess.run(argv, check=False).returncode), ""
    if not can_delegate():
        return 1, missing_tool_message(requirement)
    completed = subprocess.run(delegation_command(requirement.service, argv), check=False)
    return int(completed.returncode), ""


__all__ = [
    "AUDIT_SERVICE",
    "NESTED_ENV_FLAG",
    "PYTEST_IN_AUDIT",
    "RuntimeRequirement",
    "already_delegated",
    "can_delegate",
    "delegation_command",
    "missing_tool_message",
    "run_in_required_runtime",
]
