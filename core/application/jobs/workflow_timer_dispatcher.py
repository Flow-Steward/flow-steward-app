"""Wake parked runs: due Wait timers and finished child jobs.

One durable resume intent per wake.

The control process runs this every second. The repository claims due
``awaiting_timer`` jobs with the indexed ``due_epoch_ms`` lookup and, in the
same statement, stores a ``workflow_resume`` intent with ``reason: "timer"``
and status ``resume_pending``: the accepted command is durable before it is
published. Publication then reuses the workflow resume queue contract; a
failed publish stays with the existing republisher. No user, no approval:
the payload carries only ``wait_id`` and ``timer_elapsed``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Protocol

from core.application.jobs.workflow_resume_state import WorkflowResumeQueueRow

logger = logging.getLogger(__name__)

WORKFLOW_TIMER_SCAN_INTERVAL_SECONDS = 1.0
WORKFLOW_TIMER_BATCH_LIMIT = 100


class WorkflowTimerRepository(Protocol):
    def claim_due_timers(
        self, *, now_epoch_ms: int, limit: int
    ) -> list[tuple[WorkflowResumeQueueRow, str]]: ...

    def mark_published(
        self, *, thread_id: str, resume_revision: int, publish_token: str
    ) -> bool: ...

    def mark_publish_failed(
        self, *, thread_id: str, resume_revision: int, publish_token: str, error: str
    ) -> bool: ...


def publish_claimed_resumes(
    repository: WorkflowTimerRepository,
    publish: Callable[..., Any],
    claimed: list[tuple[WorkflowResumeQueueRow, str]],
) -> dict[str, int]:
    """Publish stored resume intents; a failed publish stays with the republisher."""
    published = failed = 0
    for row, thread_id in claimed:
        try:
            publish(
                project_id=row.project_id,
                job_id=row.job_id,
                task_id=row.task_id,
                thread_id=thread_id,
                resume_revision=row.resume_revision,
            )
        except Exception as exc:
            failed += 1
            logger.warning(
                "workflow resume publish failed for job_id=%s revision=%s; "
                "the resume republisher will retry: %s",
                row.job_id,
                row.resume_revision,
                exc,
            )
            with suppress(Exception):
                repository.mark_publish_failed(
                    thread_id=thread_id,
                    resume_revision=row.resume_revision,
                    publish_token=row.publish_token,
                    error=str(exc),
                )
            continue
        published += 1
        with suppress(Exception):
            repository.mark_published(
                thread_id=thread_id,
                resume_revision=row.resume_revision,
                publish_token=row.publish_token,
            )
    return {"claimed": len(claimed), "published": published, "failed": failed}


def dispatch_due_timers(
    repository: WorkflowTimerRepository,
    publish: Callable[..., Any],
    *,
    now_epoch_ms: int | None = None,
    limit: int = WORKFLOW_TIMER_BATCH_LIMIT,
) -> dict[str, int]:
    """Claim due timers, store their resume intents, then publish them."""
    now_ms = int(time.time() * 1000) if now_epoch_ms is None else int(now_epoch_ms)
    claimed = repository.claim_due_timers(now_epoch_ms=now_ms, limit=max(1, int(limit)))
    return publish_claimed_resumes(repository, publish, claimed)


def dispatch_finished_children(
    repository: Any,
    publish: Callable[..., Any],
    *,
    limit: int = WORKFLOW_TIMER_BATCH_LIMIT,
) -> dict[str, int]:
    """Wake parents whose child job finished: same intent, ``reason: "child"``."""
    claimed = repository.claim_finished_children(limit=max(1, int(limit)))
    return publish_claimed_resumes(repository, publish, claimed)


__all__ = [
    "WORKFLOW_TIMER_BATCH_LIMIT",
    "WORKFLOW_TIMER_SCAN_INTERVAL_SECONDS",
    "WorkflowTimerRepository",
    "dispatch_due_timers",
    "dispatch_finished_children",
    "publish_claimed_resumes",
]
