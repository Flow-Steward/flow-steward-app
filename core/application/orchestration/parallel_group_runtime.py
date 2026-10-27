"""Lanes of a container: each lane walks its own steps with its own cursor.

A lanes body (a parallel Group, or the body of a For each whose items run lanes)
holds one sequence per lane, chained by the execution graph inside the lane
scope and closed by the lane's exit. Lanes never branch, wait or
stop (the validator refuses that), so a lane is a path along success edges and
explicit error edges. This module decides, without running anything, what one
lane step's result means; the parallel executor runs lanes concurrently and
the hub turns the group outcome into the group's continuation.

Lane semantics follow the former parallel executor and the step hub:
- a lane stops at its first failure that policy does not resolve; siblings
  already running finish before the group outcome is decided;
- a lane that succeeded is never re-run when a sibling is retried or approved;
- a technical retry keeps the step execution and raises its attempt; a new
  container execution (rework re-entry, next For each item) starts every lane afresh.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.orchestration.active_step import ActiveStep
from core.application.orchestration.boundary_contract_runtime import (
    exit_applies,
    missing_exit_outputs,
)
from core.application.orchestration.run_cursor import (
    Frame,
    FrozenDefinition,
    RunCursor,
    RunCursorError,
)
from core.application.orchestration.step_failures import (
    FAILED_STATUSES,
    effective_step_policy,
    infer_failure_class,
)
from core.application.workflows.execution_graph import (
    NODE_KIND_FOR_EACH,
    NODE_KIND_STEP,
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    ForEachExitRef,
    GroupExitRef,
    NodeRef,
    Outcome,
)
from core.application.workflows.workflow_definition_validation import (
    ERROR_POLICY_AWAIT_APPROVAL,
    ERROR_POLICY_CONTINUE,
    ERROR_POLICY_RETRY,
    ERROR_POLICY_ROUTE_TO_STEP,
)

LANE_PENDING = "pending"
LANE_RUNNING = "running"
LANE_COMPLETED = "completed"
LANE_FAILED = "failed"
LANE_ESCALATED = "escalated"
LANE_RETRY_OPERATION = "retry_operation"
LANE_FINAL_STATUSES = frozenset({LANE_COMPLETED, LANE_FAILED, LANE_ESCALATED, LANE_RETRY_OPERATION})

GROUP_COMPLETED = "completed"
GROUP_FAILED = "failed"
GROUP_ESCALATED = "escalated"
GROUP_RETRY_OPERATION = "retry_operation"

ACTION_ADVANCE = "advance"
ACTION_RETRY = "retry"
ACTION_ESCALATE = "escalate"
ACTION_FAIL = "fail"
ACTION_RETRY_OPERATION = "retry_operation"

#: Successful lane results that still carry contract errors are regenerated at
#: most this many times when the project sets no retry budget (as in the hub).
DEFAULT_CONTRACT_RETRY_BUDGET = 1


@dataclass
class LaneState:
    """One lane's cursor and status, persisted in ``parallel_lanes``."""

    lane_id: str
    lane_order: int
    status: str = LANE_PENDING
    step_id: str | None = None
    step_execution_id: str | None = None
    attempt: int = 1
    completed_steps: int = 0
    position: int = 0
    failure: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    operation: dict[str, Any] | None = None

    def dump(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "lane_order": self.lane_order,
            "status": self.status,
            "step_id": self.step_id,
            "step_execution_id": self.step_execution_id,
            "attempt": self.attempt,
            "completed_steps": self.completed_steps,
            "position": self.position,
            "failure": dict(self.failure) if self.failure is not None else None,
            "decision": dict(self.decision) if self.decision is not None else None,
            "operation": dict(self.operation) if self.operation is not None else None,
        }

    @classmethod
    def load(cls, raw: Mapping[str, Any]) -> LaneState:
        def _dict(key: str) -> dict[str, Any] | None:
            value = raw.get(key)
            return dict(value) if isinstance(value, Mapping) else None

        return cls(
            lane_id=str(raw.get("lane_id") or ""),
            lane_order=int(raw.get("lane_order") or 0),
            status=str(raw.get("status") or LANE_PENDING),
            step_id=str(raw.get("step_id")) if raw.get("step_id") else None,
            step_execution_id=(
                str(raw.get("step_execution_id")) if raw.get("step_execution_id") else None
            ),
            attempt=int(raw.get("attempt") or 1),
            completed_steps=int(raw.get("completed_steps") or 0),
            position=int(raw.get("position") or 0),
            failure=_dict("failure"),
            decision=_dict("decision"),
            operation=_dict("operation"),
        )


