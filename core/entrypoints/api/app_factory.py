# mypy: ignore-errors
import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from core.entrypoints.api import route_registration as api_routes
from core.entrypoints.api.public_openapi_policy import PUBLIC_OPENAPI_POLICY
from core.infrastructure.extension_runtime.extension_runtime_helpers import (
    get_extension_query_provider,
    list_extension_runtime_records,
    resolve_extension_runtime_record,
)
from core.infrastructure.runtime import http_app_events_resume as runtime
from core.infrastructure.runtime import http_app_reactions_extensions as reactions_runtime
from core.infrastructure.runtime.workflow_resume_wiring import workflow_resume_command_ports

_TOKEN_CORE_INFRASTRUCTURE_EXTENSION_RUNTIME_EXTENSION_RUNTIME = (
    "core.infrastructure.extension_runtime.extension_runtime"
)

_APP_BOOTSTRAP_STAGES = (
    "create_app",
    "register_frontend_static_assets",
    "register_core_middleware",
    "initialize_route_dependencies",
    "register_core_routers",
    "load_optional_private_modules",
)

_logger = logging.getLogger(__name__)


def _reconcile_extension_providers_at_startup(bl: Any) -> list[dict[str, Any]]:
    """Reconcile derived provider rows without broadening startup error handling."""
    from core.application.extensions.provider_registration import (
        ProviderRegistrationError,
        reconcile_extension_providers_for_logger,
    )

    try:
        return reconcile_extension_providers_for_logger(bl)
    except ProviderRegistrationError:
        # An unreachable catalog is a degraded platform, not a platform that
        # must refuse to boot. Unexpected callback errors still propagate.
        _logger.exception("extension provider catalog reconcile failed")
        return []


def _runtime_container(current_app: FastAPI):
    return getattr(current_app.state, "runtime_container", None)


def _local_auth_enabled(current_app: FastAPI) -> bool:
    override = getattr(current_app.state, "local_auth_enabled_override", None)
    if callable(override):
        return bool(override())
    container = _runtime_container(current_app)
    metadata = getattr(container, "metadata", None)
    default_enabled = (
        bool(metadata.get("local_auth_default_enabled", True))
        if isinstance(metadata, dict)
        else True
    )
    return runtime._env_flag("FS_LOCAL_AUTH_ENABLED", default_enabled)


def _external_auth_enabled(current_app: FastAPI) -> bool:
    override = getattr(current_app.state, "external_auth_enabled_override", None)
    if callable(override):
        return bool(override())
    container = _runtime_container(current_app)
    registries = getattr(container, "registries", None)
    if not isinstance(registries, dict):
        return False
    resolvers = registries.get("external_auth_enabled")
    if callable(resolvers):
        resolvers = (resolvers,)
    return any(callable(resolver) and resolver() for resolver in resolvers or ())


def _optional_route_allowed(current_app: FastAPI, path: str, registry_key: str) -> bool:
    container = _runtime_container(current_app)
    registries = getattr(container, "registries", None)
    if not isinstance(registries, dict):
        return False
    predicate_key = f"{registry_key[:-1]}_predicates"
    return runtime._serve_auth_runtime.optional_route_path_allowed(
        path,
        paths=registries.get(registry_key),
        predicates=registries.get(predicate_key),
    )


def _is_public_auth_path(current_app: FastAPI, path: str) -> bool:
    return runtime._serve_auth_runtime.is_public_auth_path(
        path,
        public_health_paths=runtime._PUBLIC_HEALTH_PATHS,
        auth_public_paths=set(runtime._AUTH_PUBLIC_PATHS_BASE) | runtime._docs_public_auth_paths(),
        public_webhook_path_prefixes=runtime._PUBLIC_WEBHOOK_PATH_PREFIXES,
        public_frontend_path_prefixes=runtime._PUBLIC_FRONTEND_PATH_PREFIXES,
        path_login=runtime.PATH_LOGIN,
    ) or _optional_route_allowed(current_app, path, "public_auth_paths")


def _is_self_service_auth_write_route(current_app: FastAPI, path: str) -> bool:
    return runtime._serve_auth_runtime.is_self_service_auth_write_route(
        path
    ) or _optional_route_allowed(current_app, path, "self_service_auth_write_paths")


