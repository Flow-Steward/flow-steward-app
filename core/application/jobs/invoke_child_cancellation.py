"""Apply durable cancel requests to the children of cancelled parents.

A parent's cancel records ``cancel_requested_at`` on each unfinished child
link in the same transaction (``request_child_cancels``). The control process
applies them here: a running child is cancelled through the regular terminal
persistence path, which records the same requests for its own children, so
the cancel reaches grandchildren too. A child that already finished is only
marked applied: completed work and its effects are never rolled back. Every
step is idempotent; a request survives restarts until it is applied, also on
a parent that is long terminal.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core.application.jobs.invoke_child_link_repository import (
    mark_child,
    pending_child_cancels,
    settle_cancel_pending,
)

logger = logging.getLogger(__name__)

CHILD_CANCEL_BATCH_LIMIT = 100
CHILD_CANCEL_ERROR_CODE = "parent_cancelled"
CHILD_CANCEL_MESSAGE = "Cancelled because its parent workflow was cancelled"


def apply_child_cancels(
    bl: Any,
    *,
    terminal_persistence_factory: Any = None,
    limit: int = CHILD_CANCEL_BATCH_LIMIT,
) -> dict[str, int]:
    """Cancel the children whose parent's cancel is recorded but not applied."""
    from core.application.jobs.terminal_persistence import (
        TerminalPersistenceConflict,
        TerminalPersistenceService,
    )

    conn = getattr(bl, "conn", None)
    if conn is None:
        return {"parents": 0, "cancelled": 0, "already_finished": 0, "failed": 0}
    factory = terminal_persistence_factory or TerminalPersistenceService
    pending = pending_child_cancels(conn, limit=limit)
    conn.commit()
    cancelled = already_finished = failed = 0
    for parent_job_id, child_ids in pending:
        for child_job_id in child_ids:
            try:
                factory(bl).complete_failure(
                    job_id=child_job_id,
                    status="cancelled",
                    error_message=CHILD_CANCEL_MESSAGE,
                    error_code=CHILD_CANCEL_ERROR_CODE,
                    category="cancellation",
                )
                cancelled += 1
            except TerminalPersistenceConflict:
                # Already finished (or cancelled by someone else): nothing to undo.
                already_finished += 1
            except LookupError:
                # The child row is gone; the request is settled, not retried forever.
                already_finished += 1
            except Exception as exc:
                failed += 1
                logger.warning(
                    "child cancel failed parent=%s child=%s; retried next pass: %s",
                    parent_job_id,
                    child_job_id,
                    exc,
                )
                continue
            mark_child(
                conn,
                parent_job_id=parent_job_id,
                child_job_id=child_job_id,
                fields={"cancel_applied_at": datetime.now(UTC).isoformat()},
            )
            conn.commit()
        settle_cancel_pending(conn, parent_job_id=parent_job_id)
        conn.commit()
    return {
        "parents": len(pending),
        "cancelled": cancelled,
        "already_finished": already_finished,
        "failed": failed,
    }


__all__ = ["CHILD_CANCEL_BATCH_LIMIT", "CHILD_CANCEL_ERROR_CODE", "apply_child_cancels"]
