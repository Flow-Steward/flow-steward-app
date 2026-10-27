# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from .http_app_indexing_workflows import *  # noqa: F403,F401  # NOSONAR


_workflow_exists = _serve_workflow_helpers_runtime.workflow_exists


def _set_job_status_by_thread_id_lazy(thread_id: str, status: str) -> None:
    from .http_app_events_resume import _set_job_status_by_thread_id

    return _set_job_status_by_thread_id(thread_id, status)


_resolve_job_workflow_id = lambda job: _reaction_runtime.resolve_job_workflow_id(  # noqa: E731
    job,
    to_json_obj=lambda value: _to_json_obj(value),
)


def _dispatch_project_notification_event(bl, **kwargs):
    from core.application.notifications.dispatch import dispatch_project_notification_event

    return dispatch_project_notification_event(bl, **kwargs)


_reaction_value_matches = _reaction_runtime.reaction_value_matches


_lookup_dotted_value = _reaction_runtime.lookup_dotted_value


_reaction_conditions_match = lambda event, conditions: _reaction_runtime.reaction_conditions_match(  # noqa: E731
    event,
    conditions,
    to_json_obj=lambda value: _to_json_obj(value),
)


_approval_reaction_resumer = None


def configure_approval_reaction_resumer(resumer) -> None:
    global _approval_reaction_resumer
    _approval_reaction_resumer = resumer


_parse_event_datetime = _reaction_runtime.parse_event_datetime


_reaction_is_debounced = lambda bl, event, rule: _reaction_runtime.reaction_is_debounced(  # noqa: E731
    bl,
    event,
    rule,
    parse_event_datetime_fn=lambda value: _parse_event_datetime(value),
)


def _resume_job_from_reaction(
    job_id,
    comment,
    approved=True,
    action=None,
    expected_approval_id=None,
    scope=None,
    idempotency_key=None,
):
    bl = _get_job_store()
    job = bl.get_job(str(job_id)) if bl and hasattr(bl, "get_job") else None
    thread_id = str((job or {}).get("thread_id") or "").strip()
    approval_id = str(expected_approval_id or "").strip()
    if not job or not thread_id:
        return {"status": "ignored", "reason": ERR_JOB_NOT_FOUND_LOWER}
    if scope is None or not approval_id:
        return {"status": "ignored", "reason": "stale_approval_gate"}
    if not callable(_approval_reaction_resumer):
        raise RuntimeError("Approval reaction resumer is not configured")
    return _approval_reaction_resumer(
        job_id=str(job_id),
        thread_id=thread_id,
        scope=scope,
        approval_id=approval_id,
        approved=bool(approved),
        action=action,
        comment=str(comment or ""),
        idempotency_key=str(idempotency_key or "").strip()
        or f"reaction:{job_id}:{approval_id}:{'approve' if approved else 'reject'}",
    )


_reaction_action_result_executed = _reaction_runtime.reaction_action_result_executed


_reaction_action_comment = lambda event, action_payload: _reaction_runtime.reaction_action_comment(  # noqa: E731
    event,
    action_payload,
    to_json_obj=lambda value: _to_json_obj(value),
)


_cancel_job_from_reaction = lambda job_id, comment: _reaction_runtime.cancel_job_from_reaction(  # noqa: E731
    str(job_id),
    comment,
    get_logger_only=lambda: _get_job_store(),
    err_database_not_configured=ERR_DATABASE_NOT_CONFIGURED,
    err_job_not_found_lower=ERR_JOB_NOT_FOUND_LOWER,
    logger=logger,
)