def _build_event_command_service_factory(current_app: FastAPI):
    return runtime._command_service_runtime.build_event_command_service_factory(
        config=runtime.CONFIG,
        get_logger=lambda: api_routes._composed_service(current_app, "event_store"),
        get_job_store=lambda: api_routes._composed_service(current_app, "job_store"),
        get_project_run_config=lambda project_id: api_routes._composed_service(
            current_app, "project_store"
        ).get_project_run_config(project_id),
        resolve_domain_event_by_ref=runtime._resolve_domain_event_by_ref,
        to_json_obj=runtime._to_json_obj,
        serialize_domain_event_datetime=runtime._serialize_domain_event_datetime,
        sanitize_external_input=runtime.sanitize_external_input,
        enqueue_task=runtime._enqueue_task,
        resolve_workflow_selection=lambda requested, _source="", project_workflows=None: (
            runtime._resolve_workflow_selection(requested, project_workflows)
        ),
        workflow_availability_assert=runtime._assert_can_start_workflow,
        workflow_exists=lambda workflow_id: reactions_runtime._workflow_exists(workflow_id),
        evaluate_workflow_reactions=runtime._evaluate_workflow_reactions,
        reaction_action_result_executed=runtime._reaction_action_result_executed,
        dispatch_project_notification_event=runtime._dispatch_project_notification_event,
        fetch_provider_attachments=lambda *args, **kwargs: __import__(
            "core.application.extensions.provider_attachments",
            fromlist=["fetch_provider_attachments"],
        ).fetch_provider_attachments(*args, **kwargs),
        artifact_plane=runtime._artifact_plane,
    )


