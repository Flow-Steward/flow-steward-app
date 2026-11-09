"""API-backed project, workflow, and task MCP tools."""

from __future__ import annotations

from core.entrypoints.mcp.server_api_tool import api_tool_response
from core.entrypoints.mcp.server_runtime import mcp


@mcp.tool()
def project_list() -> str:
    """List projects visible to the caller."""
    return api_tool_response("project_list", locals())


@mcp.tool()
def project_jobs(project_name: str, limit: int = 10, page: int = 1, status: str = "") -> str:
    """List a project's jobs you may see, newest first, one page of `limit` (at most 50).

    status takes one status or a comma list; a status group key (failed, stuck,
    waiting, ...) selects its whole group. `total` and `total_pages` tell whether
    more pages exist.
    """
    return api_tool_response("project_jobs", locals())


@mcp.tool()
def project_connections(project_name: str) -> str:
    """List the connections a project's steps can use, without secrets.

    Each row has `connection_id`, `display_name`, `extension_id`, `connection_type`
    and its health. A connector step names its connection by this id in
    `connection_binding_jsonb.connection_id`; a connection that was deleted and
    created again has a new id, so move the steps to the new one.
    """
    return api_tool_response("project_connections", locals())


@mcp.tool()
def workflow_list(
    project_name: str,
    enabled_only: bool = True,
    include_validation: bool = False,
) -> str:
    """List workflows linked to a project."""
    return api_tool_response("workflow_list", locals())


@mcp.tool()
def workflow_validate(workflow_id: str, project_name: str = "") -> str:
    """Validate workflow readiness and contract issues of the stored definition.

    Static only: `execution_verified` is always false and `unperformed_checks`
    lists what it did not prove. A valid result is not a successful run.
    """
    return api_tool_response("workflow_validate", locals())


@mcp.tool()
def workflow_contract(workflow_id: str, project_name: str) -> str:
    """Return the normalized public workflow contract."""
    return api_tool_response("workflow_contract", locals())


@mcp.tool()
def workflow_suggestions(workflow_id: str, project_name: str = "") -> str:
    """Return repair suggestions for one workflow."""
    return api_tool_response("workflow_suggestions", locals())


@mcp.tool()
def workflow_trigger(
    project_name: str,
    workflow_id: str,
    payload: dict[str, object] | None = None,
    client_context: dict[str, object] | None = None,
    test_mode: bool = False,
    files: list[dict[str, object]] | None = None,
) -> str:
    """Start the selected workflow in a concrete project.

    Discover workflows, read workflow_contract, and ask for missing required inputs
    before calling. For file inputs, send files with input_name, filename, mime_type,
    and content_base64 (strict base64; 10 files, 1 MiB decoded total).
    Never send server paths, URLs or invented artifact IDs as files.
    Pass declared inputs in payload. Returns job_id for status and approval polling.
    """
    return api_tool_response("workflow_trigger", locals())


__all__ = [
    "project_connections",
    "project_jobs",
    "project_list",
    "workflow_contract",
    "workflow_list",
    "workflow_suggestions",
    "workflow_trigger",
    "workflow_validate",
]
