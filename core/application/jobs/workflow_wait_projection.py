"""Project a run's durable pause onto its job row.

The checkpoint owns the pause: the graph's pending interrupt carries the
``pending_wait`` the hub recorded (kind, ``wait_id``, execution identity and,
for a timer, ``due_epoch_ms``). The job row only mirrors it so the control
process can find due timers with an index and the UI can show what the run
waits for: status ``awaiting_timer`` / ``awaiting_approval`` /
``awaiting_child`` and ``jobs.meta.workflow_wait``. The projection can always
be rebuilt from the checkpoint; it is not a second state machine.

A gate without a recorded wait (a recovery or autonomy approval) is an
approval pause without ``workflow_wait``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.jobs.workflow_wait_contract import (
    DURABLE_WAIT_STATUSES,
    STATUS_AWAITING_APPROVAL,
    STATUS_AWAITING_CHILD,
    STATUS_AWAITING_TIMER,
    WAIT_KIND_APPROVAL,
    WAIT_KIND_CHILD,
    WAIT_KIND_TIMER,
    WAIT_STATUS_BY_KIND,
)
from core.application.orchestration.active_step import ActiveStep

PENDING_WAIT_KEY = "pending_wait"

#: Checkpoint marker of the timer wake already applied, written in the same
#: update as the wake so a redelivery never reaches a later pause.
WAIT_RESUME_APPLIED_KEY = "wait_resume_applied_id"

_WAIT_FIELDS = (
    "wait_id",
    "kind",
    "step_id",
    "step_execution_id",
    "attempt",
    "group_id",
    "for_each_id",
    "workflow_revision",
    "started_at",
    "duration_ms",
    "due_at",
    "due_epoch_ms",
    "approval_id",
    "child_job_id",
    "invocation_key",
    "child_revision",
)


def pending_wait_for(state: Mapping[str, Any], current: ActiveStep) -> dict[str, Any] | None:
    """The pending wait recorded for exactly this execution and attempt."""
    pending = state.get(PENDING_WAIT_KEY)
    if not isinstance(pending, Mapping):
        return None
    if (pending.get("step_execution_id"), pending.get("attempt")) != (
        current.step_execution_id,
        current.attempt,
    ):
        return None
    return dict(pending)


def pending_interrupt_payloads(state: Any) -> list[dict[str, Any]]:
    """The values of the pending interrupts in a LangGraph state snapshot."""
    payloads: list[dict[str, Any]] = []
    for task in getattr(state, "tasks", None) or ():
        for interrupt in getattr(task, "interrupts", None) or ():
            value = getattr(interrupt, "value", None)
            if isinstance(value, Mapping):
                payloads.append(dict(value))
    return payloads


def _state_values(state: Any) -> Mapping[str, Any]:
    values = getattr(state, "values", None)
    return values if isinstance(values, Mapping) else {}


def durable_wait_from_state(state: Any) -> dict[str, Any] | None:
    """The recorded wait the run is paused on, if its interrupt names it."""
    payloads = pending_interrupt_payloads(state)
    recorded = _state_values(state).get("pending_wait")
    recorded = recorded if isinstance(recorded, Mapping) else {}
    for payload in payloads:
        if str(payload.get("gate_kind") or "") == "wait" and payload.get("wait_id"):
            # A Wait interrupts with its whole recorded wait.
            return {key: payload[key] for key in _WAIT_FIELDS if key in payload}
        approval_id = str(payload.get("approval_id") or "")
        if (
            approval_id
            and recorded.get("kind") == WAIT_KIND_APPROVAL
            and str(recorded.get("approval_id") or "") == approval_id
        ):
            return {key: recorded[key] for key in _WAIT_FIELDS if key in recorded}
    values = _state_values(state)
    if (
        not payloads
        and values.get("task_source") in {"workflow_test_run", "step_test_run"}
        and values.get("status") == STATUS_AWAITING_APPROVAL
        and values.get("needs_user_approval")
        and not values.get("recovery_pending")
        and recorded.get("kind") == WAIT_KIND_APPROVAL
    ):
        from core.application.orchestration.active_step import active_step
        from core.application.orchestration.approval_payload import approval_id_for_execution

        current = active_step(values)
        if current is not None and current.step_kind == "approval":
            pending = pending_wait_for(values, current)
            if pending and pending.get("approval_id") == approval_id_for_execution(
                current.step_execution_id
            ):
                return {key: pending[key] for key in _WAIT_FIELDS if key in pending}
    return None


def wait_status(wait: Mapping[str, Any] | None) -> str:
    """The job status for a pause; an unrecorded gate is an approval pause."""
    if not wait:
        return STATUS_AWAITING_APPROVAL
    return WAIT_STATUS_BY_KIND.get(str(wait.get("kind") or ""), STATUS_AWAITING_APPROVAL)


def workflow_wait_meta(wait: Mapping[str, Any], *, thread_id: str) -> dict[str, Any]:
    """``jobs.meta.workflow_wait``: the wait identity and a checkpoint reference.

    ``due_epoch_ms`` is written as an integer so the due-timer index and query
    can cast it without failing.
    """
    projected = {key: wait[key] for key in _WAIT_FIELDS if key in wait}
    if "due_epoch_ms" in projected:
        projected["due_epoch_ms"] = int(projected["due_epoch_ms"])
    projected["checkpoint"] = {"thread_id": str(thread_id or "")}
    return projected


def pause_projection(state: Any) -> tuple[str, dict[str, Any] | None] | None:
    """The status and recorded wait of a checkpoint paused on an interrupt.

    Used to rebuild the job projection when a worker died after the pause was
    checkpointed but before the job row said so: such a run is waiting, not
    stuck. ``None`` when the checkpoint has no pending interrupt.
    """
    if not pending_interrupt_payloads(state):
        return None
    wait = durable_wait_from_state(state)
    return wait_status(wait), wait


def interrupt_matches_wait(state: Any, wait_id: str) -> bool:
    """Whether the checkpoint is paused on exactly this timer wait."""
    target = str(wait_id or "").strip()
    if not target:
        return False
    return any(
        str(payload.get("gate_kind") or "") == "wait"
        and str(payload.get("wait_id") or "") == target
        for payload in pending_interrupt_payloads(state)
    )


__all__ = [
    "DURABLE_WAIT_STATUSES",
    "STATUS_AWAITING_APPROVAL",
    "STATUS_AWAITING_CHILD",
    "STATUS_AWAITING_TIMER",
    "WAIT_KIND_APPROVAL",
    "WAIT_KIND_CHILD",
    "WAIT_KIND_TIMER",
    "WAIT_RESUME_APPLIED_KEY",
    "WAIT_STATUS_BY_KIND",
    "durable_wait_from_state",
    "interrupt_matches_wait",
    "pause_projection",
    "pending_interrupt_payloads",
    "wait_status",
    "workflow_wait_meta",
]
