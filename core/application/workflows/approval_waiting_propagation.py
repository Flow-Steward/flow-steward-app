"""Notify the waiting ancestors of an invoked child that waits for approval.

A child's ``job.approval_waiting`` is handled by the child's own reactions.
Each ancestor that waits on that child (``awaiting_child``) up to the root also
gets a notification projection of the same event, so a parent rule fires
without per-child setup:

* ``event.job_id``/``workflow_id`` name the ancestor; the step/group scope is
  the ancestor's own invoke execution (its ``workflow_wait``), never the inner
  child step;
* the payload names the origin (``origin_job_id``, ``origin_workflow_id``,
  ``origin_step_execution_id``), the ancestor's immediate child
  (``child_job_id``), the job and id of the actual Approval (``approval_job_id``,
  ``approval_id``; links and callbacks address these), the origin ``wait_id``
  and ``propagated: true``; never review content, outputs or secrets.

The lineage is read only from the host database: the child's
``meta.invoke_parent_job_id`` and the parent's ``meta.invoke_children`` link
for exactly that child, never an extension payload. Before each ancestor the
original approval must still be pending and the ancestor must still wait on
that invocation; a terminal, cancelled, consumed or unlinked ancestor stops the
walk. The ancestry is walked once from the original event with depth and cycle
guards; a propagated event does not bubble again. It is a notification
projection only: no interrupt, approval id, resume or cancel on an ancestor.

The event identity is the origin ``wait_id`` with the ancestor job; with the
reaction key and the route identity (see :mod:`workflow_reaction_events`) a
repeat of the original event or a catch-up scan never sends a duplicate to the
same rule and route.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from core.application.jobs.public_approval_serializer import bounded_approval_actions
from core.application.workflows.workflow_reaction_events import (
    APPROVAL_REASON_AUTHORED,
    EVENT_APPROVAL_WAITING,
)

logger = logging.getLogger(__name__)

STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_AWAITING_CHILD = "awaiting_child"
_DEFAULT_MAX_DEPTH = 5
_PROPAGATED_MESSAGE = "A child workflow is waiting for approval."


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _max_depth() -> int:
    try:
        from core.application.orchestration.invoke_workflow_step_node_common import (
            _resolve_invoke_workflow_max_depth,
        )

        return _resolve_invoke_workflow_max_depth()
    except Exception:
        return _DEFAULT_MAX_DEPTH


def _get_job(bl: Any, job_id: str) -> dict[str, Any]:
    if not job_id or not hasattr(bl, "get_job"):
        return {}
    try:
        row = bl.get_job(job_id)
    except Exception:
        return {}
    return dict(row) if isinstance(row, Mapping) else {}


def _workflow_id(row: Mapping[str, Any]) -> str:
    meta = _mapping(row.get("meta"))
    return _text(
        meta.get("selected_workflow_id")
        or meta.get("requested_workflow_id")
        or meta.get("workflow_id")
        or row.get("workflow_id")
    )


def _pending_approval_payload(bl: Any, job_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Current occurrence from the host's atomic durable public pause projection."""
    if _text(row.get("status")).lower() != STATUS_AWAITING_APPROVAL:
        return {}
    try:
        revision = bl.get_public_progress_snapshot(job_id)
        snapshot = revision.snapshot
        if (
            snapshot.status != STATUS_AWAITING_APPROVAL
            or _text(snapshot.job_id) != job_id
            or _text(snapshot.project_id) != _text(row.get("project_id"))
            or _text(snapshot.workflow_id) != _workflow_id(row)
            or snapshot.approval is None
        ):
            return {}
        approval = snapshot.approval.model_dump(mode="json")
    except Exception:
        return {}
    if (
        _text(approval.get("job_id")) != job_id
        or not _text(approval.get("approval_id"))
        or not _text(approval.get("wait_id"))
    ):
        return {}
    # Old projections without occurrence identity fail closed, never guess from status.
    return {
        key: approval.get(key)
        for key in (
            "approval_id",
            "wait_id",
            "reason",
            "allowed_actions",
            "step_id",
            "step_execution_id",
            "group_id",
        )
    }


def approval_still_pending(
    bl: Any, row: Mapping[str, Any], *, job_id: str, wait_id: str, approval_id: str
) -> bool:
    """Match both canonical wait identity and actual interrupt occurrence."""
    current = _pending_approval_payload(bl, job_id, row)
    return (
        bool(current)
        and _text(current.get("wait_id")) == wait_id
        and _text(current.get("approval_id")) == approval_id
    )


