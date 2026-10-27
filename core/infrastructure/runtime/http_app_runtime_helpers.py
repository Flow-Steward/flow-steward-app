# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors

from core.application.auth import request_principal_runtime as _request_principal_runtime
from datetime import datetime

from .maintenance_http import runtime_maintenance_state
from .http_app_auth_runtime import *  # noqa: F403,F401  # NOSONAR


_password_reset_token_hash = _password_reset_token_hash_impl


_normalize_requested_roles = _request_principal_runtime.normalize_requested_roles


_log_auth_audit_event = _serve_auth_runtime.build_auth_audit_logger(
    to_json_obj=_to_json_obj,
)


def _compute_readiness(bl, *, lifecycle_pause_active_fn) -> dict:
    return _readiness_runtime.compute_readiness(
        bl,
        config=CONFIG,
        env_flag_fn=_env_flag,
        watchdog_scan_fn=_watchdog_scan,
        queue_delivery_summary_fn=_queue_delivery_summary_impl,
        lifecycle_journals_fn=_readiness_runtime.load_lifecycle_journals,
        lifecycle_pause_active_fn=lifecycle_pause_active_fn,
    )


def _redact_snapshot_secrets(value: Any):
    return _redact_snapshot_secrets_impl(value, secret_key_re=_PROJECT_CONFIG_SECRET_KEY_RE)


def _build_project_config_snapshot(project_config: dict | None) -> dict:
    return _build_project_config_snapshot_impl(
        project_config,
        secret_key_re=_PROJECT_CONFIG_SECRET_KEY_RE,
    )


def _json_value_equal(left: Any, right: Any) -> bool:
    return _json_value_equal_impl(left, right)


def _project_settings_diff(
    from_snapshot: dict | None,
    to_snapshot: dict | None,
) -> dict:
    return _project_settings_diff_impl(
        from_snapshot,
        to_snapshot,
        json_value_equal_fn=_json_value_equal,
    )


def _policy_snapshot_hash(policy_snapshot: dict | None) -> str | None:
    return _policy_snapshot_hash_impl(policy_snapshot)


def _serialize_log_row(row: dict, job_id: str | None) -> dict:
    return _serialize_log_row_impl(row, job_id, to_json_obj=_to_json_obj)


def _serialize_event_row(row: dict) -> dict:
    return _serialize_event_row_impl(row, to_json_obj=_to_json_obj)


def _serialize_agent_message_row(row: dict) -> dict:
    return _serialize_agent_message_row_impl(row, to_json_obj=_to_json_obj)


def _serialize_decision_override_row(row: dict) -> dict:
    return _serialize_decision_override_row_impl(row, to_json_obj=_to_json_obj)


def _serialize_job_error_row(row: dict) -> dict:
    return _serialize_job_error_row_impl(row, to_json_obj=_to_json_obj)


def _serialize_job_step_replay_row(row: dict) -> dict:
    return _serialize_job_step_replay_row_impl(row, to_json_obj=_to_json_obj)


def _get_thread_state_values(thread_id: str) -> dict:
    """Return checkpoint state values for thread_id, or {} on failure."""
    snap = _get_thread_state_snapshot(thread_id)
    return (snap.values or {}) if snap else {}


def _compute_wait_reason(
    job: dict, runtime_state: dict, approval_payload: dict | None = None
) -> str:
    return _serve_request_runtime.compute_wait_reason(
        job,
        runtime_state,
        retryable_interrupted_step_status=_RETRYABLE_INTERRUPTED_STEP_STATUS,
        approval_payload=approval_payload,
    )


def _parse_worker_pid(worker_id: str | None) -> int | None:
    return _CONTROL_PARSE_WORKER_PID(worker_id)


def _signalable_worker_pid(worker_id: str | None) -> int | None:
    """Worker pid only when this process can legitimately signal it."""
    return _CONTROL_SIGNALABLE_WORKER_PID(worker_id)


def _pid_alive(pid: int | None) -> bool:
    return _CONTROL_PID_ALIVE(pid)


def _append_runtime_journal(
    bl, job_id: str, *, event: str, reason: str, detail: dict | None = None
) -> dict:
    return _CONTROL_APPEND_RUNTIME_JOURNAL(
        bl,
        job_id,
        event=event,
        reason=reason,
        detail=detail,
        max_entries=_WATCHDOG_MAX_JOURNAL_ENTRIES,
    )


def _lease_heartbeat_age_seconds(lease_row: dict | None, now: datetime) -> int | None:
    return _CONTROL_LEASE_HEARTBEAT_AGE_SECONDS(lease_row, now)


def _lease_timestamp_age_seconds(lease_row: dict | None, now: datetime, key: str) -> int | None:
    return _CONTROL_LEASE_TIMESTAMP_AGE_SECONDS(lease_row, now, key)


