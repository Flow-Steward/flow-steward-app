"""Maintenance MCP mutation classification and middleware tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from mcp_types import CallToolResult

from core.entrypoints.mcp.server_maintenance import (
    McpMaintenanceMiddleware,
    mcp_tool_is_mutation,
)
from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES


async def _async_value(value: bool) -> bool:
    return value


def _call(
    middleware: McpMaintenanceMiddleware,
    *,
    method: str,
    tool: str = "",
) -> tuple[object, list[str]]:
    calls: list[str] = []

    async def call_next(_ctx: object) -> dict[str, object]:
        calls.append(tool or method)
        return {"ok": True}

    ctx = SimpleNamespace(method=method, params={"name": tool} if tool else {})
    return asyncio.run(middleware(ctx, call_next)), calls


def test_tools_list_and_read_tools_remain_available() -> None:
    middleware = McpMaintenanceMiddleware(maintenance_active=lambda _bearer: _async_value(True))

    listed, list_calls = _call(middleware, method="tools/list")
    read, read_calls = _call(middleware, method="tools/call", tool="project_list")

    assert listed == {"ok": True}
    assert read == {"ok": True}
    assert list_calls == ["tools/list"]
    assert read_calls == ["project_list"]


def test_mutation_tool_returns_stable_error_without_invoking_handler() -> None:
    middleware = McpMaintenanceMiddleware(maintenance_active=lambda _bearer: _async_value(True))

    result, calls = _call(middleware, method="tools/call", tool="job_cancel")

    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["error"]["code"] == "maintenance_mode_enabled"
    assert calls == []


def test_disabled_maintenance_allows_mutation_tools() -> None:
    result, calls = _call(
        McpMaintenanceMiddleware(maintenance_active=lambda _bearer: _async_value(False)),
        method="tools/call",
        tool="job_cancel",
    )

    assert result == {"ok": True}
    assert calls == ["job_cancel"]


def test_every_scoped_tool_has_an_explicit_read_or_mutation_classification() -> None:
    classified = {tool: mcp_tool_is_mutation(tool) for tool in MCP_TOOL_SCOPES}

    assert set(classified) == set(MCP_TOOL_SCOPES)
    assert classified["project_list"] is False
    assert classified["job_status"] is False
    assert classified["job_cancel"] is True
    assert classified["workflow_trigger"] is True
