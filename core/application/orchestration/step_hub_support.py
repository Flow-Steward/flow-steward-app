"""Shared, phase-free building blocks of the step hub.

The hub routes one cursor through the pinned definition. These helpers name the
executor node of a step, build journal entries and the state slices the hub
writes on every routing decision, and carry review feedback forward exactly as
the previous router did.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.orchestration.active_step import ActiveStep, message_matches_execution
from core.application.orchestration.models import normalize_phase_status
from core.application.orchestration.run_cursor import (
    RUNTIME_SCHEMA_VERSION,
    RunCursor,
    RunCursorError,
)
from core.application.orchestration.step_failures import (
    normalize_failure_class,
)
from core.application.workflows.execution_graph import (
    OUTCOME_CASE,
    OUTCOME_ERROR,
    OUTCOME_NAMES,
    OUTCOME_SUCCESS,
    Outcome,
)
from core.application.workflows.workflow_definition_validation import step_can_be_disabled
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_AGENT,
    STEP_KIND_APPROVAL,
    STEP_KIND_CONNECTOR,
    STEP_KIND_FILE_FROM_BASE64,
    STEP_KIND_HTTP_REQUEST,
    STEP_KIND_INVOKE_WORKFLOW,
    STEP_KIND_TABULAR_FEED,
    STEP_KIND_TEMPLATE,
    STEP_KIND_TRANSFORM,
    STEP_KIND_WAIT,
    is_control_flow_step_kind,
    normalize_step_kind,
    runtime_actor_for_step,
)

HUB_NODE = "orchestrator"
VALIDATION_NODE = "orchestrator_validation"
APPROVAL_NODE = "human_approval_gate"
OPERATION_RETRY_NODE = "operation_retry_wait"
PARALLEL_NODE = "parallel_executor"
CONTROL_FLOW_NODE = "control_flow_step"
INVOKE_NODE = "invoke_workflow_step"
EXECUTE_STEP_NODE = "execute_step"
WAIT_NODE = "wait_step"

MAX_TOTAL_ITERATIONS = 50

_EXECUTE_STEP_KINDS = frozenset(
    {
        STEP_KIND_CONNECTOR,
        STEP_KIND_FILE_FROM_BASE64,
        STEP_KIND_HTTP_REQUEST,
        STEP_KIND_TABULAR_FEED,
        STEP_KIND_TEMPLATE,
        STEP_KIND_TRANSFORM,
    }
)


#: Journal event of a step the run did not execute because its author disabled it.
STEP_SKIPPED_EVENT = "step_skipped"
STEP_SKIPPED_REASON = "disabled"


def step_is_disabled(step: Mapping[str, Any]) -> bool:
    """Whether the author disabled *step*: a run skips it and continues on its Next edge."""
    return step.get("disabled") is True


def disabled_step_can_be_skipped(step: Mapping[str, Any]) -> bool:
    """Whether a disabled *step* has the single Next outcome a skip continues on.

    The validator refuses any other disabled step, so a pinned definition never
    holds one; the runtime still does not guess a branch for it.
    """
    kind = (
        normalize_step_kind(step.get("step_kind"), default="", preserve_unknown=True)
        or STEP_KIND_AGENT
    )
    override = step.get("override")
    config = override.get(kind) if isinstance(override, Mapping) else None
    return step_can_be_disabled(kind, config if isinstance(config, Mapping) else {})


class UnsupportedStepKindError(RuntimeError):
    """The pinned step has no executor in this runtime build."""


def executor_node_for_step(step: Mapping[str, Any]) -> str:
    """The graph node that executes *step*; never a guess from sort order."""
    kind = normalize_step_kind(step.get("step_kind"), default="", preserve_unknown=True)
    if is_control_flow_step_kind(kind):
        return CONTROL_FLOW_NODE
    if kind == STEP_KIND_INVOKE_WORKFLOW:
        return INVOKE_NODE
    if kind in _EXECUTE_STEP_KINDS:
        return EXECUTE_STEP_NODE
    if kind == STEP_KIND_APPROVAL:
        return APPROVAL_NODE
    if kind == STEP_KIND_WAIT:
        return WAIT_NODE
    node = runtime_actor_for_step(step, default="")
    if not node:
        raise UnsupportedStepKindError(
            f"Step '{step.get('step_id')}' of kind '{kind or 'unknown'}' has no executor"
        )
    return node


def journal_entry(
    *,
    iteration: int,
    event: str,
    message: str,
    step_id: str = "",
    action: str = "",
    reason: str = "",
    detail: Mapping[str, Any] | None = None,
    severity: str = "info",
) -> dict[str, Any]:
    from core.application.orchestration.journal import make_journal_entry

    return make_journal_entry(
        iteration=iteration,
        event=event,
        step_id=step_id,
        action=action,
        reason=reason,
        message=message,
        detail=dict(detail or {}),
        severity=severity,
    )


def cursor_update(cursor: RunCursor) -> dict[str, Any]:
    """The state slice that records where the run is."""
    group = next(
        (frame.container_id for frame in reversed(cursor.frames) if frame.kind == "group"), None
    )
    return {
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "run_cursor": cursor.dump(),
        "current_step_id": cursor.step_id,
        "current_group_id": group,
    }


def retry_continuation_updates() -> dict[str, Any]:
    """Clear stale terminal/approval state when work resumes."""
    return {
        "status": "in_progress",
        "error": None,
        "final_report": None,
        "needs_user_approval": False,
        "approval_context": None,
    }


def _text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _workflow_data_text(state: Mapping[str, Any], key: str) -> str | None:
    workflow_data = state.get("workflow_data")
    if isinstance(workflow_data, Mapping):
        return _text_or_none(workflow_data.get(key))
    return None


def carry_forward_review_feedback(state: Mapping[str, Any]) -> Any:
    if state.get("user_approved") is False:
        return None
    feedback = state.get("review_feedback")
    text = _text_or_none(feedback) or _workflow_data_text(state, "review_feedback")
    if text:
        return text
    return feedback if feedback is not None and state.get("user_approved") is True else None


def carry_forward_reviewer_comments(state: Mapping[str, Any]) -> Any:
    if state.get("user_approved") is False:
        return None
    return (
        _text_or_none(state.get("reviewer_comments"))
        or _workflow_data_text(state, "reviewer_comments")
        or carry_forward_review_feedback(state)
    )


def carry_forward_risk_notes(state: Mapping[str, Any]) -> Any:
    if state.get("user_approved") is False:
        return None
    text = _text_or_none(state.get("risk_notes")) or _workflow_data_text(state, "risk_notes")
    if text:
        return text
    feedback = carry_forward_review_feedback(state)
    if feedback:
        return feedback
    if state.get("user_approved") is True:
        return "Approved without additional risk notes."
    return None


def graph_end_node() -> str:
    try:
        from langgraph.graph import END

        return str(END)
    except Exception:
        return "__end__"


__all__ = [
    "APPROVAL_NODE",
    "CONTROL_FLOW_NODE",
    "EXECUTE_STEP_NODE",
    "HUB_NODE",
    "INVOKE_NODE",
    "MAX_TOTAL_ITERATIONS",
    "OPERATION_RETRY_NODE",
    "PARALLEL_NODE",
    "STEP_SKIPPED_EVENT",
    "STEP_SKIPPED_REASON",
    "VALIDATION_NODE",
    "UnsupportedStepKindError",
    "carry_forward_review_feedback",
    "carry_forward_reviewer_comments",
    "carry_forward_risk_notes",
    "cursor_update",
    "disabled_step_can_be_skipped",
    "executor_node_for_step",
    "graph_end_node",
    "journal_entry",
    "retry_continuation_updates",
    "step_is_disabled",
]


#: failure_summary classifies a run ended by Stop Fail by this prefix.
STOPPED_BY_WORKFLOW_PREFIX = "Stopped by the workflow: "

RECOVERY_FAILURE = "failure"
RECOVERY_REWORK_BUDGET = "rework_budget"
RECOVERY_AUTONOMY = "autonomy"
RECOVERY_LANE = "lane_failure"
RECOVERY_GROUP_CONTRACT = "group_contract"

#: Successful results that still carry schema/contract errors are regenerated
#: at most this many times when the project sets no retry budget.
DEFAULT_CONTRACT_RETRY_BUDGET = 1


def _project_policy(state: Mapping[str, Any]) -> Mapping[str, Any]:
    policy = state.get("project_policy")
    if isinstance(policy, Mapping):
        return policy
    config = state.get("project_config")
    nested = config.get("project_policy") if isinstance(config, Mapping) else None
    return nested if isinstance(nested, Mapping) else {}


def _policy_int(state: Mapping[str, Any], key: str, default: int) -> int:
    raw = _project_policy(state).get(key, default)
    if isinstance(raw, bool) or raw in (None, ""):
        return default
    try:
        return max(0, int(str(raw)))
    except (TypeError, ValueError):
        return default


def _retry_backoff_seconds(
    state: Mapping[str, Any], *, failure_class: str, retryable: bool | None, attempt: int
) -> int:
    """The project retry backoff for transient/provider failures (existing formula)."""
    if (
        normalize_failure_class(failure_class) not in {"transient", "provider"}
        or retryable is False
    ):
        return 0
    strategy = str(_project_policy(state).get("retry_strategy") or "bounded").strip().lower()
    if strategy not in {"failure_class", "bounded", "flat"}:
        return 0
    initial = _policy_int(state, "retry_backoff_initial_seconds", 2) or 2
    maximum = _policy_int(state, "retry_backoff_max_seconds", 120) or 120
    if maximum and initial > maximum:
        initial = maximum
    if strategy == "flat":
        return min(maximum, initial) if maximum > 0 else initial
    delay = initial * (2 ** max(0, attempt - 1))
    return max(0, int(min(delay, maximum) if maximum > 0 else delay))


def _structured_contract_issues(message: Mapping[str, Any]) -> list[str]:
    """Schema/contract errors the runtime attached to a result, whatever its status."""
    artifacts = message.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    issues: list[str] = []
    for raw in (
        *(artifacts.get("_schema_errors") or []),
        *(artifacts.get("_contract_errors") or []),
    ):
        text = str(raw or "").strip()
        if text and text not in issues:
            issues.append(text)
    return issues


def _contract_issues(message: Mapping[str, Any]) -> list[str]:
    """Everything that makes a nominally successful result unacceptable."""
    artifacts = message.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    issues = _structured_contract_issues(message)
    for raw in message.get("errors") or []:
        text = str(raw or "").strip()
        if text and text not in issues:
            issues.append(text)
    phase_result = artifacts.get("phase_result_v2")
    if isinstance(phase_result, Mapping):
        status = normalize_phase_status(phase_result.get("status"), default="")
        if status and status != "pass":
            issues.append(f"result status={phase_result.get('status')} (expected 'pass')")
    return issues


def result_outcome(message: Mapping[str, Any]) -> Outcome:
    """The authored outcome a successful result reports; ``success`` by default."""
    artifacts = message.get("artifacts")
    raw = artifacts.get("_outcome") if isinstance(artifacts, Mapping) else None
    if not isinstance(raw, Mapping):
        return Outcome(OUTCOME_SUCCESS)
    name = str(raw.get("name") or "")
    if name not in OUTCOME_NAMES or name == OUTCOME_ERROR:
        raise RunCursorError(f"Step result reports an unsupported outcome '{name}'")
    case_id = raw.get("case_id")
    if name == OUTCOME_CASE and not case_id:
        raise RunCursorError("A case outcome must name its case_id")
    return Outcome(name, str(case_id) if name == OUTCOME_CASE else None)


def _boundary(owner: Mapping[str, Any]) -> Mapping[str, Any]:
    contract = owner.get("boundary_contract")
    return contract if isinstance(contract, Mapping) else {}


def accepted_message(state: Mapping[str, Any], current: ActiveStep) -> dict[str, Any] | None:
    """The latest result of exactly this execution and attempt, if any."""
    from core.application.orchestration.agent_messages import get_state_messages

    for message in reversed(get_state_messages(state)):
        if message_matches_execution(message, current):
            return message
    return None
