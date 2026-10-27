# mypy: ignore-errors
"""Reusable helpers for workflow override validation and state derivation."""

from __future__ import annotations

from typing import Any

from core.application.workflows.workflow_step_scope import (
    runtime_step_scope_id,  # noqa: F401 - public compatibility export
)


def override_workflow_id_from_value(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("workflow_id", "selected_workflow_id", "override", "value"):
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate
        return ""
    return str(value or "").strip()


def is_empty_original_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, dict):
        return len(value) == 0
    return False


def latest_runtime_message_id(
    runtime_state: dict[str, Any], *, step_execution_id: str = ""
) -> str | None:
    if not isinstance(runtime_state, dict):
        return None
    messages = runtime_state.get("agent_messages")
    if not isinstance(messages, list):
        return None
    target_execution = str(step_execution_id or "").strip()
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if (
            target_execution
            and str(message.get("step_execution_id") or "").strip() != target_execution
        ):
            continue
        raw_message_id = message.get("id")
        if raw_message_id in (None, ""):
            continue
        if isinstance(raw_message_id, bool):
            continue
        message_id = str(raw_message_id).strip()
        if message_id and message_id != "0":
            return message_id
    return None


def _project_has_inline_workflow_link(project_config: dict | None, workflow_id: str) -> bool:
    project_cfg = project_config if isinstance(project_config, dict) else {}
    project_links = project_cfg.get("project_workflows")
    if not isinstance(project_links, list):
        return False
    normalized_workflow_id = str(workflow_id or "").strip()
    if not normalized_workflow_id:
        return False
    return any(
        isinstance(item, dict)
        and bool(item.get("enabled", True))
        and not item.get("deleted_at")
        and str(item.get("workflow_id") or "").strip() == normalized_workflow_id
        for item in project_links
    )


def _is_retryable_override_validation_error(exc: Exception) -> bool:
    text = str(exc or "").strip().lower()
    if not text:
        return False
    retry_markers = (
        "was not found",
        "not linked to this project",
        "task_class_mapping_unavailable",
        "project_workflow_mapping_unavailable",
    )
    return any(marker in text for marker in retry_markers)


def validate_explicit_workflow_override(
    *,
    selected_workflow_id: str,
    selection_source: str,
    task_summary: str,
    project_name: str,
    project_config: dict | None,
    build_task_envelope_for_preview,
    extra_state: dict | None = None,
) -> None:
    """Fail fast when explicit workflow override cannot satisfy task-class routing."""
    workflow_id = str(selected_workflow_id or "").strip()
    source = str(selection_source or "").strip().lower()
    if not workflow_id or source != "explicit_request":
        return

    from core.application.orchestration.state import normalize_orchestrator_state
    from core.application.orchestration.supervisor import OrchestratorSupervisor
    from core.application.registry.contract_validation import validate_workflow_readiness
    from core.application.registry.registry_manager import RegistryManager

    run_cfg = project_config if isinstance(project_config, dict) else {}
    registry = RegistryManager.instance()
    if (
        _project_has_inline_workflow_link(run_cfg, workflow_id)
        and registry.workflows.get_workflow(workflow_id) is None
    ):
        registry.reload_all()
    validation = validate_workflow_readiness(registry.db_store, workflow_id)
    if not validation.get("valid", False):
        issues = [
            str(item.get("message") or "").strip() for item in (validation.get("errors") or [])
        ]
        issues = [message for message in issues if message]
        preview = "; ".join(issues[:3]) if issues else "unknown contract error"
        suffix = " ..." if len(issues) > 3 else ""
        raise ValueError(
            f"Workflow '{workflow_id}' has unresolved contract errors and cannot be invoked: {preview}{suffix}"
        )

    envelope = build_task_envelope_for_preview(task_summary)
    state_payload = {
        "task_summary": task_summary,
        "project_name": project_name or run_cfg.get("project_name", ""),
        "project_config": run_cfg,
        "workflow_id": workflow_id,
        "workflow_selection_source": "explicit_request",
    }
    if isinstance(extra_state, dict):
        state_payload.update(extra_state)
    state = normalize_orchestrator_state(state_payload)
    supervisor = OrchestratorSupervisor(registry)
    try:
        routing = supervisor.resolve_workflow_routing(envelope, state=state)
    except Exception as exc:
        if not _is_retryable_override_validation_error(exc):
            raise ValueError(
                f"Workflow '{workflow_id}' override could not be validated: {exc}"
            ) from exc
        registry.reload_all()
        supervisor = OrchestratorSupervisor(registry)
        try:
            routing = supervisor.resolve_workflow_routing(envelope, state=state)
        except Exception as retry_exc:
            raise ValueError(
                f"Workflow '{workflow_id}' override could not be validated: {retry_exc}"
            ) from retry_exc

    resolved_workflow_id = str(getattr(routing, "workflow_id", "") or "").strip()
    if not resolved_workflow_id or resolved_workflow_id == workflow_id:
        return

    raise ValueError(
        f"Workflow '{workflow_id}' could not be validated for this run. "
        f"Runtime resolved '{resolved_workflow_id}' instead."
    )


