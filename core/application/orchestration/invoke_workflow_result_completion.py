"""Validate and publish a completed invoked workflow's result to its parent."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.application.orchestration.invoke_workflow_step_node_common import STATE_TARGET_PREFIX
from core.application.orchestration.invoke_workflow_step_node_results import (
    _artifact_key_for_state_target,
    _set_nested_state_update,
)

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState
    from core.application.registry.workflow_contract import WorkflowInterface


@dataclass(frozen=True, slots=True)
class ChildOutputContract:
    declared_outputs: list[str]
    required_outputs: list[str]
    declared_artifacts: list[str]
    required_artifacts: list[str]


@dataclass(frozen=True, slots=True)
class ChildInvocationResult:
    workflow_id: str
    job_id: Any
    status: str
    interface: WorkflowInterface
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    artifacts: dict[str, Any]
    debug_result: dict[str, Any]
    ephemeral: bool


def child_output_contract(
    child_interface: WorkflowInterface,
    *,
    callable_outputs: list[dict[str, Any]],
    callable_artifacts: list[dict[str, Any]],
) -> ChildOutputContract:
    """Keep the callable interface's required and declared result names distinct."""
    declared_child_outputs = (
        [
            str(item.get("name") or "").strip()
            for item in callable_outputs
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        if child_interface.variable_outputs_required()
        else []
    )
    required_child_outputs = (
        [
            str(item.get("name") or "").strip()
            for item in callable_outputs
            if (
                isinstance(item, dict)
                and str(item.get("name") or "").strip()
                and bool(item.get("required", True))
            )
        ]
        if child_interface.variable_outputs_required()
        else []
    )
    declared_child_artifacts = (
        [
            str(item.get("name") or "").strip()
            for item in callable_artifacts
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        if child_interface.artifacts_required()
        else []
    )
    required_child_artifacts = (
        [
            str(item.get("name") or "").strip()
            for item in callable_artifacts
            if (
                isinstance(item, dict)
                and str(item.get("name") or "").strip()
                and bool(item.get("required", True))
            )
        ]
        if child_interface.artifacts_required()
        else []
    )

    return ChildOutputContract(
        declared_outputs=declared_child_outputs,
        required_outputs=required_child_outputs,
        declared_artifacts=declared_child_artifacts,
        required_artifacts=required_child_artifacts,
    )


def _artifact_id_from_child_reference(value: Any) -> str:
    if isinstance(value, dict):
        raw = value.get("artifact_id") or value.get("id")
    elif isinstance(value, str):
        raw = value
    else:
        return ""
    artifact_id = str(raw or "").strip()
    if artifact_id.startswith("artifact:"):
        artifact_id = artifact_id.removeprefix("artifact:").strip()
    return artifact_id


def _artifact_ids_from_child_value(value: Any) -> list[str]:
    """Every file handle in a returned value; empty if any element has none."""
    items = value if isinstance(value, list) else [value]
    ids = [_artifact_id_from_child_reference(item) for item in items]
    return ids if ids and all(ids) else []


_RETURNED_FILE_METADATA = ("filename", "content_type", "size_bytes", "sha256")


def _returned_file_detail(artifact_id: str, adopted: Any) -> dict[str, Any]:
    """The adopted file's handle and metadata, for the parent's run results."""
    row = adopted if isinstance(adopted, dict) else {}
    detail: dict[str, Any] = {
        "artifact_id": artifact_id,
        "artifact_handle": f"artifact:{artifact_id}",
    }
    for key in _RETURNED_FILE_METADATA:
        if row.get(key) not in (None, ""):
            detail[key] = row.get(key)
    return detail


def _child_input_preview(child_inputs: dict[str, Any], child_interface: Any) -> Any:
    """What the parent passed to the child, secrets masked, for Job Details."""
    from core.application.jobs.job_invocation_artifacts import (
        build_effective_input_projection,
    )

    declared_secret_inputs = {
        str(item.name or "").strip()
        for item in getattr(child_interface, "expected_inputs", None) or []
        if str(item.name or "").strip()
        and str(getattr(item, "type", "") or "").strip().lower() == "secret"
    }
    return build_effective_input_projection(
        inputs=child_inputs,
        declared_secret_inputs=declared_secret_inputs,
        max_bytes=1500,
    )["mapped_inputs"]


def complete_child_invocation_result(
    state: OrchestratorState,
    *,
    child: ChildInvocationResult,
    contract: ChildOutputContract,
    step_agent: str,
    step_skill: str,
    step_started_at: str,
    project_id: str,
    parent_job_id: str,
    output_mapping: dict[str, Any],
    artifact_mapping: dict[str, Any],
    artifact_plane_provider: Callable[[], Any],
    append_agent_message: Callable[..., dict[str, Any]],
    hook_plan: Any = None,
) -> dict[str, Any]:
    """Validate, adopt, then map the result; publish success only after all three.

    For a hook (*hook_plan*), the child result that passed its own Interface is
    selected by the binding's output mapping and the whole hook output is
    validated; it is published at ``state.hooks.<hook_id>`` in one update, or
    the step fails and nothing is published.
    """
    from core.application.orchestration.agents.common import emit_event, update_runtime_truth

    child_workflow_id, child_job_id, child_status = child.workflow_id, child.job_id, child.status
    child_interface, child_inputs = child.interface, child.inputs
    child_outputs, child_artifacts = child.outputs, child.artifacts
    debug_result, is_ephemeral_child_test_run = child.debug_result, child.ephemeral
    declared_child_outputs, required_child_outputs = (
        contract.declared_outputs,
        contract.required_outputs,
    )
    declared_child_artifacts, required_child_artifacts = (
        contract.declared_artifacts,
        contract.required_artifacts,
    )

    missing_outputs = [name for name in required_child_outputs if name not in child_outputs]
    if missing_outputs:
        missing_text = ", ".join(missing_outputs[:20])
        message = append_agent_message(
            state=state,
            from_agent=step_agent,
            skill=step_skill,
            status="fail",
            summary=f"Child workflow '{child_workflow_id}' completed but missing declared outputs: {missing_text}",
            artifacts={
                "_invoke_workflow_error": "missing_child_outputs",
                "_invoke_workflow_child_job_id": child_job_id,
                "_contract_errors": [f"missing child outputs: {missing_text}"],
            },
            errors=[f"missing child outputs: {missing_text}"],
            started_at=step_started_at,
        )
        return {"agent_messages": [*(state.get("agent_messages") or []), message]}

    missing_artifacts = [name for name in required_child_artifacts if name not in child_artifacts]
    if missing_artifacts:
        missing_text = ", ".join(missing_artifacts[:20])
        message = append_agent_message(
            state=state,
            from_agent=step_agent,
            skill=step_skill,
            status="fail",
            summary=(
                f"Child workflow '{child_workflow_id}' completed but missing declared "
                f"artifacts: {missing_text}"
            ),
            artifacts={
                "_invoke_workflow_error": "missing_child_artifacts",
                "_invoke_workflow_child_job_id": child_job_id,
                "_contract_errors": [f"missing child artifacts: {missing_text}"],
            },
            errors=[f"missing child artifacts: {missing_text}"],
            started_at=step_started_at,
        )
        return {"agent_messages": [*(state.get("agent_messages") or []), message]}

    returned_file_details: dict[str, Any] = {}
    if not is_ephemeral_child_test_run and child_artifacts:
        try:
            artifact_plane = artifact_plane_provider()
            for child_artifact_name, child_artifact in child_artifacts.items():
                artifact_ids = _artifact_ids_from_child_value(child_artifact)
                if not artifact_ids:
                    raise ValueError(
                        f"returned child file '{child_artifact_name}' has no valid handle"
                    )
                details = []
                for artifact_id in artifact_ids:
                    adopted = artifact_plane.transfer_child_artifact_to_parent(
                        artifact_id,
                        project_id=project_id,
                        child_job_id=child_job_id,
                        parent_job_id=parent_job_id,
                    )
                    details.append(_returned_file_detail(artifact_id, adopted))
                # A collection is returned whole; one file stays one file.
                returned_file_details[child_artifact_name] = (
                    details if isinstance(child_artifact, list) else details[0]
                )
        except Exception as exc:
            error_text = f"Child workflow '{child_workflow_id}' artifact adoption failed: {exc}"
            emit_event(
                state,
                "orchestrator",
                "invoke_workflow_artifact_adoption_failed",
                error_text,
                {
                    "child_job_id": child_job_id,
                    "child_workflow_id": child_workflow_id,
                    "declared_artifacts": sorted(child_artifacts),
                },
                severity="error",
            )
            message = append_agent_message(
                state=state,
                from_agent=step_agent,
                skill=step_skill,
                status="fail",
                summary=error_text,
                artifacts={
                    "_invoke_workflow_error": "child_artifact_adoption_failed",
                    "_invoke_workflow_child_job_id": child_job_id,
                },
                errors=[error_text],
                started_at=step_started_at,
            )
            return {"agent_messages": [*(state.get("agent_messages") or []), message]}

    mapped_artifacts: dict[str, Any] = {}
    state_updates: dict[str, Any] = {}
    if hook_plan is not None:
        from core.application.workflows.workflow_hook_contract import HookContractError

        try:
            state_updates, hook_output = hook_plan.output_updates(state, child_outputs)
        except HookContractError as exc:
            message = append_agent_message(
                state=state,
                from_agent=step_agent,
                skill=step_skill,
                status="fail",
                summary=f"Child workflow '{child_workflow_id}' result does not fit the hook: {exc}",
                # An ordinary Invoke error, not a contract-health issue: the
                # same child result never fits on a retry, and the hook's
                # On error or its container's policy must be able to take it.
                artifacts={
                    "_invoke_workflow_error": exc.code,
                    "_invoke_workflow_child_job_id": child_job_id,
                    "hook_error": exc.issue(),
                },
                errors=[exc.code],
                started_at=step_started_at,
            )
            return {"agent_messages": [*(state.get("agent_messages") or []), message]}
        mapped_artifacts["hook"] = hook_plan.summary()
        mapped_artifacts["hook_output"] = hook_output

    for child_output_name, target in output_mapping.items():
        child_key = str(child_output_name or "").strip()
        target_key = str(target or "").strip()
        if not child_key or not target_key:
            continue
        if child_key not in child_outputs:
            continue
        value = child_outputs.get(child_key)
        if target_key.startswith(STATE_TARGET_PREFIX):
            # Preserve state-targeted child outputs on the parent message as well.
            # Hub transitions and observability fallbacks may only have the latest
            # active-step message available when validating the next step.
            mapped_artifacts[child_key] = value
            artifact_key = _artifact_key_for_state_target(target_key)
            if artifact_key:
                mapped_artifacts[artifact_key] = value
            _set_nested_state_update(
                state=state,
                updates=state_updates,
                state_target=target_key,
                value=value,
            )
        else:
            mapped_artifacts[target_key] = value

    for child_artifact_name, target in artifact_mapping.items():
        child_key = str(child_artifact_name or "").strip()
        target_key = str(target or "").strip()
        if not child_key or not target_key:
            continue
        if child_key not in child_artifacts:
            continue
        value = child_artifacts.get(child_key)
        if target_key.startswith(STATE_TARGET_PREFIX):
            mapped_artifacts[child_key] = value
            artifact_key = _artifact_key_for_state_target(target_key)
            if artifact_key:
                mapped_artifacts[artifact_key] = value
            _set_nested_state_update(
                state=state,
                updates=state_updates,
                state_target=target_key,
                value=value,
            )
        else:
            mapped_artifacts[target_key] = value

    mapped_artifacts["_invoke_workflow"] = {
        "child_job_id": child_job_id,
        "child_workflow_id": child_workflow_id,
        "child_status": child_status,
        "output_mode": child_interface.output_mode,
        "declared_outputs": sorted(declared_child_outputs),
        "declared_artifacts": sorted(declared_child_artifacts),
        "mapped_outputs": {str(k): str(v) for k, v in output_mapping.items()},
        "mapped_artifacts": {str(k): str(v) for k, v in artifact_mapping.items()},
    }
    if returned_file_details:
        mapped_artifacts["_invoke_workflow"]["returned_files"] = returned_file_details
    from core.application.jobs.job_invocation_artifacts import (
        build_effective_input_projection,
    )

    declared_secret_inputs = {
        str(item.name or "").strip()
        for item in child_interface.expected_inputs
        if str(item.name or "").strip()
        and str(getattr(item, "type", "") or "").strip().lower() == "secret"
    }
    input_projection = build_effective_input_projection(
        inputs=child_inputs,
        declared_secret_inputs=declared_secret_inputs,
        max_bytes=12 * 1024,
    )
    mapped_artifacts["input_payload"] = input_projection["mapped_inputs"]
    mapped_artifacts["_invoke_workflow"]["input_projection_truncated"] = bool(
        input_projection["truncated"]
    )
    if debug_result:
        mapped_artifacts["_invoke_workflow"]["nested_trace"] = debug_result.get("trace")
    if state_updates:
        mapped_artifacts["_state_patch"] = copy.deepcopy(state_updates)
        for key, value in state_updates.items():
            mapped_artifacts.setdefault(key, value)

    emit_event(
        state,
        "orchestrator",
        "invoke_workflow_completed",
        f"Child workflow '{child_workflow_id}' completed",
        {
            "child_job_id": child_job_id,
            "child_workflow_id": child_workflow_id,
            "mapped_output_targets": sorted(mapped_artifacts.keys()),
            "state_targets": sorted(
                [
                    str(k)
                    for k in [*output_mapping.values(), *artifact_mapping.values()]
                    if str(k).startswith(STATE_TARGET_PREFIX)
                ]
            ),
        },
    )

    message = append_agent_message(
        state=state,
        from_agent=step_agent,
        skill=step_skill,
        status="pass",
        summary=f"Child workflow '{child_workflow_id}' completed successfully",
        artifacts=mapped_artifacts,
        errors=[],
        started_at=step_started_at,
    )

    update_runtime_truth(
        state,
        current_agent=step_agent,
        selected_workflow=state.get("workflow_id", ""),
        clear_last_error=True,
        last_response_excerpt=message.get("summary", ""),
    )

    return {
        "agent_messages": [*(state.get("agent_messages") or []), message],
        **state_updates,
    }
