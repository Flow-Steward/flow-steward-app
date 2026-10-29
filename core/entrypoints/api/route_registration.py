# ruff: noqa: F405, RUF100
# mypy: ignore-errors
from typing import Any

from core.application.extensions.artifact_presentation import ArtifactPresentationResolver
from core.application.management.command_models import ApprovalDecisionInput
from core.application.management.errors import ServiceConflictError
from core.application.workflows.workflow_secret_default_service import (
    WorkflowSecretDefaultService,
)
from core.entrypoints.api.first_admin_runtime import build_first_admin_service
from core.entrypoints.api.runtime_wiring import *  # noqa: F403,F401  # NOSONAR
from core.infrastructure.runtime import http_app_reactions_extensions as _reactions_runtime
from core.infrastructure.runtime.http_app_events_resume import (
    _approval_fallback_payload,
    _ws_get_approval_payload,
)
from core.infrastructure.runtime.http_app_indexing_workflows import (
    _build_rerun_workflow_selection,
    _cron_human_summary,
    _cron_next_run_at,
    _list_workflow_choices,
    _normalize_cron_expr,
    _parse_cron_expression,
    _request_model_selection_context,
)
from core.infrastructure.runtime.http_app_reactions_extensions import (  # noqa: F401
    _build_extension_compiled_manifest,
    _extension_identifier,
    _extension_operation_failure_status,
    _extension_operation_handler_mode,
    _extension_page_projects,
    _extension_validation_summary,
    _model_provider_command_service,
    _resolve_extension_runtime_record,
)
from core.infrastructure.runtime.http_app_reactions_extensions import (
    _trash_lifecycle_service as _build_trash_lifecycle_service,
)
from core.infrastructure.runtime.http_app_scheduled_jobs import (
    _assert_can_start_workflow,
)
from core.infrastructure.runtime.workflow_db_adapters import (
    create_workflow_secret_default_repository,
)


def _composed_service(current_app: FastAPI, name: str):
    container = getattr(current_app.state, "runtime_container", None)
    service_factories = getattr(container, "service_factories", None)
    if not isinstance(service_factories, dict) or name not in service_factories:
        raise RuntimeError(f"HTTP service factory is not configured: {name}")
    metadata = getattr(container, "metadata", None)
    legacy_db_factory = service_factories.get("db")
    default_legacy_db_factory = (
        metadata.get("default_legacy_db_factory") if isinstance(metadata, dict) else None
    )
    if (
        name.endswith("_store")
        and callable(legacy_db_factory)
        and legacy_db_factory is not default_legacy_db_factory
    ):
        # Compatibility seam for existing app-embedding tests and private editions
        # that override the historical ``db`` factory.  The default production
        # composition never enters this branch and always resolves a narrow store.
        return legacy_db_factory()
    return service_factories[name]()


class _ProjectStoreWithExtensionList:
    """The project store, plus the installed-extension list that lives on the identity store.

    A workflow's provider triggers belong to the project store, while the trigger
    types an extension declares are read from the extension list. Without the
    list the catalog holds only the built-in webhook and every provider trigger
    is "not found".
    """

    def __init__(self, project_store: Any, identity_store: Any) -> None:
        self._project_store = project_store
        self._identity_store = identity_store

    def __getattr__(self, name: str) -> Any:
        if name == "list_extensions":
            return self._identity_store.list_extensions
        return getattr(self._project_store, name)


def _workflow_trigger_store(current_app: FastAPI) -> Any:
    project_store = _composed_service(current_app, "project_store")
    if hasattr(project_store, "list_extensions"):
        return project_store
    return _ProjectStoreWithExtensionList(
        project_store, _composed_service(current_app, "identity_store")
    )


def _trash_lifecycle_service():
    project_store = _get_project_store()
    return _build_trash_lifecycle_service(
        workflow_secret_default_service=WorkflowSecretDefaultService(
            create_workflow_secret_default_repository(project_store)
        )
    )


def _register_internal_admin_surfaces(current_app: FastAPI) -> None:
    from core.entrypoints.control.api import create_control_router

    current_app.include_router(create_control_router())
    logger.info("Admin API routes mounted (/api/agents, /api/tools, /api/workflows, ...)")


def _app_container(current_app: FastAPI):
    return getattr(current_app.state, "runtime_container", None)


def _app_registries(current_app: FastAPI) -> dict:
    registries = getattr(_app_container(current_app), "registries", None)
    return registries if isinstance(registries, dict) else {}


def _app_edition(current_app: FastAPI) -> str:
    metadata = getattr(_app_container(current_app), "metadata", None)
    if not isinstance(metadata, dict):
        return "community"
    return str(metadata.get("edition") or "community").strip() or "community"


def _app_platform_contract(current_app: FastAPI, contract_key: str):
    contracts = _app_registries(current_app).get("platform_contracts")
    if not isinstance(contracts, dict):
        return None
    return contracts.get(str(contract_key or ""))


