"""Low-level CRUD helpers for persisted report artifacts."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from core.infrastructure.db import psycopg_runtime as psycopg_runtime

logger = logging.getLogger(__name__)


def _tenant_id(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("tenant_id is required")
    return normalized


def create_report_run(
    *,
    conn,
    account_id: str,
    tenant_id: str | None,
    project_id: str | None,
    report_type: str,
    params: dict[str, Any] | None = None,
    summary_text: str = "",
    payload: dict[str, Any] | None = None,
    chart_payload: dict[str, Any] | None = None,
    status: str = "generated",
    generated_by: str = "",
) -> dict[str, Any]:
    resolved_tenant_id = _tenant_id(tenant_id)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO report_runs
                (account_id, tenant_id, project_id,
                 report_type, params_jsonb, summary_text, payload_jsonb, chart_payload_jsonb,
                 status, generated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                account_id,
                resolved_tenant_id,
                project_id,
                report_type,
                json.dumps(params or {}),
                summary_text,
                json.dumps(payload or {}),
                json.dumps(chart_payload or {}),
                status,
                generated_by,
            ),
        )
        row = cur.fetchone()
    return dict(row or {})


def get_report_run(
    *,
    conn,
    report_id: str | UUID,
    account_id: str,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    try:
        resolved_tenant_id = _tenant_id(tenant_id)
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM report_runs
                WHERE id = %s
                  AND account_id = %s
                  AND tenant_id = %s
                LIMIT 1
                """,
                (str(report_id), account_id, resolved_tenant_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.debug("get_report_run failed: %s", exc)
        return None


def list_report_runs(
    *,
    conn,
    account_id: str,
    tenant_id: str | None = None,
    report_type: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    try:
        resolved_tenant_id = _tenant_id(tenant_id)
        where = ["account_id = %s", "tenant_id = %s"]
        params: list[Any] = [account_id, resolved_tenant_id]
        if report_type is not None:
            where.append("report_type = %s")
            params.append(report_type)
        if project_id is not None:
            where.append("project_id = %s")
            params.append(project_id)
        params.append(max(1, min(int(limit or 50), 500)))
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT *
                FROM report_runs
                WHERE {" AND ".join(where)}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                tuple(params),
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        logger.debug("list_report_runs failed: %s", exc)
        return []
