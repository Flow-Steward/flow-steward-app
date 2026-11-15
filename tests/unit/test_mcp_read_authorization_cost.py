"""MCP reads authorize projects without hydrating their unrelated job history."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.management.mcp_connector_services import McpConnectorQueryService
from core.application.management.mcp_server_catalog_install import McpServerCatalogService
from core.application.management.query_models import McpConnectorListView
from core.application.management.query_services import ProjectQueryService
from core.application.management.scope import ScopeContext
from core.entrypoints.api.integrations_mcp_routes import register_mcp_routes
from core.entrypoints.api.mcp_server_catalog import create_mcp_server_catalog_router

_PROJECT_ID = "10000000-0000-0000-0000-000000000001"


def _response(exc, *_args, **_kwargs):
    return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "status_code", 500))


def _app(monkeypatch, access):
    project = {
        "id": _PROJECT_ID,
        "account_id": "account-a",
        "workspace_id": "workspace-a",
    }
    if access == "foreign_account":
        project["account_id"] = "account-b"
    elif access == "foreign_workspace":
        project["workspace_id"] = "workspace-b"
    store = SimpleNamespace(
        get_project=lambda _project_id: None if access == "missing" else project
    )

    def unrelated_job_reader():
        raise AssertionError("MCP authorization must not read the project's job history")

    queries = ProjectQueryService(
        get_logger=lambda: None if access == "database_missing" else store,
        get_job_store=unrelated_job_reader,
        serialize_projects_grid_row=lambda row: row,
        get_queue_depth=lambda *_args: 0,
        get_ready_queue_depth=lambda: 0,
    )
    # Membership enumeration has its own service coverage; the real per-project
    # account, workspace and allowed-project guards remain in this request path.
    monkeypatch.setattr(
        queries,
        "list_projects",
        lambda **_kwargs: SimpleNamespace(
            projects=[SimpleNamespace(id=_PROJECT_ID)], total_pages=1
        ),
    )

    def request_scope(_request, project_id=None):
        return ScopeContext(
            account_id="account-a",
            tenant_id="tenant-a",
            project_id=project_id,
            principal_type="operator",
            allowed_workspace_ids=frozenset({"workspace-a"}),
            allowed_project_ids=frozenset(
                {"other-project"} if access == "forbidden_project" else {_PROJECT_ID}
            ),
        )

    monkeypatch.setattr(
        McpConnectorQueryService,
        "list_connectors",
        lambda *_args, **_kwargs: McpConnectorListView(connectors=[], total=0),
    )
    monkeypatch.setattr(
        McpServerCatalogService, "list_catalog", lambda *_args, **_kwargs: {"items": []}
    )
    monkeypatch.setattr(
        McpServerCatalogService, "get_item", lambda *_args, **_kwargs: {"item_id": "demo"}
    )
    bindings = {
        "api_error_response": _response,
        "get_logger": lambda: store,
        "project_query_service": lambda: queries,
        "request_scope": request_scope,
        "service_error_response": _response,
    }
    router = APIRouter()
    register_mcp_routes(router=router, err_not_found="Not found", **bindings)
    app = FastAPI()
    # Static catalog paths precede the connector-id catch-all, as in the app.
    app.include_router(create_mcp_server_catalog_router(**bindings))
    app.include_router(router)
    return app


@pytest.mark.parametrize("path", ["/api/mcp", "/api/mcp/catalog", "/api/mcp/catalog/demo"])
@pytest.mark.parametrize("single_project", [True, False])
@pytest.mark.parametrize(
    "access,status",
    [
        ("allowed", 200),
        ("foreign_account", 403),
        ("foreign_workspace", 403),
        ("forbidden_project", 403),
        ("missing", 404),
        ("database_missing", 503),
    ],
)
def test_mcp_read_needs_project_authorization_but_not_job_history(
    monkeypatch, path, single_project, access, status
):
    with TestClient(_app(monkeypatch, access)) as client:
        response = client.get(path, params={"project_id": _PROJECT_ID} if single_project else {})
    assert response.status_code == status, response.text
    if status == 200:
        expected = (
            {"connectors": [], "total": 0}
            if path == "/api/mcp"
            else {"items": []}
            if path == "/api/mcp/catalog"
            else {"item_id": "demo"}
        )
        assert response.json() == expected
