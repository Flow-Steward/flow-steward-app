from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from core.application.lifecycle.contracts import (
    ComposeTarget,
)
from core.application.lifecycle.stack_service import (
    StackImageTransition,
    StackRollbackSnapshot,
    StackRuntimeState,
)

_PATH_FLOW_STEWARD = ".flow-steward"


_MESSAGE_JSON = "{{json .}}"


_FORMAT_IMAGE_ID = "{{.Id}}"


_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")


_DOCKER_TIMEOUT_SECONDS = 30.0


_BUNDLED_STORAGE_PROBE_TIMEOUT_SECONDS = 40.0


_WORKFLOW_KNOWLEDGE_RECONCILIATION_TIMEOUT_SECONDS = 300.0


_MAX_CANDIDATE_READINESS_OUTPUT_BYTES = 1024 * 1024


class DockerCommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        action: str,
        timeout_seconds: float | None,
        output_sink: Callable[[str], None] | None = None,
        heartbeat: Callable[[str], None] | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        separate_stderr: bool = False,
    ) -> Any: ...


class ComposeCommandRunner(Protocol):
    def __call__(
        self,
        target: ComposeTarget,
        arguments: Sequence[str],
        *,
        action: str,
        timeout_seconds: float | None,
        output_sink: Callable[[str], None] | None = None,
        heartbeat: Callable[[str], None] | None = None,
        additional_compose_files: tuple[Path, ...] = (),
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        separate_stderr: bool = False,
    ) -> Any: ...


class DockerStatusReader(Protocol):
    def __call__(self, target: ComposeTarget) -> StackRuntimeState: ...


class DockerRollbackCapture(Protocol):
    def __call__(
        self,
        target: ComposeTarget,
        *,
        operation_id: str,
        selected_services: tuple[str, ...],
        managed_services: tuple[str, ...],
    ) -> StackRollbackSnapshot: ...


class DockerImageDriftReader(Protocol):
    def __call__(
        self, target: ComposeTarget, snapshot: StackRollbackSnapshot
    ) -> tuple[StackImageTransition, ...]: ...


class DockerProjectServiceMutation(Protocol):
    def __call__(
        self,
        project_name: str,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: Callable[[str], None],
        heartbeat: Callable[[str], None],
    ) -> None: ...


class DockerTargetStop(Protocol):
    def __call__(
        self,
        target: ComposeTarget,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: Callable[[str], None],
        heartbeat: Callable[[str], None],
    ) -> None: ...


@dataclass(frozen=True)
class DockerRollbackDependencies:
    project_root: Path
    run: DockerCommandRunner
    run_compose: ComposeCommandRunner
    stop_project_services: DockerProjectServiceMutation
    remove_project_services: DockerProjectServiceMutation
    capture_rollback: DockerRollbackCapture
    runtime_image_drift: DockerImageDriftReader
    stop: DockerTargetStop
