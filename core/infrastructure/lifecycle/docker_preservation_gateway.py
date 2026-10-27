from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.application.lifecycle.preservation_service import (
    UNKNOWN,
)
from core.infrastructure.lifecycle import docker_runtime_support

from .docker_gateway_contracts import _DOCKER_TIMEOUT_SECONDS, _MESSAGE_JSON


@dataclass(frozen=True)
class DockerPreservationSnapshot:
    fields: Mapping[str, Any]


ProcessRunner = Callable[..., Any]


class DockerPreservationGateway:
    """Capture stable Docker volume identities using argument-only subprocesses."""

    def __init__(
        self,
        *,
        project_root: Path,
        runner: ProcessRunner | None = None,
        timeout_seconds: float = _DOCKER_TIMEOUT_SECONDS,
    ) -> None:
        self._project_root = Path(project_root).resolve()
        self._runner = runner or docker_runtime_support.run_process
        self._timeout_seconds = max(1.0, float(timeout_seconds))

    def capture(self, project_name: str) -> DockerPreservationSnapshot:
        normalized = str(project_name).strip()
        if not docker_runtime_support.SAFE_PROJECT_NAME.fullmatch(normalized):
            raise ValueError("Compose project name contains unsafe characters")
        fields: dict[str, Any] = {
            "compose_project": normalized,
            "durable_volumes": UNKNOWN,
        }
        try:
            listing = self._run(
                (
                    "docker",
                    "volume",
                    "ls",
                    "--filter",
                    f"label=com.docker.compose.project={normalized}",
                    "--format",
                    "{{.Name}}",
                )
            )
            if int(listing.returncode) != 0:
                return DockerPreservationSnapshot(fields)
            names = tuple(
                sorted(line.strip() for line in str(listing.stdout).splitlines() if line.strip())
            )
            if not names:
                fields["durable_volumes"] = ()
                return DockerPreservationSnapshot(fields)
            inspected = self._run(
                (
                    "docker",
                    "volume",
                    "inspect",
                    "--format",
                    _MESSAGE_JSON,
                    *names,
                )
            )
            if int(inspected.returncode) != 0:
                return DockerPreservationSnapshot(fields)
            identities = tuple(
                sorted(
                    (
                        docker_runtime_support.volume_identity(item)
                        for item in docker_runtime_support.decode_inspections(inspected.stdout)
                    ),
                    key=lambda item: item["name"],
                )
            )
            if tuple(item["name"] for item in identities) != names:
                return DockerPreservationSnapshot(fields)
            fields["durable_volumes"] = identities
        except (OSError, ValueError, subprocess.SubprocessError):
            return DockerPreservationSnapshot(fields)
        return DockerPreservationSnapshot(fields)

    def _run(self, command: Sequence[str]) -> Any:
        return self._runner(
            list(command),
            cwd=self._project_root,
            timeout=self._timeout_seconds,
        )