def _retry_interrupted_step_from_reaction(job_id, requested_by="automation"):
    return _reaction_runtime.retry_interrupted_step_from_reaction(
        str(job_id),
        requested_by=requested_by,
        get_logger_only=lambda: _get_job_store(),
        err_database_not_configured=ERR_DATABASE_NOT_CONFIGURED,
        err_job_not_found_lower=ERR_JOB_NOT_FOUND_LOWER,
        to_json_obj=lambda value: _to_json_obj(value),
        sanitize_retryable_interrupted_step_snapshot=lambda raw: (
            _sanitize_retryable_interrupted_step_snapshot(raw)
        ),
        retryable_interrupted_step_status=_RETRYABLE_INTERRUPTED_STEP_STATUS,
        resolve_job_project_name=lambda bl, job: _resolve_job_project_name(bl, job),
        get_project_run_config=lambda project_id: _get_project_store().get_project_run_config(
            project_id
        ),
        build_rerun_workflow_selection=lambda job, *, replay_snapshot: (
            _build_rerun_workflow_selection(job, replay_snapshot=replay_snapshot)
        ),
        load_replay_snapshot=lambda **kwargs: _load_job_replay_snapshot(
            artifact_plane=_artifact_plane,
            **kwargs,
        ),
        enqueue_task_in_queue=lambda **kwargs: _enqueue_task_in_queue(**kwargs),
        append_runtime_journal=lambda bl, job_id, event, reason, detail=None: (
            _append_runtime_journal(
                bl,
                job_id,
                event=event,
                reason=reason,
                detail=detail,
            )
        ),
    )


_normalize_reaction_route_keys = _reaction_runtime.normalize_reaction_route_keys


def _dispatch_notification_from_reaction(event, action_payload):
    return _reaction_runtime.dispatch_notification_from_reaction(
        event,
        action_payload,
        get_logger_only=lambda: _get_job_store(),
        err_database_not_configured=ERR_DATABASE_NOT_CONFIGURED,
        to_json_obj=lambda value: _to_json_obj(value),
        resolve_job_workflow_id=lambda job: _resolve_job_workflow_id(job),
        logger=logger,
    )


_apply_reaction_action = lambda event, rule, scope=None: _reaction_runtime.apply_reaction_action(  # noqa: E731
    event,
    rule,
    to_json_obj=lambda value: _to_json_obj(value),
    reaction_action_comment=lambda current_event, action_payload: _reaction_action_comment(
        current_event,
        action_payload,
    ),
    reaction_action_result_executed=lambda result: _reaction_action_result_executed(result),
    resume_job_from_reaction=lambda *args, **kwargs: _resume_job_from_reaction(*args, **kwargs),
    cancel_job_from_reaction=lambda *args, **kwargs: _cancel_job_from_reaction(*args, **kwargs),
    retry_interrupted_step_from_reaction=lambda *args, **kwargs: (
        _retry_interrupted_step_from_reaction(
            *args,
            **kwargs,
        )
    ),
    dispatch_notification_from_reaction=lambda current_event, action_payload: (
        _dispatch_notification_from_reaction(
            current_event,
            action_payload,
        )
    ),
    scope=scope,
)


def _evaluate_workflow_reactions(bl, event, scope=None):
    return _reaction_runtime.evaluate_workflow_reactions(
        bl,
        event,
        to_json_obj=lambda value: _to_json_obj(value),
        reaction_scope_matches_event=lambda current_event, rule: _reaction_scope_matches_event(
            current_event,
            rule,
        ),
        reaction_conditions_match=lambda current_event, conditions: _reaction_conditions_match(
            current_event,
            conditions,
        ),
        reaction_is_debounced=lambda current_bl, current_event, rule: _reaction_is_debounced(
            current_bl,
            current_event,
            rule,
        ),
        apply_reaction_action=lambda current_event, rule, scope=None: _apply_reaction_action(
            current_event, rule, scope=scope
        ),
        reaction_action_result_executed=lambda result: _reaction_action_result_executed(result),
        logger=logger,
        scope=scope,
    )


def _normalize_reaction_scope(scope_kind: str | None, scope_id: str | None) -> tuple[str, str]:
    return normalize_reaction_scope(scope_kind, scope_id)


def _compose_scoped_reaction_key(scope_kind: str, scope_id: str, reaction_key: str) -> str:
    return compose_scoped_reaction_key(scope_kind, scope_id, reaction_key)


def _split_scoped_reaction_key(reaction_key: str | None) -> tuple[str, str, str]:
    return split_scoped_reaction_key(reaction_key)


def _reaction_scope_matches_target(
    reaction_key: str | None, scope_kind: str, scope_id: str
) -> bool:
    return reaction_scope_matches_target(reaction_key, scope_kind, scope_id)


def _reaction_scope_matches_event(rule: dict, event: dict) -> bool:
    return _reaction_runtime.reaction_scope_matches_event(
        rule,
        event,
        split_scoped_reaction_key_fn=split_scoped_reaction_key,
        to_json_obj=_to_json_obj,
    )