def build_explicit_workflow_override_validator(*, build_task_envelope_for_preview):
    def _validate(
        *,
        selected_workflow_id: str,
        selection_source: str,
        task_summary: str,
        project_name: str,
        project_config: dict | None,
        extra_state: dict | None = None,
    ) -> None:
        return validate_explicit_workflow_override(
            selected_workflow_id=selected_workflow_id,
            selection_source=selection_source,
            task_summary=task_summary,
            project_name=project_name,
            project_config=project_config,
            build_task_envelope_for_preview=build_task_envelope_for_preview,
            extra_state=extra_state,
        )

    return _validate


def _effective_override_context(
    *,
    job: dict[str, Any],
    runtime_state: dict[str, Any],
    meta: dict[str, Any],
    step_execution_id: str,
    workflow_id: str,
    step_id: str,
) -> tuple[str, str, str]:
    """The step scope an override applies to: explicit, else where the run cursor is.

    The cursor's execution id is only taken for the cursor's own step, so an
    override aimed at another step never borrows the active execution.
    """
    cursor = runtime_state.get("run_cursor")
    cursor = cursor if isinstance(cursor, dict) else {}
    cursor_step = str(cursor.get("step_id") or "").strip()
    effective_step = str(
        step_id
        or runtime_state.get("current_step_id")
        or cursor_step
        or job.get("current_step_id")
        or ""
    ).strip()
    effective_execution = str(step_execution_id or "").strip()
    if not effective_execution and effective_step and effective_step == cursor_step:
        effective_execution = str(cursor.get("step_execution_id") or "").strip()
    effective_workflow = str(
        workflow_id
        or runtime_state.get("workflow_id")
        or job.get("selected_workflow")
        or job.get("workflow_id")
        or meta.get("requested_workflow_id")
        or ""
    ).strip()
    return effective_execution, effective_workflow, effective_step


def _gate_outcome_value(
    *,
    job: dict[str, Any],
    bl,
    to_json_obj,
    effective_execution: str,
    effective_workflow: str,
    effective_step: str,
) -> dict[str, Any]:
    action = None
    reason = ""
    target_step = ""
    try:
        latest_route = (
            bl.get_latest_event(job["id"], ["routing_decision", "decision"]) if bl else None
        )
        route_detail = to_json_obj((latest_route or {}).get("detail")) or {}
        if isinstance(route_detail, dict):
            route_action = str(
                route_detail.get("action") or route_detail.get("decision") or ""
            ).strip()
            action = route_action or None
            reason = str(route_detail.get("reason") or "").strip()
            target_step = str(route_detail.get("step_id") or "").strip()
    except Exception:
        action = None
    return {
        "action": action,
        "step_id": effective_step or None,
        "step_execution_id": effective_execution or None,
        "workflow_id": effective_workflow or None,
        "target_step": target_step or None,
        "reason": reason or None,
    }


def derive_override_original_value(
    *,
    job: dict[str, Any],
    domain: str,
    step_execution_id: str,
    workflow_id: str,
    step_id: str,
    bl,
    get_thread_state_values,
    to_json_obj,
) -> Any:
    normalized_domain = str(domain or "").strip().lower()
    runtime_state = get_thread_state_values(job.get("thread_id") or "")
    if not isinstance(runtime_state, dict):
        runtime_state = {}
    meta = to_json_obj(job.get("meta")) or {}
    if not isinstance(meta, dict):
        meta = {}

    effective_execution, effective_workflow, effective_step = _effective_override_context(
        job=job,
        runtime_state=runtime_state,
        meta=meta,
        step_execution_id=step_execution_id,
        workflow_id=workflow_id,
        step_id=step_id,
    )

    if normalized_domain == "gate_outcome":
        return _gate_outcome_value(
            job=job,
            bl=bl,
            to_json_obj=to_json_obj,
            effective_execution=effective_execution,
            effective_workflow=effective_workflow,
            effective_step=effective_step,
        )

    return {}


def build_original_value_deriver(*, get_thread_state_values, to_json_obj):
    def _derive(
        *,
        job: dict[str, Any],
        domain: str,
        step_execution_id: str,
        workflow_id: str,
        step_id: str,
        bl,
    ) -> Any:
        return derive_override_original_value(
            job=job,
            domain=domain,
            step_execution_id=step_execution_id,
            workflow_id=workflow_id,
            step_id=step_id,
            bl=bl,
            get_thread_state_values=get_thread_state_values,
            to_json_obj=to_json_obj,
        )

    return _derive