def _build_composed_service_factories(
    current_app: FastAPI,
) -> dict[str, Callable[[], object]]:
    get_job_store = lambda: api_routes._composed_service(current_app, "job_store")  # noqa: E731
    get_project_store = lambda: api_routes._composed_service(  # noqa: E731
        current_app, "project_store"
    )
    get_event_store = lambda: api_routes._composed_service(  # noqa: E731
        current_app, "event_store"
    )
    get_observability_store = lambda: api_routes._composed_service(  # noqa: E731
        current_app, "observability_store"
    )
    get_identity_store = lambda: api_routes._composed_service(  # noqa: E731
        current_app, "identity_store"
    )
    get_integration_store = lambda: api_routes._composed_service(  # noqa: E731
        current_app, "integration_store"
    )
    (
        job_query_service,
        project_query_service,
        job_command_service,
        project_command_service,
    ) = runtime._build_app_scoped_service_factories(
        get_logger_only=get_project_store,
        get_job_store=get_job_store,
        get_project_store=get_project_store,
        get_event_store=get_event_store,
        get_trace_store=get_observability_store,
    )
    event_command_service = _build_event_command_service_factory(current_app)
    event_query_service = runtime._query_service_runtime.build_event_query_service_factory(
        get_logger=get_event_store,
        event_project_name=lambda *args, **kwargs: runtime._event_project_name(*args, **kwargs),
        resolve_domain_event_by_ref=lambda *args, **kwargs: runtime._resolve_domain_event_by_ref(
            *args, **kwargs
        ),
        serialize_domain_event_summary=lambda *args, **kwargs: (
            runtime._serialize_domain_event_summary(*args, **kwargs)
        ),
        serialize_domain_event_datetime=lambda *args, **kwargs: (
            runtime._serialize_domain_event_datetime(*args, **kwargs)
        ),
        to_json_obj=lambda value: runtime._to_json_obj(value),
        workflow_exists=lambda *args, **kwargs: reactions_runtime._workflow_exists(*args, **kwargs),
    )
    approval_query_service = runtime._query_service_runtime.build_approval_query_service_factory(
        get_logger=get_job_store,
        get_project_by_id=lambda project_id: get_project_store().get_project(project_id),
        get_thread_state_snapshot=lambda *args, **kwargs: runtime._get_thread_state_snapshot(
            *args, **kwargs
        ),
        approval_fallback_payload=lambda *args, **kwargs: runtime._approval_fallback_payload(
            *args, **kwargs
        ),
        to_json_obj=lambda value: runtime._to_json_obj(value),
    )
    extension_query_service = runtime._query_service_runtime.build_extension_query_service_factory(
        get_logger=get_identity_store,
        resolve_extension_runtime_record=lambda extension_id, *, account_id=None: (
            resolve_extension_runtime_record(
                extension_id,
                get_logger_only=get_identity_store,
                account_id=account_id,
            )
        ),
        list_extension_runtime_records=list_extension_runtime_records,
        build_extension_compiled_manifest=lambda *args, **kwargs: (
            api_routes._build_extension_compiled_manifest(*args, **kwargs)
        ),
        extension_validation_summary=lambda *args, **kwargs: (
            api_routes._extension_validation_summary(*args, **kwargs)
        ),
        list_extension_projects=lambda *args, **kwargs: api_routes._extension_page_projects(
            get_project_store(), *args, **kwargs
        ),
        compile_extension_preview=lambda *args, **kwargs: __import__(
            _TOKEN_CORE_INFRASTRUCTURE_EXTENSION_RUNTIME_EXTENSION_RUNTIME,
            fromlist=["compile_extension_preview"],
        ).compile_extension_preview(*args, **kwargs),
        run_extension_health=lambda *args, **kwargs: __import__(
            _TOKEN_CORE_INFRASTRUCTURE_EXTENSION_RUNTIME_EXTENSION_RUNTIME,
            fromlist=["run_extension_health"],
        ).run_extension_health(*args, **kwargs),
        extension_governance_status=lambda extension_id, account_id, extension_install_id: dict(
            __import__(
                _TOKEN_CORE_INFRASTRUCTURE_EXTENSION_RUNTIME_EXTENSION_RUNTIME,
                fromlist=["ExtensionCircuitBreaker"],
            )
            .ExtensionCircuitBreaker.for_extension(
                extension_id,
                account_id=account_id,
                extension_install_id=extension_install_id,
            )
            .status()
        ),
        get_install_store=lambda: api_routes._get_extension_install_store(),
        get_runtime_store=lambda: api_routes._get_extension_runtime_store(),
    )
    approval_command_service = (
        runtime._command_service_runtime.build_approval_command_service_factory(
            resume_shutdown_in_progress=runtime._resume_shutdown_in_progress,
            get_checkpointer=runtime._get_checkpointer,
            get_logger=get_job_store,
            get_thread_state_snapshot=runtime._get_thread_state_snapshot,
            **workflow_resume_command_ports(),
        )
    )
    extension_binding_service = runtime._command_service_runtime.build_extension_binding_service_factory(
        # Lifecycle keeps the identity store. Operations — the path the
        # interface actually exercises — get the store that can persist
        # project connections, routes and ingress; the identity store
        # exposes none of them, which is why every messaging-extension save answered
        # `project_connection_storage_unavailable` while tests stayed green.
        get_logger=get_identity_store,
        get_operation_store=get_integration_store,
        # Direct actions mint and read artifacts through the same plane as
        # workflow connector steps; without it their grant fields were dropped.
        get_artifact_plane=lambda: runtime._artifact_plane,
        get_project_by_id=lambda project_id: get_project_store().get_project(project_id),
        resolve_extension_runtime_record=lambda extension_id: resolve_extension_runtime_record(
            extension_id, get_logger_only=get_integration_store
        ),
        extension_identifier=lambda *args, **kwargs: api_routes._extension_identifier(
            *args, **kwargs
        ),
        extension_operation_handler_mode=lambda *args, **kwargs: (
            api_routes._extension_operation_handler_mode(*args, **kwargs)
        ),
        extension_operation_failure_status=lambda *args, **kwargs: (
            api_routes._extension_operation_failure_status(*args, **kwargs)
        ),
        get_extension_install_store=lambda: api_routes._get_extension_install_store(),
        get_extension_runtime_store=lambda: api_routes._get_extension_runtime_store(),
        event_command_service_factory=event_command_service,
        extension_query_service_factory=lambda *, bl, install_store, runtime_store: (
            runtime._query_service_runtime.create_extension_query_service(
                get_logger=lambda: bl,
                get_install_store=lambda: install_store,
                get_runtime_store=lambda: runtime_store,
                resolve_extension_runtime_record=lambda *args, **kwargs: (
                    resolve_extension_runtime_record(*args, **kwargs)
                ),
                list_extension_runtime_records=list_extension_runtime_records,
                build_extension_compiled_manifest=lambda *args, **kwargs: (
                    api_routes._build_extension_compiled_manifest(*args, **kwargs)
                ),
                extension_validation_summary=lambda *args, **kwargs: (
                    api_routes._extension_validation_summary(*args, **kwargs)
                ),
                list_extension_projects=lambda *args, **kwargs: api_routes._extension_page_projects(
                    bl, *args, **kwargs
                ),
                compile_extension_preview=lambda *args, **kwargs: __import__(
                    _TOKEN_CORE_INFRASTRUCTURE_EXTENSION_RUNTIME_EXTENSION_RUNTIME,
                    fromlist=["compile_extension_preview"],
                ).compile_extension_preview(*args, **kwargs),
                extension_governance_status=lambda extension_id, account_id, extension_install_id: (
                    dict(
                        __import__(
                            _TOKEN_CORE_INFRASTRUCTURE_EXTENSION_RUNTIME_EXTENSION_RUNTIME,
                            fromlist=["ExtensionCircuitBreaker"],
                        )
                        .ExtensionCircuitBreaker.for_extension(
                            extension_id,
                            account_id=account_id,
                            extension_install_id=extension_install_id,
                        )
                        .status()
                    )
                ),
            )
        ),
    )
    external_agent_command_service = (
        runtime._command_service_runtime.build_external_agent_command_service_factory(
            get_logger=get_job_store,
        )
    )
    return {
        "approval_query_service": approval_query_service,
        "approval_command_service": approval_command_service,
        "event_query_service": event_query_service,
        "event_command_service": event_command_service,
        "job_query_service": job_query_service,
        "job_command_service": job_command_service,
        "project_query_service": project_query_service,
        "project_command_service": project_command_service,
        "extension_query_service": extension_query_service,
        "extension_binding_service": extension_binding_service,
        "external_agent_command_service": external_agent_command_service,
        "tool_execution_service": (
            runtime._command_service_runtime.build_tool_execution_service_factory(
                get_logger=get_job_store,
                # The job store cannot resolve extensions; extension lookup needs
                # the integration store, which is what every other extension route
                # already uses.
                get_extension_store=get_integration_store,
                artifact_plane=runtime._artifact_plane,
                event_command_service_factory=event_command_service,
            )
        ),
    }


