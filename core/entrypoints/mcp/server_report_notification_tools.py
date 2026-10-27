"""API-backed report and notification MCP tools."""

from __future__ import annotations

from core.entrypoints.mcp.server_api_tool import api_tool_response
from core.entrypoints.mcp.server_runtime import mcp


@mcp.tool()
def report_generate(
    report_type: str,
    params: dict[str, object] | None = None,
    persist: bool = True,
) -> str:
    """Generate a scoped report, optionally persisting the result.

    Operational types: activity_daily, project_summary, project_stats. For these,
    params accepts project_id, exact workflow_id, range (default '7d'), or
    start_date/end_date (ISO dates). Omitted filters retain the caller's scope.
    Job types: job_summary, job_cost_breakdown (params.job_id required).
    core_health_summary accepts params.window_hours (default 24).
    """
    return api_tool_response("report_generate", locals())


@mcp.tool()
def report_get(report_id: str) -> str:
    """Fetch one persisted report."""
    return api_tool_response("report_get", locals())


@mcp.tool()
def project_notification_routes(project_name: str, enabled_only: bool = False) -> str:
    """List notification routes configured for one project."""
    return api_tool_response("project_notification_routes", locals())


@mcp.tool()
def project_notification_deliveries(
    project_name: str,
    job_id: str = "",
    limit: int = 200,
) -> str:
    """List notification delivery records for one project."""
    return api_tool_response("project_notification_deliveries", locals())


@mcp.tool()
def dispatch_project_notification(
    project_name: str,
    event_type: str,
    severity: str = "info",
    workflow_id: str = "",
    job_id: str = "",
    payload: dict[str, object] | None = None,
) -> str:
    """Dispatch a project notification event."""
    return api_tool_response("dispatch_project_notification", locals())


__all__ = [
    "dispatch_project_notification",
    "project_notification_deliveries",
    "project_notification_routes",
    "report_generate",
    "report_get",
]