def _lease_is_live(lease_row: dict | None, now: datetime) -> bool:
    return _CONTROL_LEASE_IS_LIVE(lease_row, now, _WATCHDOG_ORPHAN_GRACE_SECONDS)


def _index_active_project_leases(bl) -> dict[str, list[dict]]:
    return _CONTROL_INDEX_ACTIVE_PROJECT_LEASES(bl)


def _list_watchdog_recovery_candidates(bl, *, limit: int = 500) -> list[dict]:
    return _CONTROL_LIST_WATCHDOG_RECOVERY_CANDIDATES(bl, limit=limit)


def _classify_runtime_health(
    job: dict, now: datetime, *, project_lease: dict | None = None
) -> dict | None:
    return _CONTROL_CLASSIFY_RUNTIME_HEALTH(
        job,
        now,
        project_leases=project_lease,
        cfg=_watchdog_runtime_config(),
    )


def _classify_watchdog_recovery(
    job: dict, now: datetime, *, project_lease: dict | None = None
) -> dict | None:
    return _CONTROL_CLASSIFY_WATCHDOG_RECOVERY(
        job,
        now,
        project_leases=project_lease,
        cfg=_watchdog_runtime_config(),
    )


def _update_job_status_with_reason(bl, job_id, status, reason, heartbeat_now=False):
    return _CONTROL_UPDATE_JOB_STATUS_WITH_REASON(
        bl,
        job_id,
        status=status,
        reason=reason,
        heartbeat_now=heartbeat_now,
    )


def _watchdog_scan(bl, apply=False):
    maintenance_activated_at = None
    try:
        maintenance_state = runtime_maintenance_state()
        if maintenance_state is not None:
            parsed_activated_at = datetime.fromisoformat(str(maintenance_state["activated_at"]))
            if parsed_activated_at.tzinfo is not None:
                maintenance_activated_at = parsed_activated_at
    except (TypeError, ValueError):
        maintenance_activated_at = None
    return _watchdog_runtime.run_watchdog_scan(
        bl,
        apply=apply,
        watchdog_runtime_config=_watchdog_runtime_config,
        control_scans_module=_control_scans,
        control_watchdog_scan=_CONTROL_WATCHDOG_SCAN,
        detect_retryable_interrupted_step_snapshot=_detect_retryable_interrupted_step_snapshot,
        retryable_interrupted_step_status=_RETRYABLE_INTERRUPTED_STEP_STATUS,
        append_runtime_journal=_append_runtime_journal,
        update_job_status_with_reason=lambda bl_arg, job_id_arg, status, reason, heartbeat_now=False: (
            _update_job_status_with_reason(
                bl_arg,
                job_id_arg,
                status=status,
                reason=reason,
                heartbeat_now=heartbeat_now,
            )
        ),
        maintenance_activated_at=maintenance_activated_at,
    )


_terminate_worker_pid = _process_runtime.build_terminate_worker_pid_handler(
    grace_seconds=3.0,
    pid_alive_fn=_pid_alive,
    kill_fn=os.kill,
    sigterm=signal.SIGTERM,
    sigkill=signal.SIGKILL,
    now_fn=time.time,
    sleep_fn=time.sleep,
)


# NOTE: _watchdog_loop, _start_watchdog_thread, _stop_watchdog_thread have been
# moved to control/loops.py.  The _startup() lifespan now uses
# start_control_threads() with a scan callback that calls _watchdog_scan().


_build_runtime_payload = _job_runtime_payload.build_runtime_payload_builder(
    get_thread_state_values=lambda thread_id: _get_thread_state_values(thread_id),
    to_json_obj=_to_json_obj,
    serialize_job_error_row=_serialize_job_error_row,
    is_graph_interrupt_trace=lambda value: _serve_interrupt_helpers.is_graph_interrupt_trace(value),
    ws_get_approval_payload=lambda job: _serve_interrupt_helpers.ws_get_approval_payload(
        job,
        get_thread_state_snapshot=lambda thread_id: _get_thread_state_snapshot(thread_id),
        state_based_approval_payload=_state_based_approval_payload_impl,
        get_logger_only=lambda: _get_job_store(),
        logger=logger,
    ),
    sanitize_retryable_interrupted_step_snapshot=lambda snapshot: (
        _sanitize_retryable_interrupted_step_snapshot(snapshot)
    ),
    compute_wait_reason=lambda job, runtime_state, approval_payload: _compute_wait_reason(
        job,
        runtime_state,
        approval_payload,
    ),
    serialize_event_row=lambda row: _serialize_event_row(row),
    serialize_agent_message_row=lambda row: _serialize_agent_message_row(row),
)