def create_app(
    *,
    initialize_api_dependencies: Callable[..., None] | None = None,
    runtime_service_type: str = "app",
) -> FastAPI:
    from core.application.jobs.submission_admission import public_submission_pause_active
    from core.application.monitoring.runtime_composition import start_optional_process_reporting
    from core.infrastructure.config.runtime_env import runtime_env

    current_app = runtime._create_base_app()
    runtime._register_static_assets(current_app)
    current_route_registered = runtime._build_route_registered_resolver(current_app)
    composed_service_factories = _build_composed_service_factories(current_app)

    runtime._configure_lifecycle_runtime(
        current_app,
        runtime._LifecycleRuntimeBindings(
            get_logger_only=lambda: api_routes._composed_service(current_app, "lifecycle_store"),
            prewarm_state_reader_graph=runtime._prewarm_state_reader_graph,
            watchdog_scan=runtime._watchdog_scan,
            scheduled_jobs_scan=runtime._scheduled_jobs_scan,
            dashboard_rollup_scan=runtime._dashboard_rollup_scan,
            external_effect_maintenance_scan=runtime._external_effect_maintenance_scan,
            reconcile_extension_providers=_reconcile_extension_providers_at_startup,
            public_submission_pause_active=public_submission_pause_active,
            local_auth_enabled=lambda: _local_auth_enabled(current_app),
            external_auth_enabled=lambda: _external_auth_enabled(current_app),
            is_public_auth_path=lambda path: _is_public_auth_path(current_app, path),
            route_registered=current_route_registered,
            resolve_request_principal=lambda request, bl: api_routes._resolve_request_principal(
                current_app, request, bl
            ),
            normalize_live_principal=lambda user, **kwargs: api_routes._normalize_live_principal(
                user, **kwargs
            ),
            auth_tables_available=lambda bl: api_routes._auth_tables_available(bl),
            get_user_by_session_token=lambda bl, token: api_routes._get_user_by_session_token(
                bl, token
            ),
            local_auth_bootstrap_required=lambda bl=None: api_routes._local_auth_bootstrap_required(
                bl,
                get_logger_only_fn=lambda: api_routes._composed_service(
                    current_app, "identity_store"
                ),
            ),
            auth_account_tables_available=lambda bl: api_routes._auth_account_tables_available(bl),
            roles_intersect=lambda actual, required: api_routes._roles_intersect(actual, required),
            is_admin_only_route=lambda path: api_routes._is_admin_only_route(path),
            is_registry_read_route=lambda path: api_routes._is_registry_read_route(path),
            is_self_service_auth_write_route=lambda path: _is_self_service_auth_write_route(
                current_app, path
            ),
            csrf_request_valid=lambda request: api_routes._csrf_request_valid(request),
            start_runtime_reporting=lambda: start_optional_process_reporting(
                runtime_values=dict(runtime_env), service_type=runtime_service_type
            ),
        ),
    )
    runtime._register_core_exception_handlers(current_app)
    if runtime.CONFIG.get("extension_installer_proxy_enabled", False):
        from core.application.extensions.installer_projection_refresh import (
            InstallerProjectionSync,
            refresh_installer_projections,
        )
        from core.entrypoints.api.extension_installer_proxy import ExtensionInstallerProxy

        def refresh() -> None:
            refresh_installer_projections(
                get_install_store=api_routes._get_extension_install_store,
                get_runtime_store=api_routes._get_extension_runtime_store,
            )

        current_app.add_middleware(
            ExtensionInstallerProxy,
            refresh=refresh,
            synchronize=InstallerProjectionSync(
                get_install_store=api_routes._get_extension_install_store, refresh=refresh
            ),
        )
    runtime._register_core_http_middleware(current_app)

    operator_roles = set(runtime._AUTH_WRITE_ROLES) - set(runtime._AUTH_ADMIN_ROLES)
    platform_contracts = runtime._platform_contracts.build_platform_contracts(
        admin_roles=runtime._AUTH_ADMIN_ROLES,
        operator_roles=operator_roles or {"operator"},
        viewer_roles={"viewer"},
    )
    default_legacy_db_factory = runtime._get_connection_store

    container = runtime._build_runtime_container(
        app=current_app,
        config=runtime.CONFIG,
        auth_helpers={
            "local_auth_enabled": runtime._local_auth_enabled,
            "require_authenticated_user": runtime._require_authenticated_user,
            "request_scope": runtime._request_scope,
            "request_principal": runtime._request_principal,
            "request_auth_user_payload": runtime._request_auth_user_payload,
            "api_error_response": runtime._api_error_response,
            "service_error_response": runtime._service_error_response,
            "public_base_url": runtime._public_base_url,
            "session_token_hash": runtime._session_token_hash,
            "auth_cookie_secure": runtime._auth_cookie_secure,
            "auth_cookie_name": lambda: runtime._AUTH_COOKIE_NAME,
            "auth_csrf_cookie_name": lambda: runtime._AUTH_CSRF_COOKIE_NAME,
            "log_auth_audit_event": runtime._log_auth_audit_event,
            "redirect": api_routes._redirect,
            "flash": runtime._flash,
        },
        service_factories={
            "db": default_legacy_db_factory,
            "connection_store": runtime._get_connection_store,
            "identity_store": runtime._get_identity_store,
            "project_store": runtime._get_project_store,
            "project_dataset_catalog": reactions_runtime._get_project_dataset_catalog_store,
            "job_store": runtime._get_job_store,
            "event_store": runtime._get_event_store,
            "schedule_store": runtime._get_schedule_store,
            "observability_store": runtime._get_observability_store,
            "worker_store": runtime._get_worker_store,
            "integration_store": runtime._get_integration_store,
            "provider_webhook_store": runtime._get_provider_webhook_store,
            "reporting_store": runtime._get_reporting_store,
            "lifecycle_store": runtime._get_lifecycle_store,
            "ui_store": runtime._get_ui_store,
            "approval_query_service": composed_service_factories["approval_query_service"],
            "approval_command_service": composed_service_factories["approval_command_service"],
            "event_query_service": composed_service_factories["event_query_service"],
            "event_command_service": composed_service_factories["event_command_service"],
            "extension_query_service": composed_service_factories["extension_query_service"],
            "extension_binding_service": composed_service_factories["extension_binding_service"],
            "job_query_service": composed_service_factories["job_query_service"],
            "job_command_service": composed_service_factories["job_command_service"],
            "project_query_service": composed_service_factories["project_query_service"],
            "project_command_service": composed_service_factories["project_command_service"],
            "tool_execution_service": composed_service_factories["tool_execution_service"],
            "model_provider_command_service": (lambda: runtime._model_provider_command_service()),
            "external_agent_command_service": composed_service_factories[
                "external_agent_command_service"
            ],
            "authorization_service": (
                lambda contracts=platform_contracts: contracts["authorization_service"]
            ),
            "entitlement_service": (
                lambda contracts=platform_contracts: contracts["entitlement_service"]
            ),
            "audit_sink": lambda contracts=platform_contracts: contracts["audit_sink"],
            "secret_provider": lambda contracts=platform_contracts: contracts["secret_provider"],
            "extension_governance_policy": (
                lambda contracts=platform_contracts: contracts["extension_governance_policy"]
            ),
        },
        registries={
            "settings_pages": {},
            "registered_pages": {},
            "navigation_items": {},
            "public_auth_paths": set(),
            "public_auth_path_predicates": [],
            "self_service_auth_write_paths": set(),
            "self_service_auth_write_path_predicates": [],
            "request_principal_resolvers": [],
            "extension_query_provider_factory": get_extension_query_provider,
            "platform_contracts": platform_contracts,
        },
        metadata={
            "edition": "community",
            "bootstrap_stages": list(_APP_BOOTSTRAP_STAGES),
            "default_legacy_db_factory": default_legacy_db_factory,
        },
    )
    runtime._ui_page_registry.register_core_settings_pages(container.registries)
    from core.entrypoints.api.runtime_port_manifest import RUNTIME_SERVICE_PORT_CONTRACTS
    from core.infrastructure.runtime.app_factory import validate_runtime_service_ports

    container.metadata["validated_runtime_services"] = sorted(
        validate_runtime_service_ports(container, RUNTIME_SERVICE_PORT_CONTRACTS)
    )
    current_app.state.runtime_container = container
    current_app.state.route_registered = current_route_registered
    current_app.state.bootstrap_stages = list(_APP_BOOTSTRAP_STAGES)

    api_routes._initialize_shared_route_dependencies(current_app)
    if initialize_api_dependencies is not None:
        from core.infrastructure.runtime.state_reader_runtime import (
            get_committed_checkpoint_values,
        )

        initialize_api_dependencies(
            get_logger_only=runtime._get_job_store,
            get_thread_state_snapshot=runtime._get_thread_state_snapshot,
            get_committed_checkpoint_values=lambda thread_id: get_committed_checkpoint_values(
                thread_id, config=runtime.CONFIG, logger=runtime.logger
            ),
            to_json_obj=runtime._to_json_obj,
            resolve_artifact=runtime._artifact_plane.get_artifact,
            artifact_presigned_url=runtime._artifact_plane.artifact_public_download_url,
            artifact_local_stream=runtime._artifact_plane.artifact_local_stream,
            artifact_read_bytes=runtime._artifact_plane.read_artifact_bytes,
        )
    api_routes._register_core_routes(current_app)

    optional_private_modules_loaded = runtime._load_optional_private_modules(
        current_app,
        container,
        logger=runtime.logger,
    )
    container.metadata["optional_private_modules_loaded"] = list(optional_private_modules_loaded)
    current_app.state.optional_private_modules_loaded = list(optional_private_modules_loaded)
    api_routes._register_registered_frontend_shell_routes(current_app)
    runtime._register_curated_openapi_routes(
        current_app,
        public_openapi_policy=PUBLIC_OPENAPI_POLICY,
    )

    return current_app


def _auth_users_route_endpoint(path: str, method: str):
    from core.entrypoints.api.app import app

    return runtime._serve_request_runtime.route_endpoint(
        app.state.auth_users_router,
        path,
        method,
    )


# Health, readiness, and metrics routes are in routes/health.py

__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
