"""Parallel executor node: runs the lanes of the active real group concurrently.

The hub enters a lanes body (a parallel group, or the item body of a loop whose
items run lanes) and routes here. Every lane walks its own steps with its own
cursor (``parallel_group_runtime``); lanes run in a thread pool capped by the
body's ``concurrency_limit`` or the project concurrency cap. When every lane
has stopped, results are merged in the existing order — step position within
the lane, then lane order — with the existing top-level state-update
assignment, and the group outcome goes back to the hub. Lanes that already
completed are never re-run: a technical retry or an operator decision resumes
only the lane it belongs to.
"""

from __future__ import annotations

import copy
import logging
import time
import traceback
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.application.orchestration.active_step import (
    ActiveStep,
    message_matches_execution,
    run_cursor,
)
from core.application.orchestration.boundary_contract_runtime import (
    check_entry,
    missing_context_inject,
)
from core.application.orchestration.parallel_group_runtime import (
    ACTION_ADVANCE,
    ACTION_ESCALATE,
    ACTION_FAIL,
    ACTION_RETRY,
    ACTION_RETRY_OPERATION,
    LANE_COMPLETED,
    LANE_ESCALATED,
    LANE_FAILED,
    LANE_PENDING,
    LANE_RETRY_OPERATION,
    LANE_RUNNING,
    LaneState,
    LaneStepDecision,
    begin_lane_step,
    decide_lane_result,
    group_outcome,
    lane_active_step,
    lane_cursor,
    load_lanes,
    next_lane_step,
)
from core.application.orchestration.run_cursor import Frame, FrozenDefinition, RunCursorError
from core.application.orchestration.run_definition import load_run_definition
from core.application.orchestration.step_autonomy import autonomy_gate_reason
from core.application.orchestration.step_guardrails import (
    GUARDRAIL_ESCALATE,
    GUARDRAIL_RETRY,
    check_step_guardrails,
)
from core.application.orchestration.step_hub_support import (
    STEP_SKIPPED_REASON,
    disabled_step_can_be_skipped,
    executor_node_for_step,
    step_is_disabled,
)
from core.application.orchestration.step_operation_retry import has_unknown_external_effect
from core.application.workflows.execution_graph import OUTCOME_SUCCESS, Outcome
from core.application.workflows.workflow_lifecycle_event_sink import (
    publish_active_step_event,
)
from core.application.workflows.workflow_reaction_events import (
    EVENT_STEP_COMPLETED,
    EVENT_STEP_FAILED,
    EVENT_STEP_STARTED,
)

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState

logger = logging.getLogger(__name__)

_AGENT_NODE_CACHE: dict[str, object] = {}
PARALLEL_STEP_FAILED_MESSAGE = "parallel step failed"

