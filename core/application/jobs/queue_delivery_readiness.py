"""Lightweight queue-delivery counts for readiness checks."""

from __future__ import annotations

from typing import Any

_EXECUTION_FAILURE_JOB_STATUSES = frozenset(
    {
        "failed",
        "max_iterations",
        "retryable_interrupted_step",
    }
)
_READINESS_ATTENTION_JOB_STATUSES = (
    "triage_needed",
    "failed",
    "max_iterations",
    "retryable_interrupted_step",
)


# Every query shape is assembled once from source-controlled fragments. Schema
# introspection selects one of four fixed variants; job statuses remain bound.
_PENDING_COUNT_SQL = """
            SELECT
                COUNT(*) FILTER (
                    WHERE COALESCE(meta->'queue_delivery'->>'state', 'queued') <> 'retrying_delivery'
                )::int AS queued_pending_jobs,
                COUNT(*) FILTER (
                    WHERE COALESCE(meta->'queue_delivery'->>'state', '') = 'retrying_delivery'
                )::int AS retrying_delivery_jobs
            FROM jobs
            WHERE status = 'pending'
            """
_ATTENTION_COUNT_SQL = """
            SELECT
                COUNT(*) FILTER (
                    WHERE status = 'triage_needed'
                      AND COALESCE(meta->'queue_delivery'->>'state', '') = 'dead_lettered'
                )::int AS dead_lettered_jobs,
                COUNT(*) FILTER (
                    WHERE status = ANY(%s)
                      AND COALESCE(meta->'queue_delivery'->>'delivery_succeeded', 'false') = 'true'
                      AND COALESCE(meta->'queue_delivery'->>'state', '') <> 'dead_lettered'
                )::int AS post_claim_execution_failed_jobs
            FROM jobs
            WHERE status = ANY(%s)
            """
_DELETED_FILTER_SQL = " AND deleted_at IS NULL"
_ACKNOWLEDGED_FILTER_SQL = " AND acknowledged_at IS NULL"
_READINESS_SQL_BY_SCHEMA = {
    (False, False): (_PENDING_COUNT_SQL, _ATTENTION_COUNT_SQL),
    (True, False): (
        "".join((_PENDING_COUNT_SQL, _DELETED_FILTER_SQL)),
        "".join((_ATTENTION_COUNT_SQL, _DELETED_FILTER_SQL)),
    ),
    (False, True): (
        _PENDING_COUNT_SQL,
        "".join((_ATTENTION_COUNT_SQL, _ACKNOWLEDGED_FILTER_SQL)),
    ),
    (True, True): (
        "".join((_PENDING_COUNT_SQL, _DELETED_FILTER_SQL)),
        "".join((_ATTENTION_COUNT_SQL, _DELETED_FILTER_SQL, _ACKNOWLEDGED_FILTER_SQL)),
    ),
}


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def load_queue_delivery_readiness_counts(bl) -> dict[str, int]:
    if not bl or not hasattr(bl, "conn"):
        return {
            "queued_pending_jobs": 0,
            "retrying_delivery_jobs": 0,
            "dead_lettered_jobs": 0,
            "post_claim_execution_failed_jobs": 0,
        }
    column_exists = getattr(bl, "_column_exists", None)
    has_deleted_at = bool(callable(column_exists) and column_exists("jobs", "deleted_at"))
    has_acknowledged_at = bool(callable(column_exists) and column_exists("jobs", "acknowledged_at"))
    pending_sql, attention_sql = _READINESS_SQL_BY_SCHEMA[(has_deleted_at, has_acknowledged_at)]
    with bl.conn.cursor() as cur:
        cur.execute(pending_sql)
        pending_row = cur.fetchone() or (0, 0)
        cur.execute(
            attention_sql,
            (
                list(_EXECUTION_FAILURE_JOB_STATUSES),
                list(_READINESS_ATTENTION_JOB_STATUSES),
            ),
        )
        attention_row = cur.fetchone() or (0, 0)

    if isinstance(pending_row, dict):
        queued_pending_jobs = max(0, _coerce_int(pending_row.get("queued_pending_jobs"), 0))
        retrying_delivery_jobs = max(
            0,
            _coerce_int(pending_row.get("retrying_delivery_jobs"), 0),
        )
    else:
        queued_pending_jobs = max(0, _coerce_int(pending_row[0] if len(pending_row) > 0 else 0, 0))
        retrying_delivery_jobs = max(
            0,
            _coerce_int(pending_row[1] if len(pending_row) > 1 else 0, 0),
        )

    if isinstance(attention_row, dict):
        dead_lettered_jobs = max(0, _coerce_int(attention_row.get("dead_lettered_jobs"), 0))
        post_claim_execution_failed_jobs = max(
            0,
            _coerce_int(attention_row.get("post_claim_execution_failed_jobs"), 0),
        )
    else:
        dead_lettered_jobs = max(
            0, _coerce_int(attention_row[0] if len(attention_row) > 0 else 0, 0)
        )
        post_claim_execution_failed_jobs = max(
            0,
            _coerce_int(attention_row[1] if len(attention_row) > 1 else 0, 0),
        )
    return {
        "queued_pending_jobs": queued_pending_jobs,
        "retrying_delivery_jobs": retrying_delivery_jobs,
        "dead_lettered_jobs": dead_lettered_jobs,
        "post_claim_execution_failed_jobs": post_claim_execution_failed_jobs,
    }
