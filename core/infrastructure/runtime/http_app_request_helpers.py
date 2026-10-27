# ruff: noqa: F405, RUF100
# mypy: ignore-errors
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime.artifact_storage_factory import build_artifact_storage

from .http_app_constants import *  # noqa: F403,F401  # NOSONAR

_TOKEN_CORE_INFRASTRUCTURE_EXTENSION_RUNTIME_EXTENSION_RUNTIME = (
    "core.infrastructure.extension_runtime.extension_runtime"
)


def _env_flag(name: str, default: bool) -> bool:
    raw = _runtime_env.get(name)
    if raw is None:
        return bool(default)
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        return float(default)
    if parsed < 0:
        return float(default)
    return parsed


def _auth_cookie_secure() -> bool:
    return _env_flag("FS_AUTH_COOKIE_SECURE", True)


def _request_correlation_id(request: Request | None, *, fallback: str = "") -> str:
    return _serve_request_runtime.request_correlation_id(
        request,
        fallback=fallback,
        new_id_fn=lambda: f"err_{uuid.uuid4().hex[:16]}",
    )


def _error_code_for_status(status_code: int) -> str:
    return _serve_request_runtime.error_code_for_status(status_code)


_error_payload = _serve_request_runtime.build_error_payload_factory(
    err_request_failed=ERR_REQUEST_FAILED,
    default_correlation_id_fn=lambda: _request_correlation_id(None),
)


_api_error_response = _serve_request_runtime.build_api_error_response_factory(
    logger=logger,
    error_payload=_error_payload,
    request_correlation_id=_request_correlation_id,
)


def _service_error_response(exc: ServiceError, request: Request | None = None) -> JSONResponse:
    return _serve_request_runtime.service_error_response(
        exc,
        request,
        error_payload=_error_payload,
        request_correlation_id=_request_correlation_id,
        error_code_for_status=_error_code_for_status,
        err_request_failed=ERR_REQUEST_FAILED,
    )


def _view_payload_dict(view: Any) -> dict[str, Any]:
    return _serve_request_runtime.view_payload_dict(view)


def _extension_governance_status(
    extension_id: str,
    *,
    account_id: str,
    extension_install_id: str,
) -> dict[str, Any]:
    from core.infrastructure.extension_runtime.extension_runtime import ExtensionCircuitBreaker

    return dict(
        ExtensionCircuitBreaker.for_extension(
            extension_id,
            account_id=account_id,
            extension_install_id=extension_install_id,
        ).status()
    )


_event_query_service = _query_service_runtime.build_event_query_service_factory(
    get_logger=lambda: _get_event_store(),
    event_project_name=lambda *args, **kwargs: _event_project_name(*args, **kwargs),
    resolve_domain_event_by_ref=lambda *args, **kwargs: _resolve_domain_event_by_ref(
        *args,
        **kwargs,
    ),
    serialize_domain_event_summary=lambda *args, **kwargs: _serialize_domain_event_summary(
        *args,
        **kwargs,
    ),
    serialize_domain_event_datetime=lambda *args, **kwargs: _serialize_domain_event_datetime(
        *args,
        **kwargs,
    ),
    to_json_obj=lambda value: _to_json_obj(value),
    workflow_exists=lambda *args, **kwargs: _workflow_exists(*args, **kwargs),
)


_approval_query_service = _query_service_runtime.build_approval_query_service_factory(
    get_logger=lambda: _get_job_store(),
    get_project_by_id=lambda project_id: _get_project_store().get_project(project_id),
    get_thread_state_snapshot=lambda *args, **kwargs: _get_thread_state_snapshot(
        *args,
        **kwargs,
    ),
    approval_fallback_payload=lambda *args, **kwargs: _approval_fallback_payload(
        *args,
        **kwargs,
    ),
    to_json_obj=lambda value: _to_json_obj(value),
)


_extension_query_service = _query_service_runtime.build_extension_query_service_factory(
    get_logger=lambda: _get_identity_store(),
    resolve_extension_runtime_record=lambda *args, **kwargs: _resolve_extension_runtime_record(
        *args,
        **kwargs,
    ),
    list_extension_runtime_records=(_extension_runtime_helpers.list_extension_runtime_records),
    list_extension_projects=lambda *, account_id: (
        _get_project_store().get_projects(account_id=account_id)
        if _get_project_store() is not None
        else []
    ),
    build_extension_compiled_manifest=lambda *args, **kwargs: _build_extension_compiled_manifest(
        *args,
        **kwargs,
    ),
    extension_validation_summary=lambda *args, **kwargs: _extension_validation_summary(
        *args,
        **kwargs,
    ),
    compile_extension_preview=lambda *args, **kwargs: __import__(
        _TOKEN_CORE_INFRASTRUCTURE_EXTENSION_RUNTIME_EXTENSION_RUNTIME,
        fromlist=["compile_extension_preview"],
    ).compile_extension_preview(*args, **kwargs),
    run_extension_health=lambda *args, **kwargs: __import__(
        _TOKEN_CORE_INFRASTRUCTURE_EXTENSION_RUNTIME_EXTENSION_RUNTIME,
        fromlist=["run_extension_health"],
    ).run_extension_health(*args, **kwargs),
    extension_governance_status=_extension_governance_status,
    get_install_store=lambda: _get_extension_install_store(),
    get_runtime_store=lambda: _get_extension_runtime_store(),
)


