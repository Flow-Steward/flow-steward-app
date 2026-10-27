"""Persistence helpers for scheduled-job execution locks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

try:
    from core.infrastructure.db.psycopg_runtime import extras as _psycopg_runtime_extras
except Exception:  # pragma: no cover - test environments may not ship psycopg_runtime
    _psycopg_runtime_extras = None

_SCHEDULED_JOB_LOCKS_TABLE = "scheduled_job_execution_locks"
_TERMINAL_SCHEDULED_LOCK_JOB_STATUSES = (
    "completed",
    "failed",
    "cancelled",
    "max_iterations",
)


class ScheduledJobExecutionLockSchemaUnavailableError(RuntimeError):
    """Raised when scheduled-job execution lock tables are missing in DB schema."""


def _cursor_kwargs() -> dict[str, Any]:
    if _psycopg_runtime_extras is None:
        return {}
    return {"cursor_factory": _psycopg_runtime_extras.RealDictCursor}


def normalize_scheduled_job_execution_lock_row(
    row: dict | None,
    *,
    coerce_int,
) -> dict[str, Any]:
    payload = dict(row or {})
    payload["id"] = str(payload.get("id") or "").strip()
    payload["scheduled_job_id"] = str(payload.get("scheduled_job_id") or "").strip()
    payload["project_id"] = str(payload.get("project_id") or "").strip()
    payload["job_id"] = str(payload.get("job_id") or "").strip() or None
    payload["slot_ordinal"] = max(1, coerce_int(payload.get("slot_ordinal"), 1))
    payload["account_id"] = str(payload.get("account_id") or "").strip()
    payload["workflow_id"] = str(payload.get("workflow_id") or "").strip()
    payload["lock_key"] = str(payload.get("lock_key") or "").strip()
    payload["run_type"] = str(payload.get("run_type") or "").strip().lower() or "scheduled"
    payload["status"] = str(payload.get("status") or "").strip().lower() or "pre_publish"
    payload["scheduler_instance_id"] = str(payload.get("scheduler_instance_id") or "").strip()
    payload["worker_instance_id"] = str(payload.get("worker_instance_id") or "").strip()
    payload["release_reason"] = str(payload.get("release_reason") or "").strip()
    return payload


def _locks_table_exists(table_exists) -> bool:
    return bool(table_exists(_SCHEDULED_JOB_LOCKS_TABLE))


def acquire_scheduled_job_execution_lock(
    *,
    conn,
    table_exists,
    coerce_int,
    scheduled_job_id: str,
    account_id: str,
    project_id: str,
    workflow_id: str,
    lock_key: str,
    slot_ordinal: int = 1,
    job_id: str | None = None,
    run_type: str = "scheduled",
    status: str = "pre_publish",
    scheduled_for: datetime | None = None,
    scheduler_instance_id: str = "",
    worker_instance_id: str = "",
    expires_at: datetime | None = None,
) -> dict[str, Any] | None:
    if not _locks_table_exists(table_exists):
        raise ScheduledJobExecutionLockSchemaUnavailableError(
            "scheduled_job_execution_locks table is not available in this schema"
        )
    normalized_account_id = str(account_id or "").strip()
    normalized_workflow_id = str(workflow_id or "").strip()
    normalized_lock_key = str(lock_key or "").strip()
    normalized_run_type = str(run_type or "").strip().lower() or "scheduled"
    normalized_status = str(status or "").strip().lower() or "pre_publish"
    normalized_scheduler_id = str(scheduler_instance_id or "").strip()
    normalized_worker_id = str(worker_instance_id or "").strip()
    normalized_slot = max(1, int(slot_ordinal or 1))
    normalized_scheduled_job_id = str(scheduled_job_id or "").strip()
    if not normalized_scheduled_job_id:
        raise ValueError("scheduled_job_id is required")
    normalized_project_id = str(project_id or "").strip()
    normalized_job_id = str(job_id or "").strip() or None
    if not normalized_project_id:
        raise ValueError("project_id is required")
    if not normalized_account_id:
        raise ValueError("account_id is required")
    if not normalized_workflow_id:
        raise ValueError("workflow_id is required")
    if not normalized_lock_key:
        raise ValueError("lock_key is required")

    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            f"""
            INSERT INTO {_SCHEDULED_JOB_LOCKS_TABLE} (
                scheduled_job_id,
                account_id,
                project_id,
                workflow_id,
                lock_key,
                slot_ordinal,
                job_id,
                run_type,
                status,
                scheduled_for,
                scheduler_instance_id,
                worker_instance_id,
                expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING *
            """,
            (
                normalized_scheduled_job_id,
                normalized_account_id,
                normalized_project_id,
                normalized_workflow_id,
                normalized_lock_key,
                normalized_slot,
                normalized_job_id,
                normalized_run_type,
                normalized_status,
                scheduled_for,
                normalized_scheduler_id,
                normalized_worker_id,
                expires_at,
            ),
        )
        row = cur.fetchone()
    return (
        normalize_scheduled_job_execution_lock_row(dict(row), coerce_int=coerce_int)
        if row
        else None
    )


def activate_scheduled_job_execution_lock(
    *,
    conn,
    table_exists,
    coerce_int,
    lock_id: Any,
    worker_instance_id: str = "",
    status: str = "active",
    expires_at: datetime | None = None,
) -> dict[str, Any] | None:
    if not _locks_table_exists(table_exists):
        return None
    normalized_status = str(status or "").strip().lower() or "active"
    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            f"""
            UPDATE {_SCHEDULED_JOB_LOCKS_TABLE}
            SET
                status = %s,
                worker_instance_id = CASE
                    WHEN %s = '' THEN worker_instance_id
                    ELSE %s
                END,
                activated_at = COALESCE(activated_at, now()),
                expires_at = %s,
                updated_at = now()
            WHERE id::text = %s
              AND released_at IS NULL
            RETURNING *
            """,
            (
                normalized_status,
                str(worker_instance_id or "").strip(),
                str(worker_instance_id or "").strip(),
                expires_at,
                str(lock_id),
            ),
        )
        row = cur.fetchone()
    return (
        normalize_scheduled_job_execution_lock_row(dict(row), coerce_int=coerce_int)
        if row
        else None
    )


def release_scheduled_job_execution_lock_by_job_id(
    *,
    conn,
    table_exists,
    coerce_int,
    job_id: str,
    release_reason: str = "",
    status: str = "released",
    worker_instance_id: str = "",
) -> dict[str, Any] | None:
    if not _locks_table_exists(table_exists):
        return None
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return None
    normalized_reason = str(release_reason or "").strip()
    normalized_status = str(status or "").strip().lower() or "released"
    normalized_worker_id = str(worker_instance_id or "").strip()
    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            f"""
            UPDATE {_SCHEDULED_JOB_LOCKS_TABLE}
            SET
                status = %s,
                worker_instance_id = CASE
                    WHEN %s = '' THEN worker_instance_id
                    ELSE %s
                END,
                released_at = now(),
                release_reason = %s,
                updated_at = now()
            WHERE job_id = %s
              AND released_at IS NULL
            RETURNING *
            """,
            (
                normalized_status,
                normalized_worker_id,
                normalized_worker_id,
                normalized_reason,
                normalized_job_id,
            ),
        )
        row = cur.fetchone()
        if row:
            return normalize_scheduled_job_execution_lock_row(dict(row), coerce_int=coerce_int)
        cur.execute(
            f"""
            SELECT *
            FROM {_SCHEDULED_JOB_LOCKS_TABLE}
            WHERE job_id = %s
            ORDER BY acquired_at DESC, id DESC
            LIMIT 1
            """,
            (normalized_job_id,),
        )
        row = cur.fetchone()
    return (
        normalize_scheduled_job_execution_lock_row(dict(row), coerce_int=coerce_int)
        if row
        else None
    )


def release_scheduled_job_execution_lock_by_id(
    *,
    conn,
    table_exists,
    coerce_int,
    lock_id: Any,
    release_reason: str = "",
    status: str = "released",
) -> dict[str, Any] | None:
    if not _locks_table_exists(table_exists):
        return None
    normalized_reason = str(release_reason or "").strip()
    normalized_status = str(status or "").strip().lower() or "released"
    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            f"""
            UPDATE {_SCHEDULED_JOB_LOCKS_TABLE}
            SET
                status = %s,
                released_at = now(),
                release_reason = %s,
                updated_at = now()
            WHERE id::text = %s
              AND released_at IS NULL
            RETURNING *
            """,
            (
                normalized_status,
                normalized_reason,
                str(lock_id),
            ),
        )
        row = cur.fetchone()
        if row:
            return normalize_scheduled_job_execution_lock_row(dict(row), coerce_int=coerce_int)
        cur.execute(
            f"SELECT * FROM {_SCHEDULED_JOB_LOCKS_TABLE} WHERE id::text = %s",
            (str(lock_id),),
        )
        row = cur.fetchone()
    return (
        normalize_scheduled_job_execution_lock_row(dict(row), coerce_int=coerce_int)
        if row
        else None
    )


def list_active_scheduled_job_execution_locks(
    *,
    conn,
    table_exists,
    coerce_int,
    account_id: str | None = None,
    project_id: str | None = None,
    scheduled_job_id: str | None = None,
    lock_key: str | None = None,
    job_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if not _locks_table_exists(table_exists):
        return []
    where = ["released_at IS NULL"]
    params: list[Any] = []
    if str(account_id or "").strip():
        where.append("account_id = %s")
        params.append(str(account_id).strip())
    if project_id is not None:
        where.append("project_id = %s")
        params.append(str(project_id).strip())
    if scheduled_job_id is not None:
        where.append("scheduled_job_id = %s")
        params.append(str(scheduled_job_id).strip())
    if str(lock_key or "").strip():
        where.append("lock_key = %s")
        params.append(str(lock_key).strip())
    if job_id is not None:
        where.append("job_id = %s")
        params.append(str(job_id).strip())
    params.append(max(1, min(int(limit or 100), 1000)))
    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            f"""
            SELECT *
            FROM {_SCHEDULED_JOB_LOCKS_TABLE}
            WHERE {" AND ".join(where)}
            ORDER BY acquired_at DESC, id DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    return [
        normalize_scheduled_job_execution_lock_row(dict(row), coerce_int=coerce_int) for row in rows
    ]


def reconcile_scheduled_job_execution_locks(
    *,
    conn,
    table_exists,
    coerce_int,
    now: datetime | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    if not _locks_table_exists(table_exists):
        return {"checked": 0, "released": 0, "stale": 0, "details": []}
    current_now = now or datetime.now(UTC)
    safe_limit = max(1, min(int(limit or 200), 1000))
    summary: dict[str, Any] = {
        "checked": 0,
        "released": 0,
        "stale": 0,
        "activated": 0,
        "details": [],
    }
    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            f"""
            SELECT
                lock_row.*,
                job_row.id AS linked_job_exists,
                job_row.status AS linked_job_status
            FROM {_SCHEDULED_JOB_LOCKS_TABLE} AS lock_row
            LEFT JOIN jobs AS job_row ON job_row.id = lock_row.job_id
            WHERE
                lock_row.released_at IS NULL
                AND (
                    (
                        lock_row.status = 'pre_publish'
                        AND lock_row.expires_at IS NOT NULL
                        AND lock_row.expires_at <= %s
                    )
                    OR lock_row.job_id IS NULL
                    OR job_row.id IS NULL
                    OR job_row.status = ANY(%s)
                )
            ORDER BY lock_row.acquired_at ASC, lock_row.id ASC
            LIMIT %s
            """,
            (
                current_now,
                list(_TERMINAL_SCHEDULED_LOCK_JOB_STATUSES),
                safe_limit,
            ),
        )
        rows = cur.fetchall()

    for row in rows:
        payload = dict(row or {})
        summary["checked"] += 1
        lock_id = str(payload.get("id") or "").strip()
        if not lock_id:
            continue
        lock_status = str(payload.get("status") or "").strip().lower()
        linked_job_exists = bool(payload.get("linked_job_exists"))
        linked_job_status = str(payload.get("linked_job_status") or "").strip().lower()
        if (
            lock_status == "pre_publish"
            and payload.get("expires_at")
            and payload.get("expires_at") <= current_now
        ):
            # If post-publish activation failed but the linked job is still non-terminal,
            # self-heal by promoting the lock to active instead of releasing overlap protection.
            if linked_job_exists and linked_job_status not in _TERMINAL_SCHEDULED_LOCK_JOB_STATUSES:
                promoted = activate_scheduled_job_execution_lock(
                    conn=conn,
                    table_exists=table_exists,
                    coerce_int=coerce_int,
                    lock_id=lock_id,
                    status="active",
                    expires_at=None,
                )
                if promoted:
                    summary["activated"] += 1
                    summary["details"].append(
                        {
                            "lock_id": lock_id,
                            "job_id": promoted.get("job_id"),
                            "scheduled_job_id": promoted.get("scheduled_job_id"),
                            "status": "activated",
                            "release_reason": "pre_publish_promoted_active",
                        }
                    )
                continue
            release_status = "stale"
            release_reason = "pre_publish_expired"
        elif not linked_job_exists:
            release_status = "stale"
            release_reason = "job_missing"
        elif linked_job_status in _TERMINAL_SCHEDULED_LOCK_JOB_STATUSES:
            release_status = "released"
            release_reason = f"job_{linked_job_status}"
        else:
            continue

        released = release_scheduled_job_execution_lock_by_id(
            conn=conn,
            table_exists=table_exists,
            coerce_int=coerce_int,
            lock_id=lock_id,
            release_reason=release_reason,
            status=release_status,
        )
        if not released:
            continue
        summary[release_status] += 1
        summary["details"].append(
            {
                "lock_id": lock_id,
                "job_id": released.get("job_id"),
                "scheduled_job_id": released.get("scheduled_job_id"),
                "status": release_status,
                "release_reason": release_reason,
            }
        )
    return summary


def default_pre_publish_lock_expiry(
    *,
    now: datetime | None = None,
    seconds: int = 300,
) -> datetime:
    current_now = now or datetime.now(UTC)
    return current_now + timedelta(seconds=max(30, int(seconds or 300)))
