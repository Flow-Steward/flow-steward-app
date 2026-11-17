"""API-backed report and notification MCP tools."""

from __future__ import annotations

from core.entrypoints.mcp.server_api_tool import McpToolResult, api_tool_response
from core.entrypoints.mcp.server_runtime import mcp


@mcp.tool()
def report_generate(
    report_type: str,
    params: dict[str, object] | None = None,
    persist: bool = True,
) -> McpToolResult:
    """Generate a scoped report, optionally persisting the result.

    Operational types: activity_daily, project_summary, project_stats. They are
    three views of one dataset: the same payload and chart (KPIs, daily rows,
    data_as_of); only summary_text differs (activity_daily: daily activity,
    project_summary: summary with a next step, project_stats: flat counts).
    They count runs finished up to the moment of generation (payload.data_as_of);
    finished jobs are completed and failed runs, and cancelled runs are counted
    apart (kpis.cancelled_count). For these, params accepts project_id, exact
    workflow_id, range (default '7d'), or start_date/end_date (ISO dates).
    Omitted filters retain the caller's scope.
    Job types: job_summary, job_usage_breakdown (params.job_id required).
    core_health_summary accepts params.window_hours (default 24).
    """
    return api_tool_response("report_generate", locals())


@mcp.tool()
def report_get(report_id: str) -> McpToolResult:
    """Fetch one persisted report."""
    return api_tool_response("report_get", locals())


@mcp.tool()
def project_notification_routes(project_name: str, enabled_only: bool = False) -> McpToolResult:
    """List notification routes configured for one project."""
    return api_tool_response("project_notification_routes", locals())


@mcp.tool()
def project_notification_deliveries(
    project_name: str,
    job_id: str = "",
    limit: int = 200,
) -> McpToolResult:
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
) -> McpToolResult:
    """Dispatch a project notification event through the project's notification routes.

    Returns notification_deliveries (one per route that accepted the event) and
    routes_matched. When nothing was delivered, `reason` says why
    (no_enabled_route: the project has no enabled route; no_route_matched_event:
    the routes' event type, severity or workflow filters skipped it) and
    `message` names what to configure.
    """
    return api_tool_response("dispatch_project_notification", locals())


__all__ = [
    "dispatch_project_notification",
    "project_notification_deliveries",
    "project_notification_routes",
    "report_generate",
    "report_get",
]