def _app_local_auth_enabled(current_app: FastAPI) -> bool:
    override = getattr(current_app.state, "local_auth_enabled_override", None)
    if callable(override):
        return bool(override())
    metadata = getattr(_app_container(current_app), "metadata", None)
    default_enabled = (
        bool(metadata.get("local_auth_default_enabled", True))
        if isinstance(metadata, dict)
        else True
    )
    return _env_flag("FS_LOCAL_AUTH_ENABLED", default_enabled)


def _app_external_auth_enabled(current_app: FastAPI) -> bool:
    override = getattr(current_app.state, "external_auth_enabled_override", None)
    if callable(override):
        return bool(override())
    resolvers = _app_registries(current_app).get("external_auth_enabled")
    if callable(resolvers):
        resolvers = (resolvers,)
    for resolver in resolvers or ():
        if not callable(resolver):
            continue
        try:
            if resolver():
                return True
        except Exception:
            logger.debug("External auth enabled resolver failed", exc_info=True)
    return False


def _resolve_request_principal(current_app: FastAPI, request, bl):
    resolvers = _app_registries(current_app).get("request_principal_resolvers")
    if not isinstance(resolvers, (list, tuple)):
        return None
    container = _app_container(current_app)
    for resolver in resolvers:
        if not callable(resolver):
            continue
        try:
            payload = resolver(request=request, bl=bl, container=container)
        except TypeError:
            payload = resolver(request, bl)
        except Exception:
            logger.debug("External principal resolver failed", exc_info=True)
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _app_registered_settings_pages(current_app: FastAPI, *, auth_user=None, locale=None):
    registries = _app_registries(current_app)
    return _ui_page_registry.resolve_registered_settings_pages(
        registries,
        edition=_app_edition(current_app),
        auth_user=auth_user,
        locale=locale,
        include_core=True,
        authorization_service=_app_platform_contract(current_app, "authorization_service"),
        entitlement_service=_app_platform_contract(current_app, "entitlement_service"),
    )


def _app_registered_pages(current_app: FastAPI, *, auth_user=None, locale=None):
    registries = _app_registries(current_app)
    return _ui_page_registry.resolve_registered_pages(
        registries,
        edition=_app_edition(current_app),
        auth_user=auth_user,
        locale=locale,
        authorization_service=_app_platform_contract(current_app, "authorization_service"),
        entitlement_service=_app_platform_contract(current_app, "entitlement_service"),
    )


def _app_shell_navigation(current_app: FastAPI, *, auth_user=None, locale=None):
    registries = _app_registries(current_app)
    return _ui_page_registry.resolve_registered_navigation_sections(
        registries,
        edition=_app_edition(current_app),
        auth_user=auth_user,
        locale=locale,
        authorization_service=_app_platform_contract(current_app, "authorization_service"),
        entitlement_service=_app_platform_contract(current_app, "entitlement_service"),
    )


def _app_external_sign_in(current_app: FastAPI) -> dict:
    payload = _app_registries(current_app).get("external_sign_in")
    return dict(payload) if isinstance(payload, Mapping) else {}


def _initialize_shared_route_dependencies(current_app: FastAPI) -> None:
    _route_deps.init(
        get_logger_only=lambda: _composed_service(current_app, "ui_store"),
        get_platform_logger=_get_platform_logger,
        enqueue_task=_enqueue_task,
        get_ready_queue_depth=_get_ready_queue_depth,
        get_queue_depth=_get_queue_depth,
        get_checkpointer=_get_checkpointer,
        get_thread_state_snapshot=_get_thread_state_snapshot,
        compute_readiness=_compute_readiness,
        watchdog_scan=_watchdog_scan,
        auth_csrf_cookie_name=_AUTH_CSRF_COOKIE_NAME,
        env_flag=_env_flag,
        api_error_response=_api_error_response,
        to_json_obj=_to_json_obj,
        local_auth_enabled=lambda: _app_local_auth_enabled(current_app),
        external_auth_enabled=lambda: _app_external_auth_enabled(current_app),
        require_authenticated_user=_require_authenticated_user,
        optional_actor_username=_optional_actor_username,
        resolve_actor_username=_resolve_actor_username,
        request_auth_token=_request_auth_token,
        request_client_ip=_request_client_ip,
        redirect=_redirect,
        flash=_flash,
        resolve_artifact=ArtifactPresentationResolver(_artifact_plane),
        artifact_plane=_artifact_plane,
    )


def _register_foundation_routes(current_app: FastAPI) -> None:
    current_app.include_router(_health_router)
    current_app.include_router(_security_router)
    current_app.include_router(_events_stream_router)
    current_app.include_router(_workload_router)


