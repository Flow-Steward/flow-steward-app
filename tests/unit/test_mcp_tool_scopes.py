from __future__ import annotations

import asyncio
from collections import defaultdict
from types import SimpleNamespace

import pytest

from core.application.auth.auth_http_runtime import (
    _HTTP_SAFE_METHODS,
    _is_project_extension_action_write,
    _is_project_extension_callback_write,
    _is_workflow_authoring_create,
    _required_http_scopes,
)
from core.entrypoints.mcp.server_api_client import MCP_TOOL_ROUTES
from core.entrypoints.mcp.server_discovery import McpToolDiscoveryMiddleware, visible_mcp_tool_names
from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES

_ROUTE_IDENTIFIERS = {
    "agent_id": "agent_1",
    "skill_id": "skill_1",
    "group_id": "group-1",
    "hook_id": "hook-1",
    "entry_key": "entry-1",
    "job_id": "job-1",
    "project_id": "project-1",
    "report_id": "report-1",
    "thread_id": "thread-1",
    "workflow_id": "workflow-1",
}


def test_scope_and_route_tables_cover_exact_public_inventory() -> None:
    assert len(MCP_TOOL_SCOPES) == 74
    assert set(MCP_TOOL_SCOPES) == set(MCP_TOOL_ROUTES)
    assert {
        "kb_search",
        "kb_write",
        "project_profile_get",
        "project_profile_update",
    }.isdisjoint(MCP_TOOL_SCOPES)


def test_all_tool_discovery_scopes_match_canonical_http_route_scopes() -> None:
    """A bearer advertised by MCP discovery must be accepted by the API route."""
    from core.application.auth.route_permissions import route_admin_only
    from core.infrastructure.runtime.http_app_auth_runtime import _is_registry_read_route

    mcp_scopes_by_route: dict[tuple[str, str], set[str]] = defaultdict(set)
    for tool, route in MCP_TOOL_ROUTES.items():
        resolved_path = route.path.format(**_ROUTE_IDENTIFIERS)
        mcp_scopes_by_route[(route.method, resolved_path)].update(MCP_TOOL_SCOPES[tool])

    for (method, path), mcp_scopes in mcp_scopes_by_route.items():
        # Match the real middleware route context; workflow registry writes
        # require admin even when they are not in the admin-only route list.
        registry_read = bool(_is_registry_read_route(path))
        http_scopes = set(
            _required_http_scopes(
                path,
                method,
                admin_required=bool(
                    route_admin_only(method, path)
                    or method == "DELETE"
                    or _is_workflow_authoring_create(path, method)
                ),
                project_callback_write=_is_project_extension_callback_write(path, method),
                project_action_write=_is_project_extension_action_write(path, method),
                registry_read=registry_read,
                registry_write=registry_read and method not in _HTTP_SAFE_METHODS,
            )
        )
        assert mcp_scopes == http_scopes, (method, path, mcp_scopes, http_scopes)


def test_discovery_is_request_local_presentation_not_authorization() -> None:
    read_tools = visible_mcp_tool_names(["read:*"], auth_enabled=True)
    write_tools = visible_mcp_tool_names(["command:*"], auth_enabled=True)

    assert "project_list" in read_tools
    assert "job_cancel" not in read_tools
    assert "job_cancel" in write_tools
    assert "project_list" not in write_tools
    assert visible_mcp_tool_names([], auth_enabled=True) == set()
    assert visible_mcp_tool_names([], auth_enabled=False) == set(MCP_TOOL_SCOPES)


def test_discovery_filters_the_official_typed_tools_list_result(monkeypatch) -> None:
    from mcp.types import ListToolsResult, Tool

    monkeypatch.setattr(
        "mcp.server.auth.middleware.auth_context.get_access_token",
        lambda: SimpleNamespace(scopes=["projects.read"]),
    )
    result = ListToolsResult(
        tools=[
            Tool(name="project_list", inputSchema={}),
            Tool(name="job_cancel", inputSchema={}),
        ]
    )

    filtered = asyncio.run(
        McpToolDiscoveryMiddleware(auth_enabled=True)(
            SimpleNamespace(method="tools/list"),
            lambda _ctx: _async_value(result),
        )
    )

    assert isinstance(filtered, ListToolsResult)
    assert [tool.name for tool in filtered.tools] == ["project_list"]


