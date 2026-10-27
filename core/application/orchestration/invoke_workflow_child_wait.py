"""Durable wait of a serial ``invoke_workflow`` step for its child job.

The parent never blocks a worker on its child. The hub records a ``child``
pending wait at dispatch; once the child job exists (one per invocation, see
``invoke_child_link_repository``) the node pauses with that wait plus the
child's identity. The control process wakes the parent when the child job is
finished, with ``{reason: "child", wait_id, child_job_id, child_status}``; the
node accepts only the wake of its own wait and child, and re-reads the child
row itself, so a stale or foreign completion never advances the parent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.jobs.workflow_resume_state import RESUME_REASON_CHILD
from core.application.orchestration.active_step import ActiveStep
from core.application.orchestration.wait_step_runtime import WAIT_KIND_CHILD, pending_wait_for

#: Child job states that end the parent's wait. A stuck child is a failure of
#: the invocation, never a success.
CHILD_FINISHED_STATUSES = frozenset({"completed", "failed", "cancelled", "max_iterations", "stuck"})


class ChildWaitNotRecorded(RuntimeError):
    """The hub recorded no child wait for this execution."""


def child_wake_is_valid(pending: Mapping[str, Any], value: Any, *, child_job_id: str) -> bool:
    """A wake resumes the parent only for its own wait and its own child."""
    if not isinstance(value, Mapping):
        return False
    if str(value.get("reason") or "") != RESUME_REASON_CHILD:
        return False
    if str(value.get("wait_id") or "") != str(pending.get("wait_id") or ""):
        return False
    return str(value.get("child_job_id") or "") == str(child_job_id)


def child_wait_payload(
    pending: Mapping[str, Any],
    *,
    child_job_id: str,
    invocation_key: str,
    child_revision: int | None,
) -> dict[str, Any]:
    """The interrupt payload: the recorded wait plus the child it waits for."""
    return {
        **dict(pending),
        "gate_kind": "wait",
        "kind": WAIT_KIND_CHILD,
        "child_job_id": str(child_job_id),
        "invocation_key": invocation_key,
        "child_revision": child_revision,
    }


def await_child(
    state: Mapping[str, Any],
    current: ActiveStep,
    *,
    bl: Any,
    child_job_id: str,
    invocation_key: str,
    child_revision: int | None,
) -> tuple[dict[str, Any] | None, str]:
    """Pause until the child job is finished; return its row and status."""
    from langgraph.types import interrupt

    pending = pending_wait_for(state, current)
    if pending is None or pending.get("kind") != WAIT_KIND_CHILD:
        raise ChildWaitNotRecorded(
            f"step '{current.step_id}' has no recorded child wait for its execution"
        )
    payload = child_wait_payload(
        pending,
        child_job_id=child_job_id,
        invocation_key=invocation_key,
        child_revision=child_revision,
    )
    while True:
        # Every wake value stays in the checkpoint: a re-executed node replays
        # them in order and pauses again at the same place.
        if not child_wake_is_valid(pending, interrupt(payload), child_job_id=child_job_id):
            continue
        row = bl.get_job(child_job_id)
        if row is None:
            return None, "missing"
        status = str((row or {}).get("status") or "").strip().lower()
        if status == "stuck" and not row.get("finished_at"):
            # Watchdog marks are recoverable; only a graph-finished stuck
            # child ends the invocation (the same distinction as recovery).
            continue
        if status in CHILD_FINISHED_STATUSES:
            return (dict(row) if isinstance(row, Mapping) else None), status


def child_revision_of(child_row: Mapping[str, Any] | None) -> int | None:
    """The definition revision the child job was pinned to at creation."""
    import json

    meta = (child_row or {}).get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    ref = meta.get("workflow_definition_ref") if isinstance(meta, Mapping) else None
    revision = ref.get("revision") if isinstance(ref, Mapping) else None
    try:
        return int(revision) if revision is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "CHILD_FINISHED_STATUSES",
    "ChildWaitNotRecorded",
    "await_child",
    "child_revision_of",
    "child_wait_payload",
    "child_wake_is_valid",
]
