"""Register mcp routes on the shared API router."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.management import ServiceError, ServiceValidationError
from core.application.management.command_models import (
    McpConnectorCreateInput,
    McpConnectorUpdateInput,
)
from core.application.management.requests import (
    McpConnectorCreateRequest,
    McpConnectorDeleteConfirmRequest,
    McpConnectorOAuthCredentialsRequest,
    McpConnectorOAuthStartRequest,
    McpConnectorUpdateRequest,
)
from core.entrypoints.api.integrations_request_helpers import (
    _MESSAGE_DATABASE_IS_NOT_CONFIGURED,
    _valid_project_id,
)


def register_mcp_routes(
    *,
    api_error_response: Callable[..., JSONResponse],
    err_not_found: str,
    get_logger: Callable[[], Any],
    project_query_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    router: APIRouter,
    service_error_response: Callable[..., JSONResponse],
) -> None:
    def _mcp_project_scope(request: Request, project_id: str) -> Any:
        normalized_project_id = str(project_id or "").strip()
        if not _valid_project_id(normalized_project_id):
            raise ServiceValidationError("MCP connector project_id is invalid")
        scope = request_scope(request, project_id=normalized_project_id)
        project_query_service().get_project_detail(normalized_project_id, scope=scope)
        return scope

    @router.get("/api/mcp")
    async def api_mcp_connectors_list(request: Request, project_id: str = ""):
        """One project's connectors, or with no project_id every project the caller may see."""
        try:
            from core.application.management.mcp_connector_common import accessible_project_ids
            from core.application.management.mcp_connector_services import McpConnectorQueryService

            svc = McpConnectorQueryService(get_logger=get_logger)
            if str(project_id or "").strip():
                payload = svc.list_connectors(scope=_mcp_project_scope(request, project_id))
            else:
                project_ids = accessible_project_ids(
                    project_query_service().list_projects, scope=request_scope(request)
                )
                payload = svc.list_connectors_in_projects(
                    scopes=[_mcp_project_scope(request, pid) for pid in project_ids]
                )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/mcp")
    async def api_mcp_connector_create(
        request: Request, project_id: str, body: McpConnectorCreateRequest
    ):
        try:
            from core.application.management.mcp_connector_services import (
                McpConnectorCommandService,
            )

            svc = McpConnectorCommandService(get_logger=get_logger)
            payload = svc.create_connector(
                McpConnectorCreateInput(
                    display_name=body.display_name,
                    description=body.description,
                    transport=body.transport,
                    base_url=body.base_url,
                    auth_mode=body.auth_mode,
                    headers=body.headers,
                    oauth_config=body.oauth_config,
                    allowlisted_tools=body.allowlisted_tools,
                    namespace_prefix=body.namespace_prefix,
                    enabled=body.enabled,
                ),
                scope=_mcp_project_scope(request, project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/mcp/{connector_id}")
    async def api_mcp_connector_get(request: Request, connector_id: str, project_id: str):
        try:
            from core.application.management.mcp_connector_services import McpConnectorQueryService

            svc = McpConnectorQueryService(get_logger=get_logger)
            payload = svc.get_connector(connector_id, scope=_mcp_project_scope(request, project_id))
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.put("/api/mcp/{connector_id}")
    async def api_mcp_connector_update(
        request: Request, connector_id: str, project_id: str, body: McpConnectorUpdateRequest
    ):
        try:
            from core.application.management.mcp_connector_services import (
                McpConnectorCommandService,
            )

            svc = McpConnectorCommandService(get_logger=get_logger)
            payload = svc.update_connector(
                connector_id,
                McpConnectorUpdateInput(
                    display_name=body.display_name,
                    description=body.description,
                    transport=body.transport,
                    base_url=body.base_url,
                    auth_mode=body.auth_mode,
                    headers=body.headers,
                    headers_mode=body.headers_mode,
                    oauth_config=body.oauth_config,
                    allowlisted_tools=body.allowlisted_tools,
                    namespace_prefix=body.namespace_prefix,
                    enabled=body.enabled,
                ),
                scope=_mcp_project_scope(request, project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.delete("/api/mcp/{connector_id}")
    async def api_mcp_connector_delete(
        request: Request,
        connector_id: str,
        project_id: str,
        revision: str = "",
    ):
        try:
            from core.application.management.mcp_connector_services import (
                McpConnectorCommandService,
            )

            svc = McpConnectorCommandService(get_logger=get_logger)
            result = svc.delete_connector(
                connector_id,
                plan_revision=revision,
                scope=_mcp_project_scope(request, project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return result

    @router.get("/api/mcp/{connector_id}/delete-plan")
    async def api_mcp_connector_delete_plan(request: Request, connector_id: str, project_id: str):
        try:
            from core.application.management.mcp_connector_services import (
                McpConnectorCommandService,
            )

            svc = McpConnectorCommandService(get_logger=get_logger)
            return svc.preview_delete_connector(
                connector_id,
                scope=_mcp_project_scope(request, project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.post("/api/mcp/{connector_id}/delete")
    async def api_mcp_connector_delete_confirm(
        request: Request,
        connector_id: str,
        body: McpConnectorDeleteConfirmRequest,
        project_id: str,
    ):
        try:
            from core.application.management.mcp_connector_services import (
                McpConnectorCommandService,
            )

            svc = McpConnectorCommandService(get_logger=get_logger)
            return svc.delete_connector(
                connector_id,
                plan_revision=body.revision,
                scope=_mcp_project_scope(request, project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.post("/api/mcp/{connector_id}/health")
    async def api_mcp_connector_health(request: Request, connector_id: str, project_id: str):
        try:
            from core.application.management.mcp_connector_services import (
                McpConnectorCommandService,
            )

            svc = McpConnectorCommandService(get_logger=get_logger)
            payload = svc.check_health(connector_id, scope=_mcp_project_scope(request, project_id))
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/mcp/{connector_id}/sync")
    async def api_mcp_connector_sync(request: Request, connector_id: str, project_id: str):
        try:
            from core.application.management.mcp_connector_services import (
                McpConnectorCommandService,
            )

            svc = McpConnectorCommandService(get_logger=get_logger)
            payload = svc.sync_connector(
                connector_id, scope=_mcp_project_scope(request, project_id)
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/mcp/{connector_id}/oauth/start")
    async def api_mcp_connector_oauth_start(
        request: Request,
        connector_id: str,
        project_id: str,
        body: McpConnectorOAuthStartRequest,
    ):
        try:
            from core.application.auth.mcp_connector_oauth import start_mcp_connector_oauth
            from core.application.management.mcp_connector_common import (
                _require_connector_mutation_access,
            )
            from core.application.management.mcp_connector_services import McpConnectorStore

            scope = _mcp_project_scope(request, project_id)
            _require_connector_mutation_access(scope)
            account_id = str(scope.account_id or "").strip()
            project_id = str(scope.project_id or "").strip()
            tenant_id = str(scope.tenant_id or "").strip()
            bl = get_logger()
            if not bl:
                raise ServiceValidationError(_MESSAGE_DATABASE_IS_NOT_CONFIGURED)
            connector = McpConnectorStore().get_connector_row(
                bl,
                connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )
            if not connector:
                return JSONResponse({"error": err_not_found}, status_code=404)
            payload = start_mcp_connector_oauth(
                bl,
                connector_row=connector,
                return_path=body.return_path,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            if getattr(e, "reason_code", "") == "manual_credentials_required":
                return service_error_response(
                    ServiceValidationError(
                        str(e),
                        payload={
                            "code": "manual_credentials_required",
                            "message": str(e),
                        },
                    )
                )
            if e.__class__.__name__ == "OAuthConnectionError":
                return service_error_response(ServiceValidationError(str(e)))
            return api_error_response(e)
        return payload

    @router.post("/api/mcp/{connector_id}/oauth/credentials")
    async def api_mcp_connector_oauth_credentials(
        request: Request,
        connector_id: str,
        project_id: str,
        body: McpConnectorOAuthCredentialsRequest,
    ):
        try:
            from core.application.auth.mcp_connector_oauth import (
                save_mcp_connector_oauth_credentials,
            )
            from core.application.management.mcp_connector_common import (
                _require_connector_mutation_access,
            )
            from core.application.management.mcp_connector_services import McpConnectorStore

            scope = _mcp_project_scope(request, project_id)
            _require_connector_mutation_access(scope)
            bl = get_logger()
            if not bl:
                raise ServiceValidationError(_MESSAGE_DATABASE_IS_NOT_CONFIGURED)
            connector = McpConnectorStore().get_connector_row(
                bl,
                connector_id,
                account_id=str(scope.account_id or "").strip(),
                project_id=project_id,
                tenant_id=str(scope.tenant_id or "").strip(),
            )
            if not connector:
                return JSONResponse({"error": err_not_found}, status_code=404)
            return save_mcp_connector_oauth_credentials(
                bl,
                connector_row=connector,
                client_id=body.client_id,
                client_secret=body.client_secret,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            if e.__class__.__name__ == "OAuthConnectionError":
                return service_error_response(ServiceValidationError(str(e)))
            return api_error_response(e)

    @router.get("/api/mcp/{connector_id}/oauth/status")
    async def api_mcp_connector_oauth_status(request: Request, connector_id: str, project_id: str):
        try:
            from core.application.auth.mcp_connector_oauth import mcp_connector_oauth_status
            from core.application.management.mcp_connector_services import McpConnectorStore

            scope = _mcp_project_scope(request, project_id)
            account_id = str(scope.account_id or "").strip()
            tenant_id = str(scope.tenant_id or "").strip()
            bl = get_logger()
            if not bl:
                raise ServiceValidationError(_MESSAGE_DATABASE_IS_NOT_CONFIGURED)
            connector = McpConnectorStore().get_connector_row(
                bl,
                connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )
            if not connector:
                return JSONResponse({"error": err_not_found}, status_code=404)
            payload = mcp_connector_oauth_status(bl, connector_row=connector)
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            if e.__class__.__name__ == "OAuthConnectionError":
                return service_error_response(ServiceValidationError(str(e)))
            return api_error_response(e)
        return payload

    @router.post("/api/mcp/{connector_id}/oauth/disconnect")
    async def api_mcp_connector_oauth_disconnect(
        request: Request, connector_id: str, project_id: str
    ):
        try:
            from core.application.auth.mcp_connector_oauth import revoke_mcp_connector_oauth
            from core.application.management.mcp_connector_common import (
                _require_connector_mutation_access,
            )
            from core.application.management.mcp_connector_services import McpConnectorStore

            scope = _mcp_project_scope(request, project_id)
            _require_connector_mutation_access(scope)
            account_id = str(scope.account_id or "").strip()
            tenant_id = str(scope.tenant_id or "").strip()
            bl = get_logger()
            if not bl:
                raise ServiceValidationError(_MESSAGE_DATABASE_IS_NOT_CONFIGURED)
            connector = McpConnectorStore().get_connector_row(
                bl,
                connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )
            if not connector:
                return JSONResponse({"error": err_not_found}, status_code=404)
            revoke_mcp_connector_oauth(bl, connector_row=connector)
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            if e.__class__.__name__ == "OAuthConnectionError":
                return service_error_response(ServiceValidationError(str(e)))
            return api_error_response(e)
        return {"status": "disconnected", "connector_id": connector_id}

    @router.get("/api/mcp/oauth/client-metadata/{connector_id}.json")
    async def api_mcp_connector_oauth_client_metadata(connector_id: str):
        try:
            from core.application.auth.mcp_connector_oauth import mcp_client_metadata_document

            return mcp_client_metadata_document(connector_id=connector_id)
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            if e.__class__.__name__ == "OAuthConnectionError":
                return service_error_response(ServiceValidationError(str(e)))
            return api_error_response(e)
