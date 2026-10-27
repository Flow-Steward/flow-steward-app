from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.logger_scheduled_providers_common import (
    _UNSET,
    ERR_WORKFLOW_ID_REQUIRED,
    SQL_AND,
    SQL_LAST_ERROR_EQ,
    SQL_STATUS_EQ,
    SQL_WORKFLOW_ID_EQ,
    _coerce_bool,
    _json_dumps,
    _json_object,
    _truncate_text,
)


class PlatformLoggerScheduledJobsMixin:
    conn: Any
    _table_exists: Any

    @staticmethod
    def _normalize_scheduled_job_status(value: str | None, *, enabled: bool = True) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"active", "paused", "error"}:
            return normalized
        return "active" if enabled else "paused"

    @staticmethod
    def _normalize_scheduled_job_run_status(value: str | None) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"enqueued", "skipped", "failed"}:
            raise ValueError(f"Unsupported scheduled job run status '{value}'")
        return normalized

    @staticmethod
    def _normalize_scheduled_job_run_type(value: str | None) -> str:
        normalized = str(value or "").strip().lower() or "scheduled"
        if normalized not in {"scheduled", "manual"}:
            raise ValueError(f"Unsupported scheduled job run_type '{value}'")
        return normalized

    @staticmethod
    def _execute_with_prepare_retry(cur: Any, query: str, params: tuple[Any, ...]) -> None:
        """Retry once without server-side prepare when psycopg prepared statement names collide."""
        try:
            cur.execute(query, params)
        except psycopg.errors.DuplicatePreparedStatement:
            cur.execute(query, params, prepare=False)

    @staticmethod
    def _normalize_scheduled_job_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["id"] = str(payload.get("id") or "").strip()
        payload["project_id"] = str(payload.get("project_id") or "").strip()
        payload["account_id"] = str(payload.get("account_id") or "").strip()
        payload["workflow_id"] = str(payload.get("workflow_id") or "").strip()
        payload["cron_expr"] = str(payload.get("cron_expr") or "").strip()
        payload["inputs"] = _json_object(payload.get("inputs"))
        payload["enabled"] = _coerce_bool(payload.get("enabled"), True)
        payload["status"] = PlatformLoggerScheduledJobsMixin._normalize_scheduled_job_status(
            payload.get("status"),
            enabled=bool(payload["enabled"]),
        )
        payload["last_job_id"] = str(payload.get("last_job_id") or "").strip() or None
        payload["last_run_status"] = str(payload.get("last_run_status") or "").strip().lower()
        payload["last_skip_reason"] = str(payload.get("last_skip_reason") or "").strip()
        payload["last_error"] = str(payload.get("last_error") or "").strip()
        payload["created_by"] = str(payload.get("created_by") or "").strip()
        payload["last_job_status"] = str(payload.get("last_job_status") or "").strip().lower()
        payload["admission_allowed"] = _coerce_bool(payload.get("admission_allowed"), False)
        payload["admission_reason"] = str(
            payload.get("admission_reason") or "not_evaluated"
        ).strip()
        for key in ("admission_workflow_revision", "current_workflow_revision"):
            raw_revision = payload.get(key)
            payload[key] = int(raw_revision) if raw_revision is not None else None
        return payload

    def list_scheduled_jobs(
        self,
        project_id: str,
        *,
        enabled_only: bool = False,
        account_id: str | None = None,
    ) -> list[dict]:
        if not self._table_exists("scheduled_jobs"):
            return []
        where = ["sj.project_id = %s"]
        params: list = [str(project_id)]
        normalized_account_id = str(account_id or "").strip()
        if normalized_account_id:
            where.append("sj.account_id = %s")
            params.append(normalized_account_id)
        if enabled_only:
            where.append("sj.enabled = true")
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            self._execute_with_prepare_retry(
                cur,
                f"""
                SELECT
                    sj.*,
                    rw.revision AS current_workflow_revision,
                    j.status AS last_job_status
                FROM scheduled_jobs sj
                LEFT JOIN registry_workflows rw ON rw.workflow_id = sj.workflow_id
                LEFT JOIN jobs j ON j.id = sj.last_job_id
                WHERE {SQL_AND.join(where)}
                ORDER BY
                    sj.next_run_at ASC NULLS LAST,
                    sj.updated_at DESC,
                    sj.created_at DESC,
                    sj.id DESC
                """,
                tuple(params),
            )
            rows = cur.fetchall()
        return [self._normalize_scheduled_job_row(dict(row)) for row in rows]

    def get_scheduled_job(
        self,
        project_id: str,
        scheduled_job_id: str,
        *,
        account_id: str | None = None,
    ) -> dict | None:
        if not self._table_exists("scheduled_jobs"):
            return None
        normalized_account_id = str(account_id or "").strip()
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            self._execute_with_prepare_retry(
                cur,
                """
                SELECT
                    sj.*,
                    rw.revision AS current_workflow_revision,
                    j.status AS last_job_status
                FROM scheduled_jobs sj
                LEFT JOIN registry_workflows rw ON rw.workflow_id = sj.workflow_id
                LEFT JOIN jobs j ON j.id = sj.last_job_id
                WHERE sj.project_id = %s
                  AND sj.id = %s
                  AND (%s = '' OR sj.account_id = %s)
                """,
                (
                    str(project_id),
                    str(scheduled_job_id),
                    normalized_account_id,
                    normalized_account_id,
                ),
            )
            row = cur.fetchone()
        return self._normalize_scheduled_job_row(dict(row)) if row else None

    def list_due_scheduled_jobs(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[dict]:
        if not self._table_exists("scheduled_jobs"):
            return []
        due_at = now or datetime.now(UTC)
        safe_limit = max(1, min(int(limit or 50), 500))
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            self._execute_with_prepare_retry(
                cur,
                """
                SELECT
                    sj.*,
                    rw.revision AS current_workflow_revision,
                    j.status AS last_job_status,
                    p.schedule_timezone AS project_schedule_timezone
                FROM scheduled_jobs sj
                LEFT JOIN registry_workflows rw ON rw.workflow_id = sj.workflow_id
                LEFT JOIN jobs j ON j.id = sj.last_job_id
                LEFT JOIN projects p ON p.id = sj.project_id
                WHERE sj.enabled = true
                  AND rw.deleted_at IS NULL AND rw.workflow_id IS NOT NULL
                  AND sj.status = 'active'
                  AND sj.next_run_at IS NOT NULL
                  AND sj.next_run_at <= %s
                ORDER BY sj.next_run_at ASC, sj.id ASC
                LIMIT %s
                """,
                (due_at, safe_limit),
            )
            rows = cur.fetchall()
        return [self._normalize_scheduled_job_row(dict(row)) for row in rows]

    def claim_due_scheduled_job(
        self,
        *,
        project_id: str,
        scheduled_job_id: str,
        expected_next_run_at: datetime,
        next_run_at: datetime | None,
        now: datetime | None = None,
    ) -> dict | None:
        """
        Atomically claim one due slot by compare-and-swap on next_run_at.

        This prevents duplicate enqueues when multiple scheduler loops read the
        same due row concurrently. The winner advances next_run_at immediately.
        """
        if not self._table_exists("scheduled_jobs"):
            return None
        due_at = now or datetime.now(UTC)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                WITH claimed AS (
                    UPDATE scheduled_jobs
                    SET next_run_at = %s
                    WHERE project_id = %s
                      AND id = %s
                      AND enabled = true
                      AND status = 'active'
                      AND next_run_at IS NOT NULL
                      AND next_run_at = %s
                      AND next_run_at <= %s
                      AND EXISTS (SELECT 1 FROM registry_workflows w
                          WHERE w.workflow_id = scheduled_jobs.workflow_id AND w.deleted_at IS NULL)
                    RETURNING *
                )
                SELECT
                    claimed.*,
                    rw.revision AS current_workflow_revision,
                    j.status AS last_job_status,
                    p.schedule_timezone AS project_schedule_timezone
                FROM claimed
                LEFT JOIN registry_workflows rw ON rw.workflow_id = claimed.workflow_id
                LEFT JOIN jobs j ON j.id = claimed.last_job_id
                LEFT JOIN projects p ON p.id = claimed.project_id
                """,
                (
                    next_run_at,
                    str(project_id),
                    str(scheduled_job_id),
                    expected_next_run_at,
                    due_at,
                ),
            )
            row = cur.fetchone()
        return self._normalize_scheduled_job_row(dict(row)) if row else None

    def create_scheduled_job(
        self,
        *,
        project_id: str,
        workflow_id: str,
        account_id: str,
        cron_expr: str,
        enabled: bool = True,
        status: str | None = None,
        inputs: dict[str, Any] | None = None,
        next_run_at: datetime | None = None,
        schedule_timezone: str = "",
        admission_allowed: bool = False,
        admission_reason: str = "not_evaluated",
        admission_workflow_revision: int | None = None,
        admission_checked_at: datetime | None = None,
        created_by: str = "operator",
    ) -> dict | None:
        if not self._table_exists("scheduled_jobs"):
            return None
        clean_account_id = _truncate_text(account_id, 255) or ""
        clean_workflow_id = str(workflow_id or "").strip()
        clean_cron_expr = _truncate_text(cron_expr, 255) or ""
        clean_inputs = _json_object(inputs)
        clean_enabled = _coerce_bool(enabled, True)
        clean_status = self._normalize_scheduled_job_status(status, enabled=clean_enabled)
        clean_schedule_timezone = _truncate_text(schedule_timezone, 120) or ""
        clean_created_by = _truncate_text(created_by, 120) or "operator"
        clean_admission_reason = _truncate_text(admission_reason, 4000) or "not_evaluated"
        if not clean_account_id:
            raise ValueError("account_id is required")
        if not clean_workflow_id:
            raise ValueError(ERR_WORKFLOW_ID_REQUIRED)
        if not clean_cron_expr:
            raise ValueError("cron_expr is required")

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO scheduled_jobs (
                    account_id,
                    project_id,
                    workflow_id,
                    cron_expr,
                    enabled,
                    status,
                    inputs,
                    next_run_at,
                    schedule_timezone,
                    admission_allowed,
                    admission_reason,
                    admission_workflow_revision,
                    admission_checked_at,
                    created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    clean_account_id,
                    str(project_id),
                    clean_workflow_id,
                    clean_cron_expr,
                    clean_enabled,
                    clean_status,
                    _json_dumps(clean_inputs),
                    next_run_at,
                    clean_schedule_timezone,
                    bool(admission_allowed),
                    clean_admission_reason,
                    admission_workflow_revision,
                    admission_checked_at,
                    clean_created_by,
                ),
            )
            row = cur.fetchone()
        return self._normalize_scheduled_job_row(dict(row)) if row else None

    def update_scheduled_job(
        self,
        project_id: str,
        scheduled_job_id: str,
        *,
        workflow_id=_UNSET,
        cron_expr=_UNSET,
        inputs=_UNSET,
        schedule_timezone=_UNSET,
        enabled=_UNSET,
        status=_UNSET,
        next_run_at=_UNSET,
        last_run_at=_UNSET,
        last_job_id=_UNSET,
        last_run_status=_UNSET,
        last_skip_reason=_UNSET,
        last_error=_UNSET,
        admission_allowed=_UNSET,
        admission_reason=_UNSET,
        admission_workflow_revision=_UNSET,
        admission_checked_at=_UNSET,
        account_id: str | None = None,
    ) -> dict | None:
        if not self._table_exists("scheduled_jobs"):
            return None

        fields: list[str] = []
        values: list = []
        if workflow_id is not _UNSET:
            clean_workflow_id = _truncate_text(workflow_id, 255) or ""
            if not clean_workflow_id:
                raise ValueError(ERR_WORKFLOW_ID_REQUIRED)
            fields.append(SQL_WORKFLOW_ID_EQ)
            values.append(clean_workflow_id)
        if cron_expr is not _UNSET:
            clean_cron = _truncate_text(cron_expr, 255) or ""
            if not clean_cron:
                raise ValueError("cron_expr is required")
            fields.append("cron_expr = %s")
            values.append(clean_cron)
        if inputs is not _UNSET:
            fields.append("inputs = %s::jsonb")
            values.append(_json_dumps(inputs if isinstance(inputs, dict) else {}))
        if schedule_timezone is not _UNSET:
            fields.append("schedule_timezone = %s")
            values.append(_truncate_text(schedule_timezone, 255) or "")
        if enabled is not _UNSET:
            fields.append("enabled = %s")
            values.append(_coerce_bool(enabled, True))
        if status is not _UNSET:
            fields.append(SQL_STATUS_EQ)
            values.append(self._normalize_scheduled_job_status(status))
        if next_run_at is not _UNSET:
            fields.append("next_run_at = %s")
            values.append(next_run_at)
        if last_run_at is not _UNSET:
            fields.append("last_run_at = %s")
            values.append(last_run_at)
        if last_job_id is not _UNSET:
            fields.append("last_job_id = %s")
            values.append(str(last_job_id).strip() if last_job_id else None)
        if last_run_status is not _UNSET:
            normalized_last_run_status = str(last_run_status or "").strip().lower()
            fields.append("last_run_status = %s")
            values.append(normalized_last_run_status)
        if last_skip_reason is not _UNSET:
            fields.append("last_skip_reason = %s")
            values.append(_truncate_text(last_skip_reason, 4000) or "")
        if last_error is not _UNSET:
            fields.append(SQL_LAST_ERROR_EQ)
            values.append(_truncate_text(last_error, 4000) or "")
        if admission_allowed is not _UNSET:
            fields.append("admission_allowed = %s")
            values.append(bool(admission_allowed))
        if admission_reason is not _UNSET:
            fields.append("admission_reason = %s")
            values.append(_truncate_text(admission_reason, 4000) or "not_evaluated")
        if admission_workflow_revision is not _UNSET:
            fields.append("admission_workflow_revision = %s")
            values.append(
                int(admission_workflow_revision)
                if admission_workflow_revision is not None
                else None
            )
        if admission_checked_at is not _UNSET:
            fields.append("admission_checked_at = %s")
            values.append(admission_checked_at)
        if not fields:
            return self.get_scheduled_job(
                project_id,
                scheduled_job_id,
                account_id=account_id,
            )

        normalized_account_id = str(account_id or "").strip()
        values.extend(
            [str(project_id), str(scheduled_job_id), normalized_account_id, normalized_account_id]
        )
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE scheduled_jobs
                SET {", ".join(fields)}
                WHERE project_id = %s
                  AND id = %s
                  AND (%s = '' OR account_id = %s)
                RETURNING *
                """,
                values,
            )
            row = cur.fetchone()
        return self._normalize_scheduled_job_row(dict(row)) if row else None

    def delete_scheduled_job(
        self,
        project_id: str,
        scheduled_job_id: str,
        *,
        account_id: str | None = None,
    ) -> bool:
        if not self._table_exists("scheduled_jobs"):
            return False
        normalized_account_id = str(account_id or "").strip()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM scheduled_jobs
                WHERE project_id = %s
                  AND id = %s
                  AND (%s = '' OR account_id = %s)
                """,
                (
                    str(project_id),
                    str(scheduled_job_id),
                    normalized_account_id,
                    normalized_account_id,
                ),
            )
            return bool(cur.rowcount)

    def create_scheduled_job_run(
        self,
        *,
        scheduled_job_id: str,
        account_id: str,
        project_id: str,
        workflow_id: str,
        run_type: str = "scheduled",
        status: str,
        reason: str = "",
        job_id: str | None = None,
        scheduled_for: datetime | None = None,
        detail: dict | None = None,
    ) -> dict | None:
        if not self._table_exists("scheduled_job_runs"):
            return None
        clean_account_id = _truncate_text(account_id, 255) or ""
        clean_workflow_id = _truncate_text(workflow_id, 255) or ""
        if not clean_account_id:
            raise ValueError("account_id is required")
        if not clean_workflow_id:
            raise ValueError(ERR_WORKFLOW_ID_REQUIRED)
        clean_run_type = self._normalize_scheduled_job_run_type(run_type)
        clean_status = self._normalize_scheduled_job_run_status(status)
        clean_reason = _truncate_text(reason, 2000) or ""
        clean_detail = detail if isinstance(detail, dict) else {}

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO scheduled_job_runs (
                    scheduled_job_id,
                    account_id,
                    project_id,
                    workflow_id,
                    run_type,
                    status,
                    reason,
                    job_id,
                    scheduled_for,
                    detail_jsonb
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    str(scheduled_job_id),
                    clean_account_id,
                    str(project_id),
                    clean_workflow_id,
                    clean_run_type,
                    clean_status,
                    clean_reason,
                    str(job_id).strip() if job_id else None,
                    scheduled_for,
                    _json_dumps(clean_detail),
                ),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def list_scheduled_job_runs(
        self,
        project_id: str,
        *,
        scheduled_job_id: str | None = None,
        limit: int = 100,
        account_id: str | None = None,
    ) -> list[dict]:
        if not self._table_exists("scheduled_job_runs"):
            return []
        where = ["r.project_id = %s"]
        params: list = [str(project_id)]
        normalized_account_id = str(account_id or "").strip()
        if normalized_account_id:
            where.append("r.account_id = %s")
            params.append(normalized_account_id)
        if scheduled_job_id is not None:
            where.append("r.scheduled_job_id = %s")
            params.append(str(scheduled_job_id))
        params.append(max(1, min(int(limit or 100), 500)))
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    r.*,
                    j.status AS job_status
                FROM scheduled_job_runs r
                LEFT JOIN jobs j ON j.id = r.job_id
                WHERE {SQL_AND.join(where)}
                ORDER BY r.created_at DESC, r.id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]


__all__ = ["PlatformLoggerScheduledJobsMixin"]