def _resume_approval_reaction(
    *,
    approval_command_service,
    job_id: str,
    thread_id: str,
    scope,
    approval_id: str,
    approved: bool,
    action,
    comment: str,
    idempotency_key: str,
):
    try:
        accepted = approval_command_service().resume_task(
            thread_id=thread_id,
            scope=scope,
            decision=ApprovalDecisionInput(
                approved=approved,
                action=action,
                comment=comment,
                approval_id=approval_id,
            ),
            idempotency_key=idempotency_key,
        )
    except ServiceConflictError as exc:
        code = str((exc.payload or {}).get("code") or "").strip()
        from core.application.management.approval_outcome import (
            approval_conflict_outcome_code,
        )

        outcome_code = approval_conflict_outcome_code(code)
        if outcome_code:
            return {
                "status": "ignored",
                "reason": code,
                "outcome_code": outcome_code,
            }
        raise
    return {
        "status": "resumed",
        "job_id": job_id,
        "thread_id": thread_id,
        "approval_id": approval_id,
        "job_status": str(getattr(accepted, "status", "") or "in_progress"),
        "approval_reason": str(getattr(accepted, "approval_reason", "") or "") or None,
    }


def _register_api_route_bindings(current_app: FastAPI) -> None:
    _reactions_runtime.configure_approval_reaction_resumer(
        lambda **kwargs: _resume_approval_reaction(
            approval_command_service=lambda: _composed_service(
                current_app, "approval_command_service"
            ),
            **kwargs,
        )
    )
    _register_api_routes(
        current_app,
        get_logger_only=lambda: _composed_service(current_app, "connection_store"),
        get_logger=lambda: _composed_service(current_app, "connection_store"),
        get_project_store=lambda: _composed_service(current_app, "project_store"),
        get_job_store=lambda: _composed_service(current_app, "job_store"),
        get_identity_store=lambda: _composed_service(current_app, "identity_store"),
        get_event_store=lambda: _composed_service(current_app, "event_store"),
        get_schedule_store=lambda: _composed_service(current_app, "schedule_store"),
        get_observability_store=lambda: _composed_service(current_app, "observability_store"),
        get_worker_store=lambda: _composed_service(current_app, "worker_store"),
        get_integration_store=lambda: _composed_service(current_app, "integration_store"),
        get_extension_oauth_store=lambda: _composed_service(current_app, "integration_store"),
        get_reporting_store=lambda: _composed_service(current_app, "reporting_store"),
        get_provider_webhook_store=lambda: _composed_service(current_app, "provider_webhook_store"),
        request_scope=lambda request, **kwargs: _request_scope(request, **kwargs),
        resolve_user_role=lambda request: user_role_from_auth(_request_auth_user_payload(request)),
        to_json_obj=lambda value: _to_json_obj(value),
        api_error_response=lambda exc, request=None, **kwargs: _api_error_response(
            exc,
            request=request,
            **kwargs,
        ),
        service_error_response=lambda exc, request=None: _service_error_response(exc, request),
        secret_storage_config_error=lambda exc: _secret_storage_config_error(exc),
        project_query_service=lambda: _composed_service(current_app, "project_query_service"),
        project_command_service=lambda: _composed_service(current_app, "project_command_service"),
        event_query_service=lambda: _composed_service(current_app, "event_query_service"),
        event_command_service=lambda: _composed_service(current_app, "event_command_service"),
        job_query_service=lambda: _composed_service(current_app, "job_query_service"),
        extension_query_service=lambda: _composed_service(current_app, "extension_query_service"),
        extension_binding_service=lambda: _composed_service(
            current_app, "extension_binding_service"
        ),
        tool_execution_service=lambda: _composed_service(current_app, "tool_execution_service"),
        get_extension_install_store=lambda: _get_extension_install_store(),
        get_extension_runtime_store=lambda: _get_extension_runtime_store(),
        workflow_availability_assert=lambda **kwargs: _assert_can_start_workflow(**kwargs),
        approval_command_service=lambda: _composed_service(current_app, "approval_command_service"),
        job_command_service=lambda: _composed_service(current_app, "job_command_service"),
        redirect=lambda url, flash=None, flash_type="info": _redirect(url, flash, flash_type),
        approval_query_service=lambda: _composed_service(current_app, "approval_query_service"),
        resolve_actor_username=lambda request, requested, default="operator": (
            _resolve_actor_username(
                request,
                requested,
                default=default,
            )
        ),
        optional_actor_username=lambda request: _optional_actor_username(request),
        watchdog_scan=lambda bl, apply=False: _watchdog_scan(bl, apply=apply),
        # Signalable, not merely parseable: this callable feeds the watchdog
        # kill action, which sends signals from THIS process. See
        # control.scans.signalable_worker_pid — a containerised worker is pid 1
        # of its own container, and signalling that number here killed the API
        # container's own init.
        parse_worker_pid=lambda worker_id: _signalable_worker_pid(worker_id),
        terminate_worker_pid=lambda pid: _terminate_worker_pid(pid),
        append_runtime_journal=lambda bl, job_id, event, reason="", detail=None: (
            _append_runtime_journal(bl, job_id, event=event, reason=reason, detail=detail)
        ),
        build_rerun_workflow_selection=lambda job, *, replay_snapshot: (
            _build_rerun_workflow_selection(job, replay_snapshot=replay_snapshot)
        ),
        load_replay_snapshot=lambda job: _load_job_replay_snapshot(
            artifact_plane=_artifact_plane,
            pointer=(_to_json_obj(job.get("meta")) or {}).get("replay_snapshot"),
            project_id=str(job.get("project_id") or ""),
            job_id=str(job.get("id") or ""),
        ),
        enqueue_task=lambda **kwargs: _enqueue_task(**kwargs),
        require_authenticated_user=lambda request, required_roles=None: _require_authenticated_user(
            request,
            required_roles=required_roles,
        ),
        local_auth_enabled=lambda: _app_local_auth_enabled(current_app),
        local_auth_bootstrap_required=lambda: _local_auth_bootstrap_required(
            get_logger_only_fn=lambda: _composed_service(current_app, "identity_store")
        ),
        external_auth_enabled=lambda: _app_external_auth_enabled(current_app),
        current_app_edition=lambda: _app_edition(current_app),
        platform_contract=lambda contract_key: _app_platform_contract(current_app, contract_key),
        auth_admin_roles=_AUTH_ADMIN_ROLES,
        model_provider_command_service=lambda: _model_provider_command_service(),
        external_agent_command_service=lambda: _composed_service(
            current_app,
            "external_agent_command_service",
        ),
        request_principal=lambda request: _request_principal(request),
        real_dict_cursor_kwargs=lambda: _real_dict_cursor_kwargs(),
        trash_lifecycle_service=lambda: _trash_lifecycle_service(),
        logger=logger,
        retryable_interrupted_step_status=_RETRYABLE_INTERRUPTED_STEP_STATUS,
        err_database_not_configured=ERR_DATABASE_NOT_CONFIGURED,
        err_project_not_found=ERR_PROJECT_NOT_FOUND,
        err_not_found=ERR_NOT_FOUND,
        err_admin_role_required=ERR_ADMIN_ROLE_REQUIRED,
        dashboard_default_range=_DASHBOARD_DEFAULT_RANGE,
        dashboard_range_hours=_DASHBOARD_RANGE_HOURS,
        dashboard_metrics_page_size=_DASHBOARD_METRICS_PAGE_SIZE,
        dashboard_metrics_max_rows=_DASHBOARD_METRICS_MAX_ROWS,
        dashboard_triage_limit=_DASHBOARD_TRIAGE_LIMIT,
        dashboard_rollup_read_enabled=_DASHBOARD_ROLLUP_READ_ENABLED,
        settings_pages_resolver=lambda auth_user=None, locale=None: _app_registered_settings_pages(
            current_app, auth_user=auth_user, locale=locale
        ),
        registered_pages_resolver=lambda auth_user=None, locale=None: _app_registered_pages(
            current_app, auth_user=auth_user, locale=locale
        ),
        shell_navigation_resolver=lambda auth_user=None, locale=None: _app_shell_navigation(
            current_app, auth_user=auth_user, locale=locale
        ),
        external_sign_in_resolver=lambda: _app_external_sign_in(current_app),
        project_create_input_cls=ProjectCreateInput,
        project_update_input_cls=ProjectUpdateInput,
        service_error_cls=ServiceError,
        service_not_found_error_cls=ServiceNotFoundError,
        default_agent_models=lambda request=None: (
            _to_json_obj(_request_model_selection_context(request).get("default_agent_models"))
            or {}
        ),
        available_providers=lambda request=None: list(
            _request_model_selection_context(request).get("available_providers") or []
        ),
        available_models=lambda request=None: list(
            _request_model_selection_context(request).get("available_models") or []
        ),
        providers_with_models=lambda request=None: (
            _request_model_selection_context(request).get("providers_with_models") or {}
        ),
        resolve_model_selection_context=lambda request=None: _request_model_selection_context(
            request
        ),
    )


