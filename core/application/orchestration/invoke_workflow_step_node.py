"""
Invoke-workflow runtime node.

Executes step.override.invoke_workflow as a blocking child job:
- enqueue child workflow as a separate job
- wait until child reaches terminal status
- map declared child outputs back into parent AgentMessage artifacts
"""

from __future__ import annotations

import contextvars
import copy
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from core.application.jobs.invoke_child_link_repository import (
    InvokeChildAlreadyLinked,
    InvokeParentClosed,
    link_child_in_transaction,
)
from core.application.jobs.workflow_definition_snapshot_repository import WorkflowDefinitionPinError
from core.application.orchestration.active_step import active_step
from core.application.orchestration.child_execution_runtime import (
    current_child_execution_runtime,
)
from core.application.orchestration.helpers import has_value as _has_value
from core.application.orchestration.helpers import now_iso as _now_iso
from core.application.orchestration.helpers import resolve_step_skill as _resolve_step_skill
from core.application.orchestration.helpers import to_bool as _to_bool
from core.application.orchestration.invoke_workflow_child_wait import (
    ChildWaitNotRecorded,
    await_child,
    child_revision_of,
)
from core.application.orchestration.invoke_workflow_hook_runtime import (
    binding_compatibility_issues,
    plan_hook_for_state,
)
from core.application.orchestration.invoke_workflow_result_completion import (
    ChildInvocationResult,
    _child_input_preview,
    child_output_contract,
    complete_child_invocation_result,
)
from core.application.orchestration.invoke_workflow_step_node_common import (
    INVOKE_LANE_PLACEMENT_KEY,
    INVOKE_WORKFLOW_ANCESTRY_KEY,
    _cancel_child_job_on_timeout,
    _child_input_fingerprint,
    _coerce_positive_int_env,
    _extract_invoke_spec,
    _find_existing_child_invocation,
    _get_logger,
    _invocation_key,
    _invoke_workflow_depth_exceeded,
    _lane_child_wait_issue,
    _parent_meta,
    _resolve_child_workflow_row,
    _resolve_invoke_child_poll_seconds,
    _resolve_invoke_child_timeout_seconds,
    _resolve_invoke_input,
    _resolve_invoke_workflow_max_depth,
    _resolve_invoked_child_snapshot,
    _retry_feedback_fingerprint,
    _workflow_invocation_ancestry,
)
from core.application.orchestration.invoke_workflow_step_node_results import (
    _collect_child_artifacts,
    _collect_child_outputs,
)
from core.application.orchestration.invoke_workflow_step_node_runtime import (
    _append_agent_message,
    _build_child_summary,
    _build_child_task_envelope,
    _build_child_task_id,
    _derive_child_task_summary,
    _execute_child_job_inline,
    _resolve_child_runtime_context,
    _should_execute_child_inline,
    _wait_for_terminal_child,
)
from core.application.workflows.workflow_hook_contract import HOOK_CONFIG_KEY, HookContractError
from core.application.workflows.workflow_invocation_target import InvocationTargetError

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState

logger = logging.getLogger(__name__)

#: The child whose finished result this step execution consumed (durable path).
_CONSUMED_CHILD: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "invoke_consumed_child"
)

__all__ = [
    "_build_child_summary",
    "_build_child_task_id",
    "_cancel_child_job_on_timeout",
    "_child_input_fingerprint",
    "_coerce_positive_int_env",
    "_derive_child_task_summary",
    "_resolve_invoke_child_poll_seconds",
    "_resolve_invoke_child_timeout_seconds",
    "_wait_for_terminal_child",
    "invoke_workflow_step_node",
    "os",
    "time",
]


def _job_invocation_artifact_plane() -> Any:
    runtime = current_child_execution_runtime()
    if runtime is None or runtime.artifact_plane_provider is None:
        raise RuntimeError("Child workflow invocation artifact storage is unavailable")
    return runtime.artifact_plane_provider()


def invoke_workflow_step_node(state: OrchestratorState) -> dict:
    """Run one invoke step; on the durable path, settle its consumed child."""
    consumed: dict[str, str] = {}
    token = _CONSUMED_CHILD.set(consumed)
    try:
        update = _invoke_workflow_step(state)
    finally:
        _CONSUMED_CHILD.reset(token)
    if consumed:
        # This is only an in-memory result. Release the child retention hold
        # after the graph has durably checkpointed it, never inside the node.
        update = {**update, "pending_wait": None}
    from core.application.orchestration.returned_files_runtime import with_returned_files

    # A file the child returned can in turn be returned by this workflow.
    return with_returned_files(state, update)