def _build_runtime_payload_fallback(job: dict) -> dict:
    """Return a lightweight runtime snapshot when live state inspection is unavailable."""
    return _job_runtime_payload.build_runtime_payload_fallback(
        job,
        watchdog_stuck_seconds=_WATCHDOG_STUCK_SECONDS,
    )


async def _build_runtime_payload_for_api(job: dict, bl) -> dict:
    return await _serve_request_runtime.build_runtime_payload_for_api(
        job,
        bl,
        build_runtime_payload=_build_runtime_payload,
        build_runtime_payload_fallback=_build_runtime_payload_fallback,
        timeout_seconds=_JOB_RUNTIME_PAYLOAD_TIMEOUT_SECONDS,
        logger=logger,
        asyncio_module=asyncio,
    )


_collect_replay_step_trace = _job_trace_runtime.build_collect_replay_step_trace_handler(
    serialize_agent_message_row=_serialize_agent_message_row,
    serialize_event_row=_serialize_event_row,
    serialize_log_row=_serialize_log_row,
    to_json_obj=_to_json_obj,
)


_sanitize_retryable_interrupted_step_snapshot = (
    _interrupted_step_runtime.sanitize_interrupted_step_snapshot
)


_extract_runtime_step_hint = _interrupted_step_runtime.extract_runtime_step_hint


def _detect_retryable_interrupted_step_snapshot(bl, job: dict) -> dict | None:
    return _interrupted_step_runtime.detect_interrupted_step_snapshot(
        bl,
        job,
        get_thread_state_values_fn=_get_thread_state_values,
        to_json_obj=_to_json_obj,
        sanitize_snapshot_fn=_sanitize_retryable_interrupted_step_snapshot,
        extract_step_hint_fn=_extract_runtime_step_hint,
    )


def _job_attention_group(status: str, acknowledged_at=None) -> str:
    return _query_service_runtime.job_attention_group(
        status,
        acknowledged_at,
        retryable_interrupted_step_status=_RETRYABLE_INTERRUPTED_STEP_STATUS,
    )


_truncate_text = _query_service_runtime.truncate_text


_serialize_attention_job_row = _query_service_runtime.build_attention_job_row_serializer(
    truncate_text_fn=lambda value, max_len=4000: _truncate_text(value, max_len=max_len),
    build_display_summary_fn=build_display_summary,
)


_serialize_awaiting_approval_job_row = (
    _query_service_runtime.build_awaiting_approval_job_row_serializer(
        truncate_text_fn=lambda value, max_len=4000: _truncate_text(value, max_len=max_len),
        build_display_summary_fn=build_display_summary,
    )
)


_resolve_job_project_name = _job_trace_runtime.build_resolve_job_project_name(
    to_json_obj=lambda value: _to_json_obj(value),
)


_mermaid_label = _job_trace_runtime.mermaid_label


_build_job_causal_chain = _job_trace_runtime.build_job_causal_chain_builder(
    serialize_agent_message_row=lambda row: _serialize_agent_message_row(row),
    serialize_event_row=lambda row: _serialize_event_row(row),
    mermaid_label_fn=lambda text: _mermaid_label(text),
)


_resolve_replay_message_row = _query_service_runtime.resolve_replay_message_row


_extract_replay_prompt_capture = _step_replay_runtime.extract_replay_prompt_capture


_derive_original_replay_outcome = _step_replay_runtime.derive_original_replay_outcome


_build_replay_diff_excerpt = _step_replay_runtime.build_replay_diff_excerpt


_invoke_step_replay_llm, _invoke_step_reexecute_runtime = (
    _step_replay_runtime.build_runtime_invokers(
        policy_snapshot_hash=lambda value: _policy_snapshot_hash(value),
        to_json_obj=lambda value: _to_json_obj(value),
        serialize_agent_message_row=lambda row: _serialize_agent_message_row(row),
    )
)


_build_approval_entry = _serve_interrupt_helpers.build_approval_entry


_same_approval_decision = _serve_interrupt_helpers.same_approval_decision


_append_approval_history = _serve_interrupt_helpers.append_approval_history


_platform_store_singletons: dict[tuple[type, str], Any] = {}


def _get_platform_store(store_type):
    """Resolve one responsibility-owned API store without a cross-domain facade."""
    dsn = str(CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or "").strip()
    if not dsn:
        return None
    key = (store_type, dsn)
    current = _platform_store_singletons.get(key)
    resolved, next_store, _next_dsn = _logger_runtime.get_logger_singleton(
        config={"fs_observability_dsn": dsn},
        singleton=current,
        singleton_dsn=dsn if current is not None else "",
        create_logger_fn=store_type,
        sleep_fn=time.sleep,
        logger=logger,
    )
    if next_store is not None:
        _platform_store_singletons[key] = next_store
    return resolved


