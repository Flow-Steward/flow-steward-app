"""Application-owned repository for durable job lifecycle persistence."""

from __future__ import annotations

import json
import logging
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID, DEFAULT_WORKSPACE_ID
from core.infrastructure.db import psycopg_runtime as psycopg_runtime


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _canonical_job_status(value: Any) -> Any:
    """Keep internal error outcomes out of the public job lifecycle vocabulary."""
    if str(value or "").strip().lower() == "error":
        return "failed"
    return value


def truncate_text(value: Any, max_len: int = 4000) -> str | None:
    """Normalize bounded job text without depending on a logger-private helper."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text if max_len <= 0 else text[:max_len]


def create_job(
    *,
    conn,
    column_exists,
    truncate_text,
    project_id,
    task_id,
    task_summary=None,
    task_source="manual",
    thread_id=None,
    mode=None,
    complexity=None,
    workflow_path=None,
    status="in_progress",
    queue_name=None,
    selected_workflow_key=None,
    meta=None,
):
    status = _canonical_job_status(status)
    has_complexity_column = column_exists("jobs", "complexity")
    project_scope = {
        "tenant_id": DEFAULT_TENANT_ID,
        "account_id": DEFAULT_ACCOUNT_ID,
        "workspace_id": DEFAULT_WORKSPACE_ID,
    }
    has_scope_columns = (
        column_exists("jobs", "tenant_id")
        and column_exists("jobs", "account_id")
        and column_exists("jobs", "workspace_id")
    )
    if has_scope_columns:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT tenant_id, account_id, workspace_id
                  FROM projects
                 WHERE id = %s
                 LIMIT 1
                """,
                (project_id,),
            )
            row = cur.fetchone() or {}
            project_scope = {
                "tenant_id": str(row.get("tenant_id") or DEFAULT_TENANT_ID),
                "account_id": str(row.get("account_id") or DEFAULT_ACCOUNT_ID),
                "workspace_id": str(row.get("workspace_id") or DEFAULT_WORKSPACE_ID),
            }
    columns = [
        "project_id",
        "task_id",
        "task_summary",
        "task_source",
        "thread_id",
        "mode",
    ]
    values = [
        project_id,
        task_id,
        truncate_text(task_summary, 4000) or "",
        task_source,
        thread_id,
        mode,
    ]
    placeholders = ["%s"] * len(columns)
    if has_scope_columns:
        columns[0:0] = ["tenant_id", "account_id", "workspace_id"]
        values[0:0] = [
            project_scope["tenant_id"],
            project_scope["account_id"],
            project_scope["workspace_id"],
        ]
        placeholders[0:0] = ["%s", "%s", "%s"]
    if has_complexity_column:
        columns.append("complexity")
        values.append(complexity)
        placeholders.append("%s")
    if column_exists("jobs", "selected_workflow_key"):
        columns.append("selected_workflow_key")
        values.append(truncate_text(selected_workflow_key, 512))
        placeholders.append("%s")

    columns.extend(["workflow_path", "status", "queue_name", "queued_at", "meta"])
    placeholders.extend(
        ["%s", "%s", "%s", "CASE WHEN %s = 'pending' THEN NOW() ELSE NULL END", "%s"]
    )
    values.extend(
        [
            _json_dumps(workflow_path) if workflow_path else None,
            status,
            queue_name,
            status,
            _json_dumps(meta) if meta else None,
        ]
    )

    with conn.cursor() as cur:
        columns_sql = f"({', '.join(columns)})"  # nosec B608
        placeholders_sql = f"({', '.join(placeholders)})"  # nosec B608
        statement = f"INSERT INTO jobs {columns_sql} VALUES {placeholders_sql} RETURNING id"  # nosec B608
        cur.execute(statement, tuple(values))
        return cur.fetchone()[0]