#: Keys a lane step's update never assigns at group level: run routing and
#: terminal state belong to the hub, and messages are merged separately.
_STEP_UPDATE_BLOCKLIST = {
    "run_cursor",
    "workflow_run_trace",
    "workflow_container_progress",
    "current_step_id",
    "current_group_id",
    "dispatched_step_execution",
    "recovery_pending",
    "iteration_count",
    "orchestrator_journal",
    "parallel_lanes",
    "parallel_outcome",
    "parallel_concurrency_cap",
    "parallel_execution_order",
    "status",
    "error",
    "final_report",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_agent_node(agent_id: str):
    clean_agent = str(agent_id or "").strip()
    if not clean_agent:
        return None
    cached = _AGENT_NODE_CACHE.get(clean_agent)
    if cached:
        return cached
    try:
        from core.application.orchestration.graph.nodes import (
            load_agent_implementation,
            wrap_agent_as_node,
        )
        from core.application.registry.registry_manager import RegistryManager

        registry = RegistryManager.instance()
        manifest = registry.agents.get_agent(clean_agent)
        if manifest is None or not bool(getattr(manifest, "enabled", True)):
            return None
        impl = load_agent_implementation(manifest, registry.tools, registry.skills)
        node_fn = wrap_agent_as_node(impl, manifest)
        _AGENT_NODE_CACHE[clean_agent] = node_fn
        return node_fn
    except Exception as exc:
        logger.exception(
            "parallel_executor: could not resolve agent node '%s': %s", clean_agent, exc
        )
        return None


def _resolve_lane_step_node(step: Mapping[str, Any]) -> Callable[..., Any] | None:
    """The executor of a lane step; the same executors the hub dispatches to."""
    from core.application.orchestration.step_hub_support import (
        APPROVAL_NODE,
        CONTROL_FLOW_NODE,
        EXECUTE_STEP_NODE,
        INVOKE_NODE,
        UnsupportedStepKindError,
        executor_node_for_step,
    )

    try:
        node_name = executor_node_for_step(step)
    except UnsupportedStepKindError:
        return None
    if node_name == INVOKE_NODE:
        from core.application.orchestration.invoke_workflow_step_node import (
            invoke_workflow_step_node,
        )

        return invoke_workflow_step_node
    if node_name in {EXECUTE_STEP_NODE, "dataset_query_step", "tabular_feed_step"}:
        from core.application.orchestration.graph.nodes import execute_step_node

        return execute_step_node
    if node_name == CONTROL_FLOW_NODE:
        from core.application.orchestration.control_flow_step_node import control_flow_step_node

        return control_flow_step_node
    if node_name == APPROVAL_NODE:
        return None
    return _resolve_agent_node(node_name)


def _failed_message(current: ActiveStep, *, agent_id: str, error_text: str) -> dict[str, Any]:
    from core.application.orchestration.agent_messages import build_agent_message

    timestamp = _now_iso()
    text = str(error_text or PARALLEL_STEP_FAILED_MESSAGE)
    return build_agent_message(
        from_agent=agent_id or "parallel_executor",
        to_agent="orchestrator",
        skill="parallel_dispatch",
        status="fail",
        correlation_id=f"parallel-{current.step_execution_id}-{current.attempt}",
        artifacts={"error": text},
        summary=text.splitlines()[0][:500],
        errors=[text[:2000]],
        started_at=timestamp,
        completed_at=timestamp,
        published_at=timestamp,
        **current.message_identity(),
    )


def _accepted(messages: Any, current: ActiveStep) -> dict[str, Any] | None:
    for message in reversed(messages if isinstance(messages, list) else []):
        if isinstance(message, dict) and message_matches_execution(message, current):
            return dict(message)
    return None


def _failure(
    current: ActiveStep,
    failure_class: str,
    reason: str,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "step_id": current.step_id,
        "step_execution_id": current.step_execution_id,
        "attempt": current.attempt,
        "failure_class": failure_class or "execution",
        "reason": reason,
        "detail": dict(detail or {}),
    }


def _operation_payload(decision: LaneStepDecision, position: int) -> dict[str, Any]:
    operation = decision.operation
    return {
        "identity": operation.identity,
        "retry_number": operation.retry_number,
        "deadline_epoch_seconds": operation.deadline_epoch_seconds,
        "delay_seconds": operation.delay_seconds,
        "reason": operation.reason,
        "failure_class": operation.failure_class,
        "signal": dict(operation.signal),
        "telemetry": dict(operation.telemetry),
        "target_actor": operation.target_actor,
        "position": position,
    }


def _execute_lane_step(
    current: ActiveStep, lane_state: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Run one lane step with its own executor; return its result and update."""
    from core.application.orchestration.kept_test_output_runtime import (
        kept_output_for,
        kept_output_result,
    )

    kept = kept_output_for(lane_state, current)
    if kept is not None:
        # A workflow test run: the step's kept test output stands in for its work.
        updates, message = kept_output_result(lane_state, current, kept, to_agent="orchestrator")
        return message, updates
    node_fn = _resolve_lane_step_node(current.step)
    agent_id = str(current.step.get("agent") or current.step.get("agent_id") or "")
    if node_fn is None:
        return (
            _failed_message(
                current,
                agent_id=agent_id,
                error_text=f"parallel_executor: step '{current.step_id}' has no lane executor",
            ),
            {},
        )
    try:
        raw_update = node_fn(lane_state)
    except Exception as exc:
        logger.exception("parallel_executor lane step '%s' failed", current.step_id)
        return (
            _failed_message(
                current,
                agent_id=agent_id,
                error_text=f"parallel_executor: {exc}\n{traceback.format_exc()[:1200]}",
            ),
            {},
        )
    update = dict(raw_update or {}) if isinstance(raw_update, dict) else {}
    return _accepted(
        update.get("agent_messages", lane_state.get("agent_messages")), current
    ), update


def _publish_lane_decision(
    state: Mapping[str, Any], current: ActiveStep, decision: LaneStepDecision
) -> None:
    """The step event of a decided lane step, as the hub publishes it for serial steps.

    A retry, an operation retry or an escalation publishes nothing here: the
    next attempt reports its own start, and an operator decision comes later.
    """
    if decision.action == ACTION_ADVANCE and decision.outcome is not None:
        policy = (decision.detail or {}).get("_step_error_policy")
        if isinstance(policy, Mapping) and policy.get("applied"):
            publish_active_step_event(
                state, EVENT_STEP_FAILED, current, error=decision.reason, outcome="continued"
            )
        else:
            publish_active_step_event(
                state, EVENT_STEP_COMPLETED, current, outcome=decision.outcome.name
            )
    elif decision.action not in {ACTION_RETRY, ACTION_RETRY_OPERATION, ACTION_ESCALATE}:
        publish_active_step_event(state, EVENT_STEP_FAILED, current, error=decision.reason)


def _skipped_lane_decision(state: dict[str, Any], current: ActiveStep) -> LaneStepDecision:
    """A disabled lane step: nothing runs and no step event is published.

    The lane advances on the step's Next edge, as the hub does for a serial
    step; a disabled step without a single Next outcome fails the lane instead
    of guessing a branch (the validator refuses such a definition).
    """
    if not disabled_step_can_be_skipped(current.step):
        return LaneStepDecision(
            ACTION_FAIL,
            failure_class="execution_contract",
            reason=(
                f"Step '{current.step_id}' is disabled, but only a step with one Next "
                "outcome can be skipped"
            ),
        )
    from core.application.orchestration.agents.common import emit_event

    emit_event(
        state,
        "parallel_executor",
        "routing_decision",
        f"Skipping disabled step '{current.step_id}'",
        {**current.message_identity(), "skipped": STEP_SKIPPED_REASON},
    )
    return LaneStepDecision(
        ACTION_ADVANCE,
        outcome=Outcome(OUTCOME_SUCCESS),
        detail={"skipped": STEP_SKIPPED_REASON},
    )


def _prepare_lane_step(
    current: ActiveStep, state: dict[str, Any]
) -> tuple[LaneStepDecision | None, dict[str, Any]]:
    """Apply the same pre-effect contracts and autonomy policy as serial dispatch."""
    entry = check_entry(current.step, state, owner_path=f"steps.{current.step_id}")
    state.update(entry.updates)
    missing = [*entry.missing, *missing_context_inject(current.step, state)]
    if missing:
        return LaneStepDecision(
            ACTION_FAIL,
            failure_class="contract",
            reason=f"Step '{current.step_id}' is missing required inputs: {', '.join(missing)}",
            detail={"missing": missing, **entry.selector_failure},
        ), entry.updates
    reason, detail = autonomy_gate_reason(current, state, executor_node_for_step(current.step))
    if reason:
        return LaneStepDecision(
            ACTION_ESCALATE,
            failure_class="autonomy",
            reason=reason,
            detail={**detail, "autonomy_gate": True},
        ), entry.updates
    return None, entry.updates


def _checked_lane_result(
    state: dict[str, Any],
    definition: FrozenDefinition,
    current: ActiveStep,
    message: Mapping[str, Any] | None,
    update: dict[str, Any],
) -> LaneStepDecision:
    """Share serial guardrails before applying the lane's result and boundary checks."""
    if message is not None and has_unknown_external_effect(current, message):
        return decide_lane_result(state, definition, current, message)
    if message is not None and message.get("status") == "pass":
        from core.application.orchestration.agents.common import emit_event

        verdict = check_step_guardrails(state, current, message, emit_event=emit_event)
        if verdict is not None:
            state.update(verdict.update)
            update.update(verdict.update)
            if verdict.action in {GUARDRAIL_RETRY, GUARDRAIL_ESCALATE}:
                return LaneStepDecision(
                    ACTION_RETRY if verdict.action == GUARDRAIL_RETRY else ACTION_ESCALATE,
                    failure_class="validation",
                    reason=verdict.reason,
                    feedback=verdict.feedback,
                    detail=verdict.detail,
                )
    return decide_lane_result(state, definition, current, message)


def _run_lane(
    *,
    definition: FrozenDefinition,
    frames: list[Frame],
    lane: LaneState,
    state: dict[str, Any],
) -> tuple[LaneState, list[dict[str, Any]], list[dict[str, Any] | None]]:
    """Walk one lane until it completes or stops; return its result rows."""
    node = frames[-1].node
    group_id = node.id if node.kind == "group" else None
    lane_state = copy.deepcopy(state)
    rows: list[dict[str, Any]] = []
    trace_facts: list[dict[str, Any] | None] = []
    feedback: str | None = None
    while lane.status in {LANE_PENDING, LANE_RUNNING} and lane.step_id:
        begin_lane_step(lane)
        current = lane_active_step(definition, frames, lane)
        position = lane.position
        override = lane.decision
        lane.decision = None
        executed = False
        skipped = False
        if step_is_disabled(current.step):
            # A disabled lane step does not run; the lane continues on its Next edge.
            skipped = True
            decision = _skipped_lane_decision(state, current)
        elif override and override.get("action") == "continue":
            # An operator approved the escalated step: it moves on along its
            # success edge, recorded as an operator-continued error.
            decision = LaneStepDecision(
                ACTION_ADVANCE,
                outcome=Outcome(OUTCOME_SUCCESS),
                detail={
                    "_step_error_policy": {
                        "on_error": "await_approval",
                        "applied": True,
                        "approved": True,
                    }
                },
            )
        else:
            if override and override.get("action") in {"retry", "authorize"}:
                feedback = str(override.get("feedback") or "").strip() or None
            if override and override.get("action") == "authorize":
                approvals = dict(lane_state.get("autonomy_gate_approvals") or {})
                approvals[current.step_execution_id] = True
                lane_state["autonomy_gate_approvals"] = approvals
            lane_state.update(
                {
                    "run_cursor": lane_cursor(frames, lane).dump(),
                    "current_step_id": lane.step_id,
                    "current_group_id": group_id,
                    "current_lane_id": lane.lane_id,
                    "feedback": feedback,
                }
            )
            decision, entry_updates = _prepare_lane_step(current, lane_state)
            if decision is None:
                executed = True
                publish_active_step_event(state, EVENT_STEP_STARTED, current)
                message, update = _execute_lane_step(current, lane_state)
                update = {**entry_updates, **update}
                if lane_state.get("autonomy_gate_approvals"):
                    update["autonomy_gate_approvals"] = dict(lane_state["autonomy_gate_approvals"])
                # Later steps of the same lane see this step's state changes.
                lane_state.update(update)
                decision = _checked_lane_result(lane_state, definition, current, message, update)
                rows.append(
                    {
                        "lane_id": lane.lane_id,
                        "lane_order": lane.lane_order,
                        "position": position,
                        "step_id": current.step_id,
                        "message": message,
                        "state_update": update,
                    }
                )
            feedback = None
        if executed or (override and override.get("action") == "continue"):
            lane.position += 1
        if not skipped:
            _publish_lane_decision(state, current, decision)
        if decision.action == ACTION_ADVANCE and decision.outcome is not None:
            if not skipped:
                lane.completed_steps += 1
            try:
                following = next_lane_step(
                    definition, node, lane.lane_id, str(lane.step_id), decision.outcome
                )
            except RunCursorError as exc:
                lane.status = LANE_FAILED
                lane.failure = _failure(current, "execution_contract", str(exc))
                publish_active_step_event(state, EVENT_STEP_FAILED, current, error=str(exc))
                break
            from core.application.orchestration.run_trace import edge_fact
            from core.application.workflows.execution_graph import NodeRef

            trace_facts.append(
                {
                    "position": position,
                    "lane_order": lane.lane_order,
                    "fact": edge_fact(
                        lane_state,
                        definition,
                        definition.graph.edge_for(
                            NodeRef("step", current.step_id), decision.outcome
                        ),
                        lane_cursor(frames, lane),
                    ),
                }
            )
            lane.step_execution_id = None
            lane.attempt = 1
            if following is None:
                lane.status = LANE_COMPLETED
                lane.step_id = None
            else:
                lane.step_id = following
        elif decision.action == ACTION_RETRY:
            if decision.backoff_seconds > 0:
                time.sleep(decision.backoff_seconds)
            lane.attempt += 1
            feedback = decision.feedback or None
        elif decision.action == ACTION_RETRY_OPERATION:
            lane.status = LANE_RETRY_OPERATION
            lane.operation = _operation_payload(decision, position)
        elif decision.action == ACTION_ESCALATE:
            lane.status = LANE_ESCALATED
            lane.failure = _failure(
                current, decision.failure_class, decision.reason, decision.detail
            )
        else:
            lane.status = LANE_FAILED
            lane.failure = _failure(
                current, decision.failure_class, decision.reason, decision.detail
            )
    return lane, rows, trace_facts


def _concurrency_cap(definition: FrozenDefinition, frame: Frame, state: Mapping[str, Any]) -> int:
    from core.application.orchestration.helpers import resolve_parallel_concurrency_cap

    body = definition.container(frame.node).get("body")
    limit = body.get("concurrency_limit") if isinstance(body, Mapping) else None
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        return limit
    return max(1, int(resolve_parallel_concurrency_cap(dict(state))))


def _apply_resume(lanes: list[LaneState], state: Mapping[str, Any]) -> bool:
    """Resume the one lane a checkpointed operation retry names; report whether it did."""
    dispatch = state.get("operation_retry_dispatch")
    if not isinstance(dispatch, Mapping):
        return False
    for lane in lanes:
        if (
            lane.status == LANE_RETRY_OPERATION
            and lane.lane_id == str(dispatch.get("lane_id") or "")
            and lane.step_execution_id == str(dispatch.get("step_execution_id") or "")
        ):
            lane.status = LANE_RUNNING
            lane.attempt = int(dispatch.get("attempt") or lane.attempt + 1)
            lane.operation = None
            return True
    return False


_ABSENT = object()


def _lane_change(base: Any, value: Any, merged: Any) -> Any:
    """*merged* after one lane's write *value*: only what the lane changed lands.

    Every lane starts from the same state, and a step update carries its whole
    top-level key: ``result`` with the lane's one new field, ``workflow_data``
    with everything, execution maps with every step's entry. Assigning it would
    erase what another lane wrote beside it, so a lane's mapping is compared with
    the baseline at every depth and only its changed fields are applied. Two
    lanes changing the same field keep the existing order: the later one wins.
    """
    if isinstance(value, Mapping):
        # A map no lane had before starts empty, so two lanes creating it both land.
        baseline = base if isinstance(base, Mapping) else {}
        target = dict(merged) if isinstance(merged, Mapping) else copy.deepcopy(dict(baseline))
        for key, item in value.items():
            before = baseline.get(key, _ABSENT)
            if before is not _ABSENT and before == item:
                continue
            target[key] = _lane_change(before, item, target.get(key, _ABSENT))
        return target
    return copy.deepcopy(value)


def _merge(
    state: Mapping[str, Any], rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge lane results by step position, then lane order (the existing order)."""
    merged_messages = [
        dict(msg) for msg in (state.get("agent_messages") or []) if isinstance(msg, dict)
    ]
    known = {
        (
            str(msg.get("step_execution_id") or ""),
            msg.get("attempt"),
            str(msg.get("correlation_id") or ""),
        )
        for msg in merged_messages
    }
    merged_updates: dict[str, Any] = {}
    for row in sorted(
        rows, key=lambda item: (int(item.get("position", 0)), int(item.get("lane_order", 0)))
    ):
        message = row.get("message")
        if isinstance(message, dict):
            message = copy.deepcopy(message)
            parallel = {"lane_id": row["lane_id"], "position": int(row.get("position") or 0)}
            artifacts = dict(message.get("artifacts") or {})
            artifacts["operation_parallel"] = parallel
            message["artifacts"] = artifacts
            if isinstance(message.get("operation_retry"), dict):
                message["operation_retry"] = {
                    **message["operation_retry"],
                    "operation_parallel": parallel,
                }
            marker = (
                str(message.get("step_execution_id") or ""),
                message.get("attempt"),
                str(message.get("correlation_id") or ""),
            )
            if marker not in known:
                merged_messages.append(message)
                known.add(marker)
        update = row.get("state_update")
        if isinstance(update, dict):
            for key, value in update.items():
                if key == "agent_messages" or key in _STEP_UPDATE_BLOCKLIST:
                    continue
                merged_updates[key] = _lane_change(
                    state.get(key, _ABSENT), value, merged_updates.get(key, _ABSENT)
                )
    return merged_messages, merged_updates


def parallel_executor_node(state: OrchestratorState) -> dict:
    from core.application.orchestration.agents.common import emit_event

    state_dict: dict[str, Any] = dict(state) if isinstance(state, Mapping) else {}
    definition = load_run_definition(state_dict)
    cursor = run_cursor(state_dict)
    if not cursor.frames:
        raise RunCursorError("The parallel executor runs only inside a container")
    frame = cursor.frames[-1]
    label = f"{frame.kind} '{frame.container_id}'"
    lanes = load_lanes(state_dict.get("parallel_lanes"))
    if not lanes:
        raise RunCursorError(f"The {label} has no lanes to run")
    resumed = _apply_resume(lanes, state_dict)
    active = [lane for lane in lanes if lane.status in {LANE_PENDING, LANE_RUNNING}]
    cap = _concurrency_cap(definition, frame, state_dict)
    emit_event(
        state_dict,
        "parallel_executor",
        "routing_decision",
        f"Executing the {label} across {len(active)} lane(s)",
        {
            "container": frame.node.dump(),
            "execution_id": frame.execution_id,
            "lanes": [lane.lane_id for lane in lanes],
            "concurrency_cap": cap,
        },
    )

    trace_incomplete = False
    results: dict[str, tuple[LaneState, list[dict[str, Any]], list[dict[str, Any] | None]]] = {}
    with ThreadPoolExecutor(max_workers=cap) as pool:
        future_map = {}
        for lane in active:
            future = pool.submit(
                copy_context().run,
                _run_lane,
                definition=definition,
                frames=list(cursor.frames),
                lane=lane,
                state=state_dict,
            )
            future_map[future] = lane
        for future in as_completed(future_map):
            lane = future_map[future]
            try:
                results[lane.lane_id] = future.result()
            except Exception as exc:
                logger.exception("parallel_executor lane '%s' future failed", lane.lane_id)
                lane.status = LANE_FAILED
                lane.failure = {
                    "step_id": lane.step_id,
                    "step_execution_id": lane.step_execution_id,
                    "attempt": lane.attempt,
                    "failure_class": "execution",
                    "reason": f"parallel lane future failed: {exc}",
                    "detail": {},
                }
                results[lane.lane_id] = (lane, [], [])
                trace_incomplete = True

    final_lanes = [results.get(lane.lane_id, (lane, [], []))[0] for lane in lanes]
    rows = [row for _lane, lane_rows, _facts in results.values() for row in lane_rows]
    from core.application.orchestration.run_trace import append_run_trace

    trace_rows = [row for lane in lanes for row in results.get(lane.lane_id, (lane, [], []))[2]]
    trace_facts = [
        row["fact"]
        for row in sorted(trace_rows, key=lambda row: (row["position"], row["lane_order"]))
    ]
    trace = append_run_trace(state_dict, trace_facts)
    if trace is not None and trace_incomplete:
        trace["truncated"] = True
    from core.application.orchestration.container_progress import (
        append_container_progress,
        container_patch,
    )

    progress = append_container_progress(
        state_dict,
        [container_patch(cursor, frame, lanes=[lane.dump() for lane in final_lanes])],
        {},
    )
    merged_messages, merged_updates = _merge(state_dict, rows)
    outcome = group_outcome(final_lanes)
    emit_event(
        state_dict,
        "parallel_executor",
        "group_complete",
        f"Lanes of the {label} stopped: {outcome}",
        {"container": frame.node.dump(), "outcome": outcome, "results": len(rows)},
        severity="info" if outcome == "completed" else "warn",
    )
    update: dict[str, Any] = {
        **merged_updates,
        "agent_messages": merged_messages,
        "parallel_lanes": [lane.dump() for lane in final_lanes],
        **({"workflow_run_trace": trace} if trace is not None else {}),
        **({"workflow_container_progress": progress} if progress is not None else {}),
        "parallel_concurrency_cap": cap,
        "parallel_outcome": {
            "container": frame.node.dump(),
            "execution_id": frame.execution_id,
            "status": outcome,
        },
    }
    if resumed:
        update["operation_retry_dispatch"] = None
    return update


__all__ = [
    "LANE_ESCALATED",
    "PARALLEL_STEP_FAILED_MESSAGE",
    "parallel_executor_node",
]
