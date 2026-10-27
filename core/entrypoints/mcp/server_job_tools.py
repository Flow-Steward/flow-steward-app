"""API-backed job and approval MCP tools."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from core.entrypoints.mcp.server_api_tool import api_tool_response
from core.entrypoints.mcp.server_runtime import mcp


@mcp.tool()
def job_list(
    project_id: str = "",
    workflow_id: str = "",
    status: str = "",
    group_by: str = "",
    search: str = "",
    page: int = 1,
    per_page: int = 25,
) -> str:
    """List scoped jobs with pagination; group_by='attention' groups operator triage.

    status takes one status or a comma list (failed,stuck) and matches any of them.
    """
    return api_tool_response("job_list", locals())


@mcp.tool()
def job_approvals_pending() -> str:
    """List jobs awaiting approval within the caller's current scope."""
    return api_tool_response("job_approvals_pending", locals())


@mcp.tool()
def job_result(
    job_id: str,
    include_messages_limit: Annotated[int, Field(ge=1, le=2000)] = 100,
    include_logs_limit: Annotated[int, Field(ge=1, le=2000)] = 50,
) -> str:
    """Return a final or partial result bundle for one job."""
    return api_tool_response("job_result", locals())


@mcp.tool(structured_output=False)
def job_status(job_id: str) -> str:
    """Return a bounded polling summary for one job."""
    return api_tool_response("job_status", locals())


@mcp.tool()
def job_runtime_journal(job_id: str) -> str:
    """Return runtime journal entries for one job."""
    return api_tool_response("job_runtime_journal", locals())


@mcp.tool()
def job_decision_overrides(
    job_id: str,
    active_only: bool = False,
    domain: str = "",
    limit: int = 200,
) -> str:
    """List deterministic decision overrides recorded for a job."""
    return api_tool_response("job_decision_overrides", locals())


@mcp.tool()
def job_errors(job_id: str, limit: int = 200) -> str:
    """List structured errors recorded for one job."""
    return api_tool_response("job_errors", locals())


@mcp.tool()
def job_trace(job_id: str, limit: int = 1000) -> str:
    """Return the execution trace for one job."""
    return api_tool_response("job_trace", locals())


@mcp.tool()
def job_incident(job_id: str, limit: int = 100) -> str:
    """Return incident drilldown data for one job."""
    return api_tool_response("job_incident", locals())


@mcp.tool()
def job_cost_breakdown(job_id: str) -> str:
    """Return token and cost breakdown for one job."""
    return api_tool_response("job_cost_breakdown", locals())


@mcp.tool()
def job_events(job_id: str, since_id: str = "", limit: int = 200) -> str:
    """List domain events for one job."""
    return api_tool_response("job_events", locals())


@mcp.tool()
def job_messages(
    job_id: str,
    since_id: str = "",
    before_id: str = "",
    limit: int = 200,
) -> str:
    """List agent and runtime messages for one job."""
    return api_tool_response("job_messages", locals())


@mcp.tool()
def job_logs(
    job_id: str,
    since_id: str = "",
    before_id: str = "",
    newest: bool = False,
    limit: int = 25,
) -> str:
    """List log entries for one job."""
    return api_tool_response("job_logs", locals())


@mcp.tool()
def job_approval(job_id: str) -> str:
    """Return the active approval gate payload for one job."""
    return api_tool_response("job_approval", locals())


@mcp.tool()
def job_approvals(job_id: str) -> str:
    """List approval history for one job."""
    return api_tool_response("job_approvals", locals())


@mcp.tool()
def job_cancel(job_id: str, comment: str = "Cancelled via MCP") -> str:
    """Cancel a pending or running job."""
    return api_tool_response("job_cancel", locals())


@mcp.tool()
def job_acknowledge(job_id: str, actor: str = "") -> str:
    """Mark a job as acknowledged for operator triage."""
    return api_tool_response("job_acknowledge", locals())


@mcp.tool()
def job_unacknowledge(job_id: str) -> str:
    """Remove a job acknowledgment."""
    return api_tool_response("job_unacknowledge", locals())


@mcp.tool()
def job_restart(job_id: str) -> str:
    """Re-enqueue a failed or cancelled job with the same inputs."""
    return api_tool_response("job_restart", locals())


@mcp.tool()
def job_approval_decide(
    thread_id: str,
    approval_id: str,
    approved: bool,
    idempotency_key: str,
    comment: str = "",
    action: str = "",
) -> str:
    """Approve or reject a paused workflow thread."""
    return api_tool_response("job_approval_decide", locals())


__all__ = [name for name in globals() if name.startswith("job_")]