def _register_auth_route_bindings(current_app: FastAPI) -> None:
    current_app.state.auth_users_router = _register_auth_routes(
        current_app,
        local_auth_enabled=lambda: _app_local_auth_enabled(current_app),
        external_auth_enabled=lambda: _app_external_auth_enabled(current_app),
        get_logger_only=lambda: _composed_service(current_app, "identity_store"),
        auth_tables_available=lambda bl: _auth_tables_available(bl),
        auth_pat_tables_available=lambda bl: _auth_pat_tables_available(bl),
        auth_account_tables_available=lambda bl: _auth_account_tables_available(bl),
        auth_service_account_tables_available=lambda bl: _auth_service_account_tables_available(bl),
        auth_machine_token_tables_available=lambda bl: _auth_machine_token_tables_available(bl),
        auth_password_reset_tables_available=lambda bl: _auth_password_reset_tables_available(bl),
        auth_user_column_exists=lambda bl, column_name: _auth_user_column_exists(bl, column_name),
        require_authenticated_user=lambda request, required_roles=None: _require_authenticated_user(
            request,
            required_roles=required_roles,
        ),
        request_auth_user_payload=lambda request: _request_auth_user_payload(request),
        request_scope=lambda request, **kwargs: _request_scope(request, **kwargs),
        request_principal=lambda request: _request_principal(request),
        api_error_response=lambda exc, request=None: _api_error_response(exc, request=request),
        service_error_response=lambda exc, request=None: _service_error_response(exc, request),
        log_auth_audit_event=lambda bl, **kwargs: _log_auth_audit_event(bl, **kwargs),
        auth_login_clear_user_failures=lambda username, *, bl=None: (
            _auth_login_clear_user_failures(username)
            if bl is None
            else _auth_login_clear_user_failures(username, bl=bl)
        ),
        get_user_by_id=lambda bl, user_id: _get_user_by_id(bl, user_id),
        get_user_by_username=lambda bl, username: _get_user_by_username(bl, username),
        normalize_requested_roles=lambda raw_roles: _normalize_requested_roles(raw_roles),
        seed_default_roles=lambda bl: _seed_default_roles(bl),
        password_hash=lambda raw: _password_hash(raw),
        verify_password=lambda plain, hashed: _verify_password(plain, hashed),
        request_auth_token=lambda request: _request_auth_token(request),
        session_token_hash=lambda token: _session_token_hash(token),
        request_client_ip=lambda request: _request_client_ip(request),
        password_reset_enabled=lambda: _password_reset_enabled(),
        password_reset_throttle_check=lambda client_ip, action, subject="": (
            _password_reset_throttle_check(client_ip, action=action, subject=subject)
        ),
        password_reset_throttle_record=lambda client_ip, action, subject="": (
            _password_reset_throttle_record(client_ip, action=action, subject=subject)
        ),
        password_reset_token_hash=lambda token: _password_reset_token_hash(token),
        auth_user_deleted_condition=lambda bl, alias="u": _auth_user_deleted_condition(
            bl,
            alias=alias,
        ),
        real_dict_cursor_kwargs=lambda: _real_dict_cursor_kwargs(),
        dispatch_password_reset_email=lambda request, user, reset_token: (
            _dispatch_password_reset_email(
                request,
                user,
                reset_token,
            )
        ),
        password_reset_debug_reveal_token=lambda: _password_reset_debug_reveal_token(),
        resolve_actor_username=lambda request, requested, default="operator": (
            _resolve_actor_username(
                request,
                requested,
                default=default,
            )
        ),
        auth_cookie_secure=lambda: _auth_cookie_secure(),
        bootstrap_request_allowed=lambda request: _bootstrap_request_allowed(request),
        auth_login_lock_state=lambda username, client_ip: _auth_login_lock_state(
            username,
            client_ip,
        ),
        auth_login_record_failure=lambda username, client_ip: _auth_login_record_failure(
            username,
            client_ip,
        ),
        auth_login_clear_failures=lambda username, client_ip: _auth_login_clear_failures(
            username,
            client_ip,
        ),
        csrf_request_valid=lambda request: _csrf_request_valid(request),
        sql_user_username_column=SQL_USER_USERNAME_COLUMN,
        sql_update_user_password_by_id=SQL_UPDATE_USER_PASSWORD_BY_ID,
        sql_limit_1=SQL_LIMIT_1,
        auth_admin_roles=_AUTH_ADMIN_ROLES,
        auth_password_min_length=_AUTH_PASSWORD_MIN_LENGTH,
        auth_password_reset_ttl_minutes=_AUTH_PASSWORD_RESET_TTL_MINUTES,
        auth_session_ttl_hours=_AUTH_SESSION_TTL_HOURS,
        auth_remember_session_ttl_hours=_AUTH_REMEMBER_SESSION_TTL_HOURS,
        auth_cookie_name=_AUTH_COOKIE_NAME,
        auth_csrf_cookie_name=_AUTH_CSRF_COOKIE_NAME,
        err_authentication_required=ERR_AUTHENTICATION_REQUIRED,
        err_database_not_configured=ERR_DATABASE_NOT_CONFIGURED,
        err_auth_tables_not_available=ERR_AUTH_TABLES_NOT_AVAILABLE,
        err_admin_role_required=ERR_ADMIN_ROLE_REQUIRED,
        err_local_auth_disabled=ERR_LOCAL_AUTH_DISABLED,
        err_invalid_json_body=ERR_INVALID_JSON_BODY,
        err_username_required=ERR_USERNAME_REQUIRED,
        err_pat_tables_not_available=ERR_PAT_TABLES_NOT_AVAILABLE,
        err_token_not_found=ERR_TOKEN_NOT_FOUND,
        err_account_tables_not_available=ERR_ACCOUNT_TABLES_NOT_AVAILABLE,
        err_service_account_key_required=ERR_SERVICE_ACCOUNT_KEY_REQUIRED,
        err_machine_token_tables_not_available=ERR_MACHINE_TOKEN_TABLES_NOT_AVAILABLE,
        err_password_reset_flow_disabled=ERR_PASSWORD_RESET_FLOW_DISABLED,
        err_auth_password_reset_tables_not_available=ERR_PASSWORD_RESET_TABLES_NOT_AVAILABLE,
        err_account_disabled=ERR_ACCOUNT_DISABLED,
        err_too_many_password_reset_requests=ERR_TOO_MANY_PASSWORD_RESET_REQUESTS,
        err_too_many_login_attempts=ERR_TOO_MANY_LOGIN_ATTEMPTS,
        first_admin_service_factory=lambda bl: build_first_admin_service(
            logger=bl,
            seed_default_roles=lambda logger: _seed_default_roles(logger),
            password_hash=lambda raw: _password_hash(raw),
        ),
    )


