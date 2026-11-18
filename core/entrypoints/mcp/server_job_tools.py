"""API-backed job and approval MCP tools."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from core.entrypoints.mcp.server_api_tool import McpToolResult, api_tool_response
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
) -> McpToolResult:
    """List scoped jobs with pagination; group_by='attention' groups operator triage.

    status takes one status or a comma list (failed,stuck) and matches any of them;
    a status group key selects its whole group (stuck also lists orphaned runs,
    failed also lists max_iterations and triage_needed). Grouped attention pages
    too: has_more=true means the groups hold only this page of `total` runs.
    """
    return api_tool_response("job_list", locals())


@mcp.tool()
def job_approvals_pending(
    page: Annotated[int, Field(ge=1)] = 1,
    per_page: Annotated[int, Field(ge=1, le=200)] = 25,
) -> McpToolResult:
    """List jobs awaiting approval within the caller's current scope, one page at a time.

    `total` counts the whole scoped queue; has_more=true means later pages remain.
    """
    return api_tool_response("job_approvals_pending", locals())


@mcp.tool()
def job_result(
    job_id: str,
    include_messages_limit: Annotated[int, Field(ge=1, le=2000)] = 100,
    include_logs_limit: Annotated[int, Field(ge=1, le=2000)] = 50,
) -> McpToolResult:
    """Return a final or partial result bundle for one job.

    `completed` is true only for a successful run; `terminal` and `outcome`
    (completed, failed, cancelled, waiting, ...) tell how it ended or where it
    stands. Messages and logs are the newest window; `truncated` flags a list
    the limits cut.
    """
    return api_tool_response("job_result", locals())


@mcp.tool()
def job_status(job_id: str) -> McpToolResult:
    """Return a bounded polling summary for one job, with job_url, the run's page."""
    return api_tool_response("job_status", locals())


@mcp.tool()
def job_runtime_journal(job_id: str) -> McpToolResult:
    """Return runtime journal entries for one job."""
    return api_tool_response("job_runtime_journal", locals())


@mcp.tool()
def job_decision_overrides(
    job_id: str,
    active_only: bool = False,
    domain: str = "",
    limit: int = 200,
) -> McpToolResult:
    """List deterministic decision overrides recorded for a job."""
    return api_tool_response("job_decision_overrides", locals())


@mcp.tool()
def job_errors(job_id: str, limit: int = 200) -> McpToolResult:
    """List structured errors recorded for one job."""
    return api_tool_response("job_errors", locals())


@mcp.tool()
def job_trace(job_id: str, limit: int = 1000) -> McpToolResult:
    """Return the execution trace for one job."""
    return api_tool_response("job_trace", locals())


@mcp.tool()
def job_incident(job_id: str, limit: int = 100) -> McpToolResult:
    """Return incident drilldown data for one job."""
    return api_tool_response("job_incident", locals())


@mcp.tool()
def job_usage_breakdown(job_id: str) -> McpToolResult:
    """Return token usage breakdown for one job."""
    return api_tool_response("job_usage_breakdown", locals())


@mcp.tool()
def job_events(job_id: str, since_id: str = "", limit: int = 200) -> McpToolResult:
    """List domain events for one job."""
    return api_tool_response("job_events", locals())


@mcp.tool()
def job_messages(
    job_id: str,
    since_id: str = "",
    before_id: str = "",
    limit: int = 200,
) -> McpToolResult:
    """List agent and runtime messages for one job."""
    return api_tool_response("job_messages", locals())


@mcp.tool()
def job_logs(
    job_id: str,
    since_id: str = "",
    before_id: str = "",
    newest: bool = False,
    limit: int = 25,
) -> McpToolResult:
    """List log entries for one job."""
    return api_tool_response("job_logs", locals())


@mcp.tool()
def job_approval(job_id: str) -> McpToolResult:
    """Return the active approval gate payload for one job."""
    return api_tool_response("job_approval", locals())


@mcp.tool()
def job_approvals(job_id: str) -> McpToolResult:
    """List approval history for one job."""
    return api_tool_response("job_approvals", locals())


@mcp.tool()
def job_cancel(job_id: str, comment: str = "Cancelled via MCP") -> McpToolResult:
    """Cancel a pending, running or waiting job; the comment is kept as the reason."""
    return api_tool_response("job_cancel", locals())


@mcp.tool()
def job_acknowledge(job_id: str, actor: str = "") -> McpToolResult:
    """Mark a job as acknowledged for operator triage.

    The token's user is recorded as the actor; `actor` is journaled beside it.
    """
    return api_tool_response("job_acknowledge", locals())


@mcp.tool()
def job_unacknowledge(job_id: str) -> McpToolResult:
    """Remove a job acknowledgment."""
    return api_tool_response("job_unacknowledge", locals())


@mcp.tool()
def job_restart(job_id: str) -> McpToolResult:
    """Re-enqueue a finished job with the same inputs as a new run.

    A run that is still running, queued, or waiting (timer, child workflow,
    approval) is refused with a conflict; cancel it first.
    """
    return api_tool_response("job_restart", locals())


@mcp.tool()
def job_approval_decide(
    thread_id: str,
    approval_id: str,
    approved: bool,
    idempotency_key: str,
    comment: Annotated[
        str,
        Field(
            description=(
                "Reason for the decision. Required (non-empty) when approved is false, "
                "unless action is 'abort'; optional otherwise."
            )
        ),
    ] = "",
    action: Annotated[
        str,
        Field(
            description=(
                "Leave empty for a plain approve/reject decided by `approved`. "
                "'approve' or 'reject' must agree with `approved`. 'abort' (with "
                "approved false) stops the run as cancelled instead of following the "
                "rejected path; it needs no comment."
            )
        ),
    ] = "",
) -> McpToolResult:
    """Approve or reject a paused workflow thread.

    approved=true continues the run (an Approval step follows its approved path).
    approved=false rejects (an Approval step follows its rejected path; the
    pause's allowed_actions say what it offers). A reject requires a non-empty `comment`; a reject
    without one is refused with "comment is required when rejecting an approval".
    action='abort' with approved=false ends the run as cancelled and needs no
    comment. Take thread_id and approval_id from job_approval or
    job_approvals_pending; reuse the same idempotency_key to retry safely.
    """
    return api_tool_response("job_approval_decide", locals())


__all__ = [name for name in globals() if name.startswith("job_")]
