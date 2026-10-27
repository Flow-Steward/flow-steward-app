# ruff: noqa: F405, RUF100
# mypy: ignore-errors
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

from .http_app_scheduled_jobs import *  # noqa: F403,F401  # NOSONAR


def _override_workflow_id_from_value(value: Any) -> str:
    return _workflow_override_runtime.override_workflow_id_from_value(value)


def _is_empty_original_value(value: Any) -> bool:
    return _workflow_override_runtime.is_empty_original_value(value)


def _latest_runtime_message_id(runtime_state: dict, *, step_execution_id: str = "") -> str | None:
    return _workflow_override_runtime.latest_runtime_message_id(
        runtime_state,
        step_execution_id=step_execution_id,
    )


def _runtime_step_scope_id(runtime_state: dict) -> str:
    return _workflow_override_runtime.runtime_step_scope_id(runtime_state)


def _derive_override_original_value(**kwargs):
    return _workflow_override_runtime.derive_override_original_value(
        get_thread_state_values=lambda thread_id: _get_thread_state_values(thread_id),
        to_json_obj=lambda value: _to_json_obj(value),
        **kwargs,
    )


_serialize_domain_event_datetime = _domain_event_runtime.serialize_domain_event_datetime


_parse_iso_date_param = _domain_event_runtime.parse_iso_date_param


def _event_project_name(bl, project_id: Any, cache: dict[str, str]) -> str:
    return _domain_event_runtime.event_project_name(bl, project_id, cache)


def _project_account_id(bl, project_id: Any) -> str | None:
    return _domain_event_runtime.project_account_id(bl, project_id)


def _event_row_matches_scope(
    row, account_id=None, allowed_project_ids=None, allowed_workflow_ids=None
):
    return _domain_event_runtime.event_row_matches_scope(
        row,
        default_account_id=DEFAULT_ACCOUNT_ID,
        account_id=account_id,
        allowed_project_ids=allowed_project_ids,
        allowed_workflow_ids=allowed_workflow_ids,
    )


def _resolve_domain_event_by_ref(
    bl, event_ref, account_id=None, allowed_project_ids=None, allowed_workflow_ids=None
):
    return _domain_event_runtime.resolve_domain_event_by_ref(
        bl,
        event_ref,
        default_account_id=DEFAULT_ACCOUNT_ID,
        account_id=account_id,
        allowed_project_ids=allowed_project_ids,
        allowed_workflow_ids=allowed_workflow_ids,
    )


def _serialize_domain_event_summary(row: dict, project_name: str = "") -> dict:
    return _domain_event_runtime.serialize_domain_event_summary(
        row,
        project_name=project_name,
        to_json_obj=lambda value: _to_json_obj(value),
    )


# ---------------------------------------------------------------------------
# Existing REST API endpoints
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------

_WS_TERMINAL = {
    "completed",
    "failed",
    "max_iterations",
    "cancelled",
    _RETRYABLE_INTERRUPTED_STEP_STATUS,
}


def _is_graph_interrupt_trace(text: object | None) -> bool:
    return _serve_interrupt_helpers.is_graph_interrupt_trace(text)


def _extract_interrupt_payload_from_text(text: object | None) -> dict | None:
    return _serve_interrupt_helpers.extract_interrupt_payload_from_text(text)


def _ws_get_approval_payload(job: dict):
    return _serve_interrupt_helpers.ws_get_approval_payload(
        job,
        get_thread_state_snapshot=lambda thread_id: _get_thread_state_snapshot(thread_id),
        state_based_approval_payload=_state_based_approval_payload_impl,
        get_logger_only=lambda: _get_job_store(),
        logger=logger,
    )


def _approval_fallback_payload(job: dict) -> dict:
    return _serve_interrupt_helpers.approval_fallback_payload(
        job,
        to_json_obj=lambda value: _to_json_obj(value),
        get_logger_only=lambda: _get_job_store(),
    )


_run_graph_resume = _graph_resume_runtime.build_run_graph_resume_handler(
    resume_shutdown_in_progress=lambda: _resume_shutdown_in_progress(),
    get_checkpointer=lambda: _get_checkpointer(),
    build_graph=build_registry_graph,
    get_logger_only=lambda: _get_job_store(),
    job_thread_store=job_thread_store,
    to_json_obj=lambda value: _to_json_obj(value),
    append_approval_history=lambda *args, **kwargs: _append_approval_history(*args, **kwargs),
    build_approval_entry=lambda *args, **kwargs: _build_approval_entry(*args, **kwargs),
    logger=logger,
    resolve_artifact=_artifact_plane.get_artifact,
)


def _set_job_status_by_thread_id(thread_id: str, status: str) -> None:
    return _graph_resume_runtime.set_job_status_by_thread_id(
        thread_id,
        status,
        get_logger_only=_get_job_store,
        job_thread_store=job_thread_store,
        logger=logger,
    )


def _resume_project_id(thread_id: str) -> str | None:
    try:
        bl = _get_job_store()
        row = job_thread_store.get_job_context_by_thread_id(bl.conn, thread_id)
    except Exception:
        return None
    project_id = str((row or {}).get("project_id") or "").strip()
    return project_id or None


_start_graph_resume_thread = _graph_resume_runtime.build_start_graph_resume_thread_handler(
    run_graph_resume=lambda *args, **kwargs: _run_graph_resume(*args, **kwargs),
    unregister_resume_thread=_unregister_resume_thread,
    resume_shutdown_in_progress=lambda: _resume_shutdown_in_progress(),
    resume_threads=_resume_threads,
    resume_threads_lock=_resume_threads_lock,
    fenced_project_ids=_resume_fenced_project_ids,
    resolve_project_id=_resume_project_id,
    logger=logger,
    execution_mode=lambda: _runtime_env.get("FS_GRAPH_RESUME_EXECUTOR", "subprocess"),
)

__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
