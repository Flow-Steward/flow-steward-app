"""API-backed hook binding and where-used MCP tools.

Each tool calls the project-scoped HTTP route of the shared workflow operator
command; nothing here validates or decides on its own.
"""

from __future__ import annotations

from typing import Any

from core.entrypoints.mcp.server_api_tool import api_tool_response
from core.entrypoints.mcp.server_runtime import mcp


@mcp.tool()
def workflow_hooks_list(project_name: str, workflow_id: str) -> str:
    """List the hooks a workflow declares and their bindings in the project."""
    return api_tool_response("workflow_hooks_list", locals())


@mcp.tool()
def workflow_hook_get(project_name: str, workflow_id: str, hook_id: str) -> str:
    """Show one hook's contract and its binding in the project."""
    return api_tool_response("workflow_hook_get", locals())


@mcp.tool()
def workflow_hook_preview(
    project_name: str,
    workflow_id: str,
    hook_id: str,
    expected_revision: int,
    target_workflow_id: str,
    input_mapping: dict[str, str] | None = None,
    output_mapping: dict[str, str] | None = None,
    hook_contract_version: int | None = None,
    hook_semantic_digest: str = "",
    release_id: str = "",
    sample: dict[str, Any] | None = None,
) -> str:
    """Check a hook binding without saving it; returns every issue a save would refuse.

    input_mapping maps child inputs to hook inputs; output_mapping maps child
    outputs to the hook's mutable or output-only fields. With sample
    ``{"inputs": {...}, "child_outputs": {...}}`` (for example the child's Test
    Run result) it also returns sample_output, the hook output a run would publish.
    """
    return api_tool_response("workflow_hook_preview", locals())


@mcp.tool()
def workflow_hook_bind(
    project_name: str,
    workflow_id: str,
    hook_id: str,
    expected_revision: int,
    target_workflow_id: str,
    input_mapping: dict[str, str] | None = None,
    output_mapping: dict[str, str] | None = None,
    hook_contract_version: int | None = None,
    hook_semantic_digest: str = "",
    release_id: str = "",
) -> str:
    """Connect a callable workflow to a hook in the project (the next binding revision).

    A stale expected_revision is a conflict to reload. With release_id the
    binding is prepared for that extension release and activates with it.
    """
    return api_tool_response("workflow_hook_bind", locals())


@mcp.tool()
def workflow_hook_unbind(
    project_name: str, workflow_id: str, hook_id: str, expected_revision: int
) -> str:
    """Disconnect the hook's workflow: new runs pass the hook inputs through."""
    return api_tool_response("workflow_hook_unbind", locals())


@mcp.tool()
def workflow_hook_create_and_bind(
    project_name: str,
    workflow_id: str,
    hook_id: str,
    expected_revision: int,
    child_workflow_id: str,
    display_name: str = "",
) -> str:
    """Create the empty workflow that holds a hook's steps and connect it once."""
    return api_tool_response("workflow_hook_create_and_bind", locals())


@mcp.tool()
def workflow_where_used(project_name: str, workflow_id: str, locale: str = "") -> str:
    """List the Invoke steps and hook bindings of the account that call the workflow."""
    return api_tool_response("workflow_where_used", locals())
