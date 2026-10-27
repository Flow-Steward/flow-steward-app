"""Project connections, extensions, MCP connectors, and workflow policy routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management import ServiceConflictError, ServiceError, ServiceValidationError
from core.application.management.command_models import (
    ProjectConnectionCreateInput,
    ProjectConnectionUpdateInput,
    ProjectPolicyUpdateInput,
    ProjectWorkflowBindingsInput,
)
from core.application.management.guards import require_project_access
from core.application.management.requests import (
    ExtensionProjectStatePutRequest,
    ProjectConnectionCreateRequest,
    ProjectConnectionOAuthStartRequest,
    ProjectConnectionSecretsSaveRequest,
    ProjectConnectionUpdateRequest,
    ProjectPolicyRequest,
    ProjectWorkflowSelectionRequest,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.api.integrations_extension_routes import register_extension_routes
from core.entrypoints.api.integrations_mcp_routes import register_mcp_routes
from core.entrypoints.api.integrations_request_helpers import (
    _MESSAGE_DATABASE_IS_NOT_CONFIGURED,
    _browser_connection_view,
    _oauth_redirect_url,
    _parse_bool_query,
    _require_extension_state_connection_owner,
    _valid_project_id,
)


def create_integrations_connectors_router(
    *,
    get_logger: Callable[[], Any],
    get_integration_store: Callable[[], Any] | None = None,
    get_extension_install_store: Callable[[], Any],
    get_extension_runtime_store: Callable[[], Any],
    extension_query_service: Callable[[], Any],
    extension_binding_service: Callable[[], Any] | None = None,
    request_scope: Callable[..., Any],
    project_query_service: Callable[[], Any],
    project_command_service: Callable[[], Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
    err_not_found: str,
) -> APIRouter:
    router = APIRouter(tags=["integrations-connectors"])
    integration_store = get_integration_store or get_logger

    def _extension_state_install_id(
        *, account_id: str, extension_id: str, connection_install_id: str = ""
    ) -> str:
        if connection_install_id:
            return connection_install_id
        store = get_extension_install_store()
        record = store.get(extension_id=extension_id, account_id=account_id)
        if record is None and account_id != DEFAULT_ACCOUNT_ID:
            candidate = store.get(extension_id=extension_id, account_id=DEFAULT_ACCOUNT_ID)
            if str(getattr(candidate, "visibility", "") or "") == "global":
                record = candidate
        install_id = str(getattr(record, "install_id", "") or "").strip()
        if not install_id:
            raise ServiceValidationError("extension is not installed for this account")
        return install_id

    @router.get("/api/projects/{project_id}/connections")
    async def api_project_connections_list(request: Request, project_id: str):
        try:
            if not _valid_project_id(project_id):
                return JSONResponse({"error": err_not_found}, status_code=404)
            scope = request_scope(request, project_id=project_id)
            bl = integration_store()
            project = bl.get_project(project_id) if bl else None
            if not project:
                return JSONResponse({"error": err_not_found}, status_code=404)
            require_project_access(scope, project)
            from core.application.management.command_services import ProjectConnectionService

            svc = ProjectConnectionService(get_logger=integration_store)
            connections = svc.list_connections(project_id, scope=scope)
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return {
            "connections": [_browser_connection_view(scope, c.dump()) for c in connections],
            "project_id": project_id,
        }

    @router.post("/api/projects/{project_id}/connections")
    async def api_project_connection_create(
        request: Request, project_id: str, body: ProjectConnectionCreateRequest
    ):
        try:
            if not _valid_project_id(project_id):
                return JSONResponse({"error": err_not_found}, status_code=404)
            scope = request_scope(request, project_id=project_id)
            bl = integration_store()
            project = bl.get_project(project_id) if bl else None
            if not project:
                return JSONResponse({"error": err_not_found}, status_code=404)
            require_project_access(scope, project)
            from core.application.management.command_services import ProjectConnectionService

            svc = ProjectConnectionService(get_logger=integration_store)
            payload = svc.create_connection(
                project_id,
                ProjectConnectionCreateInput(
                    extension_id=body.extension_id or None,
                    connection_kind=body.connection_kind,
                    display_name=body.display_name,
                    connection_type=body.connection_type,
                    environment=body.environment,
                    extension_install_id=body.extension_install_id,
                    runtime_mode=body.runtime_mode,
                    remote_base_url=body.remote_base_url,
                    enabled=body.enabled,
                    connection_config=body.connection_config,
                    secret_refs=body.secret_refs,
                    connection_routes=body.connection_routes,
                    ingress_config=body.ingress_config,
                    workflow_mappings=body.workflow_mappings,
                    tags=body.tags,
                ),
                scope=scope,
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    @router.get("/api/projects/{project_id}/connections/{connection_id}")
    async def api_project_connection_get(request: Request, project_id: str, connection_id: str):
        try:
            if not _valid_project_id(project_id):
                return JSONResponse({"error": err_not_found}, status_code=404)
            scope = request_scope(request, project_id=project_id)
            bl = integration_store()
            project = bl.get_project(project_id) if bl else None
            if not project:
                return JSONResponse({"error": err_not_found}, status_code=404)
            require_project_access(scope, project)
            from core.application.management.command_services import ProjectConnectionService

            svc = ProjectConnectionService(get_logger=integration_store)
            payload = svc.get_connection(
                connection_id,
                project_id=project_id,
                scope=scope,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return _browser_connection_view(scope, payload.dump())

    @router.put("/api/projects/{project_id}/connections/{connection_id}")
    async def api_project_connection_update(
        request: Request, project_id: str, connection_id: str, body: ProjectConnectionUpdateRequest
    ):
        try:
            if not _valid_project_id(project_id):
                return JSONResponse({"error": err_not_found}, status_code=404)
            scope = request_scope(request, project_id=project_id)
            bl = integration_store()
            project = bl.get_project(project_id) if bl else None
            if not project:
                return JSONResponse({"error": err_not_found}, status_code=404)
            require_project_access(scope, project)
            from core.application.management.command_services import ProjectConnectionService

            svc = ProjectConnectionService(get_logger=integration_store)
            payload = svc.update_connection(
                connection_id,
                ProjectConnectionUpdateInput(
                    enabled=body.enabled,
                    display_name=body.display_name,
                    connection_type=body.connection_type,
                    environment=body.environment,
                    extension_install_id=body.extension_install_id,
                    runtime_mode=body.runtime_mode,
                    remote_base_url=body.remote_base_url,
                    health_status=body.health_status,
                    runtime_status=body.runtime_status,
                    last_seen_ok_at=body.last_seen_ok_at,
                    last_error=body.last_error,
                    connection_config=body.connection_config,
                    secret_refs=body.secret_refs,
                    connection_routes=body.connection_routes,
                    ingress_config=body.ingress_config,
                    workflow_mappings=body.workflow_mappings,
                    tags=body.tags,
                ),
                project_id=project_id,
                scope=scope,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/projects/{project_id}/connections/{connection_id}/secrets")
    async def api_project_connection_save_secrets(
        request: Request,
        project_id: str,
        connection_id: str,
        body: ProjectConnectionSecretsSaveRequest,
    ):
        try:
            if not _valid_project_id(project_id):
                return JSONResponse({"error": err_not_found}, status_code=404)
            scope = request_scope(request, project_id=project_id)
            bl = integration_store()
            project = bl.get_project(project_id) if bl else None
            if not project:
                return JSONResponse({"error": err_not_found}, status_code=404)
            require_project_access(scope, project)
            from core.application.management.command_services import ProjectConnectionService

            svc = ProjectConnectionService(get_logger=integration_store)
            payload = svc.save_connection_secrets(
                connection_id,
                dict(body.secrets or {}),
                project_id=project_id,
                scope=scope,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/projects/{project_id}/connections/{connection_id}/oauth/start")
    async def api_project_connection_oauth_start(
        request: Request,
        project_id: str,
        connection_id: str,
        body: ProjectConnectionOAuthStartRequest,
    ):
        try:
            if not _valid_project_id(project_id):
                return JSONResponse({"error": err_not_found}, status_code=404)
            scope = request_scope(request, project_id=project_id)
            bl = integration_store()
            project = bl.get_project(project_id) if bl else None
            if not project:
                return JSONResponse({"error": err_not_found}, status_code=404)
            require_project_access(scope, project)
            from core.application.auth.oauth_client import (
                oauth_auth_config_for_connection,
                oauth_callback_url,
                start_oauth_authorization,
            )
            from core.application.management.command_services import ProjectConnectionService

            svc = ProjectConnectionService(get_logger=integration_store)
            connection = svc.get_connection(
                connection_id,
                project_id=project_id,
                scope=scope,
            ).dump()
            auth_config = oauth_auth_config_for_connection(bl, connection)
            if not auth_config:
                raise ServiceValidationError(
                    "Connection type does not declare OAuth2 authorization"
                )
            payload = start_oauth_authorization(
                bl,
                connection=connection,
                auth_config=auth_config,
                redirect_uri=oauth_callback_url(),
                optional_scopes=list(body.optional_scopes or []),
                return_path=body.return_path,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            if e.__class__.__name__ == "OAuthConnectionError":
                return service_error_response(ServiceValidationError(str(e)))
            return api_error_response(e)
        return payload

    @router.get("/api/projects/{project_id}/connections/{connection_id}/oauth/status")
    async def api_project_connection_oauth_status(
        request: Request,
        project_id: str,
        connection_id: str,
    ):
        try:
            if not _valid_project_id(project_id):
                return JSONResponse({"error": err_not_found}, status_code=404)
            scope = request_scope(request, project_id=project_id)
            bl = integration_store()
            project = bl.get_project(project_id) if bl else None
            if not project:
                return JSONResponse({"error": err_not_found}, status_code=404)
            require_project_access(scope, project)
            from core.application.auth.oauth_client import oauth_connection_status
            from core.application.management.command_services import ProjectConnectionService

            svc = ProjectConnectionService(get_logger=integration_store)
            connection = svc.get_connection(
                connection_id,
                project_id=project_id,
                scope=scope,
            ).dump()
            payload = oauth_connection_status(bl, connection=connection)
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            if e.__class__.__name__ == "OAuthConnectionError":
                return service_error_response(ServiceValidationError(str(e)))
            return api_error_response(e)
        return payload

    @router.post("/api/projects/{project_id}/connections/{connection_id}/oauth/disconnect")
    async def api_project_connection_oauth_disconnect(
        request: Request,
        project_id: str,
        connection_id: str,
    ):
        try:
            if not _valid_project_id(project_id):
                return JSONResponse({"error": err_not_found}, status_code=404)
            scope = request_scope(request, project_id=project_id)
            bl = integration_store()
            project = bl.get_project(project_id) if bl else None
            if not project:
                return JSONResponse({"error": err_not_found}, status_code=404)
            require_project_access(scope, project)
            from core.application.management.command_services import ProjectConnectionService

            connection = (
                ProjectConnectionService(get_logger=integration_store)
                .get_connection(
                    connection_id,
                    project_id=project_id,
                    scope=scope,
                )
                .dump()
            )
            configuration_id = str(connection.get("oauth_configuration_id") or "").strip()
            if configuration_id:
                from core.application.extensions.oauth_connection_lifecycle import (
                    build_database_oauth_connection_lifecycle_service,
                )

                payload = build_database_oauth_connection_lifecycle_service(bl).disconnect(
                    scope,
                    str(connection.get("extension_install_id") or ""),
                    str(connection.get("oauth_provider_id") or ""),
                    configuration_id,
                    connection_id,
                )
            else:
                from core.application.auth.oauth_client import disconnect_connection_oauth

                payload = disconnect_connection_oauth(bl, connection=connection)
        except ServiceError as exc:
            return service_error_response(exc, request)
        except Exception as exc:
            if exc.__class__.__name__ == "OAuthConnectionError":
                return service_error_response(ServiceValidationError(str(exc)), request)
            return api_error_response(exc, request=request)
        return payload

    @router.get("/api/projects/{project_id}/extension-state/{extension_id}")
    async def api_project_extension_state_list(
        request: Request,
        project_id: str,
        extension_id: str,
    ):
        try:
            if not _valid_project_id(project_id):
                return JSONResponse({"error": err_not_found}, status_code=404)
            scope = request_scope(request, project_id=project_id)
            bl = integration_store()
            project = bl.get_project(project_id) if bl else None
            if not project:
                return JSONResponse({"error": err_not_found}, status_code=404)
            require_project_access(scope, project)
            from core.application.extensions.extension_project_state import list_state

            namespace = str(request.query_params.get("namespace") or "").strip() or None
            connection_id = str(request.query_params.get("connection_id") or "").strip() or None
            account_id = str(project.get("account_id") or scope.account_id or "").strip()
            connection_id, connection_install_id = _require_extension_state_connection_owner(
                bl,
                project_id=project_id,
                account_id=account_id,
                extension_id=extension_id,
                connection_id=connection_id,
            )
            extension_install_id = _extension_state_install_id(
                account_id=account_id,
                extension_id=extension_id,
                connection_install_id=connection_install_id,
            )
            include_all_connections = _parse_bool_query(
                request.query_params.get("include_all_connections"),
                field_name="include_all_connections",
                default=False,
            )
            try:
                limit = int(request.query_params.get("limit") or 200)
            except (TypeError, ValueError) as exc:
                raise ServiceValidationError("Invalid integer for 'limit'") from exc
            rows = list_state(
                bl,
                account_id=account_id,
                project_id=project_id,
                extension_id=extension_id,
                extension_install_id=extension_install_id,
                namespace=namespace,
                connection_id=connection_id,
                include_all_connections=include_all_connections,
                limit=limit,
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except ValueError as e:
            return service_error_response(ServiceValidationError(str(e)), request)
        except Exception as e:
            return api_error_response(e, request=request)
        return {"project_id": project_id, "extension_id": extension_id, "state": rows}

    @router.put(
        "/api/projects/{project_id}/extension-state/{extension_id}/{namespace}/{state_key:path}"
    )
    async def api_project_extension_state_put(
        request: Request,
        project_id: str,
        extension_id: str,
        namespace: str,
        state_key: str,
        body: ExtensionProjectStatePutRequest,
    ):
        try:
            if not _valid_project_id(project_id):
                return JSONResponse({"error": err_not_found}, status_code=404)
            scope = request_scope(request, project_id=project_id)
            bl = integration_store()
            project = bl.get_project(project_id) if bl else None
            if not project:
                return JSONResponse({"error": err_not_found}, status_code=404)
            require_project_access(scope, project)
            from core.application.extensions.extension_project_state import (
                ExtensionProjectStateRevisionConflict,
                put_state,
            )

            account_id = str(project.get("account_id") or scope.account_id or "").strip()
            connection_id, connection_install_id = _require_extension_state_connection_owner(
                bl,
                project_id=project_id,
                account_id=account_id,
                extension_id=extension_id,
                connection_id=body.connection_id,
            )
            extension_install_id = _extension_state_install_id(
                account_id=account_id,
                extension_id=extension_id,
                connection_install_id=connection_install_id,
            )
            try:
                row = put_state(
                    bl,
                    account_id=account_id,
                    project_id=project_id,
                    extension_id=extension_id,
                    extension_install_id=extension_install_id,
                    namespace=namespace,
                    state_key=state_key,
                    value_jsonb=dict(body.value_jsonb or {}),
                    connection_id=connection_id,
                    expected_revision=body.expected_revision,
                    expires_at=body.expires_at,
                    actor_id=str(getattr(scope, "actor_id", "") or "").strip() or None,
                )
            except ExtensionProjectStateRevisionConflict as exc:
                raise ServiceConflictError(str(exc)) from exc
        except ServiceError as e:
            return service_error_response(e, request)
        except ValueError as e:
            return service_error_response(ServiceValidationError(str(e)), request)
        except Exception as e:
            return api_error_response(e, request=request)
        return row

    @router.delete(
        "/api/projects/{project_id}/extension-state/{extension_id}/{namespace}/{state_key:path}"
    )
    async def api_project_extension_state_delete(
        request: Request,
        project_id: str,
        extension_id: str,
        namespace: str,
        state_key: str,
    ):
        try:
            if not _valid_project_id(project_id):
                return JSONResponse({"error": err_not_found}, status_code=404)
            scope = request_scope(request, project_id=project_id)
            bl = integration_store()
            project = bl.get_project(project_id) if bl else None
            if not project:
                return JSONResponse({"error": err_not_found}, status_code=404)
            require_project_access(scope, project)
            from core.application.extensions.extension_project_state import (
                ExtensionProjectStateRevisionConflict,
                delete_state,
            )

            connection_id = str(request.query_params.get("connection_id") or "").strip() or None
            account_id = str(project.get("account_id") or scope.account_id or "").strip()
            connection_id, connection_install_id = _require_extension_state_connection_owner(
                bl,
                project_id=project_id,
                account_id=account_id,
                extension_id=extension_id,
                connection_id=connection_id,
            )
            extension_install_id = _extension_state_install_id(
                account_id=account_id,
                extension_id=extension_id,
                connection_install_id=connection_install_id,
            )
            raw_revision = request.query_params.get("expected_revision")
            expected_revision = None
            if raw_revision not in (None, ""):
                try:
                    expected_revision = int(raw_revision or 0)
                except (TypeError, ValueError) as exc:
                    raise ServiceValidationError("Invalid integer for 'expected_revision'") from exc
            try:
                deleted = delete_state(
                    bl,
                    account_id=account_id,
                    project_id=project_id,
                    extension_id=extension_id,
                    extension_install_id=extension_install_id,
                    namespace=namespace,
                    state_key=state_key,
                    connection_id=connection_id,
                    expected_revision=expected_revision,
                )
            except ExtensionProjectStateRevisionConflict as exc:
                raise ServiceConflictError(str(exc)) from exc
        except ServiceError as e:
            return service_error_response(e, request)
        except ValueError as e:
            return service_error_response(ServiceValidationError(str(e)), request)
        except Exception as e:
            return api_error_response(e, request=request)
        return {"deleted": deleted, "project_id": project_id, "extension_id": extension_id}

    @router.get("/api/integrations/oauth/callback")
    def api_project_connection_oauth_callback(request: Request):
        from core.application.auth.oauth_client import (
            OAuthConnectionError,
            handle_oauth_callback,
            handle_oauth_provider_error,
            oauth_callback_context,
            oauth_provider_error_reason,
            oauth_state_type,
            sanitize_oauth_return_path,
        )

        default_return_path = "/settings/integrations"
        safe_return_path = default_return_path
        state = str(request.query_params.get("state") or "").strip()
        code = str(request.query_params.get("code") or "").strip()
        provider_error = str(request.query_params.get("error") or "").strip()
        bl = integration_store()
        try:
            if not bl:
                raise OAuthConnectionError(_MESSAGE_DATABASE_IS_NOT_CONFIGURED)
            if oauth_state_type(state) == "mcp_connector":
                from core.application.auth.mcp_connector_oauth import (
                    handle_mcp_oauth_callback,
                    handle_mcp_oauth_provider_error,
                    mcp_oauth_callback_context,
                )

                callback_context = mcp_oauth_callback_context(
                    bl,
                    state=state,
                    fallback_return_path=default_return_path,
                )
                safe_return_path = str(callback_context.get("return_path") or default_return_path)
                result = (
                    handle_mcp_oauth_provider_error(
                        bl,
                        state=state,
                        fallback_return_path=default_return_path,
                    )
                    if provider_error
                    else handle_mcp_oauth_callback(bl, state=state, code=code)
                )
                sync_outcome = ""
                if not provider_error:
                    from core.application.management.mcp_connector_services import (
                        McpConnectorCommandService,
                    )

                    callback_project_id = str(callback_context.get("project_id") or "").strip()
                    try:
                        McpConnectorCommandService(get_logger=get_logger).sync_connector(
                            str(callback_context.get("connector_id") or "").strip(),
                            scope=ScopeContext(
                                account_id=str(callback_context.get("account_id") or "").strip(),
                                project_id=callback_project_id,
                                tenant_id=str(callback_context.get("tenant_id") or "").strip(),
                                actor_id="mcp_oauth_callback",
                                principal_type="system_actor",
                                auth_source="oauth_callback",
                                allowed_project_ids=frozenset({callback_project_id}),
                            ),
                        )
                        sync_outcome = "success"
                    except Exception:
                        sync_outcome = "failed"
                return_path = sanitize_oauth_return_path(
                    str(result.get("return_path") or ""),
                    fallback=default_return_path,
                )
                return RedirectResponse(
                    _oauth_redirect_url(
                        return_path,
                        outcome="error" if provider_error else "success",
                        reason="oauth_failed" if provider_error else "",
                        sync_outcome=sync_outcome,
                    ),
                    status_code=303,
                )
            callback_context = oauth_callback_context(
                bl,
                state=state,
                fallback_return_path=default_return_path,
            )
            safe_return_path = str(callback_context.get("return_path") or default_return_path)
            result = (
                handle_oauth_provider_error(bl, state=state, provider_error=provider_error)
                if provider_error
                else handle_oauth_callback(bl, state=state, code=code)
            )
            callback_result = {
                "outcome": "error" if provider_error else "success",
                "reason": oauth_provider_error_reason(provider_error) if provider_error else "",
                "return_path": sanitize_oauth_return_path(
                    str(result.get("return_path") or ""),
                    fallback=default_return_path,
                ),
            }
            return RedirectResponse(
                _oauth_redirect_url(
                    str(callback_result["return_path"]),
                    outcome=str(callback_result["outcome"]),
                    reason=str(callback_result["reason"]),
                ),
                status_code=303,
            )
        except Exception:
            return RedirectResponse(
                _oauth_redirect_url(safe_return_path, outcome="error", reason="oauth_failed"),
                status_code=303,
            )

    @router.delete("/api/projects/{project_id}/connections/{connection_id}")
    async def api_project_connection_delete(request: Request, project_id: str, connection_id: str):
        try:
            if not _valid_project_id(project_id):
                return JSONResponse({"error": err_not_found}, status_code=404)
            scope = request_scope(request, project_id=project_id)
            bl = integration_store()
            project = bl.get_project(project_id) if bl else None
            if not project:
                return JSONResponse({"error": err_not_found}, status_code=404)
            require_project_access(scope, project)
            from core.application.management.command_services import ProjectConnectionService

            svc = ProjectConnectionService(get_logger=integration_store)
            result = svc.delete_connection(
                connection_id,
                project_id=project_id,
                scope=scope,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return result

    register_extension_routes(
        api_error_response=api_error_response,
        extension_binding_service=extension_binding_service,
        extension_query_service=extension_query_service,
        get_extension_install_store=get_extension_install_store,
        get_extension_runtime_store=get_extension_runtime_store,
        get_logger=get_logger,
        request_scope=request_scope,
        router=router,
        service_error_response=service_error_response,
    )

    register_mcp_routes(
        api_error_response=api_error_response,
        err_not_found=err_not_found,
        get_logger=get_logger,
        project_query_service=project_query_service,
        request_scope=request_scope,
        router=router,
        service_error_response=service_error_response,
    )

    @router.get("/api/projects/{project_id}/workflows")
    async def api_project_workflows(request: Request, project_id: str):
        try:
            payload = project_query_service().get_workflow_bindings(
                project_id,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.put("/api/projects/{project_id}/workflows")
    async def api_update_project_workflows(
        request: Request, project_id: str, body: ProjectWorkflowSelectionRequest
    ):
        try:
            payload = project_command_service().update_project_workflows(
                project_id,
                ProjectWorkflowBindingsInput(
                    workflow_ids=list(body.workflow_ids or []),
                    operator_default_workflow_id=body.operator_default_workflow_id,
                ),
                scope=request_scope(
                    request,
                    project_id=project_id,
                    workflow_id=body.operator_default_workflow_id or None,
                ),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/projects/{project_id}/policy")
    async def api_project_policy(request: Request, project_id: str):
        try:
            payload = project_query_service().get_policy(
                project_id,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.put("/api/projects/{project_id}/policy")
    async def api_update_project_policy(
        request: Request, project_id: str, body: ProjectPolicyRequest
    ):
        try:
            payload = project_command_service().update_project_policy(
                project_id,
                ProjectPolicyUpdateInput(
                    autonomy_mode=body.autonomy_mode,
                    autonomy_confidence_threshold=body.autonomy_confidence_threshold,
                    retry_strategy=body.retry_strategy,
                    retry_max_attempts=body.retry_max_attempts,
                    retry_backoff_initial_seconds=body.retry_backoff_initial_seconds,
                    retry_backoff_max_seconds=body.retry_backoff_max_seconds,
                    concurrency_cap=body.concurrency_cap,
                    tool_policy_mode=body.tool_policy_mode,
                ),
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    return router