def update_job(
    *,
    conn,
    column_exists,
    sql_status_eq: str,
    job_id,
    status=None,
    mode=None,
    complexity=None,
    workflow_path=None,
    queue_name=None,
    queued_at=None,
    worker_id=None,
    meta=None,
) -> None:
    fields = []
    values = []
    if status is not None:
        fields.append(sql_status_eq)
        values.append(_canonical_job_status(status))
    if mode is not None:
        fields.append("mode = %s")
        values.append(mode)
    if complexity is not None and column_exists("jobs", "complexity"):
        fields.append("complexity = %s")
        values.append(complexity)
    if workflow_path is not None:
        fields.append("workflow_path = %s")
        values.append(json.dumps(workflow_path))
    if queue_name is not None:
        fields.append("queue_name = %s")
        values.append(queue_name)
    if queued_at is not None:
        if queued_at == "NOW()":
            fields.append("queued_at = NOW()")
        else:
            fields.append("queued_at = %s")
            values.append(queued_at)
    if worker_id is not None:
        fields.append("worker_id = %s")
        values.append(worker_id)
    if meta is not None:
        fields.append("meta = %s")
        values.append(_json_dumps(meta))
    if not fields:
        return
    values.append(job_id)
    with conn.cursor() as cur:
        statement = f"UPDATE jobs SET {', '.join(fields)} WHERE id = %s"  # nosec B608
        cur.execute(statement, tuple(values))


def recover_in_progress_job(
    *,
    conn,
    job_id: str,
    expected_worker_id: str,
    reason: str,
) -> bool:
    """Fence a dead execution owner before the current delivery reclaims the job."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs
               SET status = 'orphaned',
                   worker_id = NULL,
                   heartbeat_at = NULL,
                   last_error = %s
             WHERE id = %s
               AND status = 'in_progress'
               AND worker_id = %s
            """,
            (reason, job_id, expected_worker_id),
        )
        return bool(cur.rowcount)


def merge_job_meta(
    *,
    conn,
    job_id: str,
    patch: dict | None = None,
) -> None:
    if not isinstance(patch, dict) or not patch:
        return

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute("SELECT meta FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
        row = cur.fetchone() or {}
        current_meta = row.get("meta")
        if isinstance(current_meta, str):
            try:
                current_meta = json.loads(current_meta)
            except Exception:
                current_meta = {}
        if not isinstance(current_meta, dict):
            current_meta = {}

        merged = dict(current_meta)
        for key, value in patch.items():
            if value is None:
                continue
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged.get(key), **value}
            else:
                merged[key] = value

        cur.execute(
            "UPDATE jobs SET meta = %s WHERE id = %s",
            (_json_dumps(merged), job_id),
        )


