"""Bounded status projection for repeated machine polling of an authorized job."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.management.query_models import JobStatusView

_MAX_ACTIVE_STEPS = 32
_MAX_TEXT_LENGTH = 512
_STATUS_FIELDS = (
    "id",
    "status",
    "project_id",
    "thread_id",
    "task_summary",
    "selected_workflow",
    "started_at",
    "finished_at",
    "duration_ms",
    "current_step_id",
    "current_group_id",
    "current_agent",
)
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
#: Where the innermost For each is: positions and bounded counts, never its items.
FOR_EACH_PROGRESS_FIELDS = (
    "for_each_id",
    "item_index",
    "selected_count",
    "source_count",
    "applied_limit",
    "processed_count",
    "truncated",
)
_WAIT_FIELDS = ("kind", "wait_id", "step_id", "group_id", "due_at", "child_job_id", "approval_id")


def _bounded_fields(payload: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields:
        value = payload.get(field)
        if isinstance(value, str):
            result[field] = value[:_MAX_TEXT_LENGTH]
        elif value is None or isinstance(value, (bool, int, float)):
            result[field] = value
    return result


def _active_step(step: Mapping[str, Any]) -> dict[str, Any]:
    row = _bounded_fields(step, _ACTIVE_STEP_FIELDS)
    progress = step.get("for_each_progress")
    if isinstance(progress, Mapping):
        row["for_each_progress"] = _bounded_fields(progress, FOR_EACH_PROGRESS_FIELDS)
    return row


def build_job_polling_payload(status: JobStatusView) -> dict[str, Any]:
    """Keep progress and cost; histories, inputs and drilldowns use their own tools."""
    fields = {field: getattr(status, field) for field in _STATUS_FIELDS}
    result = _bounded_fields(fields, _STATUS_FIELDS)
    result["truncated_fields"] = [
        field
        for field, value in fields.items()
        if isinstance(value, str) and len(value) > _MAX_TEXT_LENGTH
    ]
    active_steps = status.active_steps
    result["active_steps"] = [_active_step(step) for step in active_steps[:_MAX_ACTIVE_STEPS]]
    result["active_steps_total"] = len(active_steps)
    result["active_steps_truncated"] = len(active_steps) > _MAX_ACTIVE_STEPS
    wait = status.runtime.get("workflow_wait")
    result["workflow_wait"] = (
        _bounded_fields(wait, _WAIT_FIELDS) if isinstance(wait, Mapping) else None
    )
    result["cost"] = status.cost.dump()
    return result
