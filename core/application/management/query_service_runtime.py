"""Reusable factories for transport-independent query service wiring."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.extensions.query_service import create_extension_query_service
from core.application.management.query_services import (
    ApprovalQueryService,
    EventQueryService,
    ExtensionQueryService,
    JobQueryService,
    ProjectQueryService,
)
from core.application.observability_vocabulary import (
    job_attention_group as _job_attention_group_meta,
)
from core.application.observability_vocabulary import (
    job_status_meta,
)
from core.application.workflows.workflow_hook_contract import hook_run_label
from core.application.workflows.workflow_start_runtime import WORKFLOW_RUN_SUMMARY_KEY


def job_attention_group(
    status: str,
    acknowledged_at: Any = None,
    *,
    retryable_interrupted_step_status: str,
) -> str:
    return _job_attention_group_meta(
        status,
        acknowledged_at,
        retryable_interrupted_step_status=retryable_interrupted_step_status,
    )


def truncate_text(value: Any, max_len: int = 4000) -> str:
    text = str(value or "")
    limit = max(1, int(max_len or 4000))
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def serialize_attention_job_row(
    row: dict[str, Any],
    *,
    truncate_text_fn,
    build_display_summary_fn,
) -> dict[str, Any]:
    task_summary = truncate_text_fn(row.get("task_summary"), max_len=512)
    display_summary = build_display_summary_fn(task_summary)
    status = _opt_str(row.get("status")) or ""
    status_meta = job_status_meta(status)
    summary_params = row.get("task_summary_params")
    workflow_id = summary_params.get("workflow_id") if isinstance(summary_params, dict) else None
    summary_workflow_id = workflow_id.strip() if isinstance(workflow_id, str) else ""
    generated_summary = row.get("task_summary_key") == WORKFLOW_RUN_SUMMARY_KEY and bool(
        summary_workflow_id
    )
    return {
        "job_id": _opt_str(row.get("id")),
        "project_id": _opt_str(row.get("project_id")),
        "project_name": _opt_str(row.get("project_name")),
        "task_id": _opt_str(row.get("task_id")),
        "task_source": _opt_str(row.get("task_source")),
        "duration_ms": row.get("duration_ms"),
        "task_summary": task_summary,
        "task_summary_key": WORKFLOW_RUN_SUMMARY_KEY if generated_summary else None,
        "task_summary_params": {"workflow_id": summary_workflow_id} if generated_summary else {},
        "display_summary": display_summary,
        "status": status_meta["code"],
        "status_label": str(status_meta.get("label") or ""),
        "status_tone": str(status_meta.get("tone") or "gray"),
        "status_terminal": bool(status_meta.get("terminal")),
        "attention_group": _job_attention_group_meta(
            status,
            row.get("acknowledged_at"),
            retryable_interrupted_step_status="retryable_interrupted_step",
        ),
        "worker_id": _opt_str(row.get("worker_id")),
        "selected_workflow": _opt_str(row.get("selected_workflow") or row.get("workflow_id")),
        # A run of a hook's steps reads as the hook, never as its storage workflow.
        "selected_workflow_display_name": _opt_str(
            hook_run_label(row) or row.get("selected_workflow_display_name")
        ),
        "current_step_id": _opt_str(row.get("current_step_id")),
        "current_group_id": _opt_str(row.get("current_group_id")),
        "current_agent": _opt_str(row.get("current_agent")),
        "last_error": _opt_str(row.get("last_error")),
        "queued_at": row["queued_at"].isoformat() if row.get("queued_at") else None,
        "started_at": row["started_at"].isoformat() if row.get("started_at") else None,
        "finished_at": row["finished_at"].isoformat() if row.get("finished_at") else None,
        "heartbeat_at": row["heartbeat_at"].isoformat() if row.get("heartbeat_at") else None,
        "acknowledged_at": row["acknowledged_at"].isoformat()
        if row.get("acknowledged_at")
        else None,
        "acknowledged_by": _opt_str(row.get("acknowledged_by")),
    }


def build_attention_job_row_serializer(*, truncate_text_fn, build_display_summary_fn):
    def _serializer(row: dict[str, Any]) -> dict[str, Any]:
        return serialize_attention_job_row(
            row,
            truncate_text_fn=truncate_text_fn,
            build_display_summary_fn=build_display_summary_fn,
        )

    return _serializer


def serialize_awaiting_approval_job_row(
    row: dict[str, Any],
    *,
    truncate_text_fn,
    build_display_summary_fn,
) -> dict[str, Any]:
    task_summary = truncate_text_fn(row.get("task_summary"), max_len=512)
    display_summary = build_display_summary_fn(task_summary) or task_summary
    status = _opt_str(row.get("status") or "awaiting_approval") or "awaiting_approval"
    status_meta = job_status_meta(status)
    return {
        "id": _opt_str(row.get("id")),
        "project_id": _opt_str(row.get("project_id")),
        "project_name": _opt_str(row.get("project_name")),
        "task_summary": display_summary,
        "display_summary": display_summary,
        "selected_workflow": _opt_str(row.get("selected_workflow") or row.get("workflow_id")),
        "started_at": row["started_at"].isoformat() if row.get("started_at") else None,
        "thread_id": _opt_str(row.get("thread_id")),
        "status": status_meta["code"],
        "status_label": str(status_meta.get("label") or ""),
        "status_tone": str(status_meta.get("tone") or "gray"),
        "status_terminal": bool(status_meta.get("terminal")),
        "attention_group": _job_attention_group_meta(
            status,
            row.get("acknowledged_at"),
            retryable_interrupted_step_status="retryable_interrupted_step",
        ),
    }


def build_awaiting_approval_job_row_serializer(*, truncate_text_fn, build_display_summary_fn):
    def _serializer(row: dict[str, Any]) -> dict[str, Any]:
        return serialize_awaiting_approval_job_row(
            row,
            truncate_text_fn=truncate_text_fn,
            build_display_summary_fn=build_display_summary_fn,
        )

    return _serializer


def resolve_replay_message_row(
    bl,
    *,
    job_id: str,
    message_id: str = "",
    correlation_id: str = "",
) -> tuple[dict[str, Any] | None, str | None, int]:
    row = None
    normalized_message_id = str(message_id or "").strip()
    if normalized_message_id:
        candidate = bl.get_agent_message_by_id(normalized_message_id)
        if candidate and str(candidate.get("job_id") or "").strip() == str(job_id).strip():
            row = candidate
        elif candidate:
            return None, "Message does not belong to this job", 400
    elif str(correlation_id or "").strip():
        corr = str(correlation_id).strip()
        rows = bl.get_agent_messages(job_id, since_id=0, limit=2000)
        for msg_row in reversed(rows or []):
            if str((msg_row or {}).get("correlation_id") or "") == corr:
                row = msg_row
                break
    else:
        return None, "message_id or correlation_id is required", 400

    if not row:
        return None, "Message not found", 404
    return row, None, 200


def create_job_query_service(
    *,
    get_logger,
    get_event_store=None,
    get_trace_store=None,
    serialize_attention_job_row,
    serialize_awaiting_approval_job_row,
    job_attention_group,
    build_runtime_payload_for_api,
    build_display_summary,
    serialize_log_row,
    serialize_event_row,
    serialize_agent_message_row,
    serialize_decision_override_row,
    serialize_job_error_row,
    serialize_job_step_replay_row,
    build_job_causal_chain,
    resolve_replay_message_row,
    collect_replay_step_trace,
    get_thread_state_snapshot,
    build_runtime_payload,
    resolve_job_project_name,
    resolve_artifact=None,
    get_project_by_id=None,
    get_committed_checkpoint_values=None,
) -> JobQueryService:
    return JobQueryService(
        get_logger=get_logger,
        get_event_store=get_event_store,
        get_trace_store=get_trace_store,
        serialize_attention_job_row=serialize_attention_job_row,
        serialize_awaiting_approval_job_row=serialize_awaiting_approval_job_row,
        job_attention_group=job_attention_group,
        build_runtime_payload_for_api=build_runtime_payload_for_api,
        build_display_summary=build_display_summary,
        serialize_log_row=serialize_log_row,
        serialize_event_row=serialize_event_row,
        serialize_agent_message_row=serialize_agent_message_row,
        serialize_decision_override_row=serialize_decision_override_row,
        serialize_job_error_row=serialize_job_error_row,
        serialize_job_step_replay_row=serialize_job_step_replay_row,
        build_job_causal_chain=build_job_causal_chain,
        resolve_replay_message_row=resolve_replay_message_row,
        collect_replay_step_trace=collect_replay_step_trace,
        get_thread_state_snapshot=get_thread_state_snapshot,
        get_committed_checkpoint_values=get_committed_checkpoint_values,
        build_runtime_payload=build_runtime_payload,
        resolve_job_project_name=resolve_job_project_name,
        resolve_artifact=resolve_artifact,
        get_project_by_id=get_project_by_id,
    )


def create_job_query_service_from_runtime(
    *,
    get_logger,
    get_event_store=None,
    get_trace_store=None,
    serialize_attention_job_row,
    serialize_awaiting_approval_job_row,
    job_attention_group,
    build_runtime_payload_for_api,
    build_display_summary,
    build_job_causal_chain,
    resolve_replay_message_row,
    collect_replay_step_trace,
    get_thread_state_snapshot,
    build_runtime_payload,
    resolve_job_project_name,
    snapshot_serializers_module,
    resolve_artifact=None,
    get_project_by_id=None,
    get_committed_checkpoint_values=None,
) -> JobQueryService:
    return create_job_query_service(
        get_logger=get_logger,
        get_event_store=get_event_store,
        get_trace_store=get_trace_store,
        serialize_attention_job_row=serialize_attention_job_row,
        serialize_awaiting_approval_job_row=serialize_awaiting_approval_job_row,
        job_attention_group=job_attention_group,
        build_runtime_payload_for_api=build_runtime_payload_for_api,
        build_display_summary=build_display_summary,
        serialize_log_row=snapshot_serializers_module.serialize_log_row,
        serialize_event_row=snapshot_serializers_module.serialize_event_row,
        serialize_agent_message_row=snapshot_serializers_module.serialize_agent_message_row,
        serialize_decision_override_row=snapshot_serializers_module.serialize_decision_override_row,
        serialize_job_error_row=snapshot_serializers_module.serialize_job_error_row,
        serialize_job_step_replay_row=snapshot_serializers_module.serialize_job_step_replay_row,
        build_job_causal_chain=build_job_causal_chain,
        resolve_replay_message_row=resolve_replay_message_row,
        collect_replay_step_trace=collect_replay_step_trace,
        get_thread_state_snapshot=get_thread_state_snapshot,
        get_committed_checkpoint_values=get_committed_checkpoint_values,
        build_runtime_payload=build_runtime_payload,
        resolve_job_project_name=resolve_job_project_name,
        resolve_artifact=resolve_artifact,
        get_project_by_id=get_project_by_id,
    )


def build_job_query_service_factory(**kwargs) -> Callable[[], JobQueryService]:
    def _factory() -> JobQueryService:
        return create_job_query_service_from_runtime(**kwargs)

    return _factory


def create_project_query_service(
    *,
    get_logger,
    get_job_store=None,
    serialize_projects_grid_row,
    get_queue_depth,
    get_ready_queue_depth,
    to_json_obj,
    project_settings_diff,
    list_workflow_choices,
    sanitize_external_input,
    resolve_workflow_selection,
    validate_explicit_workflow_override,
    build_task_envelope_for_preview,
    normalize_orchestrator_state,
    plan_workflow_preview,
) -> ProjectQueryService:
    return ProjectQueryService(
        get_logger=get_logger,
        get_job_store=get_job_store,
        serialize_projects_grid_row=serialize_projects_grid_row,
        get_queue_depth=get_queue_depth,
        get_ready_queue_depth=get_ready_queue_depth,
        to_json_obj=to_json_obj,
        project_settings_diff=project_settings_diff,
        list_workflow_choices=list_workflow_choices,
        sanitize_external_input=sanitize_external_input,
        resolve_workflow_selection=resolve_workflow_selection,
        validate_explicit_workflow_override=validate_explicit_workflow_override,
        build_task_envelope_for_preview=build_task_envelope_for_preview,
        normalize_orchestrator_state=normalize_orchestrator_state,
        plan_workflow_preview=plan_workflow_preview,
    )


def create_project_query_service_from_runtime(
    *,
    get_logger,
    get_job_store=None,
    serialize_projects_grid_row,
    get_queue_depth,
    get_ready_queue_depth,
    to_json_obj,
    project_settings_diff,
    list_workflow_choices,
    sanitize_external_input,
    resolve_workflow_selection,
    validate_explicit_workflow_override,
    build_task_envelope_for_preview,
    plan_workflow_preview,
) -> ProjectQueryService:
    from core.application.orchestration.state import normalize_orchestrator_state

    return create_project_query_service(
        get_logger=get_logger,
        get_job_store=get_job_store,
        serialize_projects_grid_row=serialize_projects_grid_row,
        get_queue_depth=get_queue_depth,
        get_ready_queue_depth=get_ready_queue_depth,
        to_json_obj=to_json_obj,
        project_settings_diff=project_settings_diff,
        list_workflow_choices=list_workflow_choices,
        sanitize_external_input=sanitize_external_input,
        resolve_workflow_selection=resolve_workflow_selection,
        validate_explicit_workflow_override=validate_explicit_workflow_override,
        build_task_envelope_for_preview=build_task_envelope_for_preview,
        normalize_orchestrator_state=normalize_orchestrator_state,
        plan_workflow_preview=plan_workflow_preview,
    )


def build_project_query_service_factory(**kwargs) -> Callable[[], ProjectQueryService]:
    def _factory() -> ProjectQueryService:
        return create_project_query_service_from_runtime(**kwargs)

    return _factory


def create_event_query_service(
    *,
    get_logger,
    event_project_name,
    resolve_domain_event_by_ref,
    serialize_domain_event_summary,
    serialize_domain_event_datetime,
    to_json_obj,
    workflow_exists,
) -> EventQueryService:
    return EventQueryService(
        get_logger=get_logger,
        event_project_name=event_project_name,
        resolve_domain_event_by_ref=resolve_domain_event_by_ref,
        serialize_domain_event_summary=serialize_domain_event_summary,
        serialize_domain_event_datetime=serialize_domain_event_datetime,
        to_json_obj=to_json_obj,
        workflow_exists=workflow_exists,
    )


def build_event_query_service_factory(**kwargs) -> Callable[[], EventQueryService]:
    def _factory() -> EventQueryService:
        return create_event_query_service(**kwargs)

    return _factory


def create_approval_query_service(
    *,
    get_logger,
    get_thread_state_snapshot,
    approval_fallback_payload,
    to_json_obj,
    get_project_by_id=None,
) -> ApprovalQueryService:
    return ApprovalQueryService(
        get_logger=get_logger,
        get_thread_state_snapshot=get_thread_state_snapshot,
        approval_fallback_payload=approval_fallback_payload,
        to_json_obj=to_json_obj,
        get_project_by_id=get_project_by_id,
    )


def build_approval_query_service_factory(**kwargs) -> Callable[[], ApprovalQueryService]:
    def _factory() -> ApprovalQueryService:
        return create_approval_query_service(**kwargs)

    return _factory


def build_extension_query_service_factory(**kwargs) -> Callable[[], ExtensionQueryService]:
    def _factory() -> ExtensionQueryService:
        return create_extension_query_service(**kwargs)

    return _factory
