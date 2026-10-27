"""MCP server catalog routes: list, availability, icon, and install.

These live under ``/api/mcp/catalog`` next to ``/api/mcp/{connector_id}``, so
the router is included before the integrations router that owns the
connector-id routes (``tests/unit/test_static_catalog_routes.py`` pins it).

Every handler is a plain ``def``: the catalog fetch, the icon fetch and the
connector create (which resolves the endpoint host) are synchronous network
work that must not run on the event loop.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, Response

from core.application.management.errors import ServiceError, ServiceValidationError
from core.application.management.mcp_connector_common import accessible_project_ids
from core.application.management.mcp_server_catalog import (
    McpServerCatalogError,
    mcp_server_catalog_availability,
    mcp_server_catalog_icon,
)
from core.application.management.mcp_server_catalog_install import McpServerCatalogService


class McpCatalogInstallRequest(BaseModel):
    """One catalog endpoint to connect, pinned to the entry revision the form showed."""

    model_config = ConfigDict(extra="forbid")

    remote_id: str = Field(min_length=1, max_length=40)
    item_revision: int = Field(ge=1)
    display_name: str = Field(default="", max_length=200)
    variables: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    client_id: str = Field(default="", max_length=2000)
    client_secret: str = Field(default="", max_length=4000)
    enabled: bool = True
    # The OAuth settings the form showed, as the user left them; omitted keeps
    # the catalog's value.
    requested_scopes: list[str] | None = Field(default=None, max_length=50)
    authorization_parameters: dict[str, str] | None = None
    refresh_token_required: bool | None = None


def create_mcp_server_catalog_router(
    *,
    get_logger: Callable[[], Any],
    request_scope: Callable[..., Any],
    project_query_service: Callable[[], Any],
    service_error_response: Callable[..., Any],
    api_error_response: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(tags=["mcp"])

    def _project_scope(request: Request, project_id: str) -> Any:
        normalized = str(project_id or "").strip()
        try:
            uuid.UUID(normalized)
        except (TypeError, ValueError):
            raise ServiceValidationError("MCP connector project_id is invalid") from None
        scope = request_scope(request, project_id=normalized)
        project_query_service().get_project_detail(normalized, scope=scope)
        return scope

    def _project_scopes(request: Request, project_id: str) -> list[Any]:
        """One project's scope, or with no project_id one per project the caller may see."""
        if str(project_id or "").strip():
            return [_project_scope(request, project_id)]
        project_ids = accessible_project_ids(
            project_query_service().list_projects, scope=request_scope(request)
        )
        return [_project_scope(request, pid) for pid in project_ids]

    def _service() -> McpServerCatalogService:
        return McpServerCatalogService(get_logger=get_logger)

    def _catalog_error(exc: McpServerCatalogError) -> JSONResponse:
        return JSONResponse(exc.detail(), status_code=exc.status_code)

    @router.get("/api/mcp/catalog")
    def api_mcp_catalog(request: Request, project_id: str = ""):
        """The reviewed servers, each with the connectors made from it (in one project, or all)."""
        try:
            return _service().list_catalog(scopes=_project_scopes(request, project_id))
        except McpServerCatalogError as e:
            return _catalog_error(e)
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.get("/api/mcp/catalog/availability")
    def api_mcp_catalog_availability(request: Request):
        """Report catalog availability without failing when it is unusable."""
        del request
        return mcp_server_catalog_availability()

    @router.get("/api/mcp/catalog/{item_id}")
    def api_mcp_catalog_item(request: Request, item_id: str, project_id: str = ""):
        """One server's full entry: every endpoint, sign-in setting and connection."""
        try:
            return _service().get_item(item_id, scopes=_project_scopes(request, project_id))
        except McpServerCatalogError as e:
            return _catalog_error(e)
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.get("/api/mcp/catalog/{item_id}/icon")
    def api_mcp_catalog_icon(request: Request, item_id: str):
        """The entry's icon, verified against the index digest and served from here."""
        del request
        try:
            data = mcp_server_catalog_icon(item_id)
        except McpServerCatalogError as e:
            return _catalog_error(e)
        return Response(
            content=data,
            media_type="image/png",
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            },
        )

    @router.post("/api/mcp/catalog/{item_id}/install")
    def api_mcp_catalog_install(
        request: Request, item_id: str, project_id: str, body: McpCatalogInstallRequest
    ):
        """Create a connector from one catalog endpoint."""
        try:
            return _service().install(
                item_id,
                remote_id=body.remote_id,
                item_revision=body.item_revision,
                scope=_project_scope(request, project_id),
                display_name=body.display_name,
                variables=body.variables,
                headers=body.headers,
                client_id=body.client_id,
                client_secret=body.client_secret,
                enabled=body.enabled,
                requested_scopes=body.requested_scopes,
                authorization_parameters=body.authorization_parameters,
                refresh_token_required=body.refresh_token_required,
            )
        except McpServerCatalogError as e:
            return _catalog_error(e)
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    return router


__all__ = ["McpCatalogInstallRequest", "create_mcp_server_catalog_router"]