@dataclass(frozen=True)
class LaneStepDecision:
    action: str
    outcome: Outcome | None = None
    failure_class: str = ""
    reason: str = ""
    feedback: str = ""
    backoff_seconds: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    operation: Any = None


def initial_lanes(definition: FrozenDefinition, node: NodeRef) -> list[LaneState]:
    """Every lane of a fresh container execution, pending at its entry step."""
    return [
        LaneState(
            lane_id=lane_id, lane_order=order, step_id=definition.body_entry(node, lane_id).id
        )
        for order, lane_id in enumerate(definition.lane_ids(node))
    ]


def load_lanes(raw: Any) -> list[LaneState]:
    rows = raw if isinstance(raw, list) else []
    return [LaneState.load(row) for row in rows if isinstance(row, Mapping) and row.get("lane_id")]


def begin_lane_step(lane: LaneState) -> LaneState:
    """Give the lane's current step a logical execution if it has none yet."""
    if not lane.step_execution_id:
        lane.step_execution_id = str(uuid.uuid4())
        lane.attempt = 1
    lane.status = LANE_RUNNING
    return lane


def lane_cursor(frames: list[Frame], lane: LaneState) -> RunCursor:
    """The cursor a lane step executes under: the container stack and the lane's step."""
    return RunCursor(
        step_id=lane.step_id,
        step_execution_id=lane.step_execution_id,
        attempt=lane.attempt,
        frames=list(frames),
    )


def lane_active_step(
    definition: FrozenDefinition, frames: list[Frame], lane: LaneState
) -> ActiveStep:
    return ActiveStep(
        step=dict(definition.step(str(lane.step_id))),
        step_id=str(lane.step_id),
        step_execution_id=str(lane.step_execution_id),
        attempt=lane.attempt,
        frames=tuple(frames),
    )


def _owner_key(node: NodeRef) -> str:
    return "for_each_id" if node.kind == NODE_KIND_FOR_EACH else "group_id"


def next_lane_step(
    definition: FrozenDefinition, node: NodeRef, lane_id: str, step_id: str, outcome: Outcome
) -> str | None:
    """The lane's next step for *outcome*, or None when the lane reached its exit."""
    edge = definition.graph.edge_for(NodeRef(NODE_KIND_STEP, step_id), outcome)
    if edge is None:
        raise RunCursorError(f"Lane step '{step_id}' has no edge for outcome '{outcome.name}'")
    target = edge.target
    if isinstance(target, GroupExitRef | ForEachExitRef):
        owner = target.group_id if isinstance(target, GroupExitRef) else target.for_each_id
        kind = "group" if isinstance(target, GroupExitRef) else NODE_KIND_FOR_EACH
        if (kind, owner, target.lane_id) != (node.kind, node.id, lane_id):
            raise RunCursorError(f"Lane step '{step_id}' exits another lane or container")
        return None
    if target.kind != NODE_KIND_STEP:
        raise RunCursorError(f"Lane step '{step_id}' leads out of its lane")
    step = definition.step(target.id)
    if (
        str(step.get(_owner_key(node)) or "") != node.id
        or str(step.get("lane_id") or "") != lane_id
    ):
        raise RunCursorError(f"Lane step '{step_id}' leads into another lane")
    return target.id


def _policy_int(state: Mapping[str, Any], key: str, default: int) -> int:
    policy = state.get("project_policy")
    if not isinstance(policy, Mapping):
        config = state.get("project_config")
        policy = config.get("project_policy") if isinstance(config, Mapping) else None
    raw = policy.get(key, default) if isinstance(policy, Mapping) else default
    if isinstance(raw, bool) or raw in (None, ""):
        return default
    try:
        return max(0, int(str(raw)))
    except (TypeError, ValueError):
        return default