def _register_ui_route_bindings(current_app: FastAPI) -> None:
    _register_ui_routes(
        current_app,
        local_auth_enabled=lambda: _app_local_auth_enabled(current_app),
        external_auth_enabled=lambda: _app_external_auth_enabled(current_app),
        require_authenticated_user=lambda request, required_roles=None: _require_authenticated_user(
            request,
            required_roles=required_roles,
        ),
        local_auth_bootstrap_required=lambda: _local_auth_bootstrap_required(
            get_logger_only_fn=lambda: _composed_service(current_app, "identity_store")
        ),
        request_scope=lambda request, **kwargs: _request_scope(request, **kwargs),
        request_auth_user_payload=lambda request: _request_auth_user_payload(request),
        resolve_actor_username=lambda request, requested, default="operator": (
            _resolve_actor_username(
                request,
                requested,
                default=default,
            )
        ),
        service_error_response=lambda exc, request=None: _service_error_response(exc, request),
        api_error_response=lambda exc, request=None: _api_error_response(exc, request=request),
        project_query_service=lambda: _composed_service(current_app, "project_query_service"),
        project_command_service=lambda: _composed_service(current_app, "project_command_service"),
        job_query_service=lambda: _composed_service(current_app, "job_query_service"),
        approval_query_service=lambda: _composed_service(current_app, "approval_query_service"),
        get_logger_only=lambda: _composed_service(current_app, "ui_store"),
        get_logger=lambda: _composed_service(current_app, "ui_store"),
        list_workflow_choices=lambda enabled_only=False: _list_workflow_choices(
            enabled_only=enabled_only,
        ),
        env_flag=lambda name, default: _env_flag(name, default),
        route_registered=lambda path, method: _serve_request_runtime.route_registered(
            current_app.router, path, method
        ),
        ws_authenticate=lambda websocket: _serve_request_runtime.ws_authenticate(
            websocket,
            local_auth_enabled_fn=lambda: _app_local_auth_enabled(current_app),
            external_auth_enabled=_app_external_auth_enabled(current_app),
            auth_cookie_name=_AUTH_COOKIE_NAME,
            get_logger_only_fn=lambda: _composed_service(current_app, "identity_store"),
            auth_tables_available_fn=lambda bl: _auth_tables_available(bl),
            get_user_by_session_token_fn=lambda bl, token: _get_user_by_session_token(bl, token),
            normalize_live_principal_fn=lambda raw_user, default_auth_source: (
                _normalize_live_principal(
                    raw_user,
                    default_auth_source=default_auth_source,
                )
            ),
            resolved_principal_type=ResolvedPrincipal,
            err_authentication_required=ERR_AUTHENTICATION_REQUIRED,
            sleep_fn=asyncio.sleep,
        ),
        serialize_log_row=lambda row, job_id: _serialize_log_row(row, job_id),
        serialize_event_row=lambda row: _serialize_event_row(row),
        serialize_agent_message_row=lambda row: _serialize_agent_message_row(row),
        ws_get_approval_payload=lambda job: _ws_get_approval_payload(job),
        approval_fallback_payload=lambda job: _approval_fallback_payload(job),
        build_runtime_payload=lambda job, bl: _build_runtime_payload(job, bl),
        project_job_detail=lambda **kwargs: project_job_detail(**kwargs),
        user_role_from_auth=lambda auth_user: user_role_from_auth(auth_user),
        get_thread_state_values=lambda thread_id: _get_thread_state_values(thread_id),
        view_payload_dict=lambda payload: _view_payload_dict(payload),
        cron_next_run_at=lambda cron_expr, from_dt, schedule_tz="UTC": _cron_next_run_at(
            cron_expr,
            from_dt,
            schedule_tz=schedule_tz,
        ),
        normalize_cron_expr=lambda value: _normalize_cron_expr(value),
        parse_cron_expression=lambda cron_expr: _parse_cron_expression(cron_expr),
        cron_human_summary=lambda cron_expr, schedule_tz="UTC": _cron_human_summary(
            cron_expr,
            schedule_tz,
        ),
        logger=logger,
        dashboard_default_range=_DASHBOARD_DEFAULT_RANGE,
        dashboard_range_hours=_DASHBOARD_RANGE_HOURS,
        dashboard_metrics_page_size=_DASHBOARD_METRICS_PAGE_SIZE,
        dashboard_metrics_max_rows=_DASHBOARD_METRICS_MAX_ROWS,
        dashboard_triage_limit=_DASHBOARD_TRIAGE_LIMIT,
        dashboard_rollup_read_enabled=_DASHBOARD_ROLLUP_READ_ENABLED,
        auth_admin_roles=_AUTH_ADMIN_ROLES,
        auth_write_roles=_AUTH_WRITE_ROLES,
        ws_terminal={
            "completed",
            "failed",
            "max_iterations",
            "cancelled",
            _RETRYABLE_INTERRUPTED_STEP_STATUS,
        },
        project_create_input_cls=ProjectCreateInput,
        project_update_input_cls=ProjectUpdateInput,
        service_error_cls=ServiceError,
        service_not_found_error_cls=ServiceNotFoundError,
        err_authentication_required=ERR_AUTHENTICATION_REQUIRED,
        err_invalid_json_body=ERR_INVALID_JSON_BODY,
        err_database_not_configured=ERR_DATABASE_NOT_CONFIGURED,
        err_project_not_found=ERR_PROJECT_NOT_FOUND,
        err_operator_role_required=ERR_OPERATOR_ROLE_REQUIRED,
        path_login=PATH_LOGIN,
        path_bootstrap=PATH_BOOTSTRAP,
        default_agent_models=lambda request=None: (
            _to_json_obj(_request_model_selection_context(request).get("default_agent_models"))
            or {}
        ),
        available_providers=lambda request=None: list(
            _request_model_selection_context(request).get("available_providers") or []
        ),
        available_models=lambda request=None: list(
            _request_model_selection_context(request).get("available_models") or []
        ),
        providers_with_models=lambda request=None: (
            _request_model_selection_context(request).get("providers_with_models") or {}
        ),
        resolve_model_selection_context=lambda request=None: _request_model_selection_context(
            request
        ),
    )


