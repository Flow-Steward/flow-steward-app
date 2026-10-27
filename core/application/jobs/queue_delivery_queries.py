"""Operator queue-delivery views, readiness counts, and attention queries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.application.jobs.queue_delivery_readiness import (
    load_queue_delivery_readiness_counts as load_queue_delivery_readiness_counts,
)
from core.application.jobs.queue_delivery_runtime import (
    _coerce_int,
    _json_obj,
    normalize_queue_delivery_state,
)
from core.application.observability_vocabulary import (
    normalize_job_status,
    queue_delivery_status_meta,
)
from core.infrastructure.db.psycopg_runtime import sql
from core.infrastructure.runtime.worker.queue_names import (
    queue_dead_letter_queue_name as queue_dead_letter_queue_name,
)

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


_QUEUE_HEALTH_META: dict[str, dict[str, Any]] = {
    "idle": {
        "label": "Idle",
        "tone": "gray",
        "message": "No queued delivery work is waiting for this scope right now.",
        "label_key": "react.monitoring.state_idle",
        "message_key": "react.monitoring.delivery_idle_message",
    },
    "queued_pending": {
        "label": "Normal queued / pending",
        "tone": "blue",
        "message": "Jobs are queued normally and waiting for worker pickup.",
        "label_key": "react.monitoring.delivery_queued_pending_label",
        "message_key": "react.monitoring.delivery_queued_pending_message",
    },
    "retrying_delivery": {
        "label": "Retrying delivery",
        "tone": "amber",
        "message": "RabbitMQ delivery is retrying before execution has safely started.",
        "label_key": "react.monitoring.delivery_retrying_delivery_label",
        "message_key": "react.monitoring.delivery_retrying_delivery_message",
    },
    "dead_lettered": {
        "label": "Dead-lettered / requires intervention",
        "tone": "red",
        "message": "RabbitMQ delivery retries were exhausted and operator intervention is required.",
        "label_key": "react.monitoring.delivery_dead_lettered_label",
        "message_key": "react.monitoring.delivery_dead_lettered_message",
    },
    "execution_failed_after_delivery": {
        "label": "Execution failed after delivery",
        "tone": "red",
        "message": "RabbitMQ delivery succeeded, but the workflow failed after the job was claimed.",
        "label_key": "react.monitoring.delivery_execution_failed_after_delivery_label",
        "message_key": "react.monitoring.delivery_execution_failed_after_delivery_message",
    },
    "discarded": {
        "label": "Discarded / no retry",
        "tone": "gray",
        "message": "The queued message was discarded without another RabbitMQ delivery attempt.",
        "label_key": "react.monitoring.delivery_discarded_label",
        "message_key": "react.monitoring.delivery_discarded_message",
    },
    "delivery_succeeded": {
        "label": "Delivered to worker",
        "tone": "blue",
        "message": "RabbitMQ delivery succeeded and the worker claimed the job for execution.",
        "label_key": "react.monitoring.delivery_delivery_succeeded_label",
        "message_key": "react.monitoring.delivery_delivery_succeeded_message",
    },
}


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return str(isoformat())
        except Exception:
            pass
    normalized = _clean_str(value)
    return normalized or None


def _cursor_column_names(cur: Any) -> list[str]:
    columns: list[str] = []
    for description in getattr(cur, "description", None) or []:
        if isinstance(description, (tuple, list)) and description:
            columns.append(str(description[0]))
            continue
        columns.append(str(getattr(description, "name", "") or ""))
    return columns


def _row_to_mapping(row: Any, columns: list[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    mapped: dict[str, Any] = {}
    for index, column in enumerate(columns):
        if column:
            try:
                mapped[column] = row[index]
            except Exception:
                mapped[column] = None
    return mapped


def _queue_health_meta(state: str) -> dict[str, Any]:
    normalized_state = str(state or "").strip().lower()
    return dict(_QUEUE_HEALTH_META.get(normalized_state, _QUEUE_HEALTH_META["queued_pending"]))


def _queue_health_state(*, job_status: str, queue_delivery_state: dict[str, Any]) -> str:
    delivery_state = str(queue_delivery_state.get("state") or "").strip().lower()
    delivery_succeeded = bool(queue_delivery_state.get("delivery_succeeded"))
    if delivery_state == "dead_lettered":
        return "dead_lettered"
    if delivery_state == "retrying_delivery":
        return "retrying_delivery"
    if delivery_state == "discarded":
        return "discarded"
    if delivery_succeeded and job_status in _EXECUTION_FAILURE_JOB_STATUSES:
        return "execution_failed_after_delivery"
    if delivery_succeeded:
        return "delivery_succeeded"
    if job_status == "pending":
        return "queued_pending"
    return "idle"


def build_queue_delivery_view(job: Mapping[str, Any] | None, *, to_json_obj) -> dict[str, Any]:
    payload = dict(job or {})
    meta = to_json_obj(payload.get("meta")) or {}
    if not isinstance(meta, dict):
        meta = {}
    queue_name = str(payload.get("queue_name") or "").strip()
    project_policy = meta.get("project_policy_snapshot")
    delivery_state = normalize_queue_delivery_state(
        meta,
        queue_name=queue_name,
        dead_letter_queue_name=queue_dead_letter_queue_name(queue_name),
        project_policy=project_policy if isinstance(project_policy, Mapping) else None,
    )
    delivery_meta = queue_delivery_status_meta(delivery_state.get("state"))
    normalized_job_status = normalize_job_status(payload.get("status"))
    health_state = _queue_health_state(
        job_status=normalized_job_status,
        queue_delivery_state=delivery_state,
    )
    health_meta = _queue_health_meta(health_state)
    return {
        **delivery_state,
        "state": str(delivery_meta.get("code") or delivery_state.get("state") or ""),
        "state_label": str(delivery_meta.get("label") or ""),
        "state_tone": str(delivery_meta.get("tone") or "gray"),
        "health_state": health_state,
        "health_label": str(health_meta.get("label") or ""),
        "health_tone": str(health_meta.get("tone") or "gray"),
        "health_message": str(health_meta.get("message") or ""),
        "post_claim_execution_failed": (health_state == "execution_failed_after_delivery"),
        "retry_max_attempts": int(
            (delivery_state.get("policy") or {}).get("retry_max_attempts") or 0
        ),
    }


def load_queue_delivery_counts(
    bl,
    *,
    project_id: str | None = None,
) -> dict[str, int]:
    if not bl or not hasattr(bl, "conn"):
        return {
            "queued_pending_jobs": 0,
            "retrying_delivery_jobs": 0,
            "dead_lettered_jobs": 0,
            "post_claim_execution_failed_jobs": 0,
        }
    where = []
    params: list[Any] = []
    normalized_project_id = str(project_id or "").strip()
    if normalized_project_id:
        where.append(sql.SQL("project_id = %s"))
        params.append(normalized_project_id)
    column_exists = getattr(bl, "_column_exists", None)
    if callable(column_exists) and column_exists("jobs", "deleted_at"):
        where.append(sql.SQL("deleted_at IS NULL"))
    acknowledged_filter = (
        sql.SQL("AND acknowledged_at IS NULL")
        if callable(column_exists) and column_exists("jobs", "acknowledged_at")
        else sql.SQL("")
    )
    where_sql = sql.SQL("WHERE {}").format(sql.SQL(" AND ").join(where)) if where else sql.SQL("")
    with bl.conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            SELECT
                COUNT(*) FILTER (
                    WHERE LOWER(COALESCE(status, '')) = 'pending'
                      AND COALESCE(meta->'queue_delivery'->>'state', 'queued') <> 'retrying_delivery'
                )::int AS queued_pending_jobs,
                COUNT(*) FILTER (
                    WHERE COALESCE(meta->'queue_delivery'->>'state', '') = 'retrying_delivery'
                )::int AS retrying_delivery_jobs,
                COUNT(*) FILTER (
                    WHERE COALESCE(meta->'queue_delivery'->>'state', '') = 'dead_lettered'
                      {acknowledged_filter}
                )::int AS dead_lettered_jobs,
                COUNT(*) FILTER (
                    WHERE COALESCE(meta->'queue_delivery'->>'delivery_succeeded', 'false') = 'true'
                      AND COALESCE(meta->'queue_delivery'->>'state', '') <> 'dead_lettered'
                      {acknowledged_filter}
                      AND LOWER(COALESCE(status, '')) IN (
                        'failed',
                        'max_iterations',
                        'retryable_interrupted_step'
                      )
                )::int AS post_claim_execution_failed_jobs
            FROM jobs
            {where_sql}
            """).format(acknowledged_filter=acknowledged_filter, where_sql=where_sql),
            tuple(params),
        )
        row = cur.fetchone() or (0, 0, 0, 0)
    if isinstance(row, dict):
        return {
            "queued_pending_jobs": max(0, _coerce_int(row.get("queued_pending_jobs"), 0)),
            "retrying_delivery_jobs": max(0, _coerce_int(row.get("retrying_delivery_jobs"), 0)),
            "dead_lettered_jobs": max(0, _coerce_int(row.get("dead_lettered_jobs"), 0)),
            "post_claim_execution_failed_jobs": max(
                0,
                _coerce_int(row.get("post_claim_execution_failed_jobs"), 0),
            ),
        }
    return {
        "queued_pending_jobs": max(0, _coerce_int(row[0] if len(row) > 0 else 0, 0)),
        "retrying_delivery_jobs": max(0, _coerce_int(row[1] if len(row) > 1 else 0, 0)),
        "dead_lettered_jobs": max(0, _coerce_int(row[2] if len(row) > 2 else 0, 0)),
        "post_claim_execution_failed_jobs": max(
            0,
            _coerce_int(row[3] if len(row) > 3 else 0, 0),
        ),
    }


