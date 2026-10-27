"""Installed-stack requests, results and gateway contracts."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.application.lifecycle.contracts import (
    ComposeTarget,
    DeploymentConfig,
    LifecycleOperationKind,
)
from core.application.lifecycle.ports import (
    HeartbeatSink,
    TechnicalSink,
)
from core.application.lifecycle.preservation_service import (
    PreservationComparison,
    PreservationManifest,
)

DEFAULT_STACK_HEALTH_TIMEOUT_SECONDS = 60


_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StackStatusRequest(_ClosedModel):
    """Read the selected installed stack without mutation."""


class _StackMutationRequest(_ClosedModel):
    pass


class StackStartRequest(_StackMutationRequest):
    """Start the selected installed runtime."""

    adopt_image_pins: bool = False


class StackStopRequest(_StackMutationRequest):
    """Stop the selected installed runtime without deleting resources."""


class StackRestartRequest(_StackMutationRequest):
    """Restart the selected installed runtime."""


class StackRebuildRequest(_StackMutationRequest):
    """Build and recreate only the selected rebuildable runtime services."""


class StackLogsRequest(_ClosedModel):
    """Stream selected Compose logs through the technical output channel."""

    service: str | None = None
    tail: int = Field(default=200, ge=0)
    follow: bool = False


class StackContainerState(_ClosedModel):
    """Safe, structured state for one Compose container."""

    service: str
    container_id: str
    container_name: str
    state: str
    health: str = ""
    exit_code: int = 0
    published_ports: tuple[str, ...] = ()


class StackVolumeState(_ClosedModel):
    """Safe, structured state for one project-scoped Docker volume."""

    name: str
    driver: str = ""
    scope: str = ""
    created_at: str = ""


class StackRuntimeState(_ClosedModel):
    """Technical stack observation returned by the Docker adapter."""

    project_name: str
    services: tuple[StackContainerState, ...] = ()
    volumes: tuple[StackVolumeState, ...] = ()


class StackContainerRollbackState(_ClosedModel):
    """Exact pre-mutation container identity and running/stopped topology."""

    service: str
    topology: Literal["absent", "stopped", "running"]
    container_id: str = ""
    image_id: str = ""
    # Review P2: Scalable workers run as container replicas. Evidence recorded before
    # this field existed describes exactly one container, which is what the default says.
    replicas: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def require_exact_identity(self) -> StackContainerRollbackState:
        if self.topology == "absent":
            if self.container_id or self.image_id:
                raise ValueError("Absent rollback services cannot carry container identities")
            return self
        if not self.container_id.strip() or not _IMAGE_ID.fullmatch(self.image_id):
            raise ValueError("Present rollback services require an exact Docker image identity")
        return self


class StackRollbackSnapshot(_ClosedModel):
    """Secret-free rollback evidence persisted before any stack mutation."""

    schema_version: Literal[1] = 1
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    project_name: str
    selected_services: tuple[str, ...]
    services: tuple[StackContainerRollbackState, ...]

    @model_validator(mode="after")
    def validate_service_inventory(self) -> StackRollbackSnapshot:
        names = tuple(item.service for item in self.services)
        if len(names) != len(set(names)):
            raise ValueError("Rollback services must be unique")
        if not set(self.selected_services).issubset(names):
            raise ValueError("Rollback selected services must be present in the inventory")
        return self


class StackImageTransition(_ClosedModel):
    """One managed service whose running image left its recorded runtime identity."""

    service: str
    expected_image_id: str
    observed_image_id: str = ""
    expected_image_available: bool = False

    def describe(self) -> str:
        observed = self.observed_image_id or "no running container"
        return f"{self.service} was {self.expected_image_id}, now {observed}"


class StackResult(_ClosedModel):
    """Common safe result for stack status and lifecycle mutations."""

    action: Literal["status", "start", "stop", "restart", "rebuild"]
    status: str = "completed"
    project_name: str
    services: tuple[StackContainerState, ...] = ()
    volumes: tuple[StackVolumeState, ...] = ()
    target_services: tuple[str, ...] = ()
    operation_id: str | None = None
    journal_path: str = ""
    log_path: str = ""
    preservation_verified: bool = False
    runtime_restored: bool = False
    process_exit_code: int = 0


class StackStatusResult(StackResult):
    action: Literal["status"] = "status"
    status: str = "observed"


class StackMutationResult(StackResult):
    action: Literal["start", "stop", "restart", "rebuild"]
    operation_id: str
    journal_path: str
    log_path: str
    preservation_verified: bool = True


class StackLogsResult(_ClosedModel):
    """Rendered log lines plus bounded stream metadata."""

    service: str | None = None
    tail: int
    follow: bool
    lines_streamed: int = Field(ge=0)
    lines: tuple[str, ...] = ()
    stdout_full: str = Field(default="", exclude=True)


class StackGatewayError(RuntimeError):
    """Credential-free technical failure raised by the Docker adapter."""

    def __init__(self, *, action: str, detail: str) -> None:
        super().__init__(detail)
        self.action = action
        self.detail = detail


class StackDockerGateway(Protocol):
    def services(self, target: ComposeTarget) -> tuple[str, ...]: ...

    def status(self, target: ComposeTarget) -> StackRuntimeState: ...

    def capture_rollback(
        self,
        target: ComposeTarget,
        *,
        operation_id: str,
        selected_services: tuple[str, ...],
        managed_services: tuple[str, ...],
    ) -> StackRollbackSnapshot: ...

    def runtime_images_match(
        self,
        target: ComposeTarget,
        snapshot: StackRollbackSnapshot,
    ) -> bool: ...

    def runtime_image_drift(
        self,
        target: ComposeTarget,
        snapshot: StackRollbackSnapshot,
    ) -> tuple[StackImageTransition, ...]: ...

    def configured_image_identities(
        self,
        target: ComposeTarget,
    ) -> Mapping[str, str]: ...

    def start(
        self,
        target: ComposeTarget,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> None: ...

    def stop(
        self,
        target: ComposeTarget,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> None: ...

    def restart(
        self,
        target: ComposeTarget,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> None: ...

    def rebuild(
        self,
        target: ComposeTarget,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> None: ...

    def restore(
        self,
        target: ComposeTarget,
        *,
        action: str,
        snapshot: StackRollbackSnapshot,
        timeout_seconds: int,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
        environment: Mapping[str, str] | None = None,
    ) -> bool: ...

    def logs(
        self,
        target: ComposeTarget,
        *,
        service: str | None,
        tail: int,
        follow: bool,
        output_sink: TechnicalSink,
    ) -> int: ...

    def restore_mismatches(
        self,
        target: ComposeTarget,
        snapshot: StackRollbackSnapshot,
    ) -> tuple[str, ...]: ...

    def release_rollback_pins(
        self,
        snapshot: StackRollbackSnapshot,
    ) -> tuple[str, ...]: ...

    def manual_recovery_command(
        self,
        action: str,
    ) -> str: ...

    def storage_connectivity(self, target: ComposeTarget) -> bool: ...


class StackPreservationService(Protocol):
    def capture(
        self,
        *,
        operation_id: str,
        stage: Literal["before", "after"],
        compose_project: str,
        release: str,
        environment: str,
        storage_backend: str,
        storage_connectivity: bool | Literal["unknown"],
    ) -> PreservationManifest: ...

    def compare(
        self,
        before: PreservationManifest,
        after: PreservationManifest,
        *,
        operation: LifecycleOperationKind,
    ) -> PreservationComparison: ...


class StackManifestStore(Protocol):
    def save(self, manifest: PreservationManifest) -> object: ...

    def load(
        self,
        operation_id: str,
        stage: Literal["before", "after"],
    ) -> PreservationManifest | None: ...


class StackReadinessService(Protocol):
    def check(self) -> Any: ...


ReleaseProvider = Callable[[], str]


StorageConnectivityProvider = Callable[[DeploymentConfig, ComposeTarget], bool | Literal["unknown"]]


ReadinessReconciler = Callable[[float], None]
