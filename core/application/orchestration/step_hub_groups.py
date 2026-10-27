"""Container lifecycle routing: run-once Groups, For each passes and their lanes.

A Group runs its body once; a For each runs its body once per selected item
(ADR 0038). Both share one container stack in the run cursor, one way into
lanes, one exit-contract check and one failure path:

* the failure of a step resolves its policy step → For each → Group → ``fail``;
* an inherited ``route_to_step`` fails the container that declared it and
  takes that container's error edge in its parent scope;
* an unhandled failure leaves the innermost container and is offered to each
  enclosing container in turn; only a container's own ``route_to_step``
  catches it, its continue/retry/await_approval is never re-applied to the
  whole container; the root turns it into a run failure.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from langgraph.types import Command

from core.application.orchestration.active_step import (
    run_cursor,
)
from core.application.orchestration.boundary_contract_runtime import (
    check_entry,
    missing_exit_outputs,
)
from core.application.orchestration.for_each_runtime import (
    compact_item_messages,
    publish_item,
    select_items,
)
from core.application.orchestration.invoke_workflow_hook_runtime import (
    clear_hook_outputs,
    hook_ids_of_steps,
)
from core.application.orchestration.parallel_group_runtime import (
    ACTION_ADVANCE,
    ACTION_ESCALATE,
    GROUP_COMPLETED,
    GROUP_ESCALATED,
    GROUP_RETRY_OPERATION,
    LANE_COMPLETED,
    LANE_ESCALATED,
    LANE_FAILED,
    LANE_RETRY_OPERATION,
    LANE_RUNNING,
    LaneState,
    decide_lane_result,
    initial_lanes,
    lane_active_step,
    lane_cursor,
    load_lanes,
    next_lane_step,
)
from core.application.orchestration.run_cursor import (
    TRANSITION_DISPATCH,
    TRANSITION_ENTER,
    TRANSITION_LANES,
    ForEachFrame,
    Frame,
    RunCursor,
    RunCursorError,
    advance_item,
    begin_step,
    body_start,
    break_item,
    push_frame,
    resolve_after_container,
)
from core.application.orchestration.step_failures import (
    container_policy,
)
from core.application.orchestration.step_hub_support import (
    APPROVAL_NODE,
    OPERATION_RETRY_NODE,
    PARALLEL_NODE,
    RECOVERY_GROUP_CONTRACT,
    RECOVERY_LANE,
    _boundary,
    accepted_message,
    cursor_update,
    journal_entry,
    retry_continuation_updates,
)
from core.application.workflows.execution_graph import NODE_KIND_GROUP, NodeRef
from core.application.workflows.workflow_definition_validation import (
    ERROR_POLICY_FAIL,
    ERROR_POLICY_ROUTE_TO_STEP,
)
from core.application.workflows.workflow_lifecycle_event_sink import (
    publish_active_step_event,
    publish_group_event,
)
from core.application.workflows.workflow_reaction_events import (
    EVENT_GROUP_COMPLETED,
    EVENT_GROUP_FAILED,
    EVENT_GROUP_STARTED,
    EVENT_STEP_COMPLETED,
    EVENT_STEP_FAILED,
)

if TYPE_CHECKING:
    from core.application.orchestration.step_hub import _StepHub


def _label(node: NodeRef) -> str:
    return f"{'Group' if node.kind == NODE_KIND_GROUP else 'For each'} '{node.id}'"


def _path(node: NodeRef) -> str:
    return f"{'groups' if node.kind == NODE_KIND_GROUP else 'for_each'}.{node.id}"


def _progress(hub: _StepHub) -> dict[str, Any]:
    """A finite item boundary: authored progress, never a runaway."""
    return {"bounded_loop_iterations": int(hub.state.get("bounded_loop_iterations") or 0) + 1}


def _journal(update: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return list(dict(update or {}).get("orchestrator_journal") or [])


def enter_container(
    hub: _StepHub,
    cursor: RunCursor,
    node: NodeRef,
    *,
    reason: str,
    update: Mapping[str, Any] | None,
) -> Command:
    """A new container execution: a fresh frame; a For each fixes its items now.

    Every entry (a rework included) starts from the first item and the lane
    entries; a technical retry never comes through here.
    """
    container = hub.definition.container(node)
    frame = push_frame(hub.definition, cursor, node)
    hub.capture_container(cursor, frame, started=True)
    if node.kind == NODE_KIND_GROUP:
        publish_group_event(
            hub.state,
            EVENT_GROUP_STARTED,
            group_id=node.id,
            group_execution_id=frame.execution_id,
        )
    journal = _journal(update)
    state_after = {**hub.state, **dict(update or {})}
    entry = check_entry(container, state_after, owner_path=_path(node))
    if entry.missing:
        return hub.fail_container(
            cursor,
            node,
            {
                "code": entry.selector_failure.get("code") or "missing_required_inputs",
                "message": f"{_label(node)} is missing required inputs: {', '.join(entry.missing)}",
                "missing": entry.missing,
            },
            journal=journal,
            update=update,
            boundary="entry",
        )
    body_updates: dict[str, Any] = {**dict(update or {}), **dict(entry.updates)}
    if isinstance(frame, ForEachFrame):
        selection = select_items(container, frame, {**state_after, **body_updates})
        hub.capture_container(cursor, frame)
        journal.extend(hub._texts(selection.journal, "for_each_enter"))
        if selection.failure is not None:
            return hub.fail_container(
                cursor,
                node,
                selection.failure,
                journal=journal,
                update=body_updates,
                boundary="selection",
            )
        body_updates.update(selection.updates)
        if not frame.selected_count:
            return hub.finish_container(
                cursor, node, update={**body_updates, "orchestrator_journal": journal}
            )
    return _start_body(
        hub, cursor, frame, reason=reason, update={**body_updates, "orchestrator_journal": journal}
    )


def _start_body(
    hub: _StepHub,
    cursor: RunCursor,
    frame: Frame,
    *,
    reason: str,
    update: Mapping[str, Any] | None,
) -> Command:
    """Start the body (or the current item's body) of the innermost container."""
    transition = body_start(hub.definition, frame)
    if transition.kind == TRANSITION_LANES:
        return hub.start_lanes(cursor, frame, update=update)
    if transition.kind == TRANSITION_ENTER and transition.container is not None:
        return hub.enter_container(cursor, transition.container, reason=reason, update=update)
    if transition.kind != TRANSITION_DISPATCH or not transition.step_id:
        raise RunCursorError(f"{_label(frame.node)} cannot start its body")
    begin_step(cursor, transition.step_id)
    return hub.dispatch(
        cursor,
        reason=f"{reason}:{frame.kind}_body",
        update={**dict(update or {}), "feedback": None},
    )


def start_lanes(
    hub: _StepHub, cursor: RunCursor, frame: Frame, *, update: Mapping[str, Any] | None
) -> Command:
    """Hand a fresh lanes body (all lanes at their entries) to the parallel executor."""
    cursor.step_id = None
    cursor.step_execution_id = None
    cursor.attempt = 1
    lanes = initial_lanes(hub.definition, frame.node)
    hub.capture_container(cursor, frame, lanes=[lane.dump() for lane in lanes])
    hub._event(
        f"{_label(frame.node)} runs {len(lanes)} lane(s)",
        {"container": frame.node.dump(), "execution_id": frame.execution_id},
    )
    return hub._command(
        {
            **dict(update or {}),
            **cursor_update(cursor),
            "parallel_lanes": [lane.dump() for lane in lanes],
            "parallel_outcome": None,
            "dispatched_step_execution": None,
            "feedback": None,
        },
        PARALLEL_NODE,
    )


def lanes_returned(hub: _StepHub, cursor: RunCursor) -> Command:
    """The parallel executor stopped every lane: continue the container by its outcome."""
    frame = cursor.frames[-1]
    outcome = hub.state.get("parallel_outcome")
    if not isinstance(outcome, Mapping) or outcome.get("execution_id") != frame.execution_id:
        # No outcome for this container execution yet (a resume): run the lanes.
        return hub._command(cursor_update(cursor), PARALLEL_NODE)
    lanes = load_lanes(hub.state.get("parallel_lanes"))
    status = str(outcome.get("status") or "")
    if status == GROUP_COMPLETED:
        return hub.lanes_completed(cursor, frame)
    if status == GROUP_RETRY_OPERATION:
        lane = next(lane for lane in lanes if lane.status == LANE_RETRY_OPERATION)
        return hub.retry_lane_operation(cursor, frame, lane)
    if status == GROUP_ESCALATED:
        lane = next(lane for lane in lanes if lane.status == LANE_ESCALATED)
        return hub.open_lane_recovery(cursor, frame, lane)
    failed = next((lane for lane in lanes if lane.status == LANE_FAILED), None)
    failure = dict(failed.failure or {}) if failed is not None else {}
    return hub.fail_container(
        cursor,
        frame.node,
        {
            "code": failure.get("failure_class") or "lane_failed",
            "message": failure.get("reason") or f"A lane of {_label(frame.node)} failed",
            "lane_id": failed.lane_id if failed is not None else None,
            "step_id": failure.get("step_id"),
            "step_execution_id": failure.get("step_execution_id"),
            "attempt": failure.get("attempt"),
        },
        update={"parallel_outcome": None},
        boundary="parallel",
    )


def lanes_completed(hub: _StepHub, cursor: RunCursor, frame: Frame) -> Command:
    """Every lane reached its exit: the Group is done, a For each item is done."""
    return hub.body_done(cursor, frame.node, update={"parallel_outcome": None})


def retry_lane_operation(
    hub: _StepHub, cursor: RunCursor, frame: Frame, lane: LaneState
) -> Command:
    """A lane's host-owned operation retry: wait, then resume only that lane."""
    operation = dict(lane.operation or {})
    deadline = float(operation.get("deadline_epoch_seconds") or 0.0)
    delay = float(operation.get("delay_seconds") or 0.0)
    if deadline <= 0 or deadline - time.time() <= 0 or delay > deadline - time.time():
        return _terminal_lane_operation(hub, cursor, frame, lane)
    rows = hub.state.get("operation_retry_state")
    rows = {key: dict(value) for key, value in rows.items()} if isinstance(rows, Mapping) else {}
    identity = str(operation.get("identity") or "")
    rows[identity] = {
        "retry_count": int(operation.get("retry_number") or 1),
        "deadline_epoch_seconds": deadline,
        "step_execution_id": lane.step_execution_id,
    }
    return hub._command(
        {
            **retry_continuation_updates(),
            **cursor_update(cursor),
            "operation_retry_state": rows,
            "operation_retry_dispatch": {
                "identity": identity,
                "deadline_epoch_seconds": deadline,
                "step_id": lane.step_id,
                "step_execution_id": lane.step_execution_id,
                "attempt": lane.attempt + 1,
                "target_actor": str(operation.get("target_actor") or ""),
                "target": PARALLEL_NODE,
                "lane_id": lane.lane_id,
                "position": int(operation.get("position") or 0),
                "delay_seconds": delay,
                "retry_signal": dict(operation.get("signal") or {}),
            },
            "last_failure_class": str(operation.get("failure_class") or ""),
            "last_retry_reason": str(operation.get("reason") or "operation_retry"),
            "last_retry_retryable": True,
            "retry_backoff_seconds": delay,
            "retry_telemetry": dict(operation.get("telemetry") or {}) or None,
        },
        OPERATION_RETRY_NODE,
    )


def _terminal_lane_operation(
    hub: _StepHub, cursor: RunCursor, frame: Frame, lane: LaneState
) -> Command:
    """Apply authored policy when a selected lane retry expires before queueing."""
    from core.application.orchestration.step_operation_retry import trusted_operation_retry_signal

    current = lane_active_step(hub.definition, list(cursor.frames), lane)
    message = accepted_message(hub.state, current)
    if message is None or not trusted_operation_retry_signal(current, message):
        return hub.fail("Lane retry result is unavailable", event="operation_retry_invalid")
    decision = decide_lane_result(
        hub.state, hub.definition, current, message, terminal_reason="retry_delay_exceeds_deadline"
    )
    lane.operation = None
    lane.failure = {
        **current.message_identity(),
        "failure_class": decision.failure_class,
        "reason": decision.reason,
        "detail": decision.detail,
    }
    if decision.action == ACTION_ESCALATE:
        lane.status = LANE_ESCALATED
        return hub.open_lane_recovery(cursor, frame, lane)
    if decision.action == ACTION_ADVANCE and decision.outcome is not None:
        try:
            following = next_lane_step(
                hub.definition, frame.node, lane.lane_id, current.step_id, decision.outcome
            )
        except RunCursorError as exc:
            return hub.fail(str(exc), event="lane_transition_invalid")
        from core.application.orchestration.run_trace import edge_fact

        hub._trace_facts.append(
            edge_fact(
                hub.state,
                hub.definition,
                hub.definition.graph.edge_for(NodeRef("step", current.step_id), decision.outcome),
                lane_cursor(list(cursor.frames), lane),
            )
        )
        lane.completed_steps += 1
        lane.step_id = following
        lane.step_execution_id = None
        lane.attempt = 1
        lane.status = LANE_RUNNING if following else LANE_COMPLETED
        lane.failure = None
        publish_active_step_event(
            hub.state,
            EVENT_STEP_FAILED
            if decision.detail.get("_step_error_policy")
            else EVENT_STEP_COMPLETED,
            current,
            outcome="continued"
            if decision.detail.get("_step_error_policy")
            else decision.outcome.name,
        )
    else:
        lane.status = LANE_FAILED
        publish_active_step_event(hub.state, EVENT_STEP_FAILED, current, error=decision.reason)
    lanes = [
        value.dump() if value.lane_id != lane.lane_id else lane.dump()
        for value in load_lanes(hub.state.get("parallel_lanes"))
    ]
    hub.capture_container(cursor, frame, lanes=lanes)
    update = {"parallel_lanes": lanes, "parallel_outcome": None, "operation_retry_dispatch": None}
    if lane.status == LANE_FAILED:
        return hub.fail_container(
            cursor,
            frame.node,
            {
                "code": decision.failure_class,
                "message": decision.reason,
                "step_id": current.step_id,
            },
            update=update,
        )
    return hub._command({**cursor_update(cursor), **update}, PARALLEL_NODE)


def open_lane_recovery(hub: _StepHub, cursor: RunCursor, frame: Frame, lane: LaneState) -> Command:
    """One recovery pause for the container once every lane has stopped."""
    failure = dict(lane.failure or {})
    reason = str(failure.get("reason") or f"Lane '{lane.lane_id}' needs a decision")
    pending = {
        "request_id": str(uuid.uuid4()),
        "kind": RECOVERY_LANE,
        "container": frame.node.dump(),
        "execution_id": frame.execution_id,
        "lane_id": lane.lane_id,
        "step_id": failure.get("step_id") or lane.step_id,
        "step_execution_id": failure.get("step_execution_id") or lane.step_execution_id,
        "attempt": failure.get("attempt") or lane.attempt,
        "reason": reason,
        "allowed_actions": ["approve", "reject"],
        "detail": dict(failure.get("detail") or {}),
    }
    hub._event(
        f"Recovery approval for lane '{lane.lane_id}' of {frame.kind} "
        f"'{frame.container_id}': {reason}",
        {"container": frame.node.dump(), "lane_id": lane.lane_id},
        severity="warn",
    )
    lanes = [
        item.dump() if item.lane_id != lane.lane_id else lane.dump()
        for item in load_lanes(hub.state.get("parallel_lanes"))
    ]
    hub.capture_container(cursor, frame, lanes=lanes)
    return hub._command(
        {
            **cursor_update(cursor),
            "parallel_lanes": lanes,
            "recovery_pending": pending,
            "needs_user_approval": True,
            "approval_context": reason,
            "user_approved": None,
            "last_failure_class": str(failure.get("failure_class") or ""),
            "last_retry_reason": reason,
            "last_retry_retryable": False,
            "orchestrator_journal": [
                journal_entry(
                    iteration=hub.iteration,
                    event="recovery_requested",
                    step_id=str(pending["step_id"] or ""),
                    action="escalate_to_human",
                    reason=reason,
                    message=f"Recovery approval requested for lane '{lane.lane_id}': {reason}",
                    detail=pending,
                    severity="warn",
                )
            ],
        },
        APPROVAL_NODE,
    )


def resume_lane_recovery(hub: _StepHub, pending: dict[str, Any]) -> Command:
    cursor = run_cursor(hub.state)
    frame = cursor.frames[-1] if cursor.frames else None
    if frame is None or frame.execution_id != pending.get("execution_id"):
        hub.base_update["recovery_pending"] = None
        hub.state = {**hub.state, "recovery_pending": None}
        return hub.route()
    approved = hub.state.get("user_approved")
    if approved is None:
        return hub._command(
            {"needs_user_approval": True, "approval_context": pending.get("reason")},
            APPROVAL_NODE,
        )
    lanes = load_lanes(hub.state.get("parallel_lanes"))
    feedback = str(hub.state.get("review_feedback") or "").strip() or "Please revise and resubmit."
    for lane in lanes:
        if lane.lane_id == pending.get("lane_id") and lane.status == LANE_ESCALATED:
            autonomy_gate = bool((lane.failure or {}).get("detail", {}).get("autonomy_gate"))
            lane.status = LANE_RUNNING
            lane.failure = None
            if autonomy_gate:
                # Approval authorizes an unexecuted step; it must not skip its effect.
                lane.decision = {
                    "action": "authorize",
                    "feedback": None if approved else feedback,
                }
            elif approved:
                lane.decision = {"action": "continue"}
            else:
                lane.decision = {"action": "retry", "feedback": feedback}
                lane.attempt += 1
    lane_rows = [lane.dump() for lane in lanes]
    hub.capture_container(cursor, frame, lanes=lane_rows)
    return hub._command(
        {
            **retry_continuation_updates(),
            **cursor_update(cursor),
            "recovery_pending": None,
            "user_approved": None,
            "parallel_lanes": lane_rows,
            "parallel_outcome": None,
            "orchestrator_journal": [
                journal_entry(
                    iteration=hub.iteration,
                    event="recovery_decision",
                    step_id=str(pending.get("step_id") or ""),
                    action="approve" if approved else "reject",
                    reason=str(pending.get("reason") or ""),
                    message=(
                        f"Lane '{pending.get('lane_id')}' recovery "
                        f"{'approved' if approved else 'rejected'}"
                    ),
                    detail={"request_id": pending.get("request_id"), "kind": RECOVERY_LANE},
                )
            ],
        },
        PARALLEL_NODE,
    )


def body_done(
    hub: _StepHub, cursor: RunCursor, node: NodeRef, *, update: Mapping[str, Any] | None
) -> Command:
    """A body reached its exit (or Continue): the Group is done, a For each moves on."""
    frame = cursor.frames[-1] if cursor.frames else None
    if frame is None or frame.node != node:
        raise RunCursorError(f"{_label(node)} is not the active container")
    if not isinstance(frame, ForEachFrame):
        return hub.finish_container(cursor, node, update=update)
    item = hub.definition.container(node)
    progress = {**dict(update or {}), **_progress(hub)}
    compacted = compact_item_messages(hub.state.get("agent_messages"), _body_step_ids(hub, node))
    if compacted is not None:
        progress["agent_messages"] = compacted
    journal = _journal(update)
    if not advance_item(cursor, node):
        return hub.finish_container(
            cursor, node, update={**progress, "orchestrator_journal": journal}
        )
    # The next item never reads the previous item's hook output (spec §9.3).
    cleared = clear_hook_outputs(
        hub.state, hook_ids_of_steps(hub.definition.steps, _body_step_ids(hub, node))
    )
    published = publish_item(item, frame, {**hub.state, **cleared} if cleared else hub.state)
    journal.extend(hub._texts(published.journal, "for_each_item"))
    if published.failure is not None:
        return hub.fail_container(cursor, node, published.failure, journal=journal, update=progress)
    return _start_body(
        hub,
        cursor,
        frame,
        reason=f"{node.id}:next_item",
        update={**progress, **published.updates, "orchestrator_journal": journal},
    )


def break_for_each(
    hub: _StepHub, cursor: RunCursor, node: NodeRef, *, update: Mapping[str, Any] | None
) -> Command:
    """Break: leave the For each now; its exit contract still applies."""
    frame = break_item(cursor, node)
    journal = _journal(update)
    journal.extend(
        hub._texts(
            [
                f"For each '{node.id}' left at item {frame.item_index + 1}/"
                f"{frame.selected_count} by Break"
            ],
            "for_each_break",
        )
    )
    return hub.finish_container(
        cursor,
        node,
        update={**dict(update or {}), **_progress(hub), "orchestrator_journal": journal},
    )


def _body_step_ids(hub: _StepHub, node: NodeRef) -> set[str]:
    key = "group_id" if node.kind == NODE_KIND_GROUP else "for_each_id"
    return {
        step_id
        for step_id, step in hub.definition.steps.items()
        if str(step.get(key) or "") == node.id
    }


def _container_messages(
    hub: _StepHub, node: NodeRef, state: Mapping[str, Any]
) -> list[dict[str, Any]]:
    from core.application.orchestration.agent_messages import get_state_messages

    body = _body_step_ids(hub, node)
    return [
        msg
        for msg in reversed(get_state_messages(dict(state)))
        if str(msg.get("step_id") or "") in body
        or (node.kind == NODE_KIND_GROUP and msg.get("group_id") == node.id)
    ]


def finish_container(
    hub: _StepHub,
    cursor: RunCursor,
    node: NodeRef,
    *,
    update: Mapping[str, Any] | None,
    check_exit: bool = True,
) -> Command:
    """Leave a finished container on its success edge in its own scope.

    An exit contract that is not met is a container-level failure without a
    failed step: ``route_to_step`` takes the container's error edge, ``fail``
    hands it to the enclosing container, and continue/retry/await_approval ask
    an operator instead of running the whole body again.
    """
    container = hub.definition.container(node)
    if check_exit and _boundary(container).get("exit"):
        state_after = {**hub.state, **dict(update or {})}
        missing = missing_exit_outputs(
            container, state_after, _container_messages(hub, node, state_after)
        )
        if missing:
            return hub.container_contract_failure(cursor, node, missing, update=update)
    frame = cursor.frame_of(node)
    if frame is not None:
        hub.capture_container(cursor, frame, outcome="success")
        while cursor.frames and cursor.frames[-1].node != node:
            cursor.frames.pop()
        cursor.frames.pop()
    detail: dict[str, Any] = {"container": node.dump()}
    finished: dict[str, Any] = {}
    if isinstance(frame, ForEachFrame):
        detail.update(frame.counts())
        # The pass is over; its items need not stay in the checkpoint.
        finished["for_each_items"] = None
    hub._event(f"{_label(node)} finished", detail)
    if node.kind == NODE_KIND_GROUP:
        publish_group_event(
            hub.state,
            EVENT_GROUP_COMPLETED,
            group_id=node.id,
            group_execution_id=frame.execution_id if frame is not None else None,
        )
    journal = _journal(update)
    journal.append(
        journal_entry(
            iteration=hub.iteration,
            event=f"{node.kind}_completed",
            action="success",
            message=f"{_label(node)} finished",
            detail=detail,
        )
    )
    try:
        transition = replace(
            resolve_after_container(hub.definition, cursor, node), source_frame=frame
        )
    except RunCursorError as exc:
        return hub.fail(str(exc), event="container_transition_invalid")
    return hub.apply(
        cursor,
        transition,
        reason=f"{node.id}:success",
        update={**dict(update or {}), **finished, "orchestrator_journal": journal},
    )


def container_contract_failure(
    hub: _StepHub,
    cursor: RunCursor,
    node: NodeRef,
    missing: list[str],
    *,
    update: Mapping[str, Any] | None,
) -> Command:
    policy = container_policy(hub.definition.container(node))
    on_error = policy.on_error if policy is not None else ERROR_POLICY_FAIL
    message = f"{_label(node)} did not produce required outputs: {', '.join(missing)}"
    if on_error in {ERROR_POLICY_ROUTE_TO_STEP, ERROR_POLICY_FAIL}:
        return hub.fail_container(
            cursor,
            node,
            {"code": "container_exit_outputs_missing", "message": message, "missing": missing},
            update=update,
            boundary="exit",
        )
    frame = cursor.frame_of(node)
    if frame is not None:
        hub.capture_container(
            cursor,
            frame,
            failure={"code": "container_exit_outputs_missing", "message": message},
            boundary="exit",
        )
    pending = {
        "request_id": str(uuid.uuid4()),
        "execution_id": frame.execution_id if frame is not None else None,
        "kind": RECOVERY_GROUP_CONTRACT,
        "container": node.dump(),
        "reason": message,
        "allowed_actions": ["approve", "reject"],
        "detail": {"missing": missing, "on_error": on_error},
    }
    return hub._command(
        {
            **dict(update or {}),
            **cursor_update(cursor),
            "recovery_pending": pending,
            "needs_user_approval": True,
            "approval_context": message,
            "user_approved": None,
            "last_failure_class": "contract",
            "last_retry_reason": message,
            "last_retry_retryable": False,
        },
        APPROVAL_NODE,
    )


def _pending_node(pending: Mapping[str, Any]) -> NodeRef:
    raw = pending.get("container")
    if not isinstance(raw, Mapping) or not raw.get("kind") or not raw.get("id"):
        raise RunCursorError("The recovery pause names no container")
    return NodeRef(str(raw["kind"]), str(raw["id"]))


def resume_container_contract(hub: _StepHub, pending: dict[str, Any]) -> Command:
    """Approve lets this exit through unchecked; reject fails the container outward."""
    cursor = run_cursor(hub.state)
    approved = hub.state.get("user_approved")
    node = _pending_node(pending)
    if approved is None:
        return hub._command(
            {"needs_user_approval": True, "approval_context": pending.get("reason")},
            APPROVAL_NODE,
        )
    cleared = {"recovery_pending": None, "user_approved": None, **retry_continuation_updates()}
    if approved:
        frame = cursor.frame_of(node)
        if frame is not None and pending.get("execution_id") == frame.execution_id:
            hub.capture_container(
                cursor,
                frame,
                failure={
                    "code": "container_exit_outputs_missing",
                    "message": str(pending.get("reason") or ""),
                },
                boundary="exit",
            )
            patch = hub._container_patches[-1]
            if patch is not None:
                patch["failure"]["handling"] = {
                    "kind": "exit_approval",
                    "execution_id": frame.execution_id,
                    "request_id": pending["request_id"],
                }
        return hub.finish_container(cursor, node, update=cleared, check_exit=False)
    return hub.fail_container(
        cursor,
        node,
        {
            "code": "container_exit_rejected",
            "message": str(pending.get("reason") or f"{_label(node)} outputs were rejected"),
        },
        update=cleared,
        catch=False,
        boundary="exit",
    )


def fail_container(
    hub: _StepHub,
    cursor: RunCursor,
    node: NodeRef,
    failure: Mapping[str, Any],
    *,
    journal: list[dict[str, Any]] | None = None,
    update: Mapping[str, Any] | None = None,
    catch: bool = True,
    boundary: str = "body",
) -> Command:
    """A container-level failure: its own error edge, or the enclosing container's.

    The container and every frame inside it are removed before anything is
    dispatched; the frames around it stay. With ``catch`` the container's own
    ``route_to_step`` takes the failure; a rejected exit (``catch=False``) is
    offered only to the enclosing containers. Completed state is kept: nothing
    is rolled back and no default output is invented.
    """
    frame = cursor.frame_of(node)
    if node.kind == NODE_KIND_GROUP:
        publish_group_event(
            hub.state,
            EVENT_GROUP_FAILED,
            group_id=node.id,
            group_execution_id=frame.execution_id if frame is not None else None,
            error=str(failure.get("message") or ""),
            error_code=str(failure.get("code") or ""),
        )
    if frame is not None:
        while cursor.frames and cursor.frames[-1].node != node:
            hub.capture_container(
                cursor, cursor.frames[-1], outcome="error", failure=failure, boundary=boundary
            )
            cursor.frames.pop()
        hub.capture_container(cursor, frame, outcome="error", failure=failure, boundary=boundary)
        cursor.frames.pop()
    cursor.step_id = None
    cursor.step_execution_id = None
    code = str(failure.get("code") or "container_failed")
    message = str(failure.get("message") or f"{_label(node)} failed: {code}")
    detail = {key: value for key, value in failure.items() if key != "workflow_data"}
    detail.setdefault("container", node.dump())
    state_update = {**dict(update or {})}
    if "workflow_data" in failure:
        state_update["workflow_data"] = failure["workflow_data"]
    entries = [
        *(journal or []),
        journal_entry(
            iteration=hub.iteration,
            event=f"{node.kind}_failed",
            action="fail",
            reason=code,
            message=message,
            detail={**detail, "failed": node.dump()},
            severity="warn",
        ),
    ]
    policy = container_policy(hub.definition.container(node))
    if catch and policy is not None and policy.on_error == ERROR_POLICY_ROUTE_TO_STEP:
        try:
            transition = replace(
                resolve_after_container(hub.definition, cursor, node, failed=True),
                source_frame=frame,
            )
        except RunCursorError as exc:
            return hub.fail(str(exc), event="container_transition_invalid")
        return hub.apply(
            cursor,
            transition,
            reason=f"{node.id}:error",
            update={**state_update, "orchestrator_journal": entries},
        )
    if cursor.frames:
        outer = cursor.frames[-1].node
        return hub.fail_container(
            cursor,
            outer,
            {**failure, "code": code, "message": message, "inner": node.dump()},
            journal=entries,
            update=state_update,
            boundary=boundary,
        )
    return hub.fail(
        message,
        event="container_failed",
        failure_class="execution",
        unresolved={
            "status": "fail",
            "container": node.dump(),
            "group_id": node.id if node.kind == NODE_KIND_GROUP else None,
            "error_code": code,
            "message": message,
            **detail,
        },
        extra={**cursor_update(cursor), **state_update},
        journal=entries,
    )