def _get_identity_store():
    from core.infrastructure.db.platform_stores import PlatformIdentityStore

    return _get_platform_store(PlatformIdentityStore)


def _get_project_store():
    from core.infrastructure.db.platform_stores import PlatformProjectStore

    return _get_platform_store(PlatformProjectStore)


def _get_job_store():
    from core.infrastructure.db.platform_stores import PlatformJobStore

    return _get_platform_store(PlatformJobStore)


def _get_event_store():
    from core.infrastructure.db.platform_stores import PlatformReactionStore

    return _get_platform_store(PlatformReactionStore)


def _get_schedule_store():
    from core.infrastructure.db.platform_stores import PlatformControlScheduleStore

    return _get_platform_store(PlatformControlScheduleStore)


def _get_observability_store():
    from core.infrastructure.db.platform_stores import PlatformObservabilityStore

    return _get_platform_store(PlatformObservabilityStore)


def _get_worker_store():
    from core.infrastructure.db.platform_stores import PlatformWorkerStore

    return _get_platform_store(PlatformWorkerStore)


def _get_connection_store():
    from core.infrastructure.db.platform_stores import PlatformConnectionStore

    return _get_platform_store(PlatformConnectionStore)


def _get_integration_store():
    from core.infrastructure.db.platform_stores import PlatformIntegrationStore

    return _get_platform_store(PlatformIntegrationStore)


def _get_provider_webhook_store():
    from core.infrastructure.db.platform_stores import PlatformProviderWebhookStore

    return _get_platform_store(PlatformProviderWebhookStore)


def _get_reporting_store():
    from core.infrastructure.db.platform_stores import PlatformReportingStore

    return _get_platform_store(PlatformReportingStore)


def _get_lifecycle_store():
    from core.infrastructure.db.platform_stores import PlatformLifecycleStore

    return _get_platform_store(PlatformLifecycleStore)


def _get_ui_store():
    from core.infrastructure.db.platform_stores import PlatformUiStore

    return _get_platform_store(PlatformUiStore)


def _enqueue_task_in_queue(**kwargs):
    """Factory wrapper to publish a job into the worker queue."""
    from core.infrastructure.runtime.worker.queue_publisher import enqueue_task

    return enqueue_task(**kwargs)


def _get_queue_depth(project_id: str) -> int:
    """Factory wrapper for project queue depth."""
    from core.infrastructure.runtime.worker.queue_broker import get_queue_depth

    return int(get_queue_depth(project_id))


def _get_ready_queue_depth() -> int:
    """Factory wrapper for shared ready queue depth."""
    from core.infrastructure.runtime.worker.queue_broker import get_ready_queue_depth

    return int(get_ready_queue_depth())


def _get_platform_logger(project_name="default"):
    """Return (Flow Steward logger, project_id) or (None, None) if DSN not configured."""
    bl = _get_project_store()
    if bl is None:
        return None, None
    project_id = bl.add_project(project_name, workspace_id=_default_workspace_id_impl())
    return bl, project_id


def _get_extension_store_logger(store_name: str):
    """Reject configured DB outages while preserving explicit offline mode."""
    bl = _get_identity_store()
    storage_configured = any(
        str(CONFIG.get(key) or "").strip() for key in ("fs_observability_dsn", "fs_db_dsn")
    )
    if bl is None and storage_configured:
        raise ServiceUnavailableError(f"Extension {store_name} store is unavailable")
    return bl


_get_extension_install_store = lambda: _extension_services.get_default_extension_install_store(  # noqa: E731
    get_logger=lambda: _get_extension_store_logger("install")
)


_get_extension_runtime_store = lambda: _extension_services.get_default_extension_runtime_store(  # noqa: E731
    get_logger=lambda: _get_extension_store_logger("runtime")
)


def _project_account_id(bl, project_id: Any) -> str | None:
    return _domain_event_runtime.project_account_id(bl, project_id)


_enqueue_task = _task_enqueue_runtime.build_enqueue_task_handler(
    get_config=lambda: CONFIG,
    create_logger_fn=lambda _dsn: _get_job_store(),
    enqueue_task_fn=lambda **kwargs: _enqueue_task_in_queue(**kwargs),
    build_project_config_snapshot=lambda project_config: _build_project_config_snapshot(
        project_config
    ),
    project_account_id_fn=lambda bl, project_id: _project_account_id(bl, project_id),
    logger=logger,
    log_prefix="_enqueue_task",
    artifact_plane=lambda: _artifact_plane,
)


def _flash(request: Request, message: str, flash_type: str = "info"):
    """Store a flash message in request state for API/auth callback handling."""
    request.state.flash = message
    request.state.flash_type = flash_type


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
