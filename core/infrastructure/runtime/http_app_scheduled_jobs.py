# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from .http_app_reactions_extensions import *  # noqa: F403,F401  # NOSONAR


_workflow_availability_service = None


def _assert_can_start_workflow(
    *,
    workflow_id: str,
    account_id: str,
    project_id: str | None = None,
) -> None:
    global _workflow_availability_service
    if _get_identity_store() is None:
        return
    if _workflow_availability_service is None:
        _workflow_availability_service = _build_workflow_availability_service_impl(
            get_logger=lambda: _get_identity_store()
        )
    try:
        _workflow_availability_service.assert_can_start_workflow(
            workflow_id=workflow_id,
            account_id=account_id,
            project_id=str(project_id) if project_id is not None else None,
        )
    except Exception as exc:
        payload = getattr(exc, "payload", {}) if hasattr(exc, "payload") else {}
        reason_code = str((payload or {}).get("availability_reason_code") or "").strip().lower()
        if reason_code in {"workflow_not_found"}:
            logger.debug("Workflow availability bypassed for unknown workflow %s", workflow_id)
            return
        raise


def _write_project_profile_to_kb(project_row: dict) -> None:
    return _write_project_profile_impl(project_row, logger=logger)


def _derive_override_original_value(**kwargs):
    return _workflow_override_runtime.derive_override_original_value(
        get_thread_state_values=lambda thread_id: _get_thread_state_values(thread_id),
        to_json_obj=lambda value: _to_json_obj(value),
        **kwargs,
    )


def _build_app_scoped_service_factories(
    *,
    get_logger_only,
    get_job_store=None,
    get_project_store=None,
    get_event_store=None,
    get_trace_store=None,
):
    return _serve_service_wiring.build_service_factories(
        config=CONFIG,
        artifact_plane=_artifact_plane,
        get_logger_only=get_logger_only,
        get_job_store=get_job_store,
        get_project_store=get_project_store,
        get_event_store=get_event_store,
        get_trace_store=get_trace_store,
        serialize_attention_job_row=lambda row: _serialize_attention_job_row(row),
        serialize_awaiting_approval_job_row=lambda row: _serialize_awaiting_approval_job_row(row),
        job_attention_group=lambda status, acknowledged_at=None: _job_attention_group(
            status,
            acknowledged_at,
        ),
        build_runtime_payload_for_api=lambda job, bl: _build_runtime_payload_for_api(job, bl),
        build_display_summary=lambda value: build_display_summary(value),
        build_job_causal_chain=lambda bl, job_id: _build_job_causal_chain(bl, job_id),
        resolve_replay_message_row=lambda *args, **kwargs: _resolve_replay_message_row(
            *args,
            **kwargs,
        ),
        collect_replay_step_trace=lambda bl, job_id, row: _collect_replay_step_trace(
            bl, job_id, row
        ),
        get_thread_state_snapshot=lambda thread_id: _get_thread_state_snapshot(thread_id),
        build_runtime_payload=lambda job, bl: _build_runtime_payload(job, bl),
        resolve_job_project_name=lambda bl, job: _resolve_job_project_name(bl, job),
        snapshot_serializers_module=_serve_snapshot_serializers_runtime,
        serialize_projects_grid_row=lambda row: _serialize_projects_grid_row(row),
        get_queue_depth=lambda project_id: _get_queue_depth(project_id),
        get_ready_queue_depth=lambda: _get_ready_queue_depth(),
        to_json_obj=lambda value: _to_json_obj(value),
        project_settings_diff=lambda from_snapshot, to_snapshot: _project_settings_diff(
            from_snapshot,
            to_snapshot,
        ),
        list_workflow_choices=lambda **kwargs: _list_workflow_choices(**kwargs),
        sanitize_external_input=lambda value: sanitize_external_input(value),
        resolve_workflow_selection=lambda requested, _selection_source="", project_workflows=None: (
            _resolve_workflow_selection(
                requested,
                project_workflows,
            )
        ),
        validate_explicit_workflow_override=lambda **kwargs: _validate_explicit_workflow_override(
            **kwargs,
        ),
        derive_override_original_value=lambda **kwargs: _derive_override_original_value(**kwargs),
        build_task_envelope_for_preview=lambda task_summary: _build_task_envelope_for_preview(
            task_summary,
        ),
        plan_workflow_preview=lambda envelope, state: _plan_project_workflow_preview(
            envelope, state
        ),
        build_rerun_workflow_selection=lambda job, *, replay_snapshot: (
            _build_rerun_workflow_selection(job, replay_snapshot=replay_snapshot)
        ),
        load_replay_snapshot=lambda **kwargs: _load_job_replay_snapshot(
            artifact_plane=_artifact_plane,
            **kwargs,
        ),
        enqueue_task=lambda **kwargs: _enqueue_task(**kwargs),
        get_thread_state_values=lambda thread_id: _get_thread_state_values(thread_id),
        append_runtime_journal=lambda *args, **kwargs: _append_runtime_journal(*args, **kwargs),
        enqueue_task_in_queue=lambda **kwargs: _enqueue_task_in_queue(**kwargs),
        invoke_step_reexecute_runtime=lambda **kwargs: _invoke_step_reexecute_runtime(**kwargs),
        invoke_step_replay_llm=lambda **kwargs: _invoke_step_replay_llm(**kwargs),
        workflow_override_runtime_module=_workflow_override_runtime,
        interrupted_step_runtime_module=_interrupted_step_runtime,
        query_service_runtime_module=_query_service_runtime,
        step_replay_runtime_module=_step_replay_runtime,
        retryable_interrupted_step_status=_RETRYABLE_INTERRUPTED_STEP_STATUS,
        run_scheduled_job_once=lambda *args, **kwargs: _run_scheduled_job_once(*args, **kwargs),
        workflow_availability_assert=lambda **kwargs: _assert_can_start_workflow(**kwargs),
        write_project_profile_to_kb=lambda profile: _write_project_profile_to_kb(profile),
        workflow_helpers_module=_serve_workflow_helpers_runtime,
    )