def _extension_identifier(payload: dict | None) -> str:
    return _extension_runtime_helpers.extension_identifier(payload)


def _extension_manifest_has_ui(payload: dict | None) -> bool:
    return _extension_runtime_helpers.extension_manifest_has_ui(payload)


def _extension_operation_handler_mode(
    extension: dict | None, *, operation_type: str, operation_id: str
) -> str:
    return _extension_runtime_helpers.extension_operation_handler_mode(
        extension,
        operation_type=operation_type,
        operation_id=operation_id,
    )


def _extension_operation_failure_status(invocation: dict | None) -> int:
    return _extension_runtime_helpers.extension_operation_failure_status(invocation)


def _merge_extension_runtime_rows(
    db_extension: dict | None, fs_extension: dict | None
) -> dict | None:
    return _extension_runtime_helpers.merge_extension_runtime_rows(db_extension, fs_extension)


def _load_extension_from_filesystem(extension_id: str) -> dict | None:
    return _extension_runtime_helpers.load_extension_from_filesystem(extension_id)


def _resolve_extension_runtime_record(
    extension_id: str,
    *,
    account_id: str | None = None,
) -> tuple[dict | None, Any]:
    return _extension_runtime_helpers.resolve_extension_runtime_record(
        extension_id,
        get_logger_only=_get_identity_store,
        account_id=account_id,
    )


def _build_extension_compiled_manifest(extension: dict | None) -> dict:
    return _extension_runtime_helpers.build_extension_compiled_manifest(extension)


def _normalize_extension_issue(item: Any, *, default_severity: str) -> dict | None:
    return _extension_runtime_helpers.normalize_extension_issue(
        item,
        default_severity=default_severity,
    )


def _extension_issue_fingerprint(item: dict | None) -> tuple[str, str, str, str, str]:
    return _extension_runtime_helpers.extension_issue_fingerprint(item)


def _extension_validation_summary(compiled_manifest: dict | None) -> dict:
    return _extension_runtime_helpers.extension_validation_summary(compiled_manifest)


def _extension_page_projects(bl, *, account_id: str | None = None) -> list[dict]:
    return _extension_runtime_helpers.extension_page_projects(bl, account_id=account_id)


# ---------------------------------------------------------------------------
# Trash lifecycle API
# ---------------------------------------------------------------------------


_project_dataset_catalog_store = None


def _get_project_dataset_catalog_store():
    global _project_dataset_catalog_store
    if _project_dataset_catalog_store is None:
        dsn = str(CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or "").strip()
        if not dsn:
            return None
        _project_dataset_catalog_store = (
            _command_service_runtime.create_project_dataset_catalog_store(dsn)
        )
    return _project_dataset_catalog_store


def _trash_lifecycle_service(*, workflow_secret_default_service):
    from core.infrastructure.runtime.worker.queue_broker import delete_project_queues

    project_store = _get_project_store()

    return _command_service_runtime.create_trash_lifecycle_service(
        get_logger=lambda: project_store,
        get_audit_store=_get_identity_store,
        artifact_plane=_artifact_plane,
        get_project_dataset_catalog=_get_project_dataset_catalog_store,
        delete_project_queues=delete_project_queues,
        begin_project_executor_quiescence=lambda project_id: (
            _graph_resume_runtime.fence_project_resume_threads(
                project_id,
                resume_threads=_resume_threads,
                resume_threads_lock=_resume_threads_lock,
                fenced_project_ids=_resume_fenced_project_ids,
                timeout_seconds=_RESUME_JOIN_TIMEOUT_SECONDS,
            )
            == 0
        ),
        workflow_secret_default_service=workflow_secret_default_service,
        end_project_executor_quiescence=lambda project_id: (
            _graph_resume_runtime.unfence_project_resume_threads(
                project_id,
                resume_threads_lock=_resume_threads_lock,
                fenced_project_ids=_resume_fenced_project_ids,
            )
        ),
    )


# ---------------------------------------------------------------------------
# Model Providers API (self-hosted only)
# ---------------------------------------------------------------------------


def _model_provider_command_service():
    return _command_service_runtime.create_model_provider_command_service(
        get_logger=_get_schedule_store
    )


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
