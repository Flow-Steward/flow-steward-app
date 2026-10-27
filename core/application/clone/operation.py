"""Lifecycle-backed destructive Clone restore operation."""

from __future__ import annotations

import shlex
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field

from core.application.clone.planning import (
    CloneReplacementPlan,
    CloneTargetShell,
    PreparedCloneRestore,
)
from core.application.lifecycle.contracts import (
    CancellationToken,
    LifecycleOperationDefinition,
    LifecycleOperationKind,
    LifecycleOperationStep,
)
from core.application.lifecycle.ports import HeartbeatSink, TechnicalSink

_RESTORE_PHASES = (
    ("persist_plan", "Persist confirmed replacement plan and target shell"),
    ("stop_target", "Stop target Flow Steward services"),
    ("replace_filesystems", "Replace allowlisted filesystem and volume classes"),
    ("restore_database", "Restore source PostgreSQL dump"),
    ("restore_service_state", "Restore broker and storage state"),
    ("restore_identity", "Restore source identity and retain target shell"),
    ("regenerate_configuration", "Regenerate target runtime configuration"),
    ("activate_release", "Activate archived exact release without migrations"),
    ("start_maintenance", "Start target while retaining maintenance"),
    ("verify_restored_target", "Verify restored target parity and health"),
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CloneRestoreConfirmation(_ClosedModel):
    target_installation_id: str
    target_profile: Literal["compact", "scalable"]
    public_host: str
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_plan(cls, plan: CloneReplacementPlan) -> Self:
        return cls(
            target_installation_id=plan.target_installation_id,
            target_profile=plan.target_profile,
            public_host=plan.retained_shell.configuration.public_host,
            plan_digest=plan.plan_digest,
        )


class CloneRestoreResult(_ClosedModel):
    status: Literal["restored"] = "restored"
    operation_id: str
    target_installation_id: str
    release_version: str
    plan_digest: str
    maintenance_retained: bool = True
    log_path: str


class CloneRestoreRuntimePort(Protocol):
    def current_target_shell(self) -> CloneTargetShell: ...

    def execute_phase(self, prepared: PreparedCloneRestore, phase: str) -> None: ...

    def postcondition_holds(self, prepared: PreparedCloneRestore, phase: str) -> bool: ...

    def cleanup(self, prepared: PreparedCloneRestore) -> None: ...


def clone_restore_definition(prepared: PreparedCloneRestore) -> LifecycleOperationDefinition:
    operation_id = prepared.operation_id
    recovery_command = shlex.join(
        (
            "flow-steward",
            "clone",
            "restore",
            prepared.archive_path,
            "--operation-id",
            operation_id,
        )
    )
    return LifecycleOperationDefinition(
        operation_id=operation_id,
        kind=LifecycleOperationKind.CLONE_RESTORE,
        steps=tuple(
            LifecycleOperationStep(step_id=step_id, label=label)
            for step_id, label in _RESTORE_PHASES
        ),
        parameters={
            "plan": prepared.plan.model_dump(mode="json"),
            "release": prepared.exact_release.model_dump(mode="json"),
        },
        recovery_command=recovery_command,
        allow_recovery_resume=True,
    )


class CloneRestoreOperationGateway:
    def __init__(
        self,
        *,
        runtime: CloneRestoreRuntimePort,
        prepared: PreparedCloneRestore,
    ) -> None:
        self.runtime = runtime
        self.prepared = prepared

    def postcondition_holds(
        self,
        definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
    ) -> bool:
        del definition
        return self.runtime.postcondition_holds(self.prepared, step.step_id)

    def execute_step(
        self,
        definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
        cancellation: CancellationToken,
    ) -> None:
        del definition, heartbeat, cancellation
        output_sink(f"Executing Clone restore phase: {step.label}.")
        self.runtime.execute_phase(self.prepared, step.step_id)


__all__ = [
    "CloneRestoreConfirmation",
    "CloneRestoreOperationGateway",
    "CloneRestoreResult",
    "CloneRestoreRuntimePort",
    "clone_restore_definition",
]