def _register_frontend_shell_routes(current_app: FastAPI) -> None:
    from core.entrypoints.web.frontend_shell import create_frontend_shell_router
    from core.infrastructure.runtime.frontend_shell_runtime import (
        build_frontend_shell_artifacts,
        frontend_shell_available,
        load_frontend_manifest,
        manifest_asset_paths,
        resolve_frontend_asset,
    )

    current_app.include_router(
        create_frontend_shell_router(
            build_artifacts=lambda: build_frontend_shell_artifacts(),
            frontend_shell_available=frontend_shell_available,
            load_frontend_manifest=load_frontend_manifest,
            manifest_asset_paths=manifest_asset_paths,
            resolve_frontend_asset=resolve_frontend_asset,
        )
    )


def _frontend_shell_route_registered(
    current_app: FastAPI, *, path: str, method: str = "GET"
) -> bool:
    required_method = str(method or "GET").strip().upper()
    for route in getattr(current_app, "routes", ()):
        route_path = str(getattr(route, "path", "") or "")
        methods = {str(item).upper() for item in set(getattr(route, "methods", set()) or set())}
        if route_path == path and required_method in methods:
            return True
    return False


def _register_registered_frontend_shell_routes(current_app: FastAPI) -> None:
    from core.entrypoints.web.frontend_shell import create_registered_frontend_shell_router
    from core.infrastructure.runtime.frontend_shell_runtime import (
        build_frontend_shell_artifacts,
        frontend_shell_available,
    )

    paths = [
        path
        for path in _ui_page_registry.resolve_registered_shell_paths(_app_registries(current_app))
        if not _frontend_shell_route_registered(current_app, path=path)
    ]
    if not paths:
        return

    current_app.include_router(
        create_registered_frontend_shell_router(
            registered_paths=paths,
            build_artifacts=lambda: build_frontend_shell_artifacts(),
            frontend_shell_available=frontend_shell_available,
        )
    )


