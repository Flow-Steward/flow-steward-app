# mypy: ignore-errors
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from core.infrastructure.db.logger_events_dispatch_common import (
    _UNSET,
    ERR_EVENT_ID_REQUIRED,
    SQL_AND,
    SQL_EVENT_ID_EQ,
    _as_dict,
    _coerce_int,
    _default_account_id,
    psycopg_runtime,
)


class PlatformLoggerEventsDeliveryAttemptsMixin:
    conn: Any
    _column_exists: Any
    _table_exists: Any

    @staticmethod
    def _normalize_event_delivery_attempt_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["request"] = _as_dict(payload.get("request") or payload.get("request_jsonb"))
        payload["response"] = _as_dict(payload.get("response") or payload.get("response_jsonb"))
        payload["event_id"] = str(payload.get("event_id") or "").strip()
        payload["dispatch_id"] = str(payload.get("dispatch_id") or "").strip()
        payload["status"] = str(payload.get("status") or "queued").strip().lower() or "queued"
        payload["worker_id"] = str(payload.get("worker_id") or "").strip() or None
        payload["handler"] = str(payload.get("handler") or "").strip() or None
        payload["error_text"] = str(payload.get("error_text") or "").strip() or None
        payload["attempt_no"] = max(1, _coerce_int(payload.get("attempt_no"), 1))
        payload.pop("request_jsonb", None)
        payload.pop("response_jsonb", None)
        return payload

    def record_event_delivery_attempt(
        self,
        *,
        event_id: str,
        dispatch_id: str,
        status: str,
        attempt_no: int | None = None,
        worker_id: str | None = None,
        handler: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int | None = None,
        request: dict | None = None,
        response: dict | None = None,
        error_text: str | None = None,
        retry_in_seconds: int | None = None,
        dispatch_reason_code: str | None | object = _UNSET,
        dispatch_reason_detail: str | None | object = _UNSET,
        external_effect_id: str | None = None,
    ) -> dict | None:
        if not self._table_exists("event_delivery_attempts"):
            return None
        normalized_event_id = str(event_id or "").strip()
        normalized_dispatch_id = str(dispatch_id or "").strip()
        if not normalized_event_id:
            raise ValueError(ERR_EVENT_ID_REQUIRED)
        if not normalized_dispatch_id:
            raise ValueError("dispatch_id is required")
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {
            "queued",
            "running",
            "succeeded",
            "failed",
            "timeout",
            "dead_lettered",
            "skipped",
        }:
            raise ValueError(f"Unsupported delivery attempt status '{status}'")

        resolved_attempt_no = int(attempt_no) if attempt_no is not None else None
        current_attempt_count = 0
        dispatch_max_attempts = 3
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT status, attempt_count, max_attempts
                FROM event_dispatches
                WHERE dispatch_id = %s
                LIMIT 1
                """,
                (normalized_dispatch_id,),
            )
            dispatch_row = cur.fetchone()
            if not isinstance(dispatch_row, dict):
                raise ValueError(f"Unknown dispatch_id '{normalized_dispatch_id}'")
            current_attempt_count = max(0, _coerce_int(dispatch_row.get("attempt_count"), 0))
            dispatch_max_attempts = max(1, _coerce_int(dispatch_row.get("max_attempts"), 3))

            if resolved_attempt_no is None:
                cur.execute(
                    """
                    SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_attempt_no
                    FROM event_delivery_attempts
                    WHERE dispatch_id = %s
                    """,
                    (normalized_dispatch_id,),
                )
                row = cur.fetchone()
                resolved_attempt_no = max(1, _coerce_int((row or {}).get("next_attempt_no"), 1))

            columns = [
                "event_id",
                "dispatch_id",
                "attempt_no",
                "status",
                "worker_id",
                "handler",
                "started_at",
                "finished_at",
                "duration_ms",
                "request_jsonb",
                "response_jsonb",
                "error_text",
                "retry_in_seconds",
            ]
            values: list[Any] = [
                normalized_event_id,
                normalized_dispatch_id,
                max(1, int(resolved_attempt_no)),
                normalized_status,
                str(worker_id or "").strip() or None,
                str(handler or "").strip() or None,
                str(started_at or datetime.now(UTC).isoformat()).strip(),
                str(finished_at or "").strip() or None,
                int(duration_ms) if duration_ms is not None else None,
                json.dumps(_as_dict(request)),
                json.dumps(_as_dict(response)),
                str(error_text or "").strip() or None,
                int(retry_in_seconds) if retry_in_seconds is not None else None,
            ]
            returning_columns = [
                "id",
                "event_id",
                "dispatch_id",
                "attempt_no",
                "status",
                "worker_id",
                "handler",
                "started_at",
                "finished_at",
                "duration_ms",
                "request_jsonb",
                "response_jsonb",
                "error_text",
                "retry_in_seconds",
            ]
            if self._column_exists("event_delivery_attempts", "external_effect_id"):
                columns.append("external_effect_id")
                values.append(str(external_effect_id or "").strip() or None)
                returning_columns.append("external_effect_id")
            returning_columns.append("created_at")
            cur.execute(
                f"""
                INSERT INTO event_delivery_attempts (
                    {", ".join(columns)}
                ) VALUES ({", ".join(["%s"] * len(columns))})
                RETURNING
                    {", ".join(returning_columns)}
                """,
                tuple(values),
            )
            attempt_row = cur.fetchone()

        attempt_increment = 0 if normalized_status == "queued" else 1
        next_attempt_count = current_attempt_count + attempt_increment
        next_attempt_at: str | None | object = _UNSET
        resolved_reason_code = dispatch_reason_code
        resolved_reason_detail = dispatch_reason_detail

        if normalized_status in {"queued", "running"}:
            dispatch_status = "queued"
        elif normalized_status == "succeeded":
            dispatch_status = "succeeded"
        elif normalized_status == "skipped":
            dispatch_status = "skipped"
        elif normalized_status == "dead_lettered":
            dispatch_status = "dead_lettered"
            if resolved_reason_code is _UNSET:
                resolved_reason_code = "dead_lettered"
        else:
            if next_attempt_count >= dispatch_max_attempts:
                dispatch_status = "dead_lettered"
                if resolved_reason_code is _UNSET:
                    resolved_reason_code = "max_retries_exhausted"
                if resolved_reason_detail is _UNSET:
                    resolved_reason_detail = (
                        f"dispatch reached max_attempts={dispatch_max_attempts}"
                    )
            else:
                dispatch_status = "failed"
                if retry_in_seconds is not None:
                    retry_delay_seconds = max(0, int(retry_in_seconds))
                    if retry_delay_seconds > 0:
                        next_attempt_at = (
                            datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)
                        ).isoformat()
                    else:
                        next_attempt_at = None

        self.update_event_dispatch_status(
            normalized_dispatch_id,
            status=dispatch_status,
            reason_code=resolved_reason_code,
            reason_detail=resolved_reason_detail,
            next_attempt_at=next_attempt_at,
            attempt_count_increment=attempt_increment,
            record_attempt=True,
        )
        return (
            self._normalize_event_delivery_attempt_row(dict(attempt_row)) if attempt_row else None
        )

    def list_event_delivery_attempts(
        self,
        *,
        dispatch_id: str | None = None,
        event_id: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        if not self._table_exists("event_delivery_attempts"):
            return []
        conditions: list[str] = []
        params: list[Any] = []
        if dispatch_id:
            conditions.append("dispatch_id = %s")
            params.append(str(dispatch_id).strip())
        if event_id:
            conditions.append(SQL_EVENT_ID_EQ)
            params.append(str(event_id).strip())
        where_sql = f"WHERE {SQL_AND.join(conditions)}" if conditions else ""
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    id,
                    event_id,
                    dispatch_id,
                    attempt_no,
                    status,
                    worker_id,
                    handler,
                    started_at,
                    finished_at,
                    duration_ms,
                    request_jsonb,
                    response_jsonb,
                    error_text,
                    retry_in_seconds,
                    external_effect_id,
                    created_at
                FROM event_delivery_attempts
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (*params, max(1, int(limit or 1))),
            )
            rows = cur.fetchall()
        return [self._normalize_event_delivery_attempt_row(dict(row)) for row in rows]

    def list_event_delivery_attempts_by_dispatch_ids(
        self,
        *,
        dispatch_ids: list[str],
        limit_per_dispatch: int = 200,
    ) -> dict[str, list[dict]]:
        if not self._table_exists("event_delivery_attempts"):
            return {}
        normalized_dispatch_ids = [
            str(dispatch_id or "").strip()
            for dispatch_id in dispatch_ids
            if str(dispatch_id or "").strip()
        ]
        if not normalized_dispatch_ids:
            return {}
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                WITH ranked_attempts AS (
                    SELECT
                        id,
                        event_id,
                        dispatch_id,
                        attempt_no,
                        status,
                        worker_id,
                        handler,
                        started_at,
                        finished_at,
                        duration_ms,
                        request_jsonb,
                        response_jsonb,
                        error_text,
                        retry_in_seconds,
                        external_effect_id,
                        created_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY dispatch_id
                            ORDER BY created_at DESC, id DESC
                        ) AS attempt_rank
                    FROM event_delivery_attempts
                    WHERE dispatch_id = ANY(%s)
                )
                SELECT
                    id,
                    event_id,
                    dispatch_id,
                    attempt_no,
                    status,
                    worker_id,
                    handler,
                    started_at,
                    finished_at,
                    duration_ms,
                    request_jsonb,
                    response_jsonb,
                    error_text,
                    retry_in_seconds,
                    created_at
                FROM ranked_attempts
                WHERE attempt_rank <= %s
                ORDER BY dispatch_id ASC, created_at DESC, id DESC
                """,
                (
                    normalized_dispatch_ids,
                    max(1, int(limit_per_dispatch or 1)),
                ),
            )
            rows = cur.fetchall()

        attempts_by_dispatch: dict[str, list[dict]] = {
            dispatch_id: [] for dispatch_id in normalized_dispatch_ids
        }
        for row in rows:
            normalized_row = self._normalize_event_delivery_attempt_row(dict(row))
            dispatch_id = str(normalized_row.get("dispatch_id") or "").strip()
            if not dispatch_id:
                continue
            attempts_by_dispatch.setdefault(dispatch_id, []).append(normalized_row)
        return attempts_by_dispatch

    def list_command_executions(
        self,
        *,
        event_id: str | None = None,
        dispatch_id: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        if not self._table_exists("command_audit_log"):
            return []

        has_event_id = self._column_exists("command_audit_log", "event_id")
        has_dispatch_id = self._column_exists("command_audit_log", "dispatch_id")
        conditions: list[str] = []
        params: list[Any] = []
        if event_id and has_event_id:
            conditions.append(SQL_EVENT_ID_EQ)
            params.append(str(event_id).strip())
        if dispatch_id and has_dispatch_id:
            conditions.append("dispatch_id = %s")
            params.append(str(dispatch_id).strip())
        where_sql = f"WHERE {SQL_AND.join(conditions)}" if conditions else ""

        select_columns = [
            "id",
            "command_id",
            "command_type",
            "account_id",
            "actor_type",
            "actor_id",
            "target_jsonb",
            "payload_jsonb",
            "status",
            "result_jsonb",
            "error_text",
            "created_at",
            "updated_at",
        ]
        if has_event_id:
            select_columns.append("event_id")
        if has_dispatch_id:
            select_columns.append("dispatch_id")

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    {", ".join(select_columns)}
                FROM command_audit_log
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (*params, max(1, int(limit or 1))),
            )
            rows = cur.fetchall()

        normalized: list[dict] = []
        for row in rows:
            payload_row = dict(row or {})
            payload_row["target"] = _as_dict(payload_row.get("target_jsonb"))
            payload_row["payload"] = _as_dict(payload_row.get("payload_jsonb"))
            payload_row["result"] = _as_dict(payload_row.get("result_jsonb"))
            payload_row["command_id"] = str(payload_row.get("command_id") or "").strip()
            payload_row["command_type"] = str(payload_row.get("command_type") or "").strip()
            payload_row["account_id"] = (
                str(payload_row.get("account_id") or _default_account_id()).strip()
                or _default_account_id()
            )
            payload_row["actor_type"] = str(payload_row.get("actor_type") or "").strip()
            payload_row["actor_id"] = str(payload_row.get("actor_id") or "").strip()
            payload_row["status"] = (
                str(payload_row.get("status") or "").strip().lower() or "accepted"
            )
            payload_row["error_text"] = str(payload_row.get("error_text") or "").strip() or None
            if has_event_id:
                payload_row["event_id"] = str(payload_row.get("event_id") or "").strip() or None
            if has_dispatch_id:
                payload_row["dispatch_id"] = (
                    str(payload_row.get("dispatch_id") or "").strip() or None
                )
            payload_row.pop("target_jsonb", None)
            payload_row.pop("payload_jsonb", None)
            payload_row.pop("result_jsonb", None)
            normalized.append(payload_row)
        return normalized


__all__ = ["PlatformLoggerEventsDeliveryAttemptsMixin"]
