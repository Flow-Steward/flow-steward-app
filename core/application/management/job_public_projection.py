"""Bounded job summary projection for public CLI and MCP polling surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.jobs.job_polling import FOR_EACH_PROGRESS_FIELDS

_TOP_LEVEL_FIELDS = (
    "id",
    "status",
    "project_id",
    "thread_id",
    "task_summary",
    "mode",
    "selected_workflow",
    "selected_workflow_display_name",
    "selected_workflow_source",
    "workflow_selection_reason",
    "started_at",
    "finished_at",
    "duration_ms",
    "current_step_id",
    "current_group_id",
    "current_agent",
)
_RUNTIME_FIELDS = (
    "current_step_id",
    "current_group_id",
    "active_agent",
    "current_node",
    "heartbeat_at",
    "last_progress_at",
    "progress_percent",
    "queue_name",
    "worker_id",
)
_COST_FIELDS = (
    "tokens_prompt",
    "tokens_completion",
    "tokens_total",
    "cost_usd",
    "known_cost_usd",
    "unknown_cli_tokens",
    "cost_knownness",
    "llm_calls",
)
# These must be the keys build_job_failure_summary actually produces. They were
# not: the list selected code/message/reason/phase/agent, none of which the
# summary emits, so the only overlapping key was `retryable` — and _select drops
# falsy values, so a non-retryable failure projected to {} and MCP job_status
# reported a failed job with no reason at all, while REST showed the cause.
_FAILURE_FIELDS = (
    "present",
    "kind",
    "status",
    "title",
    "summary",
    "detail",
    "severity",
    "retryable",
    "source",
)
_TEXT_LIMIT = 2_000


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "dump", None)
    if callable(dump):
        payload = dump()
        return dict(payload) if isinstance(payload, Mapping) else {}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="json")
        return dict(payload) if isinstance(payload, Mapping) else {}
    return {}


def _bounded_scalar(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _TEXT_LIMIT:
        return f"{value[:_TEXT_LIMIT]}…"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _select(payload: object, fields: tuple[str, ...]) -> dict[str, Any]:
    source = _mapping(payload)
    return {
        field: _bounded_scalar(source[field])
        for field in fields
        if field in source and source[field] not in (None, "", [], {})
    }


_ACTIVE_STEP_FIELDS = (
    "step_id",
    "step_execution_id",
    "attempt",
    "group_id",
    "for_each_id",
    "lane_id",
    "iteration",
    "status",
)
_ACTIVE_STEP_LIMIT = 64


def _active_steps(value: object) -> list[dict[str, Any]]:
    """In-flight step executions with fixed scalar fields, bounded in count."""
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value[:_ACTIVE_STEP_LIMIT]:
        row = _mapping(item)
        if row:
            projected = {field: _bounded_scalar(row.get(field)) for field in _ACTIVE_STEP_FIELDS}
            progress = _mapping(row.get("for_each_progress"))
            if progress:
                projected["for_each_progress"] = {
                    field: _bounded_scalar(progress.get(field))
                    for field in FOR_EACH_PROGRESS_FIELDS
                }
            rows.append(projected)
    return rows


def job_public_summary_projection(status: object) -> dict[str, Any]:
    """Return polling-safe state while directing bulk data to bounded subresources."""
    source = _mapping(status)
    projected = _select(source, _TOP_LEVEL_FIELDS)
    projected["active_steps"] = _active_steps(source.get("active_steps"))
    projected["runtime"] = _select(source.get("runtime"), _RUNTIME_FIELDS)
    projected["cost"] = _select(source.get("cost"), _COST_FIELDS)
    projected["failure_summary"] = _select(source.get("failure_summary"), _FAILURE_FIELDS)
    projected["detail_subresources"] = {
        "logs": "/api/jobs/{job_id}/logs",
        "messages": "/api/jobs/{job_id}/messages",
        "errors": "/api/jobs/{job_id}/errors",
        "events": "/api/jobs/{job_id}/events",
        "diagnostics": "/api/jobs/{job_id}/incident",
    }
    return projected