def _invoke_workflow_step(
    state: OrchestratorState, *, _existing_child_id: str | None = None
) -> dict:
    from core.application.orchestration.agents.common import emit_event, update_runtime_truth
    from core.application.registry.registry_manager import RegistryManager

    step_started_at = _now_iso()
    current = active_step(state)
    step = dict(current.step) if current is not None else {}
    step_id = current.step_id if current is not None else ""
    step_execution_id = current.step_execution_id if current is not None else ""
    step_agent = str(step.get("agent") or step.get("agent_id") or "").strip() or "invoke_workflow"
    from core.application.orchestration.workflow_state_selector_contract import (
        RUNTIME_ERROR_CODE,
        validate_step_state_selectors,
    )

    selector_issues = validate_step_state_selectors(step)
    if selector_issues:
        issue = selector_issues[0]
        message = _append_agent_message(
            state=state,
            from_agent=step_agent,
            skill=str(step.get("skill") or step.get("skill_id") or "invoke_workflow").strip(),
            status="fail",
            summary="invoke_workflow has an invalid input selector",
            artifacts={
                "_invoke_workflow_error": RUNTIME_ERROR_CODE,
                "selector": issue["selector"],
                "path": issue["path"].removeprefix("step."),
            },
            errors=[RUNTIME_ERROR_CODE],
        )
        return {"agent_messages": [*(state.get("agent_messages") or []), message]}
    step_skill = _resolve_step_skill(
        step,
        state=dict(state) if isinstance(state, dict) else None,
        default="invoke_workflow",
    )

    def _fail(**fields: Any) -> dict:
        message = _append_agent_message(
            state=state, from_agent=step_agent, skill=step_skill, status="fail", **fields
        )
        return {"agent_messages": [*(state.get("agent_messages") or []), message]}

    spec = _extract_invoke_spec(step)
    if not isinstance(spec, dict):
        return _fail(
            summary="invoke_workflow step is missing override.invoke_workflow configuration",
            artifacts={"_invoke_workflow_error": "missing_invoke_spec"},
            errors=["missing invoke_workflow configuration"],
        )

    hook_plan = None
    if HOOK_CONFIG_KEY in spec:
        # A hook is an ordinary Invoke of the child its pinned binding names.
        try:
            hook_plan = plan_hook_for_state(
                state, spec, step_execution_id=step_execution_id, get_logger=_get_logger
            )
            if hook_plan.empty:
                hook_updates, hook_output = hook_plan.output_updates(state, None)
        except (HookContractError, InvocationTargetError) as exc:
            return _fail(
                summary=f"Hook could not run: {exc}",
                artifacts={"_invoke_workflow_error": exc.code},
                errors=[exc.code],
            )
        if hook_plan.empty:
            message = _append_agent_message(
                state=state,
                from_agent=step_agent,
                skill=step_skill,
                status="pass",
                summary=f"Hook '{hook_plan.hook.hook_id}' has no connected workflow",
                artifacts={
                    "hook": hook_plan.summary(),
                    "hook_output": hook_output,
                    "summary_key": "react.jobs.step_hook_empty",
                    "summary_params": {"hook": hook_plan.hook.display_name},
                },
                errors=[],
                started_at=step_started_at,
            )
            return {
                "agent_messages": [*(state.get("agent_messages") or []), message],
                **hook_updates,
            }
        spec = hook_plan.direct_spec()

    child_workflow_id = str(spec.get("workflow_id") or "").strip()
    if not child_workflow_id:
        return _fail(
            summary="invoke_workflow.workflow_id is required",
            artifacts={"_invoke_workflow_error": "missing_workflow_id"},
            errors=["invoke_workflow.workflow_id is required"],
        )

    workflow_ancestry = _workflow_invocation_ancestry(state)
    if child_workflow_id in set(workflow_ancestry):
        cycle_path = " -> ".join([*workflow_ancestry, child_workflow_id])
        return _fail(
            summary=f"invoke_workflow recursive call blocked: {cycle_path}",
            artifacts={
                "_invoke_workflow_error": "recursive_workflow_invocation",
                "_invoke_workflow_child_workflow_id": child_workflow_id,
                "_invoke_workflow_ancestry": workflow_ancestry,
                "_invoke_workflow_cycle_path": cycle_path,
            },
            errors=[f"recursive workflow invocation: {cycle_path}"],
        )

    child_ancestry = [*workflow_ancestry, child_workflow_id]
    if _invoke_workflow_depth_exceeded(child_ancestry):
        max_depth = _resolve_invoke_workflow_max_depth()
        chain = " -> ".join(child_ancestry)
        return _fail(
            summary=(
                f"invoke_workflow nesting depth {len(child_ancestry)} exceeds the maximum "
                f"of {max_depth}: {chain}"
            ),
            artifacts={
                "_invoke_workflow_error": "invocation_depth_exceeded",
                "_invoke_workflow_child_workflow_id": child_workflow_id,
                "_invoke_workflow_ancestry": workflow_ancestry,
                "_invoke_workflow_max_depth": max_depth,
            },
            errors=[f"invoke_workflow nesting depth exceeded (max {max_depth}): {chain}"],
        )

    update_runtime_truth(
        state,
        current_agent=step_agent,
        selected_workflow=state.get("workflow_id", ""),
    )

    input_mapping = spec.get("input_mapping")
    output_mapping = spec.get("output_mapping")
    artifact_mapping = spec.get("artifact_mapping")
    if not isinstance(input_mapping, dict):
        input_mapping = {}
    if not isinstance(output_mapping, dict):
        output_mapping = {}
    if not isinstance(artifact_mapping, dict):
        artifact_mapping = {}

    parent_job_id = str(state.get("job_id") or "").strip()
    project_id = str(state.get("project_id") or "").strip()
    if not project_id:
        state_inputs = state.get("inputs")
        if isinstance(state_inputs, dict):
            project_id = str(state_inputs.get("project_id") or "").strip()
    if not project_id:
        project_cfg = state.get("project_config")
        if isinstance(project_cfg, dict):
            project_id = str(project_cfg.get("project_id") or project_cfg.get("id") or "").strip()
    project_name = str(state.get("project_name") or "").strip()
    child_project_config, child_runtime_state = _resolve_child_runtime_context(state)
    parent_job: dict[str, Any] = {}
    bl = None
    if parent_job_id:
        try:
            bl = _get_logger()
        except Exception as exc:
            return _fail(
                summary=f"invoke_workflow logger initialization failed: {exc}",
                artifacts={"_invoke_workflow_error": "logger_init_failed"},
                errors=[str(exc)],
            )
        try:
            parent_job = bl.get_job(parent_job_id) or {}
        except Exception:
            parent_job = {}
        project_id = (
            str(parent_job.get("project_id") or "").strip() if isinstance(parent_job, dict) else ""
        )
        if not project_name:
            project_name = str((parent_job or {}).get("project_name") or "").strip()
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService

    rm = RegistryManager.instance()
    db = getattr(rm, "db_store", None)
    workflow_registry = getattr(rm, "workflows", None)
    existing_child_id = _existing_child_id or _find_existing_child_invocation(
        parent_job, invocation_key=_invocation_key(parent_job_id, step_execution_id)
    )
    if existing_child_id:
        resolved_child = _resolve_invoked_child_snapshot(bl, existing_child_id, child_workflow_id)
    else:
        # Select contract and revision together. The job pin rejects an edit
        # between this read and enqueue, before any child can be dispatched.
        getter = getattr(db, "get_workflow", None)
        canonical_child = getter(child_workflow_id) if callable(getter) else None
        resolved_child = (
            canonical_child
            if isinstance(canonical_child, dict)
            and str(canonical_child.get("projection_mode") or "").strip().lower() != "derived"
            and not (
                child_workflow_id.startswith("extension.")
                and canonical_child.get("source_install_id")
            )
            else _resolve_child_workflow_row(
                workflow_registry=workflow_registry,
                db=db,
                child_workflow_id=child_workflow_id,
            )
        )
        # The winning child may have committed while the live definition was
        # being read. Its archived contract takes precedence before validation.
        if parent_job_id:
            assert bl is not None
            parent_job = bl.get_job(parent_job_id) or {}
            existing_child_id = _find_existing_child_invocation(
                parent_job, invocation_key=_invocation_key(parent_job_id, step_execution_id)
            )
            if existing_child_id:
                resolved_child = _resolve_invoked_child_snapshot(
                    bl, existing_child_id, child_workflow_id
                )
    if resolved_child is None:
        # Fail closed: an id that resolves to no workflow is a different problem from
        # a workflow that exists but is not callable, and saying so is what lets the
        # operator see a mis-scoped extension child instead of a generic refusal.
        return _fail(
            summary=f"Child workflow '{child_workflow_id}' was not found",
            artifacts={
                "_invoke_workflow_error": "child_not_found",
                "_invoke_workflow_child_workflow_id": child_workflow_id,
            },
            errors=[f"workflow '{child_workflow_id}' was not found"],
        )
    if isinstance(resolved_child, dict):
        child_settings = resolved_child.get("settings") or {}
        child_label = str(resolved_child.get("display_name") or "").strip()
    else:
        child_settings = getattr(resolved_child, "settings", {}) or {}
        child_label = str(getattr(resolved_child, "display_name", "") or "").strip()
    child_label = child_label or child_workflow_id
    interface_service = WorkflowInterfaceService()
    child_interface = interface_service.interface_from_definition(resolved_child)
    callable_enabled = bool(child_interface.enabled)
    hook_problems = (
        binding_compatibility_issues(hook_plan, child_settings)
        if hook_plan is not None and not existing_child_id
        else []
    )
    if hook_problems:
        return _fail(
            summary=f"Workflow '{child_workflow_id}' no longer fits the hook it is connected to",
            # An ordinary Invoke error that authored policies handle (see the result check).
            artifacts={"_invoke_workflow_error": "hook_binding_incompatible"},
            errors=hook_problems,
        )
    optional_child_inputs = {
        str(row.name or "").strip()
        for row in child_interface.expected_inputs
        if str(row.name or "").strip() and not bool(row.required)
    }
    callable_outputs = [row.dump() for row in child_interface.expected_outputs]
    callable_artifacts = [row.dump() for row in child_interface.returned_files]
    if not callable_enabled:
        return _fail(
            summary=f"Child workflow '{child_workflow_id}' is not callable",
            artifacts={"_invoke_workflow_error": "child_not_callable"},
            errors=[f"workflow '{child_workflow_id}' is not callable"],
        )

    child_inputs: dict[str, Any] = {}
    input_errors: list[str] = []
    for child_input_name, selector in input_mapping.items():
        key = str(child_input_name or "").strip()
        src = str(selector or "").strip()
        if not key or not src:
            continue
        if child_interface.input_mode == "json_schema" and src.startswith("state."):
            from core.application.orchestration.state_paths import (
                StatePathError,
                resolve_state_path_with_workflow_data,
            )

            try:
                _value, found = resolve_state_path_with_workflow_data(dict(state), src)
            except StatePathError:
                found = True  # the normal resolver below reports invalid syntax
            if not found:
                # Missing is not JSON null. The child's schema owns whether the
                # absent property is optional or required.
                continue
        value, source_error = _resolve_invoke_input(state, src, key)
        if source_error:
            input_errors.append(source_error)
            continue
        # A JSON Schema decides whether an empty collection, blank string or
        # null is valid. Do not discard it before that boundary is evaluated.
        if child_interface.input_mode != "json_schema" and not _has_value(value):
            if key in optional_child_inputs:
                continue
            input_errors.append(f"invoke.input_mapping.{key} resolved empty value from '{src}'")
            continue
        child_inputs[key] = value

    if input_errors:
        return _fail(
            summary="invoke_workflow input mapping failed",
            artifacts={
                "_invoke_workflow_error": "input_mapping_failed",
                "_contract_errors": input_errors,
            },
            errors=input_errors,
        )

    input_validation = interface_service.validate_delegated_inputs(
        child_interface,
        child_inputs,
        channel="parent_workflow",
    )
    if not input_validation.ok:
        errors = [
            str(item.get("message") or item.get("code") or "").strip()
            for item in input_validation.errors
            if isinstance(item, dict)
        ]
        return _fail(
            summary="invoke_workflow child input contract failed",
            artifacts={
                "_invoke_workflow_error": "child_input_contract_failed",
                "_contract_errors": errors,
                "workflow_interface": input_validation.interface,
            },
            errors=errors,
        )
    child_inputs = dict(input_validation.inputs)

    result_contract = child_output_contract(
        child_interface, callable_outputs=callable_outputs, callable_artifacts=callable_artifacts
    )

    if not project_id:
        return _fail(
            summary="invoke_workflow could not resolve parent project_id",
            artifacts={"_invoke_workflow_error": "missing_project_id"},
            errors=["parent project_id is required for child invoke"],
        )

    parent_workflow_id = str(state.get("workflow_id") or "").strip()
    # Called from a parallel lane, here or by any ancestor: the child keeps the
    # finite invocation path and so does every descendant. The mark lives in
    # host-written launch metadata, never in author input.
    lane_placement = bool(current is not None and current.lane_id) or _to_bool(
        _parent_meta(parent_job).get(INVOKE_LANE_PLACEMENT_KEY), False
    )
    child_extra_state = {
        **dict(child_inputs.items()),
        "workflow_id": child_workflow_id,
        # Identify child steps in nested traces and the run view.
        "invoke_child_label": child_label,
        INVOKE_WORKFLOW_ANCESTRY_KEY: list(child_ancestry),
        "workflow_selection_source": "parent_call",
        "invoke_parent_job_id": parent_job_id or None,
        "invoke_parent_step_id": step_id or None,
        "invoke_parent_step_agent_id": step_agent or None,
        "invoke_parent_step_skill_id": step_skill or None,
        "parent_job_id": parent_job_id or None,
        "parent_task_id": str(state.get("task_id") or "").strip() or None,
        "parent_workflow_id": parent_workflow_id or None,
        **child_runtime_state,
    }
    child_meta = {
        "invoke_parent_job_id": parent_job_id or None,
        "invoke_parent_task_id": str(state.get("task_id") or "").strip() or None,
        "invoke_parent_workflow_id": parent_workflow_id or None,
        "invoke_parent_step_id": step_id or None,
        "invoke_parent_step_agent_id": step_agent or None,
        "invoke_parent_step_skill_id": step_skill or None,
        "invoke_child_workflow_id": child_workflow_id,
        INVOKE_WORKFLOW_ANCESTRY_KEY: list(child_ancestry),
        "parent_job_id": parent_job_id or None,
        "parent_workflow_id": parent_workflow_id or None,
    }
    selected_revision = (
        resolved_child.get("revision")
        if isinstance(resolved_child, dict)
        else getattr(resolved_child, "revision", None)
    )
    if selected_revision is not None:
        child_meta["expected_workflow_revision"] = int(selected_revision)
    if lane_placement:
        child_meta[INVOKE_LANE_PLACEMENT_KEY] = True
        lane_issue = (
            _lane_child_wait_issue(db, child_workflow_id, ancestry=list(workflow_ancestry))
            if not existing_child_id
            else ""
        )
        if lane_issue:
            return _fail(
                summary=lane_issue,
                artifacts={
                    "_invoke_workflow_error": "child_waits_in_lane",
                    "_invoke_workflow_child_workflow_id": child_workflow_id,
                },
                errors=[lane_issue],
            )
    from core.application.registry.workflow_secret_defaults import (
        delegated_workflow_secret_launch_snapshot,
    )

    child_secret_ref_snapshot = delegated_workflow_secret_launch_snapshot(
        parent_state=state,
        child_interface=child_interface,
        child_inputs=child_inputs,
    )
    child_extra_state["_workflow_secret_ref_fingerprints"] = child_secret_ref_snapshot
    if _to_bool(state.get("test_mode"), False):
        child_extra_state["test_mode"] = True
        child_meta["test_mode"] = True

    scoped_child_task_summary = _derive_child_task_summary(
        str(state.get("task_summary") or "").strip(),
        child_workflow_id,
        step_id,
        child_inputs=child_inputs,
    )
    child_extra_state["task_summary"] = scoped_child_task_summary
    child_extra_state["inputs"] = {
        **dict(child_inputs.items()),
        "task_summary": scoped_child_task_summary,
    }
    child_extra_state["task_envelope"] = _build_child_task_envelope(
        state=state,
        child_workflow_id=child_workflow_id,
        child_task_summary=scoped_child_task_summary,
        child_inputs=dict(child_extra_state.get("inputs") or {}),
        project_id=project_id,
        parent_workflow_id=parent_workflow_id,
        step_id=step_id,
    )
    child_task_id = _build_child_task_id(
        str(state.get("task_id") or "task"),
        step_id,
        child_workflow_id,
    )
    child_summary = _build_child_summary(
        scoped_child_task_summary,
        child_workflow_id,
        step_id,
        hook_label=(
            (hook_plan.hook.display_name or hook_plan.hook.hook_id) if hook_plan is not None else ""
        ),
    )
    retry_feedback = str(state.get("feedback") or "").strip()
    retry_feedback_hash = _retry_feedback_fingerprint(retry_feedback)
    fingerprint_inputs = dict(child_inputs)
    if retry_feedback_hash:
        fingerprint_inputs["_invoke_parent_feedback_hash"] = retry_feedback_hash
    child_input_key = _child_input_fingerprint(fingerprint_inputs)
    child_meta["invoke_input_fingerprint"] = child_input_key
    if retry_feedback_hash:
        child_extra_state.setdefault("feedback", retry_feedback)
        child_extra_state["invoke_parent_feedback"] = retry_feedback
        child_meta["invoke_parent_feedback_hash"] = retry_feedback_hash

    child_status = ""
    child_job_id: Any = ""
    child_row = None
    existing_child = None
    child_outputs: dict[str, Any] = {}
    child_artifacts: dict[str, Any] = {}
    debug_result: dict[str, Any] = {}
    task_source = str(state.get("task_source") or "").strip().lower()
    # A persisted test run needs the same durable child identity and human
    # approval/resume path as every other parent job. Only an in-memory caller
    # without a parent job can use the ephemeral runtime.
    is_ephemeral_child_test_run = not parent_job_id and (
        _to_bool(state.get("debug_test_run"), False)
        or task_source in {"workflow_test_run", "step_test_run"}
    )

    if is_ephemeral_child_test_run:
        from core.application.workflows.platform_test_run_runtime import (
            run_workflow_test_run_ephemeral,
        )

        try:
            debug_result = run_workflow_test_run_ephemeral(
                workflow_id=child_workflow_id,
                project_id=project_id,
                task_summary=scoped_child_task_summary,
                task_id=child_task_id,
                task_envelope=dict(child_extra_state.get("task_envelope") or {}),
                project_name=project_name,
                project_config=child_project_config,
                extra_state=child_extra_state,
            )
        except Exception as exc:
            return _fail(
                summary=f"Child workflow '{child_workflow_id}' test run failed: {exc}",
                artifacts={
                    "_invoke_workflow_error": "ephemeral_child_failed",
                    "_invoke_workflow_child_workflow_id": child_workflow_id,
                },
                errors=[str(exc)],
            )

        child_status = str(
            debug_result.get("final_status") or debug_result.get("status") or ""
        ).strip()
        raw_child_outputs = debug_result.get("outputs")
        child_outputs = dict(raw_child_outputs) if isinstance(raw_child_outputs, dict) else {}
        raw_child_artifacts = debug_result.get("artifacts")
        child_artifacts = dict(raw_child_artifacts) if isinstance(raw_child_artifacts, dict) else {}
        if child_status != "completed":
            error_text = f"Child workflow '{child_workflow_id}' finished with status '{child_status or 'failed'}'"
            return _fail(
                summary=error_text,
                artifacts={
                    "_invoke_workflow_error": "child_failed",
                    "_invoke_workflow_child_status": child_status,
                    "_invoke_workflow_nested_trace": debug_result.get("trace"),
                    "input_payload": _child_input_preview(child_inputs, child_interface),
                    "output_payload": {"child_status": child_status or "failed"},
                },
                errors=[error_text],
                started_at=step_started_at,
            )

    if not is_ephemeral_child_test_run:
        if bl is None:
            try:
                bl = _get_logger()
            except Exception as exc:
                return _fail(
                    summary=f"invoke_workflow logger initialization failed: {exc}",
                    artifacts={"_invoke_workflow_error": "logger_init_failed"},
                    errors=[str(exc)],
                )

        invocation_key = _invocation_key(parent_job_id, step_execution_id)
        durable = bool(parent_job_id) and current is not None and not lane_placement
        child_job_id = existing_child_id
        if child_job_id:
            try:
                existing_child = bl.get_job(child_job_id)
            except Exception:
                existing_child = None
            if not isinstance(existing_child, dict):
                # An authoritative link must never turn into a second launch.
                raise RuntimeError(f"Invoked child '{child_job_id}' is unavailable")

        if child_job_id:
            emit_event(
                state,
                "orchestrator",
                "invoke_workflow_resume_existing_child",
                f"Resuming child workflow '{child_workflow_id}' from existing job",
                {
                    "parent_job_id": parent_job_id,
                    "parent_workflow_id": parent_workflow_id,
                    "step_id": step_id,
                    "step_execution_id": step_execution_id,
                    "step_agent": step_agent,
                    "step_skill": step_skill,
                    "child_job_id": child_job_id,
                    "child_workflow_id": child_workflow_id,
                    "mapped_inputs": sorted(child_inputs.keys()),
                },
            )
        else:
            emit_event(
                state,
                "orchestrator",
                "invoke_workflow_start",
                f"Invoking child workflow '{child_workflow_id}'",
                {
                    "parent_job_id": parent_job_id,
                    "parent_workflow_id": parent_workflow_id,
                    "step_id": step_id,
                    "step_execution_id": step_execution_id,
                    "step_agent": step_agent,
                    "step_skill": step_skill,
                    "child_workflow_id": child_workflow_id,
                    "mapped_inputs": sorted(child_inputs.keys()),
                },
            )

            from core.application.jobs.task_enqueue_runtime import enqueue_job
            from core.application.workflows.workflow_start_runtime import enqueue_workflow_start

            # A serial invocation never runs its child in the parent's call
            # stack: the parent parks and a single worker can run the child.
            attempted_inline = (not durable) and _should_execute_child_inline(parent_job)

            def _dispatch_child(**task_message: Any) -> None:
                if attempted_inline:
                    emit_event(
                        state,
                        "orchestrator",
                        "invoke_workflow_inline_execute",
                        f"Executing child workflow '{child_workflow_id}' inline under current project lease",
                        {
                            "child_job_id": task_message.get("job_id"),
                            "child_workflow_id": child_workflow_id,
                            "parent_job_id": parent_job_id,
                        },
                    )
                    try:
                        disposition = _execute_child_job_inline(dict(task_message))
                        action = str(disposition.get("action") or "").strip()
                        if action == "retry":
                            from core.infrastructure.runtime.worker.queue_publisher import (
                                enqueue_task,
                            )

                            enqueue_task(**task_message)
                            return
                        if action != "ack":
                            reason = str(disposition.get("reason") or "inline_execution_incomplete")
                            raise RuntimeError(
                                f"inline child execution was not acknowledged: {reason}"
                            )
                    except Exception as inline_error:
                        logger.warning(
                            "invoke_workflow inline child execution raised job_id=%s child_job_id=%s: %s",
                            parent_job_id,
                            task_message.get("job_id"),
                            inline_error,
                        )
                        raise
                    return

                from core.infrastructure.runtime.worker.queue_publisher import enqueue_task

                enqueue_task(**task_message)

            try:

                def _link_child(conn: Any, created_job_id: str) -> None:
                    # Linked in the creating transaction under the parent row
                    # lock: one child per invocation, none after a parent cancel.
                    link_child_in_transaction(
                        conn,
                        parent_job_id=parent_job_id,
                        child_job_id=created_job_id,
                        entry={
                            "child_workflow_id": child_workflow_id,
                            "step_id": step_id,
                            "step_execution_id": step_execution_id,
                            "invocation_key": invocation_key,
                            "step_agent": step_agent,
                            "step_skill": step_skill,
                            "input_fingerprint": child_input_key,
                            "created_at": _now_iso(),
                        },
                    )

                def _enqueue_child(**kwargs: Any) -> str:
                    return str(
                        enqueue_job(
                            bl=bl,
                            enqueue_task_fn=_dispatch_child,
                            build_project_config_snapshot=lambda config: copy.deepcopy(config),
                            logger=logger,
                            log_prefix="invoke_workflow",
                            artifact_plane=_job_invocation_artifact_plane(),
                            job_created_hook=_link_child if parent_job_id else None,
                            **kwargs,
                        )
                    )

                launched_child_job_id = enqueue_workflow_start(
                    enqueue_task_fn=_enqueue_child,
                    task_id=child_task_id,
                    task_summary=child_summary,
                    task_source="invoke_workflow",
                    project_id=project_id,
                    project_name=project_name,
                    project_config=child_project_config,
                    selected_workflow_id=child_workflow_id,
                    workflow_selection_source="parent_call",
                    entry_path="invoke_workflow",
                    input_contract="task_envelope",
                    source_system="invoke_workflow",
                    requested_workflow_id=child_workflow_id,
                    source_event_id=child_task_id,
                    source_object_id=parent_job_id,
                    test_mode=_to_bool(child_extra_state.get("test_mode"), False),
                    task_envelope=dict(child_extra_state.get("task_envelope") or {}),
                    inputs=dict(child_extra_state.get("inputs") or {}),
                    files=list(child_extra_state.get("files") or []),
                    extra_state=child_extra_state,
                    workflow_secret_ref_fingerprints=child_secret_ref_snapshot,
                    job_meta=child_meta,
                )
                child_job_id = (
                    int(launched_child_job_id)
                    if str(launched_child_job_id).isdigit()
                    else launched_child_job_id
                )
            except InvokeChildAlreadyLinked as linked:
                # Re-resolve every derived input/output rule against the
                # winner's archive. The replay has a child ID and cannot enqueue.
                if _existing_child_id is not None:
                    raise
                return _invoke_workflow_step(state, _existing_child_id=linked.child_job_id)
            except InvokeParentClosed as closed:
                return _fail(
                    summary=f"invoke_workflow did not start a child: the parent job is {closed.status}",
                    artifacts={"_invoke_workflow_error": "parent_closed"},
                    errors=[str(closed)],
                )
            except Exception as enqueue_error:
                # Pinning precedes the transactional link hook. A revision
                # conflict may therefore hide a child another attempt committed.
                if (
                    isinstance(enqueue_error, WorkflowDefinitionPinError)
                    and parent_job_id
                    and _existing_child_id is None
                ):
                    latest_parent = bl.get_job(parent_job_id) or {}
                    winner_id = _find_existing_child_invocation(
                        latest_parent, invocation_key=invocation_key
                    )
                    if winner_id:
                        return _invoke_workflow_step(state, _existing_child_id=winner_id)
                return _fail(
                    summary=f"invoke_workflow enqueue failed: {enqueue_error}",
                    artifacts={
                        "_invoke_workflow_error": "enqueue_failed",
                        "_invoke_workflow_child_job_id": child_job_id,
                    },
                    errors=[str(enqueue_error)],
                )
        try:
            bl.update_job_runtime(
                child_job_id,
                selected_workflow=child_workflow_id,
                heartbeat_now=True,
            )
        except Exception as runtime_err:
            logger.debug(
                "invoke_workflow: could not persist child selected_workflow: %s", runtime_err
            )

        if durable and current is not None:
            try:
                child_row, child_status = await_child(
                    state,
                    current,
                    bl=bl,
                    child_job_id=str(child_job_id),
                    invocation_key=invocation_key,
                    child_revision=child_revision_of(bl.get_job(child_job_id)),
                )
                _CONSUMED_CHILD.get().update(
                    {"parent_job_id": parent_job_id, "child_job_id": str(child_job_id)}
                )
            except ChildWaitNotRecorded as missing:
                return _fail(
                    summary=str(missing),
                    artifacts={
                        "_invoke_workflow_error": "child_wait_not_recorded",
                        "_invoke_workflow_child_job_id": child_job_id,
                    },
                    errors=[str(missing)],
                )
        else:
            child_row, child_status = _wait_for_terminal_child(
                bl,
                child_job_id,
                parent_job_id=parent_job_id,
                parent_agent=step_agent,
                child_workflow_id=child_workflow_id,
            )
        if child_status != "completed":
            error_text = (
                f"Child workflow '{child_workflow_id}' finished with status '{child_status}'"
                if child_status != "timeout"
                else f"Child workflow '{child_workflow_id}' timed out while waiting for terminal status"
            )
            emit_event(
                state,
                "orchestrator",
                "invoke_workflow_failed",
                error_text,
                {
                    "child_job_id": child_job_id,
                    "child_status": child_status,
                    "child_error": str((child_row or {}).get("error") or ""),
                    "child_workflow_id": child_workflow_id,
                },
                severity="error",
            )
            return _fail(
                summary=error_text,
                artifacts={
                    "_invoke_workflow_error": "child_failed",
                    "_invoke_workflow_child_job_id": child_job_id,
                    "_invoke_workflow_child_status": child_status,
                    "input_payload": _child_input_preview(child_inputs, child_interface),
                    "output_payload": {
                        "child_status": child_status,
                        "child_job": str(child_job_id),
                    },
                    "_invoke_workflow_child_error": str((child_row or {}).get("error") or ""),
                },
                errors=[error_text],
                started_at=step_started_at,
            )

        child_outputs = _collect_child_outputs(bl, child_job_id, result_contract.declared_outputs)
        child_artifacts = _collect_child_artifacts(
            bl, child_job_id, result_contract.declared_artifacts
        )

    return complete_child_invocation_result(
        state,
        child=ChildInvocationResult(
            workflow_id=child_workflow_id,
            job_id=child_job_id,
            status=child_status,
            interface=child_interface,
            inputs=child_inputs,
            outputs=child_outputs,
            artifacts=child_artifacts,
            debug_result=debug_result,
            ephemeral=is_ephemeral_child_test_run,
        ),
        contract=result_contract,
        step_agent=step_agent,
        step_skill=step_skill,
        step_started_at=step_started_at,
        project_id=project_id,
        parent_job_id=parent_job_id,
        output_mapping=output_mapping,
        artifact_mapping=artifact_mapping,
        artifact_plane_provider=_job_invocation_artifact_plane,
        append_agent_message=_append_agent_message,
        hook_plan=hook_plan,
    )
