"""Rules and audit records of the operator actions on one job (restart, cancel, acknowledge)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from core.application.jobs.workflow_wait_contract import DURABLE_WAIT_STATUSES
from core.application.management.errors import ServiceConflictError

logger = logging.getLogger(__name__)

#: A run in one of these statuses is still running, queued, or paused on a
#: durable wait (approval, timer, child workflow): restarting it would start a
#: second run beside the live one, so restart refuses it.
RESTART_REFUSED_STATUSES = frozenset(
    {"pending", "queued", "resume_pending", "in_progress", *DURABLE_WAIT_STATUSES}
)


def require_restartable(job_id: Any, job: dict[str, Any]) -> None:
    """Refuse restarting a live run with a conflict naming its status."""
    status = str(job.get("status") or "").strip().lower()
    if status not in RESTART_REFUSED_STATUSES:
        return
    message = (
        f"Cannot restart job #{job_id} while it is {status}; "
        "cancel it first or wait for it to finish"
    )
    raise ServiceConflictError(
        message,
        payload={
            "error": message,
            "code": "job_restart_active",
            "job_id": str(job_id),
            "status": status,
        },
    )


def journal_acknowledge(
    append_runtime_journal: Callable[..., Any] | None,
    bl: Any,
    job_id: Any,
    actor: str,
    reported_actor: str = "",
) -> None:
    """Journal who acknowledged: the authenticated actor, and the one the caller named.

    The named actor is not verified, so it is kept beside the authenticated one.
    """
    detail = {"acknowledged_by": actor}
    if str(reported_actor or "").strip():
        detail["reported_actor"] = str(reported_actor).strip()
    journal_operator_action(
        append_runtime_journal,
        bl,
        str(job_id),
        event="manual_acknowledge",
        reason="Acknowledged by operator",
        detail=detail,
    )


def journal_cancel(
    append_runtime_journal: Callable[..., Any] | None,
    bl: Any,
    job_id: Any,
    actor: str,
    comment: str,
    previous_status: str,
) -> None:
    """Journal who cancelled a run, why, and what it was doing."""
    journal_operator_action(
        append_runtime_journal,
        bl,
        str(job_id),
        event="manual_cancel",
        reason=comment,
        detail={
            "cancelled_by": str(actor or "operator").strip() or "operator",
            "comment": comment,
            "previous_status": previous_status,
        },
    )


def journal_operator_action(
    append_runtime_journal: Callable[..., Any] | None,
    bl: Any,
    job_id: str,
    *,
    event: str,
    reason: str,
    detail: dict[str, Any],
) -> None:
    """Record who took an operator action, and why, in the job's runtime journal."""
    if append_runtime_journal is None:
        return
    try:
        append_runtime_journal(bl, job_id, event=event, reason=reason, detail=detail)
    except Exception as exc:
        # The action already happened; a journal failure must not undo or hide it.
        logger.warning("%s journal entry for job %s failed: %s", event, job_id, exc)


__all__ = [
    "RESTART_REFUSED_STATUSES",
    "journal_acknowledge",
    "journal_cancel",
    "journal_operator_action",
    "require_restartable",
]
