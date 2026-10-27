"""Human approval node of the registry graph.

The node serves exactly the pauses of the active step execution, never a phase:

* a recovery approval the hub opened (``state["recovery_pending"]``) for a failed
  execution, an exhausted rework budget or a pre-step autonomy policy; the node
  records the decision and the hub resumes the recovery;
* an authored Approval step; the node records the decision as that execution's
  step message with an ``approved``/``rejected`` outcome, and the hub follows the
  execution graph edge for it.

Test runs pause by state instead of a LangGraph interrupt: the run parks at
validation with ``awaiting_approval`` and the decision comes back as
``user_approved``/``review_feedback`` in the state.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.application.orchestration.active_step import (
    step_message_fields,
)
from core.application.orchestration.approval_payload import (
    APPROVAL_NODE,
    _approval_config,
    _content_to_text,
    _extract_artifact_text,
    _Gate,
    _is_meaningful_content,
    _latest_message_artifact_text,
    _latest_message_artifacts,
    _pick_gate_content,
    _resolve_gate,
    approval_id_for_execution,
)
from core.application.orchestration.step_templating import set_state_path
from core.application.orchestration.task_envelope import TEST_RUN_SOURCES
from core.application.workflows.workflow_step_kinds import STEP_KIND_APPROVAL

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState

HUB_NODE = "orchestrator"
VALIDATION_NODE = "orchestrator_validation"

DEFAULT_REJECT_FEEDBACK = "Please revise and resubmit for approval."


def _journal(
    state: Mapping[str, Any],
    gate: _Gate,
    *,
    approved: bool,
    action: str,
    feedback: str,
    route: str,
    override_content: str = "",
) -> dict[str, Any]:
    from core.application.orchestration.journal import make_journal_entry

    try:
        iteration = int(state.get("iteration_count") or 0)
    except (TypeError, ValueError):
        iteration = 0
    detail: dict[str, Any] = {
        "approved": approved,
        "approval_action": action,
        "operator_comment": feedback,
        "override_content_provided": bool(override_content),
        "target": HUB_NODE,
        "gate_kind": gate.kind,
    }
    if gate.approval_id:
        detail["approval_id"] = gate.approval_id
    if gate.pending:
        detail["request_id"] = gate.pending.get("request_id")
    return make_journal_entry(
        iteration=iteration,
        event="approval_resume_decision",
        step_id=gate.step_id,
        action="resume",
        reason=f"operator_{action}",
        message=f"Operator approval decision accepted; resuming workflow via {route}.",
        detail=detail,
        severity="info",
    )


def _decision_message(
    state: Mapping[str, Any],
    gate: _Gate,
    *,
    approved: bool,
    feedback: str,
    state_updates: dict[str, Any],
) -> dict[str, Any]:
    """The authored Approval step's result: one message of this execution.

    The result is kept whether or not the step maps it; an authored
    ``output_mapping`` copies its outputs to state with the step output mapper.
    """
    from core.application.orchestration.agent_messages import build_agent_message
    from core.application.orchestration.agents.common import publish_agent_message
    from core.application.orchestration.graph.nodes_contracts import apply_step_output_mapping

    decision = "approved" if approved else "rejected"
    timestamp = datetime.now(UTC).isoformat()
    artifacts: dict[str, Any] = {
        "step_kind": STEP_KIND_APPROVAL,
        "step_id": gate.step_id,
        "decision": decision,
        "approved": approved,
        "feedback": feedback,
        "approval_id": gate.approval_id,
        "_outcome": {"name": decision},
    }
    output_mapping = (
        _approval_config(gate.current).get("output_mapping") if gate.current is not None else None
    )
    if isinstance(output_mapping, Mapping):
        apply_step_output_mapping(
            output_mapping=output_mapping,
            artifacts=artifacts,
            state=state,
            state_updates=state_updates,
        )
    message = build_agent_message(
        from_agent=APPROVAL_NODE,
        to_agent=HUB_NODE,
        skill="approval",
        status="pass",
        correlation_id=uuid.uuid4().hex,
        caused_by=[],
        artifacts=artifacts,
        summary=f"Approval step '{gate.step_id}' {decision}",
        errors=[],
        started_at=timestamp,
        completed_at=timestamp,
        published_at=timestamp,
        **step_message_fields(state),
    )
    publish_agent_message(dict(state), message)
    return message


def _decide(
    state: Mapping[str, Any],
    gate: _Gate,
    *,
    approved: bool,
    action: str,
    comment: str,
    override_content: str = "",
    route: str,
    prefer_state_notes: bool = False,
    resolution_ref: Any = None,
):
    from langgraph.types import Command

    from core.application.orchestration.external_effect_recovery import (
        CARRIER,
        checked_gate_resolution_ref,
    )

    resolution_ref = checked_gate_resolution_ref(state, resolution_ref, approved=approved)
    feedback = comment.strip()
    if not approved and not feedback:
        feedback = DEFAULT_REJECT_FEEDBACK
    approved_feedback = (feedback or "Approved.") if approved else ""
    update: dict[str, Any] = {
        "needs_user_approval": False,
        CARRIER: resolution_ref,
        "approval_context": None,
        "review_feedback": approved_feedback if approved else feedback,
        "status": "in_progress",
        "orchestrator_journal": [
            _journal(
                state,
                gate,
                approved=approved,
                action=action,
                feedback=feedback,
                route=route,
                override_content=override_content,
            )
        ],
    }
    if approved:
        reviewer_comments = str(state.get("reviewer_comments") or "").strip()
        risk_notes = str(state.get("risk_notes") or "").strip()
        update["reviewer_comments"] = (
            reviewer_comments if prefer_state_notes and reviewer_comments else approved_feedback
        )
        update["risk_notes"] = (
            risk_notes
            if prefer_state_notes and risk_notes
            else feedback or "Approved without additional risk notes."
        )
    else:
        update.update(
            {
                "last_failure_class": "approval_rejected",
                "last_retry_reason": feedback,
                "last_retry_retryable": True,
                "retry_backoff_seconds": 0,
            }
        )
    if gate.authored:
        # The decision is the step's result; the graph edge for its outcome picks
        # what runs next. ``user_approved`` stays clear so no retry router acts
        # on it and the rejection feedback reaches the rework target.
        mapped_state: dict[str, Any] = {}
        message = _decision_message(
            state,
            gate,
            approved=approved,
            feedback=approved_feedback if approved else feedback,
            state_updates=mapped_state,
        )
        update.update(mapped_state)
        update["user_approved"] = None
        update["pending_wait"] = None
        update["agent_messages"] = [*(state.get("agent_messages") or []), message]
        if approved and override_content:
            for target_key in gate.override_targets:
                set_state_path(
                    update,
                    dict(state),
                    target_key,
                    override_content,
                    mirror_workflow_data=False,
                )
    else:
        update["user_approved"] = approved
    return Command(update=update, goto=HUB_NODE)


def _abort(comment: str):
    from langgraph.types import Command

    return Command(
        update={
            "user_approved": False,
            "needs_user_approval": False,
            "review_feedback": comment or "Job aborted by user.",
            "status": "cancelled",
            "external_effect_resolution_ref": None,
        },
        goto=HUB_NODE,
    )


def _pause_test_run(gate: _Gate):
    from langgraph.types import Command

    message = str(gate.payload.get("message") or "").strip()
    return Command(
        update={
            "user_approved": None,
            "needs_user_approval": True,
            "approval_context": message or "Workflow test run is waiting for human approval.",
            "review_feedback": None,
            "status": "awaiting_approval",
        },
        goto=VALIDATION_NODE,
    )


def _registry_approval_gate_node(state: OrchestratorState):
    """Pause the active step execution for a human decision, then return to the hub."""
    from langgraph.types import interrupt

    gate = _resolve_gate(state)
    task_source = str(state.get("task_source") or "").strip().lower()

    if task_source in TEST_RUN_SOURCES:
        decision = state.get("user_approved")
        if decision is None or (decision is False and not gate.allows("reject")):
            return _pause_test_run(gate)
        approved = bool(decision)
        return _decide(
            state,
            gate,
            approved=approved,
            action="approve" if approved else "reject",
            comment=str(state.get("review_feedback") or ""),
            route="workflow test run state",
            prefer_state_notes=True,
            resolution_ref=state.get("external_effect_resolution_ref"),
        )

    while True:
        response = interrupt(gate.payload)
        response_dict = response if isinstance(response, dict) else {}
        action = str(response_dict.get("action") or "").strip().lower()
        if action == "abort":
            return _abort(str(response_dict.get("comment") or "").strip())
        approved = bool(response_dict.get("approved", False)) if response_dict else bool(response)
        if action == "override" or (
            action == "skip" and state.get("external_effect_resolution_ref") is None
        ):
            approved = True
        if approved or gate.allows("reject"):
            break
        # A reject this gate does not offer (an exhausted rework budget) is not
        # a decision; ask again instead of acting on it.

    return _decide(
        state,
        gate,
        approved=approved,
        action=action or ("approve" if approved else "reject"),
        comment=str(response_dict.get("comment") or ""),
        override_content=str(response_dict.get("override_content") or "").strip(),
        route="interrupt resume",
        resolution_ref=response_dict.get("external_effect_resolution_ref"),
    )


__all__ = [
    "APPROVAL_NODE",
    "_content_to_text",
    "_extract_artifact_text",
    "_is_meaningful_content",
    "_latest_message_artifact_text",
    "_latest_message_artifacts",
    "_pick_gate_content",
    "_registry_approval_gate_node",
    "approval_id_for_execution",
]
