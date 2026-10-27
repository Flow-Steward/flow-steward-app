"""Non-auth API and service route registration helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request

from core.application.settings.ui_i18n import build_client_i18n_payload
from core.entrypoints.api.app_bootstrap import create_api_app_bootstrap_router
from core.entrypoints.api.contracts_v2 import create_api_contracts_v2_router
from core.entrypoints.api.dashboard_metrics import create_dashboard_metrics_router
from core.entrypoints.api.events import create_api_events_router
from core.entrypoints.api.extension_catalog import create_extension_catalog_router
from core.entrypoints.api.extension_oauth import create_extension_oauth_router
from core.entrypoints.api.external_agents import create_api_external_agents_router
from core.entrypoints.api.integrations_connectors import create_integrations_connectors_router
from core.entrypoints.api.jobs import create_api_jobs_router
from core.entrypoints.api.jobs_control import create_jobs_control_router
from core.entrypoints.api.llm_provider_extensions import (
    create_api_llm_provider_extensions_router,
)
from core.entrypoints.api.mcp_server_catalog import create_mcp_server_catalog_router
from core.entrypoints.api.messages import create_api_messages_router
from core.entrypoints.api.model_catalog import create_api_model_catalog_router
from core.entrypoints.api.model_providers import create_api_model_providers_router
from core.entrypoints.api.processes import router as processes_router
from core.entrypoints.api.project_detail import create_api_project_detail_router
from core.entrypoints.api.project_integrations import create_project_integrations_router
from core.entrypoints.api.projects import create_api_projects_router
from core.entrypoints.api.reactions_extensions import create_reactions_extensions_router
from core.entrypoints.api.reports import create_api_reports_router
from core.entrypoints.api.search import create_api_search_router
from core.entrypoints.api.system_maintenance import create_system_maintenance_router
from core.entrypoints.api.system_monitoring import create_api_system_monitoring_router
from core.entrypoints.api.tabular_feed import create_api_tabular_feed_router
from core.entrypoints.api.trash import create_api_trash_router
from core.entrypoints.api.workflow_authoring import create_workflow_authoring_router
from core.entrypoints.api.workflow_operator import create_workflow_operator_router

if TYPE_CHECKING:
    from core.application.management.scope import ScopeContext
    from core.application.workflows.authoring.service import WorkflowAuthoringService


def build_global_search_service(*, get_logger_only):
    from core.application.search.global_search_service import GlobalSearchService

    return GlobalSearchService(get_logger=get_logger_only)


def request_admin_authority(
    request: Request,
    *,
    require_authenticated_user: Callable[..., dict[str, Any]],
    auth_admin_roles: set[str],
) -> Callable[[ScopeContext, str], None]:
    """The authoring check of one request: an administrator, and ``admin:all`` for a token."""

    def authorize(_scope: ScopeContext, action: str) -> None:
        from core.application.auth.auth_http_runtime import _principal_requires_http_scopes
        from core.application.auth.principal import principal_has_required_scopes
        from core.application.management.errors import ServiceForbiddenError

        try:
            user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError as exc:
            raise ServiceForbiddenError(f"{action} requires an administrator") from exc
        if _principal_requires_http_scopes(user) and not principal_has_required_scopes(
            user, ["admin:all"]
        ):
            raise ServiceForbiddenError(f"{action} requires admin:all token authority")

    return authorize


def build_workflow_authoring_service(
    *,
    request: Request,
    get_project_store: Callable[[], Any],
    get_integration_store: Callable[[], Any],
    get_extension_install_store: Callable[[], Any],
    require_authenticated_user: Callable[..., dict[str, Any]],
    auth_admin_roles: set[str],
) -> WorkflowAuthoringService:
    """Assemble current stores and request-bound creation authority without a Builder session."""
    import logging

    from core.application.management.errors import (
        ServiceUnavailableError,
        safe_operator_exception,
    )
    from core.application.registry.registry_manager import RegistryManager
    from core.application.workflows.authoring.service import WorkflowAuthoringService
    from core.application.workflows.discovery.capability_discovery_helpers import (
        _lexical_fallback_enabled,
    )
    from core.infrastructure.runtime.workflow_db_adapters import create_workflow_knowledge_store

    registry = RegistryManager.instance()
    db = registry.db_store
    if db is None:
        raise ServiceUnavailableError("Workflow registry is unavailable")

    authority = request_admin_authority(
        request,
        require_authenticated_user=require_authenticated_user,
        auth_admin_roles=auth_admin_roles,
    )

    def authorize_creation(scope: ScopeContext) -> None:
        authority(scope, "Workflow creation")

    knowledge = None
    dsn = str(getattr(db, "dsn", "") or "").strip()
    if dsn:
        try:
            knowledge = create_workflow_knowledge_store(dsn)
            knowledge.enforce_runtime(require_graph=True, require_vector=True, ensure_indexes=False)
        except Exception as exc:
            if not _lexical_fallback_enabled():
                raise ServiceUnavailableError("Workflow knowledge backend is unavailable") from exc
            knowledge = None
            logging.getLogger(__name__).warning(
                "Authoring workflow knowledge unavailable; lexical fallback: %s",
                safe_operator_exception(exc),
            )
    return WorkflowAuthoringService(
        db,
        project_store=get_project_store(),
        project_connection_store=get_integration_store(),
        get_install_store=get_extension_install_store,
        workflow_knowledge_backend=knowledge,
        creation_authorizer=authorize_creation,
        after_save=registry.refresh_workflow_cache,
    )


def _build_extension_oauth_router(
    *,
    get_extension_oauth_store,
    request_scope,
    require_authenticated_user,
    service_error_response,
    api_error_response,
):
    return create_extension_oauth_router(
        get_logger_only=get_extension_oauth_store,
        request_scope=request_scope,
        require_authenticated_user=require_authenticated_user,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
    )


def register_api_routes(
    app: FastAPI,
    *,
    get_logger_only,
    get_logger,
    request_scope,
    resolve_user_role,
    to_json_obj,
    api_error_response,
    service_error_response,
    secret_storage_config_error,
    project_query_service,
    project_command_service,
    event_query_service,
    event_command_service,
    job_query_service,
    extension_query_service,
    extension_binding_service,
    tool_execution_service,
    get_extension_install_store,
    get_extension_runtime_store,
    workflow_availability_assert,
    approval_command_service,
    job_command_service,
    redirect,
    approval_query_service,
    resolve_actor_username,
    optional_actor_username,
    watchdog_scan,
    parse_worker_pid,
    terminate_worker_pid,
    append_runtime_journal,
    build_rerun_workflow_selection,
    load_replay_snapshot,
    enqueue_task,
    require_authenticated_user,
    local_auth_enabled,
    local_auth_bootstrap_required,
    external_auth_enabled,
    current_app_edition,
    platform_contract,
    auth_admin_roles,
    model_provider_command_service,
    external_agent_command_service,
    request_principal,
    real_dict_cursor_kwargs,
    trash_lifecycle_service,
    logger,
    retryable_interrupted_step_status,
    err_database_not_configured,
    err_project_not_found,
    err_not_found,
    err_admin_role_required,
    dashboard_default_range,
    dashboard_range_hours,
    dashboard_metrics_page_size,
    dashboard_metrics_max_rows,
    dashboard_triage_limit,
    dashboard_rollup_read_enabled,
    settings_pages_resolver,
    registered_pages_resolver,
    shell_navigation_resolver,
    external_sign_in_resolver=None,
    project_create_input_cls,
    project_update_input_cls,
    service_error_cls,
    service_not_found_error_cls,
    default_agent_models,
    available_providers,
    available_models,
    providers_with_models=None,
    resolve_model_selection_context=None,
    get_project_store=None,
    get_job_store=None,
    get_identity_store=None,
    get_event_store=None,
    get_schedule_store=None,
    get_observability_store=None,
    get_worker_store=None,
    get_integration_store=None,
    get_extension_oauth_store=None,
    get_reporting_store=None,
    get_provider_webhook_store=None,
):
    _ = redirect
    get_project_store = get_project_store or get_logger_only
    get_job_store = get_job_store or get_logger_only
    get_identity_store = get_identity_store or get_logger_only
    get_event_store = get_event_store or get_logger
    get_schedule_store = get_schedule_store or get_logger_only
    get_observability_store = get_observability_store or get_logger_only
    get_worker_store = get_worker_store or get_logger_only
    get_integration_store = get_integration_store or get_logger_only
    get_extension_oauth_store = get_extension_oauth_store or get_integration_store
    get_reporting_store = get_reporting_store or get_logger_only
    get_provider_webhook_store = get_provider_webhook_store or get_logger

    api_app_bootstrap_router = create_api_app_bootstrap_router(
        request_principal=request_principal,
        local_auth_enabled=local_auth_enabled,
        local_auth_bootstrap_required=local_auth_bootstrap_required,
        external_auth_enabled=external_auth_enabled,
        current_app_edition=current_app_edition,
        platform_contract=platform_contract,
        build_client_i18n_payload=build_client_i18n_payload,
        settings_pages_resolver=settings_pages_resolver,
        registered_pages_resolver=registered_pages_resolver,
        shell_navigation_resolver=shell_navigation_resolver,
        external_sign_in_resolver=external_sign_in_resolver,
        api_error_response=api_error_response,
    )
    app.include_router(api_app_bootstrap_router)
    app.include_router(create_system_maintenance_router())

    project_integrations_router = create_project_integrations_router(
        get_logger_only=get_integration_store,
        project_query_service=project_query_service,
        project_command_service=project_command_service,
        notification_command_service=lambda: __import__(
            "core.application.management.command_services_project",
            fromlist=["ProjectCommandService"],
        ).ProjectCommandService(get_logger=get_integration_store),
        request_scope=request_scope,
        require_authenticated_user=require_authenticated_user,
        to_json_obj=to_json_obj,
        api_error_response=api_error_response,
        service_error_response=service_error_response,
        secret_storage_config_error=secret_storage_config_error,
        auth_admin_roles=auth_admin_roles,
        err_database_not_configured=err_database_not_configured,
        err_project_not_found=err_project_not_found,
        err_admin_role_required=err_admin_role_required,
    )
    app.include_router(project_integrations_router)

    workflow_operator_router = create_workflow_operator_router(
        workflow_operator_service=lambda request=None: __import__(
            "core.application.management.workflow_operator_service",
            fromlist=["WorkflowOperatorService"],
        ).WorkflowOperatorService(
            get_logger=get_worker_store,
            project_query_service=project_query_service,
            # The stores the CLI runtime passes: export, import and stats need them.
            get_project_store=get_project_store,
            get_integration_store=get_integration_store,
            get_install_store=get_extension_install_store,
            get_job_store=get_job_store,
            # Authoring over HTTP checks the request's caller, as a draft save does.
            authoring_authorizer=None
            if request is None
            else request_admin_authority(
                request,
                require_authenticated_user=require_authenticated_user,
                auth_admin_roles=auth_admin_roles,
            ),
        ),
        request_scope=request_scope,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
    )
    app.include_router(workflow_operator_router)
    app.include_router(
        create_workflow_authoring_router(
            workflow_authoring_service=lambda request: build_workflow_authoring_service(
                request=request,
                get_project_store=get_project_store,
                get_integration_store=get_integration_store,
                get_extension_install_store=get_extension_install_store,
                require_authenticated_user=require_authenticated_user,
                auth_admin_roles=auth_admin_roles,
            ),
            request_scope=request_scope,
            service_error_response=service_error_response,
            api_error_response=api_error_response,
        )
    )

    api_tabular_feed_router = create_api_tabular_feed_router(
        get_logger_only=get_project_store,
        request_scope=request_scope,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
    )
    app.include_router(api_tabular_feed_router)

    def _reload_extension_registries_after_catalog_install() -> None:
        from core.application.registry.registry_manager import RegistryManager

        RegistryManager.instance().reload_all()

    # Registered ahead of the extension routers that own `/api/extensions/{id}`
    # and `/api/extensions/{id}/install`, so `/catalog` and `/catalog/install`
    # are not captured as an extension id.
    extension_catalog_router = create_extension_catalog_router(
        get_extension_install_store=get_extension_install_store,
        extension_binding_service=extension_binding_service,
        request_scope=request_scope,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
        reload_extension_registries=_reload_extension_registries_after_catalog_install,
        get_extension_runtime_store=get_extension_runtime_store,
    )
    app.include_router(extension_catalog_router)

    reactions_extensions_router = create_reactions_extensions_router(
        event_query_service=event_query_service,
        event_command_service=event_command_service,
        job_query_service=job_query_service,
        extension_query_service=extension_query_service,
        extension_binding_service=extension_binding_service,
        tool_execution_service=tool_execution_service,
        command_dispatch_service=lambda: __import__(
            "core.application.management.command_services_dispatch",
            fromlist=["CommandDispatchService"],
        ).CommandDispatchService(
            get_logger=get_job_store,
            get_extension_store=get_integration_store,
            workflow_availability_assert=workflow_availability_assert,
            event_command_service_factory=event_command_service,
            project_command_service_factory=project_command_service,
            job_command_service_factory=job_command_service,
            approval_command_service_factory=approval_command_service,
        ),
        request_scope=request_scope,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
    )
    app.include_router(reactions_extensions_router)

    # Registered ahead of the integrations router that owns `/api/mcp/{connector_id}`,
    # so `/api/mcp/catalog` is not captured as a connector id.
    mcp_server_catalog_router = create_mcp_server_catalog_router(
        get_logger=get_identity_store,
        request_scope=request_scope,
        project_query_service=project_query_service,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
    )
    app.include_router(mcp_server_catalog_router)

    integrations_connectors_router = create_integrations_connectors_router(
        get_logger=get_identity_store,
        get_integration_store=get_integration_store,
        get_extension_install_store=get_extension_install_store,
        get_extension_runtime_store=get_extension_runtime_store,
        extension_query_service=extension_query_service,
        extension_binding_service=extension_binding_service,
        request_scope=request_scope,
        project_query_service=project_query_service,
        project_command_service=project_command_service,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
        err_not_found=err_not_found,
    )
    app.include_router(integrations_connectors_router)

    jobs_control_router = create_jobs_control_router(
        project_command_service=project_command_service,
        approval_command_service=approval_command_service,
        job_command_service=job_command_service,
        request_scope=request_scope,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
        logger=logger,
    )
    app.include_router(jobs_control_router)

    api_jobs_router = create_api_jobs_router(
        resolve_user_role=resolve_user_role,
        job_query_service=job_query_service,
        job_command_service=job_command_service,
        approval_query_service=approval_query_service,
        request_scope=request_scope,
        resolve_actor_username=resolve_actor_username,
        optional_actor_username=optional_actor_username,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
        get_logger_only=get_worker_store,
        watchdog_scan=watchdog_scan,
        parse_worker_pid=parse_worker_pid,
        terminate_worker_pid=terminate_worker_pid,
        append_runtime_journal=append_runtime_journal,
        build_rerun_workflow_selection=build_rerun_workflow_selection,
        load_replay_snapshot=load_replay_snapshot,
        enqueue_task=enqueue_task,
        retryable_interrupted_step_status=retryable_interrupted_step_status,
        err_database_not_configured=err_database_not_configured,
    )
    app.include_router(api_jobs_router)

    from core.application.reports.reporting_service import ReportingService

    api_reports_router = create_api_reports_router(
        reporting_service=lambda: ReportingService(
            get_logger_only=get_reporting_store,
            job_query_service=job_query_service,
        ),
        request_scope=request_scope,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
    )
    app.include_router(api_reports_router)

    dashboard_metrics_router = create_dashboard_metrics_router(
        request_scope=request_scope,
        get_logger_only=get_observability_store,
        get_job_metrics_store=get_job_store,
        get_slo_store=get_observability_store,
        job_query_service=job_query_service,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
        dashboard_default_range=dashboard_default_range,
        dashboard_range_hours=dashboard_range_hours,
        dashboard_metrics_page_size=dashboard_metrics_page_size,
        dashboard_metrics_max_rows=dashboard_metrics_max_rows,
        dashboard_triage_limit=dashboard_triage_limit,
        dashboard_rollup_read_enabled=dashboard_rollup_read_enabled,
    )
    app.include_router(dashboard_metrics_router)

    api_contracts_v2_router = create_api_contracts_v2_router(
        resolve_user_role=resolve_user_role,
        request_scope=request_scope,
        job_query_service=job_query_service,
        job_command_service=job_command_service,
        approval_query_service=approval_query_service,
        project_query_service=project_query_service,
        require_authenticated_user=require_authenticated_user,
        local_auth_enabled=local_auth_enabled,
        external_auth_enabled=external_auth_enabled,
        auth_admin_roles=auth_admin_roles,
        err_admin_role_required=err_admin_role_required,
    )
    app.include_router(api_contracts_v2_router)

    api_search_router = create_api_search_router(
        search_service=lambda: build_global_search_service(get_logger_only=get_project_store),
        request_scope=request_scope,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
    )
    app.include_router(api_search_router)

    api_messages_router = create_api_messages_router(
        job_query_service=job_query_service,
        request_scope=request_scope,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
    )
    app.include_router(api_messages_router)

    api_projects_router = create_api_projects_router(
        job_query_service=job_query_service,
        job_command_service=job_command_service,
        project_query_service=project_query_service,
        project_command_service=project_command_service,
        request_scope=request_scope,
        require_authenticated_user=require_authenticated_user,
        local_auth_enabled=local_auth_enabled,
        external_auth_enabled=external_auth_enabled,
        auth_admin_roles=auth_admin_roles,
        resolve_actor_username=resolve_actor_username,
        project_create_input_cls=project_create_input_cls,
        project_update_input_cls=project_update_input_cls,
        service_error_cls=service_error_cls,
        service_not_found_error_cls=service_not_found_error_cls,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
        err_admin_role_required=err_admin_role_required,
        err_database_not_configured=err_database_not_configured,
        default_agent_models=default_agent_models,
        available_providers=available_providers,
        available_models=available_models,
        providers_with_models=providers_with_models,
        resolve_model_selection_context=resolve_model_selection_context,
    )
    app.include_router(api_projects_router)

    app.include_router(
        _build_extension_oauth_router(
            get_extension_oauth_store=get_extension_oauth_store,
            request_scope=request_scope,
            require_authenticated_user=require_authenticated_user,
            service_error_response=service_error_response,
            api_error_response=api_error_response,
        )
    )

    api_project_detail_router = create_api_project_detail_router(
        project_query_service=project_query_service,
        request_scope=request_scope,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
        logger=logger,
    )
    app.include_router(api_project_detail_router)

    api_model_providers_router = create_api_model_providers_router(
        model_provider_command_service=model_provider_command_service,
        request_scope=request_scope,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
    )
    app.include_router(api_model_providers_router)

    api_model_catalog_router = create_api_model_catalog_router(
        get_logger_only=get_schedule_store,
        request_scope=request_scope,
        require_authenticated_user=require_authenticated_user,
        resolve_actor_username=resolve_actor_username,
        auth_admin_roles=auth_admin_roles,
        api_error_response=api_error_response,
    )
    app.include_router(api_model_catalog_router)

    api_llm_provider_extensions_router = create_api_llm_provider_extensions_router(
        get_logger_only=get_identity_store,
        require_authenticated_user=require_authenticated_user,
        auth_admin_roles=auth_admin_roles,
        api_error_response=api_error_response,
    )
    app.include_router(api_llm_provider_extensions_router)

    api_external_agents_router = create_api_external_agents_router(
        external_agent_command_service=external_agent_command_service,
        request_principal=request_principal,
        request_scope=request_scope,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
    )
    app.include_router(api_external_agents_router)

    api_events_router = create_api_events_router(
        event_query_service=event_query_service,
        event_command_service=event_command_service,
        request_scope=request_scope,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
    )
    app.include_router(api_events_router)

    api_trash_router = create_api_trash_router(
        require_authenticated_user=require_authenticated_user,
        auth_admin_roles=auth_admin_roles,
        get_logger_only=get_worker_store,
        real_dict_cursor_kwargs=real_dict_cursor_kwargs,
        request_scope=request_scope,
        trash_lifecycle_service=trash_lifecycle_service,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
        err_admin_role_required=err_admin_role_required,
        err_database_not_configured=err_database_not_configured,
    )
    app.include_router(api_trash_router)

    api_system_monitoring_router = create_api_system_monitoring_router(
        local_auth_enabled=local_auth_enabled,
        external_auth_enabled=external_auth_enabled,
        require_authenticated_user=require_authenticated_user,
        auth_admin_roles=auth_admin_roles,
        list_extensions=lambda request: extension_query_service().list_extensions(
            scope=request_scope(request)
        ),
    )
    app.include_router(api_system_monitoring_router)
    app.include_router(processes_router)