def update_job_queue_delivery_meta(
    *,
    conn,
    job_id: str,
    queue_delivery: dict | None,
    runtime_journal: list | None,
    status: str | None = None,
    expected_dispatch_states: set[str] | None = None,
) -> bool:
    """Atomically persist a publisher transition without overwriting a worker claim.

    Queue publication and worker pickup occur on separate processes.  A worker claim
    is stronger evidence than a late publisher marker, so a publisher transition must
    become a no-op once delivery_succeeded is durable.
    """
    if not isinstance(queue_delivery, dict):
        return False
    journal = runtime_journal if isinstance(runtime_journal, list) else []
    expected_states = sorted(
        {
            str(item).strip().lower()
            for item in (expected_dispatch_states or set())
            if str(item).strip()
        }
    )
    if not expected_states:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs
               SET status = COALESCE(%s, status),
                   meta = jsonb_set(
                   jsonb_set(
                       COALESCE(meta, '{}'::jsonb),
                       '{queue_delivery}',
                       %s::jsonb,
                       true
                   ),
                   '{runtime_journal}',
                   %s::jsonb,
                   true
               )
             WHERE id = %s
               AND status = 'pending'
               AND NULLIF(worker_id, '') IS NULL
               AND COALESCE(meta->'queue_delivery'->>'delivery_succeeded', 'false') <> 'true'
               AND COALESCE(
                    meta->'queue_delivery'->>'dispatch_state',
                    'not_started'
               ) = ANY(%s)
            RETURNING id
            """,
            (
                status,
                _json_dumps(queue_delivery),
                _json_dumps(journal),
                job_id,
                expected_states,
            ),
        )
        return cur.fetchone() is not None


def update_job_runtime(
    *,
    conn,
    sql_last_error_eq: str,
    job_id: str,
    selected_workflow: str | None = None,
    current_step_id: str | None = None,
    current_group_id: str | None = None,
    current_agent: str | None = None,
    heartbeat_now: bool = False,
    last_error: str | None = None,
    last_response_excerpt: str | None = None,
    clear_last_error: bool = False,
) -> None:
    fields: list[str] = []
    values: list = []

    if selected_workflow is not None:
        fields.append("selected_workflow = %s")
        values.append(selected_workflow or None)
    # The step scope is one pair: a root step has no group, so the group is
    # written (or cleared) together with the step and never on its own.
    if current_step_id is not None:
        fields.append("current_step_id = %s")
        values.append(current_step_id or None)
        fields.append("current_group_id = %s")
        values.append((current_group_id or None) if current_step_id else None)
    if current_agent is not None:
        fields.append("current_agent = %s")
        values.append(current_agent or None)
    if heartbeat_now:
        fields.append("heartbeat_at = NOW()")
    if clear_last_error:
        fields.append("last_error = NULL")
    elif last_error is not None:
        fields.append(sql_last_error_eq)
        values.append(last_error)
    if last_response_excerpt is not None:
        excerpt = last_response_excerpt[:4000] if last_response_excerpt else None
        fields.append("last_response_excerpt = %s")
        values.append(excerpt)

    if not fields:
        return

    values.append(job_id)
    with conn.cursor() as cur:
        statement = f"UPDATE jobs SET {', '.join(fields)} WHERE id = %s"  # nosec B608
        cur.execute(statement, tuple(values))


def finish_job(
    *,
    conn,
    logger: logging.Logger,
    merge_job_meta_func,
    update_job_runtime_func,
    job_id,
    status="completed",
    final_report=None,
    error=None,
    root_run_id=None,
) -> None:
    status = _canonical_job_status(status)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET
                status = %s,
                finished_at = NOW(),
                duration_ms = EXTRACT(EPOCH FROM (NOW() - started_at))::INTEGER * 1000,
                final_report = %s,
                error = %s
            WHERE id = %s
            """,
            (status, final_report, error, job_id),
        )
    normalized_root_run_id = str(root_run_id or "").strip()
    if normalized_root_run_id:
        try:
            merge_job_meta_func(job_id, {"root_run_id": normalized_root_run_id})
        except Exception as exc:
            logger.debug("finish_job trace metadata update failed (non-fatal): %s", exc)
    try:
        update_job_runtime_func(
            job_id,
            current_agent="",
            heartbeat_now=True,
            last_error=error if error else None,
            clear_last_error=not bool(error),
        )
    except Exception as exc:
        logger.debug("finish_job runtime update failed (non-fatal): %s", exc)


class JobLifecycleRepository:
    """Bind all job lifecycle SQL operations to one explicit connection."""

    def __init__(self, connection: Any, column_exists: Any) -> None:
        self.connection = connection
        self.column_exists = column_exists

    def create(self, **job_fields: Any) -> Any:
        return create_job(
            conn=self.connection,
            column_exists=self.column_exists,
            truncate_text=truncate_text,
            **job_fields,
        )

    def update(self, **job_fields: Any) -> None:
        update_job(
            conn=self.connection,
            column_exists=self.column_exists,
            sql_status_eq="status = %s",
            **job_fields,
        )

    def merge_meta(self, *, job_id: str, patch: dict | None = None) -> None:
        merge_job_meta(conn=self.connection, job_id=job_id, patch=patch)

    def recover_in_progress(self, *, job_id: str, expected_worker_id: str, reason: str) -> bool:
        return recover_in_progress_job(
            conn=self.connection,
            job_id=job_id,
            expected_worker_id=expected_worker_id,
            reason=reason,
        )

    def update_queue_delivery_meta(self, **values: Any) -> bool:
        return update_job_queue_delivery_meta(conn=self.connection, **values)

    def update_runtime(self, **values: Any) -> None:
        update_job_runtime(
            conn=self.connection,
            sql_last_error_eq="last_error = %s",
            **values,
        )

    def finish(self, **values: Any) -> None:
        finish_job(conn=self.connection, **values)