from core.entrypoints.api.dashboard_metrics import (  # noqa: E402, I001
    _dashboard_fetch_cost_by_job as _dashboard_fetch_cost_by_job,
)
from core.entrypoints.api.extension_artifacts import (  # noqa: E402
    create_extension_artifacts_router as _create_extension_artifacts_router,
)
from core.entrypoints.api.extension_callbacks import (  # noqa: E402
    create_extension_callbacks_router as _create_extension_callbacks_router,
)
from core.entrypoints.api.extension_oauth import (  # noqa: E402
    build_extension_oauth_callback_handler as _build_extension_oauth_callback_handler,
)
from core.entrypoints.api.job_webhook_bindings import (  # noqa: E402
    register_provider_webhook_routes as _register_provider_webhook_routes,
    register_workflow_entrypoint_routes as _register_workflow_entrypoint_routes,
)
from core.entrypoints.api.provider_webhooks import (  # noqa: E402
    _ingress_notification_provider_payload as _provider_webhooks_ingress_payload,
    _normalize_task_provider_payload as _provider_webhooks_normalize_payload,
    _provider_supports_ingress_events as _provider_webhooks_supports_ingress_events,
    _resolve_provider_extension_for_runtime as _provider_webhooks_resolve_extension,
    _resolve_task_provider_project_run_config as _provider_webhooks_resolve_project_run_config,
)

