"""
Generic orchestrator final validation.

This node intentionally avoids platform-owned SDLC evidence semantics.
It validates only:
1. the workflow reached an authored terminal success step, as recorded by the
   step hub in ``workflow_terminal`` (or a step test run's isolated terminal)
2. the selected workflow's output contract validates against current state
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.application.orchestration.active_step import run_cursor
from core.application.workflows.workflow_step_kinds import STEP_KIND_STOP_SUCCESS

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState

logger = logging.getLogger(__name__)

TERMINAL_SUCCESS_STATUSES = {"pass"}


def _successful_terminal_proof(state: OrchestratorState) -> dict:
    """The Stop Success the hub recorded when it accepted that step's result."""
    terminal = state.get("workflow_terminal")
    if not isinstance(terminal, dict):
        return {}
    kind = str(terminal.get("kind") or "").strip().lower()
    status = str(terminal.get("status") or "").strip().lower()
    step_id = str(terminal.get("step_id") or "").strip()
    step_execution_id = str(terminal.get("step_execution_id") or "").strip()
    if (
        kind != STEP_KIND_STOP_SUCCESS
        or status not in TERMINAL_SUCCESS_STATUSES
        or not step_id
        or not step_execution_id
    ):
        return {}
    return {
        "source": "state.workflow_terminal",
        "kind": kind,
        "step_id": step_id,
        "step_execution_id": step_execution_id,
    }


def _successful_step_test_run_proof(state: OrchestratorState) -> dict:
    if str(state.get("task_source") or "").strip().lower() != "step_test_run":
        return {}
    terminal = state.get("step_test_run_terminal")
    if not isinstance(terminal, dict):
        return {}
    if str(terminal.get("status") or "").strip().lower() not in TERMINAL_SUCCESS_STATUSES:
        return {}
    step_id = str(terminal.get("step_id") or "").strip()
    if not step_id:
        return {}
    context = state.get("step_test_run_context")
    if not isinstance(context, dict):
        return {}
    if str(context.get("workflow_id") or "").strip() != str(state.get("workflow_id") or "").strip():
        return {}
    if str(context.get("step_id") or "").strip() != step_id:
        return {}
    execution_id = str(terminal.get("execution_id") or "").strip()
    if not execution_id:
        return {}
    if str(context.get("execution_id") or "").strip() != execution_id:
        return {}
    if str(state.get("workflow_test_run_execution_id") or "").strip() != execution_id:
        return {}
    return {
        "source": "state.step_test_run_terminal",
        "status": "pass",
        "step_id": step_id,
        "step_execution_id": str(terminal.get("step_execution_id") or "").strip(),
        "step_kind": str(terminal.get("step_kind") or "").strip(),
        "execution_id": execution_id,
    }


