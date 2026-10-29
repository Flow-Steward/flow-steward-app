"""API-backed workflow export, import and stats MCP tools.

Each tool calls the project-scoped HTTP route of the shared workflow operator
command; nothing here validates or decides on its own.
"""

from __future__ import annotations

from typing import Any

from core.entrypoints.mcp.server_api_tool import api_tool_response
from core.entrypoints.mcp.server_runtime import mcp


@mcp.tool()
def workflow_export(
    project_name: str,
    workflow_id: str,
    include_children: bool = False,
    reference_only: bool = False,
) -> str:
    """Export a workflow linked to the project as a YAML bundle (returned as bundle_yaml text).

    include_children also exports the workflows it invokes. The bundle carries the
    definitions of its agents, skills, data contracts and guardrails unless
    reference_only names them only. The bundle is what workflow_import takes.
    """
    return api_tool_response("workflow_export", locals())


@mcp.tool()
def workflow_import(
    project_name: str,
    bundle_yaml: str,
    target_workflow_id: str = "",
    resolutions: list[dict[str, Any]] | None = None,
) -> str:
    """Import a YAML bundle into the project as disabled drafts.

    target_workflow_id imports it under another id; resolutions choose how each
    dependency the bundle names is mapped (as the import preflight lists them).
    """
    return api_tool_response("workflow_import", locals())


@mcp.tool()
def workflow_stats(project_name: str, workflow_id: str) -> str:
    """Show the run statistics of a workflow linked to the project."""
    return api_tool_response("workflow_stats", locals())
