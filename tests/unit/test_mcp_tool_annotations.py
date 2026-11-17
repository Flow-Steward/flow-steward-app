"""Every MCP tool carries honest annotations, and tools/list publishes them.

Live in claude.ai (2026-11-17) no Flow Steward tool had annotations, so the client
could not tell a read from a destructive change.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.entrypoints.mcp.server_api_client import MCP_TOOL_ROUTES
from core.entrypoints.mcp.server_discovery import McpToolDiscoveryMiddleware
from core.entrypoints.mcp.server_tool_annotations import (
    ADDITIVE_TOOLS,
    DESTRUCTIVE_TOOLS,
    IDEMPOTENT_TOOLS,
    OPEN_WORLD_TOOLS,
    READ_ONLY_TOOLS,
    tool_annotations,
)
from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES

#: Reads that the API serves over POST because they take a document to check.
_POST_PREVIEWS = {"workflow_draft_validate", "workflow_hook_preview"}


def test_every_tool_is_classified_exactly_once() -> None:
    groups = (READ_ONLY_TOOLS, DESTRUCTIVE_TOOLS, ADDITIVE_TOOLS)
    for tool in MCP_TOOL_SCOPES:
        assert sum(tool in group for group in groups) == 1, tool
    assert set().union(*groups) == set(MCP_TOOL_SCOPES)


def test_write_only_hints_apply_to_writes() -> None:
    writes = DESTRUCTIVE_TOOLS | ADDITIVE_TOOLS
    assert writes >= IDEMPOTENT_TOOLS
    assert writes >= OPEN_WORLD_TOOLS


def test_read_only_matches_the_http_method_each_tool_uses() -> None:
    for tool, route in MCP_TOOL_ROUTES.items():
        if tool in _POST_PREVIEWS:
            assert tool in READ_ONLY_TOOLS
            continue
        assert (tool in READ_ONLY_TOOLS) == (route.method == "GET"), (tool, route.method)


def test_runs_and_approvals_are_destructive_open_world_writes() -> None:
    for tool in ("workflow_trigger", "job_restart", "job_approval_decide"):
        hints = tool_annotations(tool)
        assert hints["readOnlyHint"] is False
        assert hints["destructiveHint"] is True
        assert hints["openWorldHint"] is True


def test_unknown_tool_is_a_cautious_write() -> None:
    assert tool_annotations("not_a_tool") == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }


async def _async_value(value):
    return value


def test_tools_list_publishes_annotations_and_keeps_a_title(monkeypatch) -> None:
    from mcp.types import ListToolsResult, Tool, ToolAnnotations

    monkeypatch.setattr(
        "mcp.server.auth.middleware.auth_context.get_access_token",
        lambda: SimpleNamespace(scopes=["admin:all"]),
    )
    result = ListToolsResult(
        tools=[
            Tool(name="project_list", inputSchema={}),
            Tool(
                name="workflow_run_report",
                inputSchema={},
                annotations=ToolAnnotations(title="Workflow run report"),
            ),
            Tool(name="job_cancel", inputSchema={}),
        ]
    )
    listed = asyncio.run(
        McpToolDiscoveryMiddleware(auth_enabled=True)(
            SimpleNamespace(method="tools/list"), lambda _ctx: _async_value(result)
        )
    )
    wire = {
        tool.name: tool.model_dump(by_alias=True, exclude_none=True)["annotations"]
        for tool in listed.tools
    }
    assert wire["project_list"] == {"readOnlyHint": True, "openWorldHint": False}
    assert wire["workflow_run_report"]["readOnlyHint"] is True
    assert wire["workflow_run_report"]["title"] == "Workflow run report"
    assert wire["job_cancel"]["readOnlyHint"] is False
    assert wire["job_cancel"]["destructiveHint"] is True