def orchestrator_validation_node(state: OrchestratorState) -> dict:
    """Validate generic workflow completion without SDLC-specific evidence authority."""
    from core.application.orchestration.agents.common import emit_event
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService

    emit_event(
        state,
        "orchestrator",
        "phase_complete",
        "Final validation starting",
        {"phase": "validation"},
    )

    existing_status = str(state.get("status") or "").strip().lower()
    unresolved_failure = state.get("unresolved_step_failure")
    if isinstance(unresolved_failure, dict):
        failure_message = str(
            unresolved_failure.get("message") or "Workflow has an unresolved step failure."
        ).strip()
        emit_event(
            state,
            "orchestrator",
            "phase_complete",
            "Validation failed (unresolved step failure)",
            {
                "result": "fail",
                "unresolved_step_failure": unresolved_failure,
            },
            severity="error",
        )
        return _build_result(
            state,
            "failed",
            failure_message,
            proof_of_work={
                "terminal_success": False,
                "unresolved_step_failure": unresolved_failure,
            },
            failure_class="unhandled_step_failure",
        )

    if existing_status == "awaiting_approval":
        existing_report = (
            state.get("final_report")
            or state.get("approval_context")
            or "Workflow is waiting for human approval."
        )
        emit_event(
            state,
            "orchestrator",
            "phase_complete",
            "Validation paused for human approval",
            {"result": existing_status, "reason": str(existing_report)},
            severity="warning",
        )
        return _build_result(state, existing_status, str(existing_report))

    if existing_status in {"failed", "stuck", "cancelled"}:
        existing_report = (
            state.get("final_report")
            or state.get("error")
            or "Task ended before final validation checks."
        )
        emit_event(
            state,
            "orchestrator",
            "phase_complete",
            "Validation skipped due prior terminal status",
            {"result": existing_status, "reason": str(existing_report)},
            severity="warning",
        )
        return _build_result(state, existing_status, str(existing_report))

    step_test_run_proof = _successful_step_test_run_proof(state)
    if step_test_run_proof:
        summary = str(state.get("final_report") or "Step test run completed successfully.").strip()
        emit_event(
            state,
            "orchestrator",
            "phase_complete",
            "Step test run validation passed",
            {"result": "pass", "step_test_run": step_test_run_proof},
        )
        return _build_result(
            state,
            "completed",
            summary,
            proof_of_work={"step_test_run": step_test_run_proof},
        )

    terminal_proof = _successful_terminal_proof(state)
    if not terminal_proof:
        current_step_id = run_cursor(state).step_id or ""
        summary = (
            "Workflow did not reach the required terminal success step "
            "(stop_success). Completion is invalid."
        )
        emit_event(
            state,
            "orchestrator",
            "phase_complete",
            "Validation failed (terminal success missing)",
            {
                "result": "fail",
                "existing_status": existing_status or "unknown",
                "current_step_id": current_step_id,
            },
            severity="error",
        )
        return _build_result(
            state,
            "failed",
            summary,
            proof_of_work={"terminal_success": False, "current_step_id": current_step_id},
            failure_class="workflow_completion_contract",
        )

    try:
        workflow_id = str(state.get("workflow_id") or "").strip()
        interface_service = WorkflowInterfaceService()
        if state.get("workflow_definition_ref"):
            # The output contract is the pinned definition's, not the live workflow's.
            from core.application.orchestration.output_contract_receipt import OutputProducerContext
            from core.application.orchestration.run_definition import load_run_definition

            definition = load_run_definition(state)
            output_validation = interface_service.validate_outputs(
                interface_service.interface_from_definition(definition),
                dict(state or {}),
                producer_context=OutputProducerContext.for_state(state, definition),
            )
        elif workflow_id:
            output_validation = interface_service.validate_outputs_for_workflow_id(
                workflow_id,
                dict(state or {}),
            )
        else:
            output_validation = interface_service.validate_outputs(
                interface_service.interface_from_settings({}),
                dict(state or {}),
            )
    except Exception as exc:
        emit_event(
            state,
            "orchestrator",
            "phase_complete",
            "Validation failed (workflow contract lookup)",
            {"result": "fail", "error": str(exc), "terminal_success": terminal_proof},
            severity="error",
        )
        return _build_result(
            state,
            "failed",
            f"Workflow contract lookup failed: {exc}",
            proof_of_work={"terminal_success": terminal_proof, "error": str(exc)},
            failure_class="workflow_output_contract",
        )

    workflow_outputs = output_validation.dump()
    if not output_validation.ok:
        emit_event(
            state,
            "orchestrator",
            "phase_complete",
            "Validation failed (workflow output contract)",
            {
                "result": "fail",
                "errors": output_validation.errors,
                "terminal_success": terminal_proof,
                "workflow_outputs": workflow_outputs,
            },
            severity="error",
        )
        messages = [
            str(item.get("message") or item.get("code") or "").strip()
            for item in output_validation.errors[:5]
            if isinstance(item, dict)
        ]
        summary = "Workflow output contract failed"
        if any(messages):
            summary = f"{summary}: {'; '.join(item for item in messages if item)}"
        return _build_result(
            state,
            "failed",
            summary,
            proof_of_work={
                "terminal_success": terminal_proof,
                "workflow_outputs": workflow_outputs,
            },
            failure_class="workflow_output_contract",
            workflow_outputs=workflow_outputs,
        )

    initial_req = state.get("initial_requirements") or state.get("task_summary") or ""
    if not initial_req:
        emit_event(
            state,
            "orchestrator",
            "phase_complete",
            "Validation passed (no requirements to check)",
            {
                "result": "pass",
                "terminal_success": terminal_proof,
                "workflow_outputs": workflow_outputs,
            },
        )
        return _build_result(
            state,
            "completed",
            "No initial requirements to validate against.",
            workflow_outputs=workflow_outputs,
        )

    summary = "Task completed."

    emit_event(
        state,
        "orchestrator",
        "phase_complete",
        "Validation passed (workflow contract satisfied)",
        {
            "result": "pass",
            "terminal_success": terminal_proof,
            "workflow_outputs": workflow_outputs,
        },
    )
    return _build_result(
        state,
        "completed",
        summary,
        proof_of_work={"terminal_success": terminal_proof},
        workflow_outputs=workflow_outputs,
    )


def _build_result(
    state: OrchestratorState,
    status: str,
    summary: str,
    *,
    proof_of_work: dict | None = None,
    failure_class: str | None = None,
    workflow_outputs: dict | None = None,
) -> dict:
    """Build the state update dict for validation outcome."""
    result = {
        "status": status,
        "final_report": summary,
        "workflow_validation_complete": True,
    }
    if isinstance(proof_of_work, dict):
        result["proof_of_work"] = proof_of_work
    if isinstance(workflow_outputs, dict):
        result["workflow_outputs"] = workflow_outputs
        result["outputs"] = dict(workflow_outputs.get("outputs") or {})
        result["artifacts"] = dict(workflow_outputs.get("artifacts") or {})
    if str(failure_class or "").strip():
        result["last_failure_class"] = str(failure_class).strip().lower()
    return result
