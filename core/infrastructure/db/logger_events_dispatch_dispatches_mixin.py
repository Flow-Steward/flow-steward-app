from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.db.logger_events_dispatch_common import (
    _UNSET,
    ERR_EVENT_ID_REQUIRED,
    EVENT_DISPATCH_ALLOWED_STATUSES,
    EVENT_DISPATCH_ALLOWED_TRANSITIONS,
    SQL_ACCOUNT_ID_EQ,
    SQL_AND,
    SQL_EVENT_ID_EQ,
    SQL_STATUS_EQ,
    _as_dict,
    _coerce_int,
    _default_account_id,
    psycopg_runtime,
)


class PlatformLoggerEventsDispatchesMixin:
    conn: Any
    _table_exists: Any

    @staticmethod
    def _normalize_event_dispatch_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["scope"] = _as_dict(payload.get("scope") or payload.get("scope_jsonb"))
        payload["metadata"] = _as_dict(payload.get("metadata") or payload.get("metadata_jsonb"))
        payload["dispatch_id"] = str(payload.get("dispatch_id") or "").strip()
        payload["event_id"] = str(payload.get("event_id") or "").strip()
        payload["account_id"] = (
            str(payload.get("account_id") or _default_account_id()).strip() or _default_account_id()
        )
        payload["workflow_id"] = str(payload.get("workflow_id") or "").strip() or None
        payload["target_type"] = str(payload.get("target_type") or "").strip()
        payload["target_id"] = str(payload.get("target_id") or "").strip()
        payload["target_label"] = str(payload.get("target_label") or "").strip()
        payload["status"] = str(payload.get("status") or "matched").strip().lower() or "matched"
        payload["reason_code"] = str(payload.get("reason_code") or "").strip() or None
        payload["reason_detail"] = str(payload.get("reason_detail") or "").strip() or None
        payload["attempt_count"] = max(0, _coerce_int(payload.get("attempt_count"), 0))
        payload["max_attempts"] = max(1, _coerce_int(payload.get("max_attempts"), 3))
        payload.pop("scope_jsonb", None)
        payload.pop("metadata_jsonb", None)
        return payload

    @staticmethod
    def _event_dispatch_transition_allowed(current_status: str, next_status: str) -> bool:
        current = str(current_status or "").strip().lower()
        nxt = str(next_status or "").strip().lower()
        if current not in EVENT_DISPATCH_ALLOWED_TRANSITIONS:
            return False
        return nxt in EVENT_DISPATCH_ALLOWED_TRANSITIONS[current]

    def _refresh_domain_event_status(self, event_id: str) -> None:
        normalized_event_id = str(event_id or "").strip()
        if not normalized_event_id:
            return
        if not (
            self._table_exists("domain_event_ledger") and self._table_exists("event_dispatches")
        ):
            return
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)::int AS total_count,
                    COUNT(*) FILTER (WHERE status = 'succeeded')::int AS succeeded_count,
                    COUNT(*) FILTER (WHERE status = 'skipped')::int AS skipped_count,
                    COUNT(*) FILTER (WHERE status IN ('matched', 'queued', 'dispatching'))::int AS pending_count,
                    COUNT(*) FILTER (WHERE status = 'failed')::int AS failed_count,
                    COUNT(*) FILTER (WHERE status = 'dead_lettered')::int AS dead_count
                FROM event_dispatches
                WHERE event_id = %s
                """,
                (normalized_event_id,),
            )
            stats = dict(cur.fetchone() or {})
            total_count = max(0, _coerce_int(stats.get("total_count"), 0))
            if total_count <= 0:
                next_status = "recorded"
                last_dispatch_at = None
            else:
                dead_count = max(0, _coerce_int(stats.get("dead_count"), 0))
                pending_count = max(0, _coerce_int(stats.get("pending_count"), 0))
                failed_count = max(0, _coerce_int(stats.get("failed_count"), 0))
                succeeded_count = max(0, _coerce_int(stats.get("succeeded_count"), 0))
                skipped_count = max(0, _coerce_int(stats.get("skipped_count"), 0))
                if dead_count > 0:
                    next_status = "dead_lettered"
                elif pending_count > 0:
                    next_status = "dispatching"
                elif failed_count > 0 and (succeeded_count + skipped_count) > 0:
                    next_status = "partially_failed"
                elif failed_count > 0:
                    next_status = "failed"
                else:
                    next_status = "succeeded"
                last_dispatch_at = datetime.now(UTC)

            cur.execute(
                """
                UPDATE domain_event_ledger
                SET
                    dispatch_count = %s,
                    status = %s,
                    last_dispatch_at = %s
                WHERE event_id = %s
                """,
                (total_count, next_status, last_dispatch_at, normalized_event_id),
            )

    def claim_domain_event_dispatch(
        self,
        event_id: str,
        *,
        stale_after_seconds: int = 60,
    ) -> bool:
        normalized_event_id = str(event_id or "").strip()
        if not normalized_event_id:
            return False
        if not self._table_exists("domain_event_ledger"):
            return True
        normalized_stale_after = max(10, int(stale_after_seconds or 10))
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE domain_event_ledger
                SET
                    status = 'dispatching',
                    last_dispatch_at = now()
                WHERE event_id = %s
                  AND (
                    status IN ('recorded', 'failed', 'partially_failed')
                    OR (
                        status = 'dispatching'
                        AND (
                            last_dispatch_at IS NULL
                            OR last_dispatch_at <= now() - (%s * interval '1 second')
                        )
                    )
                  )
                RETURNING event_id
                """,
                (normalized_event_id, normalized_stale_after),
            )
            row = cur.fetchone()
        return bool(row and str(row.get("event_id") or "").strip())

    def get_event_dispatch(self, dispatch_id: str) -> dict | None:
        normalized_dispatch_id = str(dispatch_id or "").strip()
        if not normalized_dispatch_id or not self._table_exists("event_dispatches"):
            return None
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id,
                    dispatch_id,
                    event_id,
                    account_id,
                    project_id,
                    workflow_id,
                    job_id,
                    target_type,
                    target_id,
                    target_label,
                    status,
                    reason_code,
                    reason_detail,
                    scope_jsonb,
                    metadata_jsonb,
                    attempt_count,
                    max_attempts,
                    last_attempt_at,
                    next_attempt_at,
                    created_at,
                    updated_at
                FROM event_dispatches
                WHERE dispatch_id = %s
                LIMIT 1
                """,
                (normalized_dispatch_id,),
            )
            row = cur.fetchone()
        return self._normalize_event_dispatch_row(dict(row)) if row else None

    def record_event_dispatch(
        self,
        *,
        event_id: str,
        target_type: str,
        target_id: str,
        account_id: str,
        project_id: Any | None = None,
        workflow_id: str | None = None,
        job_id: Any | None = None,
        target_label: str = "",
        status: str = "matched",
        reason_code: str | None = None,
        reason_detail: str | None = None,
        scope: dict | None = None,
        metadata: dict | None = None,
        max_attempts: int = 3,
        dispatch_id: str | None = None,
    ) -> dict | None:
        if not self._table_exists("event_dispatches"):
            return None
        normalized_event_id = str(event_id or "").strip()
        normalized_target_type = str(target_type or "").strip()
        normalized_target_id = str(target_id or "").strip()
        if not normalized_event_id:
            raise ValueError(ERR_EVENT_ID_REQUIRED)
        if not normalized_target_type:
            raise ValueError("target_type is required")
        if not normalized_target_id:
            raise ValueError("target_id is required")

        normalized_dispatch_id = str(dispatch_id or "").strip() or f"dsp_{uuid.uuid4().hex}"
        normalized_account_id = str(account_id or "").strip() or _default_account_id()
        normalized_status = str(status or "matched").strip().lower() or "matched"
        if normalized_status not in EVENT_DISPATCH_ALLOWED_STATUSES:
            normalized_status = "matched"

        try:
            normalized_project_id = str(project_id) if project_id is not None else None
        except (TypeError, ValueError):
            normalized_project_id = None
        normalized_workflow_id = str(workflow_id or "").strip() or None
        try:
            normalized_job_id = str(job_id) if job_id is not None else None
        except (TypeError, ValueError):
            normalized_job_id = None

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO event_dispatches (
                    dispatch_id,
                    event_id,
                    account_id,
                    project_id,
                    workflow_id,
                    job_id,
                    target_type,
                    target_id,
                    target_label,
                    status,
                    reason_code,
                    reason_detail,
                    scope_jsonb,
                    metadata_jsonb,
                    max_attempts
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (event_id, target_type, target_id) DO UPDATE SET
                    account_id = EXCLUDED.account_id,
                    project_id = EXCLUDED.project_id,
                    workflow_id = EXCLUDED.workflow_id,
                    job_id = EXCLUDED.job_id,
                    target_label = EXCLUDED.target_label,
                    status = EXCLUDED.status,
                    reason_code = EXCLUDED.reason_code,
                    reason_detail = EXCLUDED.reason_detail,
                    scope_jsonb = EXCLUDED.scope_jsonb,
                    metadata_jsonb = EXCLUDED.metadata_jsonb,
                    max_attempts = EXCLUDED.max_attempts,
                    updated_at = now()
                RETURNING
                    id,
                    dispatch_id,
                    event_id,
                    account_id,
                    project_id,
                    workflow_id,
                    job_id,
                    target_type,
                    target_id,
                    target_label,
                    status,
                    reason_code,
                    reason_detail,
                    scope_jsonb,
                    metadata_jsonb,
                    attempt_count,
                    max_attempts,
                    last_attempt_at,
                    next_attempt_at,
                    created_at,
                    updated_at
                """,
                (
                    normalized_dispatch_id,
                    normalized_event_id,
                    normalized_account_id,
                    normalized_project_id,
                    normalized_workflow_id,
                    normalized_job_id,
                    normalized_target_type,
                    normalized_target_id,
                    str(target_label or "").strip(),
                    normalized_status,
                    str(reason_code or "").strip() or None,
                    str(reason_detail or "").strip() or None,
                    json.dumps(_as_dict(scope)),
                    json.dumps(_as_dict(metadata)),
                    max(1, _coerce_int(max_attempts, 3)),
                ),
            )
            row = cur.fetchone()
        self._refresh_domain_event_status(normalized_event_id)
        return self._normalize_event_dispatch_row(dict(row)) if row else None

    def update_event_dispatch_status(
        self,
        dispatch_id: str,
        *,
        status: str,
        reason_code: str | None | object = _UNSET,
        reason_detail: str | None | object = _UNSET,
        next_attempt_at: str | None | object = _UNSET,
        metadata: dict | None | object = _UNSET,
        max_attempts: int | object = _UNSET,
        attempt_count_increment: int = 0,
        record_attempt: bool = False,
    ) -> dict | None:
        normalized_dispatch_id = str(dispatch_id or "").strip()
        if not normalized_dispatch_id or not self._table_exists("event_dispatches"):
            return None
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in EVENT_DISPATCH_ALLOWED_STATUSES:
            raise ValueError(f"Unsupported dispatch status '{status}'")

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT status, event_id
                FROM event_dispatches
                WHERE dispatch_id = %s
                LIMIT 1
                """,
                (normalized_dispatch_id,),
            )
            current_row = cur.fetchone()
        if not isinstance(current_row, dict):
            return None
        current_status = str(current_row.get("status") or "").strip().lower()
        if not self._event_dispatch_transition_allowed(current_status, normalized_status):
            raise ValueError(
                f"Invalid event dispatch status transition '{current_status or 'unknown'}' -> '{normalized_status}'"
            )

        fields = [SQL_STATUS_EQ, "updated_at = now()"]
        params: list[Any] = [normalized_status]

        if reason_code is not _UNSET:
            fields.append("reason_code = %s")
            params.append(str(reason_code or "").strip() or None)
        if reason_detail is not _UNSET:
            fields.append("reason_detail = %s")
            params.append(str(reason_detail or "").strip() or None)
        if next_attempt_at is not _UNSET:
            fields.append("next_attempt_at = %s")
            params.append(str(next_attempt_at or "").strip() or None)
        if metadata is not _UNSET:
            fields.append("metadata_jsonb = %s")
            params.append(json.dumps(_as_dict(metadata)))
        if max_attempts is not _UNSET:
            fields.append("max_attempts = %s")
            params.append(max(1, _coerce_int(max_attempts, 3)))
        if attempt_count_increment:
            fields.append("attempt_count = GREATEST(0, attempt_count + %s)")
            params.append(int(attempt_count_increment))
        if record_attempt:
            fields.append("last_attempt_at = now()")

        params.append(normalized_dispatch_id)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE event_dispatches
                SET {", ".join(fields)}
                WHERE dispatch_id = %s
                RETURNING
                    id,
                    dispatch_id,
                    event_id,
                    account_id,
                    project_id,
                    workflow_id,
                    job_id,
                    target_type,
                    target_id,
                    target_label,
                    status,
                    reason_code,
                    reason_detail,
                    scope_jsonb,
                    metadata_jsonb,
                    attempt_count,
                    max_attempts,
                    last_attempt_at,
                    next_attempt_at,
                    created_at,
                    updated_at
                """,
                tuple(params),
            )
            row = cur.fetchone()
        if row and row.get("event_id"):
            self._refresh_domain_event_status(str(row.get("event_id")))
        return self._normalize_event_dispatch_row(dict(row)) if row else None

    def list_event_dispatches(
        self,
        *,
        event_id: str | None = None,
        account_id: str | None = None,
        status: str | None = None,
        target_type: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        if not self._table_exists("event_dispatches"):
            return []
        conditions: list[str] = []
        params: list[Any] = []
        if event_id:
            conditions.append(SQL_EVENT_ID_EQ)
            params.append(str(event_id).strip())
        if account_id:
            conditions.append(SQL_ACCOUNT_ID_EQ)
            params.append(str(account_id).strip())
        if status:
            conditions.append(SQL_STATUS_EQ)
            params.append(str(status).strip().lower())
        if target_type:
            conditions.append("target_type = %s")
            params.append(str(target_type).strip())
        where_sql = f"WHERE {SQL_AND.join(conditions)}" if conditions else ""
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    id,
                    dispatch_id,
                    event_id,
                    account_id,
                    project_id,
                    workflow_id,
                    job_id,
                    target_type,
                    target_id,
                    target_label,
                    status,
                    reason_code,
                    reason_detail,
                    scope_jsonb,
                    metadata_jsonb,
                    attempt_count,
                    max_attempts,
                    last_attempt_at,
                    next_attempt_at,
                    created_at,
                    updated_at
                FROM event_dispatches
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (*params, max(1, int(limit or 1))),
            )
            rows = cur.fetchall()
        return [self._normalize_event_dispatch_row(dict(row)) for row in rows]


__all__ = ["PlatformLoggerEventsDispatchesMixin"]
