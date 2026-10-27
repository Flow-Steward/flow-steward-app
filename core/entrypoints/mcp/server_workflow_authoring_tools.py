"""Nine authenticated API-backed workflow authoring tools."""

from __future__ import annotations

from core.entrypoints.mcp.server_api_tool import api_tool_response
from core.entrypoints.mcp.server_runtime import mcp


@mcp.tool()
def capability_search(
    project_name: str, query: str, kinds: list[str] | None = None, limit: int = 12
) -> str:
    """Search permitted local components and knowledge freshness before composing."""
    return api_tool_response("capability_search", locals())


@mcp.tool()
def capability_describe(project_name: str, ref: str, operation_id: str = "") -> str:
    """Inspect a live operation contract and permitted connection readiness."""
    return api_tool_response("capability_describe", locals())


@mcp.tool()
def workflow_examples_search(project_name: str, query: str, limit: int = 5) -> str:
    """Find local workflows to reuse; definitions follow existing export policy."""
    return api_tool_response("workflow_examples_search", locals())


@mcp.tool()
def workflow_authoring_schema(step_kind: str | None = None) -> str:
    """Return the existing YAML bundle sample and authoritative persisted step fields."""
    return api_tool_response("workflow_authoring_schema", locals())


@mcp.tool()
def workflow_draft_validate(
    project_name: str, bundle_yaml: str, resolutions: list[dict[str, object]] | None = None
) -> str:
    """Validate one draft without writes; execution and external effects remain unverified."""
    return api_tool_response("workflow_draft_validate", locals())


@mcp.tool()
def workflow_draft_save(
    project_name: str, bundle_yaml: str, resolutions: list[dict[str, object]] | None = None
) -> str:
    """Create one disabled draft with current creation authority; retries never overwrite."""
    return api_tool_response("workflow_draft_save", locals())


@mcp.tool()
def workflow_definition(workflow_id: str) -> str:
    """Read one workflow's current steps, groups, execution graph and revision.

    The revision is the expected_revision of the next workflow_apply_changes.
    """
    return api_tool_response("workflow_definition", locals())


@mcp.tool()
def workflow_apply_changes(
    workflow_id: str,
    expected_revision: int,
    operations: list[dict[str, object]],
    dry_run: bool = False,
) -> str:
    """Apply one atomic set of step, group and graph changes to a workflow.

    The set becomes one new revision or nothing is saved; a stale
    expected_revision is a conflict to reload, never an overwrite. With
    dry_run the set is judged exactly as Apply would judge it and nothing is
    saved: the result lists its issues and the ones that would refuse it.
    """
    return api_tool_response("workflow_apply_changes", locals())


@mcp.tool()
def workflow_convert_group(
    workflow_id: str,
    group_id: str,
    child_workflow_id: str,
    display_name: str,
    expected_revision: int,
    dry_run: bool = True,
    preview_digest: str = "",
) -> str:
    """Preview a typed child extraction; apply only with its current preview_digest.

    The default is read-only. Boundary issues prevent all writes. Apply creates
    the child and replaces the parent group atomically at expected_revision.
    """
    return api_tool_response("workflow_convert_group", locals())
