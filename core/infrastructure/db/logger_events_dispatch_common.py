from __future__ import annotations

from core.application.orchestration.models import build_event_envelope_v1
from core.infrastructure.db import psycopg_runtime as psycopg_runtime

EVENT_TYPE_S_OR_EVENT_TYPE_LIKE_S_SQL = "(event_type = %s OR event_type LIKE %s)"
EVENT_TYPE_S_SQL = "event_type = %s"
OCCURRED_AT_S_VALUE = "occurred_at < %s"
OCCURRED_AT_S_VALUE_2 = "occurred_at >= %s"

SQL_AND = " AND "
SQL_STATUS_EQ = "status = %s"
SQL_LAST_ERROR_EQ = "last_error = %s"
SQL_ACCOUNT_ID_EQ = "account_id = %s"
SQL_PROJECT_ID_EQ = "project_id = %s"
SQL_WORKFLOW_ID_EQ = "workflow_id = %s"
SQL_JOB_ID_EQ = "job_id = %s"
SQL_EVENT_ID_EQ = "event_id = %s"

ERR_EVENT_TYPE_REQUIRED = "event_type is required"
ERR_EVENT_ID_REQUIRED = "event_id is required"
_UNSET = object()

EVENT_DISPATCH_ALLOWED_STATUSES = {
    "matched",
    "skipped",
    "queued",
    "dispatching",
    "succeeded",
    "failed",
    "dead_lettered",
}

EVENT_DISPATCH_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "matched": {"matched", "queued", "skipped", "failed", "succeeded"},
    "queued": {"queued", "dispatching", "succeeded", "failed", "dead_lettered"},
    "dispatching": {"dispatching", "succeeded", "failed", "dead_lettered"},
    "failed": {"failed", "queued", "dead_lettered"},
    "dead_lettered": {"dead_lettered", "queued"},
    "succeeded": {"succeeded"},
    "skipped": {"skipped"},
}


def _helpers():
    from core.infrastructure.db import platform_logger as _platform_logger

    return _platform_logger


def _as_dict(value):
    return _helpers()._as_dict(value)


def _coerce_bool(value, default: bool = False) -> bool:
    return _helpers()._coerce_bool(value, default=default)


def _coerce_int(value, default: int) -> int:
    return _helpers()._coerce_int(value, default)


def _default_account_id() -> str:
    return _helpers()._default_account_id()


__all__ = [
    "ERR_EVENT_ID_REQUIRED",
    "ERR_EVENT_TYPE_REQUIRED",
    "EVENT_DISPATCH_ALLOWED_STATUSES",
    "EVENT_DISPATCH_ALLOWED_TRANSITIONS",
    "EVENT_TYPE_S_OR_EVENT_TYPE_LIKE_S_SQL",
    "EVENT_TYPE_S_SQL",
    "OCCURRED_AT_S_VALUE",
    "OCCURRED_AT_S_VALUE_2",
    "SQL_ACCOUNT_ID_EQ",
    "SQL_AND",
    "SQL_EVENT_ID_EQ",
    "SQL_JOB_ID_EQ",
    "SQL_LAST_ERROR_EQ",
    "SQL_PROJECT_ID_EQ",
    "SQL_STATUS_EQ",
    "SQL_WORKFLOW_ID_EQ",
    "_UNSET",
    "_as_dict",
    "_coerce_bool",
    "_coerce_int",
    "_default_account_id",
    "build_event_envelope_v1",
    "psycopg_runtime",
]