# Re-exported symbols used by unit tests and patches.
_resolve_task_provider_project_run_config = _provider_webhooks_resolve_project_run_config
_resolve_provider_extension_for_runtime = _provider_webhooks_resolve_extension
_provider_supports_ingress_events = _provider_webhooks_supports_ingress_events
_normalize_task_provider_payload = _provider_webhooks_normalize_payload
_ingress_notification_provider_payload = _provider_webhooks_ingress_payload


def _register_job_and_webhook_routes(current_app: FastAPI) -> None:
    _register_provider_webhook_routes(
        current_app,
        env_flag=lambda name, default: _env_flag(name, default),
        request_scope=lambda request, **kwargs: _request_scope(request, **kwargs),
        to_json_obj=lambda value: _to_json_obj(value),
        get_logger_only=lambda: _composed_service(current_app, "provider_webhook_store"),
        service_error_response=lambda exc, request=None: _service_error_response(exc, request),
        api_error_response=lambda exc, request=None, **kwargs: _api_error_response(
            exc,
            request=request,
            **kwargs,
        ),
        event_command_service=lambda: _composed_service(current_app, "event_command_service"),
        resolve_task_provider_project_run_config=lambda bl, **kwargs: (
            _resolve_task_provider_project_run_config(
                bl,
                **kwargs,
            )
        ),
        resolve_provider_extension_for_runtime=lambda bl, provider, **kwargs: (
            _resolve_provider_extension_for_runtime(
                bl,
                provider,
                **kwargs,
            )
        ),
        provider_supports_ingress_events=lambda extension: _provider_supports_ingress_events(
            extension
        ),
        normalize_task_provider_payload=lambda **kwargs: _normalize_task_provider_payload(**kwargs),
        ingress_notification_provider_payload=lambda **kwargs: (
            _ingress_notification_provider_payload(**kwargs)
        ),
        logger=logger,
        err_database_not_configured=ERR_DATABASE_NOT_CONFIGURED,
        err_webhook_processing_failed=ERR_WEBHOOK_PROCESSING_FAILED,
    )

    current_app.include_router(
        _create_extension_artifacts_router(artifact_plane=_artifact_plane, logger=logger)
    )

    current_app.include_router(
        _create_extension_callbacks_router(
            get_callback_store=lambda: _composed_service(current_app, "identity_store"),
            get_project_repository=lambda: _composed_service(current_app, "project_store"),
            request_scope=lambda request, **kwargs: _request_scope(request, **kwargs),
            logger=logger,
            err_database_not_configured=ERR_DATABASE_NOT_CONFIGURED,
            event_command_service_factory=lambda: _composed_service(
                current_app, "event_command_service"
            ),
            oauth_authorization_callback=lambda **values: _build_extension_oauth_callback_handler(
                _composed_service(current_app, "integration_store")
            )(**values),
        )
    )

    _register_workflow_entrypoint_routes(
        current_app,
        get_logger_only=lambda: _composed_service(current_app, "project_store"),
        get_trigger_store=lambda: _workflow_trigger_store(current_app),
        request_scope=lambda request, **kwargs: _request_scope(request, **kwargs),
        enqueue_task=lambda **kwargs: _enqueue_task(**kwargs),
        redeliver_task=lambda **kwargs: _task_enqueue_runtime.redeliver_pre_execution_job(
            enqueue_task_fn=_enqueue_task_in_queue,
            logger=logger,
            **kwargs,
        ),
        artifact_plane=_artifact_plane,
        logger=logger,
        err_database_not_configured=ERR_DATABASE_NOT_CONFIGURED,
    )


def _register_core_routes(current_app: FastAPI) -> None:
    for register in (
        _register_frontend_shell_routes,
        _register_internal_admin_surfaces,
        _register_foundation_routes,
        _register_api_route_bindings,
        _register_auth_route_bindings,
        _register_ui_route_bindings,
        _register_job_and_webhook_routes,
    ):
        register(current_app)


# ---------------------------------------------------------------------------
# Projects CRUD
# ---------------------------------------------------------------------------

__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