def _contract_issues(message: Mapping[str, Any], *, include_errors: bool) -> list[str]:
    artifacts = message.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    issues: list[str] = []
    raws = [*(artifacts.get("_schema_errors") or []), *(artifacts.get("_contract_errors") or [])]
    if include_errors:
        raws.extend(message.get("errors") or [])
    for raw in raws:
        text = str(raw or "").strip()
        if text and text not in issues:
            issues.append(text)
    return issues


def decide_lane_result(
    state: Mapping[str, Any],
    definition: FrozenDefinition,
    current: ActiveStep,
    message: Mapping[str, Any] | None,
    *,
    terminal_reason: str = "",
) -> LaneStepDecision:
    """What one lane step's accepted result means, in the hub's order.

    Failed host transport uses its technical verdict and authored policy;
    other results retain contract health before the ordinary retry/policy path.
    """
    from core.application.orchestration.models import normalize_phase_status
    from core.application.orchestration.step_operation_retry import (
        ACTION_RETRY_OPERATION as OPERATION_RETRY,
    )
    from core.application.orchestration.step_operation_retry import (
        decide_operation_retry,
        has_unknown_external_effect,
        trusted_operation_retry_signal,
    )

    if message is None:
        return LaneStepDecision(
            ACTION_FAIL,
            failure_class="execution_contract",
            reason="The lane step returned without a result.",
        )
    unknown = has_unknown_external_effect(current, message)
    signal = trusted_operation_retry_signal(current, message)
    transport = unknown or bool(signal)
    status = "fail" if unknown else normalize_phase_status(message.get("status"))
    budget = _policy_int(state, "retry_max_attempts", DEFAULT_CONTRACT_RETRY_BUDGET)
    issues = (
        []
        if transport and status != "pass"
        else _contract_issues(message, include_errors=status == "pass")
    )
    if status == "pass" and exit_applies(current.step, Outcome(OUTCOME_SUCCESS)):
        issues.extend(
            f"Missing required output: {key}"
            for key in missing_exit_outputs(current.step, state, [message])
        )
    if issues:
        if current.attempt > budget:
            return LaneStepDecision(
                ACTION_FAIL,
                failure_class="contract",
                reason=f"Contract errors after {budget} retries: {'; '.join(issues[:6])}",
            )
        return LaneStepDecision(
            ACTION_RETRY,
            failure_class="contract",
            reason="contract_health_retry",
            feedback=(
                "Contract-health gate blocked progression for this step.\n"
                "The previous output has schema/contract issues and must be regenerated.\n"
                f"Issues: {'; '.join(issues[:6])}"
            ),
        )
    if status == "pass":
        return LaneStepDecision(ACTION_ADVANCE, outcome=Outcome(OUTCOME_SUCCESS))
    if status not in FAILED_STATUSES:
        status = "fail"
    operation = None if unknown else decide_operation_retry(state, current, message)
    if operation is not None:
        if operation.action == OPERATION_RETRY and not terminal_reason:
            return LaneStepDecision(
                ACTION_RETRY_OPERATION, operation=operation, reason=operation.reason
            )
        if not transport:
            return LaneStepDecision(
                ACTION_ESCALATE,
                reason=operation.reason,
                failure_class=operation.failure_class,
                detail={"retry_reason_code": operation.reason_code, **operation.telemetry},
            )
    failure_class, _retryable = infer_failure_class(message)
    if unknown:
        refused = "external_effect_ambiguous"
    elif terminal_reason:
        refused = terminal_reason
    elif operation is not None:
        refused = operation.reason
    else:
        refused = "host_operation_not_retryable"
    if transport:
        failure_class = str(
            operation.failure_class
            if operation is not None
            else signal.get("failure_class") or "unknown"
        )
    policy = effective_step_policy(current.step, definition.step_containers(current.step_id))
    detail = {
        "on_error": policy.on_error,
        "policy_source": policy.source,
        "original_status": status,
    }
    if transport and operation is not None:
        detail.update(
            {"retry_reason_code": terminal_reason or operation.reason_code, **operation.telemetry}
        )
    if policy.on_error == ERROR_POLICY_CONTINUE:
        return LaneStepDecision(
            ACTION_ADVANCE,
            outcome=Outcome(OUTCOME_SUCCESS),
            detail={
                "_step_error_policy": {
                    "on_error": "continue",
                    "applied": True,
                    "original_status": status,
                }
            },
        )
    if policy.on_error == ERROR_POLICY_RETRY and transport:
        return LaneStepDecision(
            ACTION_FAIL, failure_class=failure_class, reason=refused, detail=detail
        )
    if policy.on_error == ERROR_POLICY_RETRY:
        if not policy.retry_matches(failure_class):
            return LaneStepDecision(
                ACTION_ESCALATE,
                failure_class=failure_class,
                reason=f"step error_policy.retry_on does not match failure class ('{failure_class}')",
                detail=detail,
            )
        if policy.max_retries > 0 and current.attempt > policy.max_retries:
            return LaneStepDecision(
                ACTION_ESCALATE,
                failure_class=failure_class,
                reason=f"step error_policy.max_retries exhausted ({policy.max_retries})",
                detail=detail,
            )
        return LaneStepDecision(
            ACTION_RETRY,
            failure_class=failure_class,
            reason="error_policy_retry",
            feedback=str(message.get("summary") or "").strip(),
            backoff_seconds=policy.backoff_seconds,
        )
    if policy.on_error == ERROR_POLICY_ROUTE_TO_STEP and policy.source == "step":
        return LaneStepDecision(ACTION_ADVANCE, outcome=Outcome(OUTCOME_ERROR), detail=detail)
    if policy.on_error == ERROR_POLICY_AWAIT_APPROVAL:
        if unknown:
            return LaneStepDecision(
                ACTION_FAIL,
                failure_class="execution_contract",
                reason="external_effect_resolution_unsupported: parallel lane cannot resolve one exact effect",
                detail=detail,
            )
        return LaneStepDecision(
            ACTION_ESCALATE,
            failure_class=failure_class,
            reason="step error_policy.on_error=await_approval",
            detail=detail,
        )
    # fail, or route_to_step inherited from a container: the lane stops and the
    # container decides once its siblings have finished.
    return LaneStepDecision(
        ACTION_FAIL,
        failure_class=failure_class,
        reason=str(message.get("summary") or "").strip() or "Lane step failed.",
        detail=detail,
    )


