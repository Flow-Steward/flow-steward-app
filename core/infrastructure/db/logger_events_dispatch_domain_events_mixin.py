from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.db.logger_events_dispatch_common import (
    ERR_EVENT_ID_REQUIRED,
    ERR_EVENT_TYPE_REQUIRED,
    EVENT_TYPE_S_OR_EVENT_TYPE_LIKE_S_SQL,
    EVENT_TYPE_S_SQL,
    OCCURRED_AT_S_VALUE,
    OCCURRED_AT_S_VALUE_2,
    SQL_ACCOUNT_ID_EQ,
    SQL_AND,
    SQL_EVENT_ID_EQ,
    SQL_JOB_ID_EQ,
    SQL_PROJECT_ID_EQ,
    SQL_STATUS_EQ,
    SQL_WORKFLOW_ID_EQ,
    _as_dict,
    _coerce_int,
    _default_account_id,
    build_event_envelope_v1,
    psycopg_runtime,
)


class PlatformLoggerEventsDomainEventsMixin:
    conn: Any
    _table_exists: Any

    @staticmethod
    def _normalize_domain_event_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["payload"] = _as_dict(payload.get("payload") or payload.get("payload_jsonb"))
        payload["actor"] = _as_dict(payload.get("actor") or payload.get("actor_jsonb"))
        payload["urls"] = _as_dict(payload.get("urls") or payload.get("urls_jsonb"))
        payload["envelope"] = _as_dict(payload.get("envelope") or payload.get("envelope_jsonb"))
        payload["event_id"] = str(payload.get("event_id") or "").strip()
        payload["event_type"] = str(payload.get("event_type") or "").strip()
        payload["severity"] = str(payload.get("severity") or "info").strip().lower() or "info"
        payload["summary"] = str(payload.get("summary") or "").strip()
        payload["account_id"] = (
            str(payload.get("account_id") or _default_account_id()).strip() or _default_account_id()
        )
        payload["workflow_id"] = str(payload.get("workflow_id") or "").strip() or None
        payload["group_id"] = str(payload.get("group_id") or "").strip() or None
        payload["for_each_id"] = str(payload.get("for_each_id") or "").strip() or None
        payload["step_id"] = str(payload.get("step_id") or "").strip() or None
        payload["step_execution_id"] = str(payload.get("step_execution_id") or "").strip() or None
        payload["source"] = str(payload.get("source") or "runtime").strip() or "runtime"
        payload["aggregate_type"] = str(payload.get("aggregate_type") or "").strip()
        payload["aggregate_id"] = str(payload.get("aggregate_id") or "").strip()
        payload["correlation_id"] = str(payload.get("correlation_id") or "").strip() or None
        payload["status"] = str(payload.get("status") or "recorded").strip().lower() or "recorded"
        payload["dispatch_count"] = max(0, _coerce_int(payload.get("dispatch_count"), 0))
        payload.pop("payload_jsonb", None)
        payload.pop("actor_jsonb", None)
        payload.pop("urls_jsonb", None)
        payload.pop("envelope_jsonb", None)
        return payload

    def record_domain_event(
        self,
        *,
        event_id: str,
        event_type: str,
        account_id: str,
        severity: str = "info",
        summary: str = "",
        payload: dict | None = None,
        actor: dict | None = None,
        urls: dict | None = None,
        envelope: dict | None = None,
        occurred_at: str | None = None,
        project_id: Any | None = None,
        workflow_id: str | None = None,
        job_id: Any | None = None,
        group_id: str | None = None,
        for_each_id: str | None = None,
        step_id: str | None = None,
        step_execution_id: str | None = None,
        source: str | None = None,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        correlation_id: str | None = None,
        status: str = "recorded",
    ) -> dict | None:
        if not self._table_exists("domain_event_ledger"):
            return None

        normalized_envelope = _as_dict(envelope)
        normalized_event_id = str(event_id or normalized_envelope.get("event_id") or "").strip()
        if not normalized_event_id:
            raise ValueError(ERR_EVENT_ID_REQUIRED)
        normalized_event_type = str(
            event_type or normalized_envelope.get("event_type") or ""
        ).strip()
        if not normalized_event_type:
            raise ValueError(ERR_EVENT_TYPE_REQUIRED)
        normalized_account_id = (
            str(account_id or normalized_envelope.get("account_id") or "").strip()
            or _default_account_id()
        )
        normalized_occurred_at = str(
            occurred_at or normalized_envelope.get("occurred_at") or datetime.now(UTC).isoformat()
        ).strip()
        normalized_payload = _as_dict(payload) or _as_dict(normalized_envelope.get("payload"))
        normalized_actor = _as_dict(actor) or _as_dict(normalized_envelope.get("actor"))
        normalized_urls = _as_dict(urls)
        if not normalized_urls:
            normalized_urls = _as_dict(normalized_envelope.get("urls"))
            if not normalized_urls:
                url_value = str(normalized_envelope.get("url") or "").strip()
                if url_value:
                    normalized_urls = {"url": url_value}

        normalized_severity = (
            str(severity or normalized_envelope.get("severity") or "info").strip().lower() or "info"
        )
        if normalized_severity not in {"debug", "info", "warning", "error", "critical"}:
            normalized_severity = "info"
        normalized_summary = (
            str(summary or normalized_envelope.get("summary") or normalized_event_type).strip()
            or normalized_event_type
        )
        normalized_source = (
            str(source or normalized_envelope.get("source") or "runtime").strip() or "runtime"
        )
        normalized_aggregate_type = str(
            aggregate_type or normalized_envelope.get("aggregate_type") or ""
        ).strip()
        normalized_aggregate_id = str(
            aggregate_id or normalized_envelope.get("aggregate_id") or ""
        ).strip()
        normalized_correlation_id = (
            str(correlation_id or normalized_envelope.get("correlation_id") or "").strip() or None
        )
        normalized_status = str(status or "recorded").strip().lower() or "recorded"
        if normalized_status not in {
            "recorded",
            "dispatching",
            "succeeded",
            "partially_failed",
            "failed",
            "dead_lettered",
        }:
            normalized_status = "recorded"

        resolved_project_id = (
            project_id if project_id is not None else normalized_envelope.get("project_id")
        )
        try:
            normalized_project_id = (
                str(resolved_project_id) if resolved_project_id is not None else None
            )
        except (TypeError, ValueError):
            normalized_project_id = None

        resolved_workflow_id = (
            workflow_id if workflow_id is not None else normalized_envelope.get("workflow_id")
        )
        normalized_workflow_id = str(resolved_workflow_id or "").strip() or None

        resolved_job_id = job_id if job_id is not None else normalized_envelope.get("job_id")
        try:
            normalized_job_id = str(resolved_job_id) if resolved_job_id is not None else None
        except (TypeError, ValueError):
            normalized_job_id = None

        resolved_group_id = (
            group_id if group_id is not None else normalized_envelope.get("group_id")
        )
        normalized_group_id = str(resolved_group_id or "").strip() or None
        resolved_for_each_id = (
            for_each_id if for_each_id is not None else normalized_envelope.get("for_each_id")
        )
        normalized_for_each_id = str(resolved_for_each_id or "").strip() or None

        resolved_step_id = step_id if step_id is not None else normalized_envelope.get("step_id")
        normalized_step_id = str(resolved_step_id or "").strip() or None

        resolved_step_execution_id = (
            step_execution_id
            if step_execution_id is not None
            else normalized_envelope.get("step_execution_id")
        )
        normalized_step_execution_id = str(resolved_step_execution_id or "").strip() or None

        normalized_envelope = build_event_envelope_v1(
            event_type=normalized_event_type,
            account_id=normalized_account_id,
            correlation_id=normalized_correlation_id or normalized_event_id,
            severity=normalized_severity,
            summary=normalized_summary,
            payload=normalized_payload,
            project_id=normalized_project_id,
            workflow_id=normalized_workflow_id,
            job_id=normalized_job_id,
            group_id=normalized_group_id,
            for_each_id=normalized_for_each_id,
            step_id=normalized_step_id,
            step_execution_id=normalized_step_execution_id,
            source=normalized_source,
            aggregate_type=normalized_aggregate_type or None,
            aggregate_id=normalized_aggregate_id or None,
            actor=normalized_actor,
            urls=normalized_urls,
            metadata=_as_dict(normalized_envelope.get("metadata")),
            event_id=normalized_event_id,
            occurred_at=normalized_occurred_at,
        )
        normalized_correlation_id = (
            str(normalized_envelope.get("correlation_id") or normalized_event_id).strip()
            or normalized_event_id
        )

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO domain_event_ledger (
                    event_id,
                    event_type,
                    occurred_at,
                    account_id,
                    project_id,
                    workflow_id,
                    job_id,
                    group_id,
                    for_each_id,
                    step_id,
                    step_execution_id,
                    severity,
                    summary,
                    payload_jsonb,
                    actor_jsonb,
                    urls_jsonb,
                    envelope_jsonb,
                    source,
                    aggregate_type,
                    aggregate_id,
                    correlation_id,
                    status
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT (event_id) DO UPDATE SET
                    event_type = EXCLUDED.event_type,
                    occurred_at = EXCLUDED.occurred_at,
                    account_id = EXCLUDED.account_id,
                    project_id = EXCLUDED.project_id,
                    workflow_id = EXCLUDED.workflow_id,
                    job_id = EXCLUDED.job_id,
                    group_id = EXCLUDED.group_id,
                    for_each_id = EXCLUDED.for_each_id,
                    step_id = EXCLUDED.step_id,
                    step_execution_id = EXCLUDED.step_execution_id,
                    severity = EXCLUDED.severity,
                    summary = EXCLUDED.summary,
                    payload_jsonb = EXCLUDED.payload_jsonb,
                    actor_jsonb = EXCLUDED.actor_jsonb,
                    urls_jsonb = EXCLUDED.urls_jsonb,
                    envelope_jsonb = EXCLUDED.envelope_jsonb,
                    source = EXCLUDED.source,
                    aggregate_type = EXCLUDED.aggregate_type,
                    aggregate_id = EXCLUDED.aggregate_id,
                    correlation_id = EXCLUDED.correlation_id,
                    status = EXCLUDED.status,
                    updated_at = now()
                RETURNING
                    id,
                    event_id,
                    event_type,
                    occurred_at,
                    account_id,
                    project_id,
                    workflow_id,
                    job_id,
                    group_id,
                    for_each_id,
                    step_id,
                    step_execution_id,
                    severity,
                    summary,
                    payload_jsonb,
                    actor_jsonb,
                    urls_jsonb,
                    envelope_jsonb,
                    source,
                    aggregate_type,
                    aggregate_id,
                    correlation_id,
                    status,
                    dispatch_count,
                    last_dispatch_at,
                    created_at,
                    updated_at
                """,
                (
                    normalized_event_id,
                    normalized_event_type,
                    normalized_occurred_at,
                    normalized_account_id,
                    normalized_project_id,
                    normalized_workflow_id,
                    normalized_job_id,
                    normalized_group_id,
                    normalized_for_each_id,
                    normalized_step_id,
                    normalized_envelope.get("step_execution_id"),
                    normalized_severity,
                    normalized_summary,
                    json.dumps(normalized_payload),
                    json.dumps(normalized_actor),
                    json.dumps(normalized_urls),
                    json.dumps(normalized_envelope),
                    normalized_source,
                    normalized_aggregate_type,
                    normalized_aggregate_id,
                    normalized_correlation_id,
                    normalized_status,
                ),
            )
            row = cur.fetchone()
        return self._normalize_domain_event_row(dict(row)) if row else None

    def list_domain_events(
        self,
        *,
        account_id: str | None = None,
        project_id: Any | None = None,
        workflow_id: str | None = None,
        job_id: Any | None = None,
        event_type: str | None = None,
        event_type_prefix: str | None = None,
        status: str | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[dict]:
        if not self._table_exists("domain_event_ledger"):
            return []
        conditions: list[str] = []
        params: list[Any] = []
        if account_id:
            conditions.append(SQL_ACCOUNT_ID_EQ)
            params.append(str(account_id).strip())
        if project_id is not None:
            conditions.append(SQL_PROJECT_ID_EQ)
            params.append(str(project_id))
        if workflow_id:
            conditions.append(SQL_WORKFLOW_ID_EQ)
            params.append(str(workflow_id).strip())
        if job_id is not None:
            conditions.append(SQL_JOB_ID_EQ)
            params.append(str(job_id))
        if event_type:
            conditions.append(EVENT_TYPE_S_SQL)
            params.append(str(event_type).strip())
        elif event_type_prefix:
            normalized_prefix = str(event_type_prefix).strip()
            conditions.append(EVENT_TYPE_S_OR_EVENT_TYPE_LIKE_S_SQL)
            params.extend((normalized_prefix, f"{normalized_prefix}.%"))
        if status:
            conditions.append(SQL_STATUS_EQ)
            params.append(str(status).strip().lower())
        if occurred_from is not None:
            conditions.append(OCCURRED_AT_S_VALUE_2)
            params.append(occurred_from)
        if occurred_to is not None:
            conditions.append(OCCURRED_AT_S_VALUE)
            params.append(occurred_to)
        where_sql = f"WHERE {SQL_AND.join(conditions)}" if conditions else ""
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    id,
                    event_id,
                    event_type,
                    occurred_at,
                    account_id,
                    project_id,
                    workflow_id,
                    job_id,
                    group_id,
                    for_each_id,
                    step_id,
                    step_execution_id,
                    severity,
                    summary,
                    payload_jsonb,
                    actor_jsonb,
                    urls_jsonb,
                    envelope_jsonb,
                    source,
                    aggregate_type,
                    aggregate_id,
                    correlation_id,
                    status,
                    dispatch_count,
                    last_dispatch_at,
                    created_at,
                    updated_at
                FROM domain_event_ledger
                {where_sql}
                ORDER BY occurred_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (*params, max(1, min(int(limit or 25), 500)), max(0, int(offset or 0))),
            )
            rows = cur.fetchall()
        return [self._normalize_domain_event_row(dict(row)) for row in rows]

    def count_domain_events(
        self,
        *,
        account_id: str | None = None,
        project_id: Any | None = None,
        workflow_id: str | None = None,
        job_id: Any | None = None,
        event_type: str | None = None,
        event_type_prefix: str | None = None,
        status: str | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
    ) -> int:
        if not self._table_exists("domain_event_ledger"):
            return 0
        conditions: list[str] = []
        params: list[Any] = []
        if account_id:
            conditions.append(SQL_ACCOUNT_ID_EQ)
            params.append(str(account_id).strip())
        if project_id is not None:
            conditions.append(SQL_PROJECT_ID_EQ)
            params.append(str(project_id))
        if workflow_id:
            conditions.append(SQL_WORKFLOW_ID_EQ)
            params.append(str(workflow_id).strip())
        if job_id is not None:
            conditions.append(SQL_JOB_ID_EQ)
            params.append(str(job_id))
        if event_type:
            conditions.append(EVENT_TYPE_S_SQL)
            params.append(str(event_type).strip())
        elif event_type_prefix:
            normalized_prefix = str(event_type_prefix).strip()
            conditions.append(EVENT_TYPE_S_OR_EVENT_TYPE_LIKE_S_SQL)
            params.extend((normalized_prefix, f"{normalized_prefix}.%"))
        if status:
            conditions.append(SQL_STATUS_EQ)
            params.append(str(status).strip().lower())
        if occurred_from is not None:
            conditions.append(OCCURRED_AT_S_VALUE_2)
            params.append(occurred_from)
        if occurred_to is not None:
            conditions.append(OCCURRED_AT_S_VALUE)
            params.append(occurred_to)
        where_sql = f"WHERE {SQL_AND.join(conditions)}" if conditions else ""
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM domain_event_ledger {where_sql}",
                tuple(params),
            )
            row = cur.fetchone()
            return int(row[0] if row else 0)

    def list_domain_event_facets(
        self,
        *,
        account_id: str | None = None,
        project_id: Any | None = None,
        workflow_id: str | None = None,
        event_type: str | None = None,
        event_type_prefix: str | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        if not self._table_exists("domain_event_ledger"):
            return []
        conditions: list[str] = []
        params: list[Any] = []
        if account_id:
            conditions.append(SQL_ACCOUNT_ID_EQ)
            params.append(str(account_id).strip())
        if project_id is not None:
            conditions.append(SQL_PROJECT_ID_EQ)
            params.append(str(project_id))
        if workflow_id:
            conditions.append(SQL_WORKFLOW_ID_EQ)
            params.append(str(workflow_id).strip())
        if event_type:
            conditions.append(EVENT_TYPE_S_SQL)
            params.append(str(event_type).strip())
        elif event_type_prefix:
            normalized_prefix = str(event_type_prefix).strip()
            conditions.append(EVENT_TYPE_S_OR_EVENT_TYPE_LIKE_S_SQL)
            params.extend((normalized_prefix, f"{normalized_prefix}.%"))
        if occurred_from is not None:
            conditions.append(OCCURRED_AT_S_VALUE_2)
            params.append(occurred_from)
        if occurred_to is not None:
            conditions.append(OCCURRED_AT_S_VALUE)
            params.append(occurred_to)
        where_sql = f"WHERE {SQL_AND.join(conditions)}" if conditions else ""
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    workflow_id,
                    event_type,
                    status,
                    COUNT(*)::int AS count
                FROM domain_event_ledger
                {where_sql}
                GROUP BY workflow_id, event_type, status
                ORDER BY count DESC, event_type ASC, workflow_id ASC
                LIMIT %s
                """,
                (*params, max(1, min(int(limit or 5000), 10000))),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows if isinstance(row, dict)]

    def get_domain_event(self, event_id: str, *, account_id: str | None = None) -> dict | None:
        normalized_event_id = str(event_id or "").strip()
        if not normalized_event_id or not self._table_exists("domain_event_ledger"):
            return None
        conditions = [SQL_EVENT_ID_EQ]
        params: list[Any] = [normalized_event_id]
        if account_id:
            conditions.append(SQL_ACCOUNT_ID_EQ)
            params.append(str(account_id).strip())
        where_sql = SQL_AND.join(conditions)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    id,
                    event_id,
                    event_type,
                    occurred_at,
                    account_id,
                    project_id,
                    workflow_id,
                    job_id,
                    group_id,
                    for_each_id,
                    step_id,
                    step_execution_id,
                    severity,
                    summary,
                    payload_jsonb,
                    actor_jsonb,
                    urls_jsonb,
                    envelope_jsonb,
                    source,
                    aggregate_type,
                    aggregate_id,
                    correlation_id,
                    status,
                    dispatch_count,
                    last_dispatch_at,
                    created_at,
                    updated_at
                FROM domain_event_ledger
                WHERE {where_sql}
                LIMIT 1
                """,
                tuple(params),
            )
            row = cur.fetchone()
        return self._normalize_domain_event_row(dict(row)) if row else None


__all__ = ["PlatformLoggerEventsDomainEventsMixin"]