(
    _job_query_service,
    _project_query_service,
    _job_command_service,
    _project_command_service,
) = _build_app_scoped_service_factories(
    get_logger_only=lambda: _get_connection_store(),
    get_job_store=lambda: _get_job_store(),
    get_project_store=lambda: _get_project_store(),
    get_event_store=lambda: _get_event_store(),
    get_trace_store=lambda: _get_observability_store(),
)


def _as_utc_datetime(value: Any, fallback: datetime | None = None) -> datetime | None:
    return _control_scans._as_utc_datetime(value, fallback=fallback)


def _scheduled_job_next_run_at(
    cron_expr: str,
    *,
    scheduled_for: datetime,
    now_utc: datetime,
    schedule_tz: str | None = None,
) -> datetime:
    # Misfire policy: do not backfill missed slots; advance from "now" when late.
    reference_at = scheduled_for if scheduled_for >= now_utc else now_utc
    return _cron_next_run_at(cron_expr, reference_at, schedule_tz=schedule_tz)


def _get_scheduled_project_run_config(project_id: str):
    project_store = _get_project_store()
    getter = getattr(project_store, "get_project_run_config", None)
    return getter(project_id) if callable(getter) else None


(
    _record_scheduled_job_outcome,
    _run_scheduled_job_once,
    _scheduled_jobs_scan,
) = _serve_service_wiring.build_scheduled_job_bundle(
    scheduled_jobs_runtime_config_fn=_scheduled_jobs_runtime_config,
    control_run_scheduled_job_once=_CONTROL_RUN_SCHEDULED_JOB_ONCE,
    control_record_scheduled_job_outcome=lambda bl, **kwargs: _CONTROL_RECORD_SCHEDULED_JOB_OUTCOME(
        bl,
        **kwargs,
    ),
    control_scheduled_jobs_scan=_CONTROL_SCHEDULED_JOBS_SCAN,
    scheduled_job_next_run_at_fn=lambda *args, **kwargs: _scheduled_job_next_run_at(
        *args,
        **kwargs,
    ),
    as_utc_datetime_fn=lambda *args, **kwargs: _as_utc_datetime(*args, **kwargs),
    run_scheduled_job_once_getter=lambda: _run_scheduled_job_once,
    get_project_run_config_fn=_get_scheduled_project_run_config,
)


_dashboard_rollup_scan = lambda bl: _logger_runtime.dashboard_rollup_scan(  # noqa: E731
    bl,
    control_config=_control_config,
    dataclass_replace=_dataclass_replace,
)


def _external_effect_maintenance_scan(bl):
    try:
        from core.infrastructure.runtime.control.scans import external_effect_maintenance_scan

        return external_effect_maintenance_scan(
            bl,
            _control_config,
            artifact_plane=_artifact_plane,
        )
    except Exception as exc:
        logger.warning("external-effect output reconciliation scan failed: %s", exc)
        return {"scanned_accounts": 0, "failed_accounts": 1, "resolved": 0}


# NOTE: _scheduled_jobs_loop, _start_scheduled_jobs_thread, _stop_scheduled_jobs_thread
# have been moved to control/loops.py.  The _startup() lifespan now uses
# start_control_threads() with a scan callback that calls _scheduled_jobs_scan().


# ---------------------------------------------------------------------------
# Shared attachment helpers
# ---------------------------------------------------------------------------

#
# NOTE:
# - `/projects/{project_id}/tasks/with-attachments` moved to core.entrypoints.api.project_detail
# - `/processes` monitoring APIs moved to core.entrypoints.api.processes
# - `/jobs/{job_id}` is served by the React shell; job data lives under core.entrypoints.api.jobs
# - `/api/dashboard/summary` and `/api/slo/core` moved to core.entrypoints.api.dashboard_metrics

__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
