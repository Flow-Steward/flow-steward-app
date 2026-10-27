"""Shared result models for public workflow-entrypoint execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

WorkflowEntrypointFailureKind = Literal[
    "invalid_request",
    "not_found",
    "conflict",
    "gone",
    "unavailable",
    "internal",
]


@dataclass(frozen=True)
class WorkflowEntrypointPreparation:
    """Application-owned resolution result for one public entrypoint launch."""

    entrypoint: dict[str, Any] = field(default_factory=dict)
    project_id: str = ""
    run_config: dict[str, Any] = field(default_factory=dict)
    workflow_id: str = ""
    failure_kind: WorkflowEntrypointFailureKind | None = None
    code: str = ""
    error: str = ""
    operator_error: str = ""
    retryable: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.entrypoint and self.project_id and not self.failure_kind)


@dataclass(frozen=True)
class WorkflowEntrypointIdempotencyResolution:
    """Neutral command result consumed by the HTTP adapter."""

    proceed: bool = False
    claimed: bool = False
    recovered: bool = False
    claim: dict[str, Any] = field(default_factory=dict)
    identity: dict[str, str] = field(default_factory=dict)
    replay_job_id: str = ""
    failure_kind: WorkflowEntrypointFailureKind | None = None
    code: str = ""
    message: str = ""


class WorkflowEntrypointLeaseLostError(RuntimeError):
    failure_kind: WorkflowEntrypointFailureKind = "conflict"


@dataclass(frozen=True)
class WorkflowEntrypointLaunchResult:
    """Durable launch identity and the normalized summary used for audit records."""

    job_id: str
    task_summary: str


__all__ = [
    "WorkflowEntrypointFailureKind",
    "WorkflowEntrypointIdempotencyResolution",
    "WorkflowEntrypointLaunchResult",
    "WorkflowEntrypointLeaseLostError",
    "WorkflowEntrypointPreparation",
]
