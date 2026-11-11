"""An agent can see a project's connections over MCP, as the step editor does."""

from __future__ import annotations

import inspect

from core.application.auth.auth_http_runtime import _required_http_scopes
from core.entrypoints.mcp.server_api_client import MCP_TOOL_ROUTES
from core.entrypoints.mcp.server_discovery import visible_mcp_tool_names
from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES


def test_project_connections_is_registered_routed_and_read_scoped() -> None:
    from core.entrypoints.mcp.server import mcp

    tool = mcp._tool_manager._tools["project_connections"]
    assert 'api_tool_response("project_connections", locals())' in inspect.getsource(tool.fn)
    assert "connection_binding_jsonb.connection_id" in str(tool.description)
    route = MCP_TOOL_ROUTES["project_connections"]
    assert (route.method, route.path) == ("GET", "/api/projects/{project_id}/connections")
    assert "projects.read" in MCP_TOOL_SCOPES["project_connections"]


def test_a_projects_read_token_sees_the_tool_and_passes_its_http_route() -> None:
    assert "project_connections" in visible_mcp_tool_names(["projects.read"], auth_enabled=True)
    http_scopes = _required_http_scopes(
        "/api/projects/proj_1/connections",
        "GET",
        admin_required=False,
        project_callback_write=False,
        registry_read=False,
        registry_write=False,
    )
    assert "projects.read" in http_scopes