async def _async_value(value):
    return value


def test_mutating_tools_never_accept_read_only_scopes() -> None:
    for tool in (
        "job_cancel",
        "job_acknowledge",
        "job_restart",
        "job_unacknowledge",
        "job_approval_decide",
        "workflow_trigger",
        "workflow_import",
        "report_generate",
        "dispatch_project_notification",
        "agent_create",
        "agent_update",
        "agent_set_enabled",
        "agent_skill_assign",
        "agent_skill_unassign",
        "agent_skill_set_instructions",
        "skill_create",
        "skill_update",
        "skill_inputs_set",
        "skill_outputs_set",
        "skill_set_enabled",
        "workflow_set_enabled",
    ):
        assert "read:*" not in set(MCP_TOOL_SCOPES[tool]), tool


def test_aggregate_jobs_discovery_uses_existing_read_scopes():
    for name in ("job_list", "job_approvals_pending"):
        assert MCP_TOOL_SCOPES[name] == MCP_TOOL_SCOPES["job_result"]
        assert name in visible_mcp_tool_names(["jobs.read"], auth_enabled=True)
        assert name not in visible_mcp_tool_names(["jobs.write"], auth_enabled=True)


# --- ADR 0076 §6: MCP and HTTP give every system role the same answer -----------


def test_every_tool_names_the_permission_of_the_route_it_proxies() -> None:
    from core.application.auth.permissions import ALL_PERMISSION_KEYS
    from core.application.auth.route_permissions import route_permission
    from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_PERMISSIONS

    assert set(MCP_TOOL_PERMISSIONS) == set(MCP_TOOL_ROUTES)
    for tool, route in MCP_TOOL_ROUTES.items():
        match = route_permission(route.method, route.path.format(**_ROUTE_IDENTIFIERS))
        assert match is not None, tool
        assert MCP_TOOL_PERMISSIONS[tool] == match.permission, tool
        assert MCP_TOOL_PERMISSIONS[tool] in ALL_PERMISSION_KEYS


@pytest.mark.parametrize("role", ["admin", "operator", "viewer"])
@pytest.mark.parametrize("kind", ["session", "pat"])
def test_mcp_allows_a_system_role_exactly_what_http_allows(role, kind) -> None:
    """A tool is allowed when the role holds its permission; the API route it calls
    answers the same through the real middleware, in the browser and with the
    person's token the tool forwards."""
    from core.application.auth.effective_permissions import effective_permissions
    from core.application.auth.permissions import SYSTEM_ROLE_PERMISSIONS
    from core.entrypoints.mcp.server_tool_scopes import tool_allowed_for_permissions
    from tests.unit.test_route_permission_parity import _principal, middleware_decision

    person = _principal(kind, role)
    granted = effective_permissions(person, role_permissions=SYSTEM_ROLE_PERMISSIONS)
    differences = {}
    for tool, route in MCP_TOOL_ROUTES.items():
        path = route.path.format(**_ROUTE_IDENTIFIERS)
        through_mcp = tool_allowed_for_permissions(tool, granted.permissions)
        over_http, _state = middleware_decision(path, route.method, person, public=False)
        if through_mcp != over_http:
            differences[tool] = (through_mcp, over_http)

    assert differences == {}


def test_an_unknown_tool_needs_the_admins_permission() -> None:
    from core.application.auth.permissions import OPERATOR_PERMISSIONS
    from core.entrypoints.mcp.server_tool_scopes import (
        required_permission_for_tool,
        tool_allowed_for_permissions,
    )

    assert required_permission_for_tool("not_a_tool") == "platform.manage"
    assert not tool_allowed_for_permissions("not_a_tool", OPERATOR_PERMISSIONS)