def _queue_delivery_project_state(row: Mapping[str, Any]) -> str:
    if row.get("project_deleted_at"):
        return "trashed"
    if _clean_str(row.get("project_name")):
        return "active"
    return "missing"


def _queue_delivery_resolution_hint(
    *,
    attention_kind: str,
    failure_code: str,
    project_state: str,
) -> str:
    if failure_code == "project_not_found":
        if project_state == "trashed":
            return (
                "Referenced project is in trash. Restore the project if this job should run, "
                "then restart it; acknowledge the job if the deletion was intentional."
            )
        return (
            "Referenced project is missing. Restore or recreate the project if this job should "
            "run, then restart it; acknowledge the job if no retry is needed."
        )
    if attention_kind == "dead_lettered":
        return (
            "Open the job, inspect the queue-delivery failure, fix the referenced resource, "
            "then restart it or acknowledge it if the failed delivery is expected."
        )
    return (
        "Open the job, inspect the workflow failure after delivery, fix the underlying issue, "
        "then restart it or acknowledge it if no rerun is needed."
    )


def _queue_delivery_attention_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    queue_delivery = normalize_queue_delivery_state(
        {"queue_delivery": _json_obj(row.get("queue_delivery"))},
        queue_name=_clean_str(row.get("queue_name")),
        dead_letter_queue_name=queue_dead_letter_queue_name(_clean_str(row.get("queue_name"))),
    )
    status = normalize_job_status(row.get("status"))
    attention_kind = (
        "dead_lettered"
        if queue_delivery.get("state") == "dead_lettered"
        else "execution_failed_after_delivery"
    )
    health_meta = _queue_health_meta(attention_kind)
    project_state = _queue_delivery_project_state(row)
    failure_code = _clean_str(queue_delivery.get("last_failure_code"))
    failure_message = (
        _clean_str(queue_delivery.get("last_failure_message"))
        or _clean_str(row.get("last_error"))
        or _clean_str(row.get("error"))
    )
    selected_workflow = _clean_str(row.get("selected_workflow")) or _clean_str(
        row.get("selected_workflow_key")
    )
    job_id = _clean_str(row.get("job_id"))
    return {
        "job_id": job_id,
        "detail_url": f"/jobs/{job_id}" if job_id else "",
        "attention_kind": attention_kind,
        "attention_label": str(health_meta.get("label") or ""),
        "attention_label_key": str(health_meta.get("label_key") or ""),
        "attention_tone": str(health_meta.get("tone") or "gray"),
        "project_id": _clean_str(row.get("project_id")),
        "project_name": _clean_str(row.get("project_name")),
        "project_state": project_state,
        "project_deleted_at": _iso_or_none(row.get("project_deleted_at")),
        "task_id": _clean_str(row.get("task_id")),
        "task_summary": _clean_str(row.get("task_summary")),
        "selected_workflow": selected_workflow,
        "selected_workflow_key": _clean_str(row.get("selected_workflow_key")),
        "status": status,
        "error": _clean_str(row.get("error")),
        "last_error": _clean_str(row.get("last_error")),
        "queue_name": _clean_str(queue_delivery.get("queue_name")),
        "dead_letter_queue_name": _clean_str(queue_delivery.get("dead_letter_queue_name")),
        "state": _clean_str(queue_delivery.get("state")),
        "failure_class": _clean_str(queue_delivery.get("last_failure_class")),
        "failure_code": failure_code,
        "failure_message": failure_message,
        "attempt_count": max(0, _coerce_int(queue_delivery.get("attempt_count"), 0)),
        "retry_count": max(0, _coerce_int(queue_delivery.get("retry_count"), 0)),
        "retry_max_attempts": max(
            0,
            _coerce_int((queue_delivery.get("policy") or {}).get("retry_max_attempts"), 0),
        ),
        "last_failure_at": _iso_or_none(queue_delivery.get("last_failure_at")),
        "dead_lettered_at": _iso_or_none(queue_delivery.get("dead_lettered_at")),
        "queued_at": _iso_or_none(row.get("queued_at")),
        "started_at": _iso_or_none(row.get("started_at")),
        "finished_at": _iso_or_none(row.get("finished_at")),
        "acknowledged_at": _iso_or_none(row.get("acknowledged_at")),
        "resolution_hint": _queue_delivery_resolution_hint(
            attention_kind=attention_kind,
            failure_code=failure_code,
            project_state=project_state,
        ),
    }