_event_command_service = _command_service_runtime.build_event_command_service_factory(
    config=CONFIG,
    get_logger=lambda: _get_event_store(),
    get_job_store=lambda: _get_job_store(),
    get_project_run_config=lambda project_id: _get_project_store().get_project_run_config(
        project_id
    ),
    resolve_domain_event_by_ref=lambda *args, **kwargs: _resolve_domain_event_by_ref(
        *args,
        **kwargs,
    ),
    to_json_obj=lambda value: _to_json_obj(value),
    serialize_domain_event_datetime=lambda value: _serialize_domain_event_datetime(value),
    sanitize_external_input=lambda value: sanitize_external_input(value),
    enqueue_task=lambda **kwargs: _enqueue_task(**kwargs),
    resolve_workflow_selection=lambda requested, _selection_source="", project_workflows=None: (
        _resolve_workflow_selection(
            requested,
            project_workflows,
        )
    ),
    workflow_availability_assert=lambda **kwargs: _assert_can_start_workflow(**kwargs),
    workflow_exists=lambda workflow_id: _workflow_exists(workflow_id),
    evaluate_workflow_reactions=lambda *args, **kwargs: _evaluate_workflow_reactions(
        *args,
        **kwargs,
    ),
    reaction_action_result_executed=lambda *args, **kwargs: _reaction_action_result_executed(
        *args,
        **kwargs,
    ),
    dispatch_project_notification_event=lambda *args, **kwargs: (
        _dispatch_project_notification_event(
            *args,
            **kwargs,
        )
    ),
    fetch_provider_attachments=lambda *args, **kwargs: __import__(
        "core.application.extensions.provider_attachments",
        fromlist=["fetch_provider_attachments"],
    ).fetch_provider_attachments(*args, **kwargs),
    artifact_plane=lambda: _artifact_plane,
)


_external_agent_command_service = (
    _command_service_runtime.build_external_agent_command_service_factory(
        get_logger=lambda: _get_job_store(),
    )
)


_extension_binding_service = _command_service_runtime.build_extension_binding_service_factory(
    get_logger=lambda: _get_identity_store(),
    get_operation_store=lambda: _get_integration_store(),
    # Resolved per action: the plane is built further down this module.
    get_artifact_plane=lambda: _artifact_plane,
    get_project_by_id=lambda project_id: _get_project_store().get_project(project_id),
    resolve_extension_runtime_record=lambda *args, **kwargs: _resolve_extension_runtime_record(
        *args,
        **kwargs,
    ),
    extension_identifier=lambda *args, **kwargs: _extension_identifier(*args, **kwargs),
    extension_operation_handler_mode=lambda *args, **kwargs: _extension_operation_handler_mode(
        *args,
        **kwargs,
    ),
    extension_operation_failure_status=lambda *args, **kwargs: _extension_operation_failure_status(
        *args,
        **kwargs,
    ),
    event_command_service_factory=lambda: _event_command_service(),
    get_extension_install_store=lambda: _get_extension_install_store(),
    get_extension_runtime_store=lambda: _get_extension_runtime_store(),
    extension_query_service_factory=lambda *, bl, install_store, runtime_store: (
        _query_service_runtime.create_extension_query_service(
            get_logger=lambda: bl,
            get_install_store=lambda: install_store,
            get_runtime_store=lambda: runtime_store,
            resolve_extension_runtime_record=lambda *args, **kwargs: (
                _resolve_extension_runtime_record(*args, **kwargs)
            ),
            list_extension_runtime_records=(
                _extension_runtime_helpers.list_extension_runtime_records
            ),
            list_extension_projects=lambda *, account_id: _get_project_store().get_projects(
                account_id=account_id
            ),
            build_extension_compiled_manifest=_build_extension_compiled_manifest,
            extension_validation_summary=_extension_validation_summary,
            compile_extension_preview=lambda *args, **kwargs: __import__(
                _TOKEN_CORE_INFRASTRUCTURE_EXTENSION_RUNTIME_EXTENSION_RUNTIME,
                fromlist=["compile_extension_preview"],
            ).compile_extension_preview(*args, **kwargs),
            extension_governance_status=_extension_governance_status,
        )
    ),
)


_artifact_plane = build_artifact_storage(
    environ=dict(_runtime_env),
    create_storage_adapter=create_storage_adapter,
    artifact_plane_factory=ArtifactDatasetPlane,
)


_tool_execution_service = _command_service_runtime.build_tool_execution_service_factory(
    get_logger=lambda: _get_job_store(),
    artifact_plane=_artifact_plane,
    event_command_service_factory=lambda: _event_command_service(),
)


_secret_storage_config_error = _serve_request_runtime.secret_storage_config_error


_public_base_url = _serve_auth_runtime.public_base_url


def _trusted_auth_origins() -> set[str]:
    return _serve_auth_runtime.trusted_auth_origins(public_base_url_fn=_public_base_url)


_origin_host = _serve_auth_runtime.origin_host


def _expected_request_origin(request: Request) -> str:
    return _serve_auth_runtime.expected_request_origin(
        request,
        auth_cookie_secure_fn=_auth_cookie_secure,
    )


def _same_origin_request(request: Request) -> bool:
    return _serve_auth_runtime.same_origin_request(
        request,
        trusted_auth_origins_fn=_trusted_auth_origins,
        expected_request_origin_fn=_expected_request_origin,
        origin_host_fn=_origin_host,
    )


def _csrf_request_valid(request: Request) -> bool:
    return _serve_auth_runtime.csrf_request_valid(
        request,
        csrf_header_name=_AUTH_CSRF_HEADER_NAME,
        csrf_cookie_name=_AUTH_CSRF_COOKIE_NAME,
        same_origin_request_fn=_same_origin_request,
    )


def _bootstrap_request_allowed(request: Request) -> tuple[bool, str]:
    return _serve_auth_runtime.bootstrap_request_allowed(
        request,
        env_flag=_env_flag,
        bootstrap_token_header=_AUTH_BOOTSTRAP_TOKEN_HEADER,
    )


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
