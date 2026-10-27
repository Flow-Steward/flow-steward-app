"""Infrastructure-owned durable workflow resume command ports."""

from __future__ import annotations

from typing import Any


def workflow_resume_command_ports() -> dict[str, Any]:
    from core.infrastructure.db.worker import WorkflowResumeRepositoryAdapter
    from core.infrastructure.runtime.worker.queue_publisher import enqueue_workflow_resume

    return {
        "enqueue_workflow_resume": enqueue_workflow_resume,
        "workflow_resume_repository_factory": WorkflowResumeRepositoryAdapter,
    }


__all__ = ["workflow_resume_command_ports"]