def load_queue_delivery_attention_jobs(
    bl,
    *,
    limit: int = 5,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    if not bl or not hasattr(bl, "conn"):
        return []
    where = [
        sql.SQL("""
        (
            (
                LOWER(COALESCE(j.status, '')) = 'triage_needed'
                AND COALESCE(j.meta->'queue_delivery'->>'state', '') = 'dead_lettered'
            )
            OR
            (
                LOWER(COALESCE(j.status, '')) IN (
                    'failed',
                    'max_iterations',
                    'retryable_interrupted_step'
                )
                AND COALESCE(j.meta->'queue_delivery'->>'delivery_succeeded', 'false') = 'true'
                AND COALESCE(j.meta->'queue_delivery'->>'state', '') <> 'dead_lettered'
            )
        )
        """)
    ]
    params: list[Any] = []
    normalized_project_id = _clean_str(project_id)
    if normalized_project_id:
        where.append(sql.SQL("j.project_id = %s"))
        params.append(normalized_project_id)
    column_exists = getattr(bl, "_column_exists", None)
    if callable(column_exists) and column_exists("jobs", "deleted_at"):
        where.append(sql.SQL("j.deleted_at IS NULL"))
    if callable(column_exists) and column_exists("jobs", "acknowledged_at"):
        where.append(sql.SQL("j.acknowledged_at IS NULL"))
    params.append(max(1, min(25, _coerce_int(limit, 5))))
    with bl.conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            SELECT
                j.id::text AS job_id,
                j.project_id::text AS project_id,
                p.name AS project_name,
                p.deleted_at AS project_deleted_at,
                j.task_id::text AS task_id,
                j.task_summary,
                j.status,
                j.error,
                j.last_error,
                j.queue_name,
                j.selected_workflow_key,
                j.selected_workflow,
                j.queued_at,
                j.started_at,
                j.finished_at,
                j.acknowledged_at,
                j.meta->'queue_delivery' AS queue_delivery
            FROM jobs j
            LEFT JOIN projects p ON p.id = j.project_id
            WHERE {where}
            ORDER BY
                CASE
                    WHEN COALESCE(j.meta->'queue_delivery'->>'state', '') = 'dead_lettered'
                    THEN 0
                    ELSE 1
                END ASC,
                COALESCE(
                    j.meta->'queue_delivery'->>'dead_lettered_at',
                    j.finished_at::text,
                    j.started_at::text,
                    j.queued_at::text
                ) DESC NULLS LAST,
                j.id ASC
            LIMIT %s
            """).format(where=sql.SQL(" AND ").join(where)),
            tuple(params),
        )
        columns = _cursor_column_names(cur)
        rows = cur.fetchall() or []
    return [_queue_delivery_attention_payload(_row_to_mapping(row, columns)) for row in rows]


def build_queue_delivery_summary(
    counts: Mapping[str, Any] | None,
    *,
    attention_jobs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = {
        "queued_pending_jobs": max(0, _coerce_int((counts or {}).get("queued_pending_jobs"), 0)),
        "retrying_delivery_jobs": max(
            0, _coerce_int((counts or {}).get("retrying_delivery_jobs"), 0)
        ),
        "dead_lettered_jobs": max(0, _coerce_int((counts or {}).get("dead_lettered_jobs"), 0)),
        "post_claim_execution_failed_jobs": max(
            0,
            _coerce_int((counts or {}).get("post_claim_execution_failed_jobs"), 0),
        ),
    }
    if payload["retrying_delivery_jobs"] > 0:
        state = "retrying_delivery"
    elif payload["queued_pending_jobs"] > 0:
        state = "queued_pending"
    else:
        state = "idle"
    if payload["dead_lettered_jobs"] > 0:
        attention_state = "dead_lettered"
    elif payload["post_claim_execution_failed_jobs"] > 0:
        attention_state = "execution_failed_after_delivery"
    else:
        attention_state = "idle"
    state_meta = _queue_health_meta(state)
    attention_meta = _queue_health_meta(attention_state)
    state_message = str(state_meta.get("message") or "")
    state_message_key = str(state_meta.get("message_key") or "")
    if state == "idle" and attention_state == "dead_lettered":
        state_message = (
            "No active delivery backlog is waiting right now, but one or more "
            "dead-lettered jobs still require operator intervention."
        )
        state_message_key = "react.monitoring.delivery_idle_dead_lettered_message"
    elif state == "idle" and attention_state == "execution_failed_after_delivery":
        state_message = (
            "No active delivery backlog is waiting right now, but one or more "
            "jobs previously failed after delivery succeeded."
        )
        state_message_key = "react.monitoring.delivery_idle_execution_failed_message"
    payload.update(
        {
            "state": state,
            "state_label": str(state_meta.get("label") or ""),
            "state_label_key": str(state_meta.get("label_key") or ""),
            "state_tone": str(state_meta.get("tone") or "gray"),
            "state_message": state_message,
            "state_message_key": state_message_key,
            "attention_state": attention_state,
            "attention_label": str(attention_meta.get("label") or ""),
            "attention_label_key": str(attention_meta.get("label_key") or ""),
            "attention_tone": str(attention_meta.get("tone") or "gray"),
            "attention_message": str(attention_meta.get("message") or ""),
            "attention_message_key": str(attention_meta.get("message_key") or ""),
            # RC7-005: delivery failures gate readiness; execution failures do
            # not. post_claim_execution_failed_jobs counts jobs whose delivery
            # SUCCEEDED and whose execution then failed for ordinary reasons - a
            # bad input, a guardrail, a rejected contract. That is a job needing
            # an operator, not a platform that cannot accept work, and treating
            # it as unreadiness made a healthy stack report 503 until someone
            # acknowledged an ordinary failure. The count stays in the payload so
            # jobs, monitoring, REST, SSE, MCP and CLI keep showing it.
            "requires_intervention": bool(payload["dead_lettered_jobs"] > 0),
            "attention_jobs": [dict(item) for item in attention_jobs or []],
            "attention_jobs_shown": len(attention_jobs or []),
            "attention_job_count": (
                payload["dead_lettered_jobs"] + payload["post_claim_execution_failed_jobs"]
            ),
            "operator_action_key": "react.monitoring.delivery_operator_action",
            "operator_action": (
                "Open a listed job, fix the referenced project or workflow issue, "
                "then restart it or acknowledge it when no rerun is needed."
            ),
        }
    )
    return payload
