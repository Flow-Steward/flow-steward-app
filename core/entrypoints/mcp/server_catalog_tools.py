"""Catalog discovery and pinned extension lifecycle through the application API."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from core.entrypoints.mcp.server_api_tool import McpToolResult, api_tool_response
from core.entrypoints.mcp.server_runtime import mcp


@mcp.tool()
def catalog_search(
    query: Annotated[str, Field(min_length=1, max_length=200)],
    kind: Literal["all", "extension", "mcp_server", "workflow_template"] = "all",
    limit: Annotated[int, Field(ge=1, le=25)] = 12,
) -> McpToolResult:
    """Find published extensions, MCP servers, and workflow templates before choosing a project.

    Search with product names or a few task keywords. Results include published evidence,
    setup requirements and links into this installation.
    Check catalog availability before concluding there are no matches. For working operation
    contracts or connection readiness in a project, follow up with capability_search or
    capability_describe after setup.
    """
    return api_tool_response("catalog_search", locals())


@mcp.tool()
def extension_catalog_install(
    extension_id: Annotated[str, Field(min_length=1, max_length=200)],
    version: Annotated[str, Field(min_length=1, max_length=64)],
    release_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
) -> McpToolResult:
    """Install or update the exact catalog release after the user asks to apply it.

    Use the ID, version and digest returned by catalog_search. If review is required,
    open the extension's catalog page for the administrator. Enter credentials and
    configure project connections in Flow Steward.
    """
    return api_tool_response("extension_catalog_install", locals(), timeout_seconds=310)
