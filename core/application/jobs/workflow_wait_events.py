"""The notification payload of a parked run's wait.

A run parked on an approval or a Wait timer notifies with the wait's identity
and scope: ``wait_id`` (one event per wait), ``step_id``/``step_execution_id``/
``group_id``/``for_each_id`` (nullable; a group recovery has no step), ``due_at``
for a timer, ``approval_id``/``reason``/``group_label``/``for_each_label`` for an
approval. The first run reads
the pause from the checkpoint; a resume reads the projection it already built
(``workflow_wait`` and the approval payload in its deferred result).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.jobs.workflow_wait_projection import (
    STATUS_AWAITING_APPROVAL,
    STATUS_AWAITING_TIMER,
    durable_wait_from_state,
)
from core.application.workflows.workflow_reaction_events import wait_event_payload


def wait_notification_payload_from_state(status: str, state: Any) -> dict[str, Any]:
    """The wait payload of a run that just paused, from its checkpoint state."""
    if status == STATUS_AWAITING_APPROVAL:
        from core.application.jobs.workflow_run_projection import (
            approval_notification_payload_from_state,
        )

        approval = approval_notification_payload_from_state(state)
        wait = durable_wait_from_state(state)
        recorded = wait if wait is not None and wait.get("kind") == "approval" else None
        return {**approval, **wait_event_payload(recorded, approval=approval)}
    if status == STATUS_AWAITING_TIMER:
        wait = durable_wait_from_state(state)
        if wait is None or wait.get("kind") != "timer":
            return {}
        return wait_event_payload(wait)
    return {}


def wait_notification_payload_from_result(status: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """The wait payload of a resumed run that paused again, from its deferred result."""
    raw_wait = result.get("workflow_wait")
    wait = dict(raw_wait) if isinstance(raw_wait, Mapping) else {}
    if status == STATUS_AWAITING_APPROVAL:
        raw_approval = result.get("approval")
        approval = dict(raw_approval) if isinstance(raw_approval, Mapping) else {}
        recorded = wait if wait.get("kind") == "approval" else None
        return {**approval, **wait_event_payload(recorded, approval=approval)}
    if status == STATUS_AWAITING_TIMER:
        if wait.get("kind") != "timer":
            return {}
        return wait_event_payload(wait)
    return {}


__all__ = ["wait_notification_payload_from_result", "wait_notification_payload_from_state"]
