"""Normalize job-detail payloads and page durable execution history."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from core.application.jobs.workflow_run_models import WorkflowRunStateV1
from core.application.management.errors import ServiceError

_DETAIL_EXECUTION_STEP_PAGE_LIMIT = 50
_DETAIL_EXECUTION_STEP_PAGE_MAX = 100
_INVALID_EXECUTION_STEP_CURSOR = "Invalid execution-step cursor"


def _payload_dict(payload: Any) -> dict[str, Any]:
    if hasattr(payload, "dump"):
        dumped = payload.dump()
        if isinstance(dumped, dict):
            return dumped
    if isinstance(payload, dict):
        return dict(payload)
    return {}


def _workflow_run_payload(payload: Any) -> dict[str, Any] | None:
    if payload is None:
        return None
    if isinstance(payload, WorkflowRunStateV1):
        return payload.model_dump(mode="json")
    raw = _payload_dict(payload)
    if not raw:
        return None
    try:
        return WorkflowRunStateV1.model_validate(raw).model_dump(mode="json")
    except ValidationError:
        return None


def _payload_rows(payload: Any, key: str) -> list[dict[str, Any]]:
    data = _payload_dict(payload)
    rows = data.get(key)
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _payload_nested_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if isinstance(value, dict):
        return dict(value)
    return {}


def _execution_step_page_limit(value: int | None) -> int:
    try:
        requested = int(value or _DETAIL_EXECUTION_STEP_PAGE_LIMIT)
    except (TypeError, ValueError):
        requested = _DETAIL_EXECUTION_STEP_PAGE_LIMIT
    return max(1, min(requested, _DETAIL_EXECUTION_STEP_PAGE_MAX))


def _decode_execution_step_cursor(cursor: str, *, total_steps: int) -> tuple[int, str]:
    normalized = str(cursor or "").strip()
    if not normalized:
        return max(0, total_steps), ""
    before_text, separator, message_before_id = normalized.partition(":")
    try:
        before_index = int(before_text)
    except (TypeError, ValueError) as exc:
        raise ServiceError(_INVALID_EXECUTION_STEP_CURSOR, status_code=422) from exc
    if before_index < 0 or before_index > total_steps:
        raise ServiceError(_INVALID_EXECUTION_STEP_CURSOR, status_code=422)
    if separator and len(message_before_id) > 128:
        raise ServiceError(_INVALID_EXECUTION_STEP_CURSOR, status_code=422)
    return before_index, message_before_id if separator else ""


def _workflow_run_step_window(
    workflow_run: dict[str, Any] | None,
    *,
    limit: int,
    before_index: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if workflow_run is None:
        return None, {
            "durable_start": 0,
            "durable_end": 0,
            "total_durable_steps": 0,
        }
    normalized = dict(workflow_run)
    rows = normalized.get("steps")
    durable_steps = (
        [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    )
    total = len(durable_steps)
    page_end = total if before_index is None else max(0, min(int(before_index), total))
    page_start = max(0, page_end - limit)
    normalized["steps"] = durable_steps[page_start:page_end]
    return normalized, {
        "durable_start": page_start,
        "durable_end": page_end,
        "total_durable_steps": total,
    }


def _execution_step_page_meta(
    *,
    page_window: dict[str, Any],
    messages: list[dict[str, Any]],
    message_has_more: bool,
    count: int,
) -> dict[str, Any]:
    durable_start = max(0, int(page_window.get("durable_start") or 0))
    next_message_cursor = ""
    if message_has_more and messages:
        next_message_cursor = str(messages[0].get("id") or "").strip()
    has_more = durable_start > 0 or bool(next_message_cursor)
    return {
        "count": max(0, int(count or 0)),
        "total_durable_steps": max(0, int(page_window.get("total_durable_steps") or 0)),
        "has_more": has_more,
        "next_cursor": (f"{durable_start}:{next_message_cursor}" if has_more else ""),
    }


def _section_warning(
    *,
    section: str,
    code: str,
    message: str,
    error: Exception | None = None,
) -> dict[str, str]:
    warning = {
        "section": str(section or "").strip(),
        "code": str(code or "").strip(),
        "message": str(message or "").strip(),
    }
    if error is not None:
        warning["error_type"] = type(error).__name__
    return warning
