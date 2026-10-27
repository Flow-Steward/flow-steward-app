"""Application contracts and value helpers for durable workflow resumes."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

WORKFLOW_RESUME_FINALIZATION_STEPS = (
    "lock_release",
    "notification",
    "provider_sync",
)
WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS = 60
WORKFLOW_RESUME_MAX_ATTEMPTS = 3
WORKFLOW_RESUME_RETRY_DELAY_SECONDS = 30

# Why a paused run continues. One durable intent shape carries all three; only
# an approval carries a human decision, timer and child wakes come from the
# internal dispatcher.
RESUME_REASON_APPROVAL = "approval"
RESUME_REASON_TIMER = "timer"
RESUME_REASON_CHILD = "child"
RESUME_REASONS = frozenset({RESUME_REASON_APPROVAL, RESUME_REASON_TIMER, RESUME_REASON_CHILD})


@dataclass(frozen=True)
class WorkflowResumeQueueRow:
    """Queue metadata returned after a durable approval intent is recorded."""

    job_id: str
    project_id: str
    task_id: str
    resume_revision: int
    publish_token: str = ""


@dataclass(frozen=True)
class WorkflowResumeClaim:
    """Fenced worker lease acquired for one workflow resume revision."""

    claimed: bool
    lease_token: str = ""
    attempts: int = 1
    exhausted: bool = False


def coerce_resume_meta(value: Any) -> dict[str, Any]:
    """Coerce jobs.meta into a mutable dictionary."""

    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        with contextlib.suppress(Exception):
            loaded = json.loads(value)
            if isinstance(loaded, dict):
                return loaded
    return {}


def workflow_resume_intent(job_row: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the nested workflow resume intent from a jobs row."""

    meta = coerce_resume_meta(job_row.get("meta"))
    intent = meta.get("workflow_resume")
    return dict(intent) if isinstance(intent, Mapping) else {}


def workflow_resume_revision(meta: Mapping[str, Any]) -> int:
    """Return the current approval-resume revision from jobs.meta."""

    workflow_resume = meta.get("workflow_resume")
    if not isinstance(workflow_resume, Mapping):
        return 0
    try:
        return max(0, int(workflow_resume.get("revision") or 0))
    except (TypeError, ValueError):
        return 0


def workflow_resume_row_from_db(
    updated: Any,
    fallback: Mapping[str, Any],
    revision: int,
    *,
    publish_token: str = "",
) -> WorkflowResumeQueueRow:
    """Normalize UPDATE .. RETURNING row shapes from DB drivers/tests."""

    if isinstance(updated, Mapping):
        return WorkflowResumeQueueRow(
            job_id=str(updated.get("job_id") or updated.get("id") or fallback.get("job_id") or ""),
            project_id=str(updated.get("project_id") or fallback.get("project_id") or ""),
            task_id=str(updated.get("task_id") or fallback.get("task_id") or ""),
            resume_revision=int(updated.get("resume_revision") or revision),
            publish_token=str(updated.get("publish_token") or publish_token),
        )
    if isinstance(updated, Sequence) and not isinstance(updated, (str, bytes, bytearray)):
        values = list(updated)
        return WorkflowResumeQueueRow(
            job_id=str(
                values[0] if len(values) > 0 and values[0] else fallback.get("job_id") or ""
            ),
            project_id=str(
                values[1] if len(values) > 1 and values[1] else fallback.get("project_id") or ""
            ),
            task_id=str(
                values[2] if len(values) > 2 and values[2] else fallback.get("task_id") or ""
            ),
            resume_revision=int(values[3] if len(values) > 3 and values[3] else revision),
            publish_token=str(values[4] if len(values) > 4 and values[4] else publish_token),
        )
    return WorkflowResumeQueueRow(
        job_id=str(fallback.get("job_id") or ""),
        project_id=str(fallback.get("project_id") or ""),
        task_id=str(fallback.get("task_id") or ""),
        resume_revision=int(revision),
        publish_token=publish_token,
    )


class WorkflowResumeCommandRepository(Protocol):
    """Persistence port used while accepting and publishing approval intents."""

    def mark_pending(
        self,
        *,
        thread_id: str,
        job_context: Mapping[str, Any],
        approval_id: str,
        resume_value: Mapping[str, Any],
        approved: bool,
        action: str | None,
        comment: str,
        interrupt_payload: Mapping[str, Any],
        actor_username: str,
    ) -> WorkflowResumeQueueRow: ...

    def mark_publish_failed(
        self,
        *,
        thread_id: str,
        resume_revision: int,
        publish_token: str,
        error: str,
    ) -> bool: ...

    def mark_published(
        self,
        *,
        thread_id: str,
        resume_revision: int,
        publish_token: str,
    ) -> bool: ...


class WorkflowResumeExecutionRepository(Protocol):
    """Persistence port for fenced approval-resume worker execution."""

    @property
    def available(self) -> bool: ...

    def get_job(self, job_id: str) -> Mapping[str, Any] | None: ...

    def claim_execution(
        self,
        *,
        job_id: str,
        resume_revision: int,
        worker_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> WorkflowResumeClaim: ...

    def renew_execution(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        lease_seconds: int,
    ) -> bool: ...

    def complete_failed(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        worker_id: str,
        error: str,
    ) -> bool: ...

    def mark_retryable(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        error: str,
    ) -> bool: ...

    def mark_finalizing(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        final_status: str,
        result: Mapping[str, Any],
        error: str | None = None,
    ) -> bool: ...

    def complete(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str = "",
        finalizer_token: str = "",
    ) -> bool: ...

    def claim_finalizer(
        self,
        *,
        job_id: str,
        resume_revision: int,
        finalizer_token: str,
        lease_seconds: int,
    ) -> bool: ...

    def mark_finalization_step_completed(
        self,
        *,
        job_id: str,
        resume_revision: int,
        finalizer_token: str,
        step: str,
    ) -> bool: ...

    def renew_finalizer(
        self,
        *,
        job_id: str,
        resume_revision: int,
        finalizer_token: str,
        lease_seconds: int,
    ) -> bool: ...

    def release_finalizer(
        self,
        *,
        job_id: str,
        resume_revision: int,
        finalizer_token: str,
        error: str,
    ) -> bool: ...


class WorkflowResumeOutboxRepository(Protocol):
    """Persistence port for republishing pending approval-resume intents."""

    @property
    def available(self) -> bool: ...

    def pending_rows(self, *, batch_size: int) -> list[dict[str, Any]]: ...

    def mark_publish_state(
        self,
        *,
        job_id: str,
        resume_revision: int,
        publish_token: str,
        expected_state: str,
        state: str,
        error: str = "",
    ) -> bool: ...


class WorkflowResumeWorkerRepository(
    WorkflowResumeExecutionRepository,
    WorkflowResumeOutboxRepository,
    Protocol,
):
    """Combined worker dependency used for execution and outbox recovery."""


WorkflowResumeRepositoryFactory = Callable[[Any], WorkflowResumeCommandRepository]