def group_outcome(lanes: list[LaneState]) -> str:
    """The group's result once every lane has stopped.

    A failure decides the group; a pending technical retry runs before an
    operator is asked; an escalation asks once for the whole group.
    """
    statuses = {lane.status for lane in lanes}
    if LANE_FAILED in statuses:
        return GROUP_FAILED
    if LANE_RETRY_OPERATION in statuses:
        return GROUP_RETRY_OPERATION
    if LANE_ESCALATED in statuses:
        return GROUP_ESCALATED
    return GROUP_COMPLETED


__all__ = [
    "ACTION_ADVANCE",
    "ACTION_ESCALATE",
    "ACTION_FAIL",
    "ACTION_RETRY",
    "ACTION_RETRY_OPERATION",
    "GROUP_COMPLETED",
    "GROUP_ESCALATED",
    "GROUP_FAILED",
    "GROUP_RETRY_OPERATION",
    "LANE_COMPLETED",
    "LANE_ESCALATED",
    "LANE_FAILED",
    "LANE_FINAL_STATUSES",
    "LANE_PENDING",
    "LANE_RETRY_OPERATION",
    "LANE_RUNNING",
    "LaneState",
    "LaneStepDecision",
    "begin_lane_step",
    "decide_lane_result",
    "group_outcome",
    "initial_lanes",
    "lane_active_step",
    "lane_cursor",
    "load_lanes",
    "next_lane_step",
]
