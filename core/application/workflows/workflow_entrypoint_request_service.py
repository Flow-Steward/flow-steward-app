"""Transport-neutral submission use case for a prepared workflow entrypoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.application.workflows.workflow_entrypoint_execution_service import (
    WorkflowEntrypointExecutionCoordinator,
    WorkflowEntrypointPreparation,
    WorkflowEntrypointSubmissionOutcome,
)


class StagedWorkflowAttachment(Protocol):
    def cleanup(self) -> None: ...


@dataclass(frozen=True)
class WorkflowEntrypointRequestCommand:
    """Normalized application input after the transport has been authenticated and parsed."""

    entry_key: str
    task_id: str
    project_id: str
    run_config: dict[str, object]
    workflow_id: str
    request_id: str
    idempotency_mode: str
    idempotency_key: str
    replay_key: str
    mapping: dict[str, object]
    sources: dict[str, object]
    fallback_body: object
    staged_files: tuple[StagedWorkflowAttachment, ...]
    request_context: dict[str, object]

    def submission(self) -> dict[str, object]:
        return {
            "entry_key": self.entry_key,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "run_config": self.run_config,
            "workflow_id": self.workflow_id,
            "request_id": self.request_id,
            "idempotency_mode": self.idempotency_mode,
            "idempotency_key": self.idempotency_key,
            "replay_key": self.replay_key,
            "mapping": self.mapping,
            "sources": self.sources,
            "fallback_body": self.fallback_body,
            "staged_files": list(self.staged_files),
            "request_context": self.request_context,
        }


@dataclass(frozen=True)
class WorkflowEntrypointRequestResult:
    """Semantic submission result; transports map it to their own protocol."""

    outcome: WorkflowEntrypointSubmissionOutcome
    duplicate: bool


class WorkflowEntrypointRequestService:
    """Own durable preparation, submission, and staged-file cleanup."""

    def __init__(self, coordinator: WorkflowEntrypointExecutionCoordinator) -> None:
        self._coordinator = coordinator

    def prepare(self, entry_key: str) -> WorkflowEntrypointPreparation:
        return self._coordinator.prepare(str(entry_key or "").strip())

    def check_workflow_admission(
        self, workflow_id: str
    ) -> WorkflowEntrypointSubmissionOutcome | None:
        return self._coordinator.check_workflow_admission(workflow_id)

    def execute(self, command: WorkflowEntrypointRequestCommand) -> WorkflowEntrypointRequestResult:
        try:
            outcome = self._coordinator.submit(**command.submission())
        finally:
            for staged in command.staged_files:
                staged.cleanup()
        return WorkflowEntrypointRequestResult(
            outcome=outcome,
            duplicate=bool(outcome.deduplicated and outcome.replay_job_id),
        )


__all__ = [
    "StagedWorkflowAttachment",
    "WorkflowEntrypointRequestCommand",
    "WorkflowEntrypointRequestResult",
    "WorkflowEntrypointRequestService",
]