def trusted_parent_link(
    bl: Any, child_id: str, child_row: Mapping[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """The parent that linked this child, with its link entry, from the host DB."""
    parent_id = _text(_mapping(child_row.get("meta")).get("invoke_parent_job_id"))
    if not parent_id or parent_id == child_id:
        return None
    parent = _get_job(bl, parent_id)
    if not parent:
        return None
    if _text(parent.get("project_id")) != _text(child_row.get("project_id")):
        return None
    entry = _mapping(_mapping(_mapping(parent.get("meta")).get("invoke_children")).get(child_id))
    if not entry or entry.get("cancel_requested_at") or entry.get("consumed_at"):
        return None
    return parent_id, parent, entry


def ancestor_waits_on_child(
    parent: Mapping[str, Any], *, child_id: str, entry: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The ancestor's child wait for exactly this invocation, or ``None``."""
    if _text(parent.get("status")).lower() != STATUS_AWAITING_CHILD:
        return None
    wait = _mapping(_mapping(parent.get("meta")).get("workflow_wait"))
    if _text(wait.get("kind")) != "child" or _text(wait.get("child_job_id")) != child_id:
        return None
    linked_execution = _text(entry.get("step_execution_id"))
    if linked_execution and _text(wait.get("step_execution_id")) != linked_execution:
        return None
    return wait


def trusted_forwarded_approval_job(bl: Any, event: Mapping[str, Any]) -> str | None:
    """Resolve a forwarded decision target from the still-pending host lineage.

    The reaction keeps its ancestor scope and event identity. Its notification
    addresses the actual Approval job; an event payload cannot grant access to
    an unrelated job or revive a consumed invocation.
    """
    payload = _mapping(event.get("payload"))
    target_id = _text(payload.get("approval_job_id"))
    ancestor_id = _text(event.get("job_id"))
    wait_id = _text(payload.get("wait_id"))
    approval_id = _text(payload.get("approval_id"))
    if not target_id or not ancestor_id or not wait_id or not approval_id:
        return None
    child_id, child = target_id, _get_job(bl, target_id)
    if not approval_still_pending(
        bl, child, job_id=target_id, wait_id=wait_id, approval_id=approval_id
    ):
        return None
    current = _pending_approval_payload(bl, target_id, child)
    if bounded_approval_actions(payload.get("allowed_actions")) != bounded_approval_actions(
        current.get("allowed_actions")
    ):
        return None
    visited = {target_id}
    for _ in range(max(1, _max_depth())):
        link = trusted_parent_link(bl, child_id, child)
        if link is None:
            return None
        parent_id, parent, entry = link
        if parent_id in visited:
            return None
        visited.add(parent_id)
        wait = ancestor_waits_on_child(parent, child_id=child_id, entry=entry)
        if wait is None:
            return None
        if parent_id == ancestor_id:
            if _text(event.get("project_id")) != _text(parent.get("project_id")):
                return None
            if _text(event.get("workflow_id")) != _workflow_id(parent):
                return None
            if any(
                _text(payload.get(key)) != _text(wait.get(key))
                for key in ("step_id", "step_execution_id", "group_id")
            ):
                return None
            return target_id
        child_id, child = parent_id, parent
    return None


def propagated_payload(
    origin_event: Mapping[str, Any],
    *,
    child_job_id: str,
    ancestor_wait: Mapping[str, Any],
) -> dict[str, Any]:
    """Labels, ids and the ancestor's invoke scope; nothing of the child's content."""
    origin = _mapping(origin_event.get("payload"))
    payload: dict[str, Any] = {
        "status": STATUS_AWAITING_CHILD,
        "message": _PROPAGATED_MESSAGE,
        "propagated": True,
        "origin_job_id": _text(origin_event.get("job_id")),
        "origin_workflow_id": _text(origin_event.get("workflow_id")),
        "origin_step_execution_id": _text(origin.get("step_execution_id")) or None,
        "origin_step_id": _text(origin.get("step_id")) or None,
        "child_job_id": child_job_id,
        "approval_job_id": _text(origin_event.get("job_id")),
        "approval_id": _text(origin.get("approval_id")),
        "allowed_actions": bounded_approval_actions(origin.get("allowed_actions")),
        "wait_id": _text(origin.get("wait_id")),
        "reason": _text(origin.get("reason")) or APPROVAL_REASON_AUTHORED,
        "step_id": _text(ancestor_wait.get("step_id")) or None,
        "step_execution_id": _text(ancestor_wait.get("step_execution_id")) or None,
        "group_id": _text(ancestor_wait.get("group_id")) or None,
    }
    attempt = ancestor_wait.get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool):
        payload["attempt"] = attempt
    return payload


def propagate_approval_waiting(
    bl: Any,
    origin_event: Mapping[str, Any],
    *,
    evaluate: Callable[[dict[str, Any]], list[dict[str, Any]]],
    to_json_obj: Callable[[Any], Any] | None = None,
    max_depth: int | None = None,
) -> list[dict[str, Any]]:
    """Evaluate the reactions of every waiting ancestor of the approval's job."""
    from core.application.workflows.job_lifecycle_reactions import (
        build_lifecycle_reaction_event,
    )

    del to_json_obj
    origin = _mapping(origin_event.get("payload"))
    if origin.get("propagated") is True:
        return []
    approval_job_id = _text(origin_event.get("job_id"))
    wait_id = _text(origin.get("wait_id"))
    if not approval_job_id or not wait_id or not _text(origin.get("approval_id")):
        return []
    child_id = approval_job_id
    child_row = _get_job(bl, child_id)
    if not child_row or not approval_still_pending(
        bl,
        child_row,
        job_id=approval_job_id,
        wait_id=wait_id,
        approval_id=_text(origin.get("approval_id")),
    ):
        return []
    visited = {child_id}
    results: list[dict[str, Any]] = []
    for _ in range(max(1, max_depth if max_depth is not None else _max_depth())):
        link = trusted_parent_link(bl, child_id, child_row)
        if link is None:
            break
        parent_id, parent, entry = link
        if parent_id in visited:
            logger.warning("approval propagation stopped at a lineage cycle: job_id=%s", parent_id)
            break
        visited.add(parent_id)
        ancestor_wait = ancestor_waits_on_child(parent, child_id=child_id, entry=entry)
        if ancestor_wait is None:
            break
        # The approval can be decided while the walk runs: check before each delivery.
        if not approval_still_pending(
            bl,
            _get_job(bl, approval_job_id),
            job_id=approval_job_id,
            wait_id=wait_id,
            approval_id=_text(origin.get("approval_id")),
        ):
            break
        event = build_lifecycle_reaction_event(
            event_type=EVENT_APPROVAL_WAITING,
            project_id=_text(parent.get("project_id")),
            workflow_id=_workflow_id(parent) or None,
            job_id=parent_id,
            event_id=_text(origin_event.get("event_id")),
            payload=propagated_payload(
                origin_event, child_job_id=child_id, ancestor_wait=ancestor_wait
            ),
            account_id=_text(parent.get("account_id"))
            or _text(origin_event.get("account_id"))
            or None,
        )
        if event.get("workflow_id"):
            results.extend(evaluate(event))
        child_id, child_row = parent_id, parent
    return results


def pending_child_approval_event(
    bl: Any, parent_job_id: str, *, max_depth: int | None = None
) -> dict[str, Any] | None:
    """The approval event of the descendant a just-parked parent waits on.

    A child can reach its Approval before its parent's ``awaiting_child`` is
    on the row; that child's own propagation then stopped at the parent. The
    parent's park catches up through the descendant's canonical durable public
    approval projection. Authored, recovery and autonomy pauses use their actual
    stored occurrence, actions and wait identity; no synthetic runtime wait.
    """
    current_id = _text(parent_job_id)
    visited = {current_id}
    row = _get_job(bl, current_id)
    for _ in range(max(1, max_depth if max_depth is not None else _max_depth())):
        if _text(row.get("status")).lower() != STATUS_AWAITING_CHILD:
            return None
        wait = _mapping(_mapping(row.get("meta")).get("workflow_wait"))
        child_id = _text(wait.get("child_job_id"))
        if not child_id or child_id in visited:
            return None
        visited.add(child_id)
        child = _get_job(bl, child_id)
        if not child:
            return None
        status = _text(child.get("status")).lower()
        if status == STATUS_AWAITING_CHILD:
            current_id, row = child_id, child
            continue
        if status != STATUS_AWAITING_APPROVAL:
            return None
        approval = _pending_approval_payload(bl, child_id, child)
        if not approval:
            return None
        return {
            "event_type": EVENT_APPROVAL_WAITING,
            "job_id": child_id,
            "workflow_id": _workflow_id(child) or None,
            "project_id": _text(child.get("project_id")),
            "account_id": _text(child.get("account_id")) or None,
            "event_id": f"approval-catch-up:{child_id}",
            "payload": approval,
        }
    return None


def catch_up_child_approval(
    bl: Any, *, parent_job_id: str, to_json_obj: Callable[[Any], Any]
) -> list[dict[str, Any]]:
    """Propagate a descendant's pending approval once the parent has parked. Never raises."""
    from core.application.workflows.job_lifecycle_reactions import (
        build_lifecycle_reaction_evaluator,
        evaluate_lifecycle_event,
    )

    try:
        origin = pending_child_approval_event(bl, parent_job_id)
        if origin is None:
            return []
        evaluate = build_lifecycle_reaction_evaluator(bl, to_json_obj=to_json_obj)
        return propagate_approval_waiting(
            bl,
            origin,
            evaluate=lambda current: evaluate_lifecycle_event(bl, current, evaluate=evaluate),
        )
    except Exception as exc:
        logger.warning(
            "child approval catch-up failed: parent_job_id=%s error=%s", parent_job_id, exc
        )
        return []


__all__ = [
    "ancestor_waits_on_child",
    "approval_still_pending",
    "catch_up_child_approval",
    "pending_child_approval_event",
    "propagate_approval_waiting",
    "propagated_payload",
    "trusted_forwarded_approval_job",
    "trusted_parent_link",
]
