"""Reusable helpers for job decision override persistence."""

from __future__ import annotations

import json
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.platform_logger_common import _step_execution_id

SUPPORTED_OVERRIDE_DOMAINS = {
    "gate_outcome",
}


def normalize_override_domain(domain: str) -> str:
    normalized = str(domain or "").strip().lower()
    if normalized in SUPPORTED_OVERRIDE_DOMAINS:
        return normalized
    raise ValueError(f"Unsupported override domain '{domain}'")


def create_job_decision_override(
    job_id: str,
    *,
    conn,
    table_exists,
    normalize_domain=normalize_override_domain,
    domain: str,
    override_value,
    reason: str,
    original_value=None,
    created_by: str = "operator",
    active: bool = True,
    message_id: str | None = None,
    workflow_id: str | None = None,
    step_id: str | None = None,
    step_execution_id: str | None = None,
) -> dict[str, Any] | None:
    if not table_exists("job_decision_overrides"):
        return None
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise ValueError("Override reason is required")

    normalized_domain = normalize_domain(domain)
    clean_created_by = str(created_by or "operator").strip() or "operator"
    clean_workflow_id = str(workflow_id or "").strip() or None
    clean_step_id = str(step_id or "").strip() or None
    clean_step_execution_id = _step_execution_id(step_execution_id)
    clean_message_id = str(message_id or "").strip() or None
    payload_original = original_value if original_value is not None else {}
    payload_override = override_value if override_value is not None else {}

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        if active:
            cur.execute(
                """
                UPDATE job_decision_overrides
                   SET active = false
                 WHERE job_id = %s
                   AND domain = %s
                   AND active = true
                   AND message_id IS NOT DISTINCT FROM %s
                   AND workflow_id IS NOT DISTINCT FROM %s
                   AND step_id IS NOT DISTINCT FROM %s
                   AND step_execution_id IS NOT DISTINCT FROM %s
                """,
                (
                    job_id,
                    normalized_domain,
                    clean_message_id,
                    clean_workflow_id,
                    clean_step_id,
                    clean_step_execution_id,
                ),
            )
        cur.execute(
            """
            INSERT INTO job_decision_overrides (
                job_id,
                message_id,
                workflow_id,
                step_id,
                step_execution_id,
                domain,
                original_value,
                override_value,
                reason,
                created_by,
                active
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
            """,
            (
                job_id,
                clean_message_id,
                clean_workflow_id,
                clean_step_id,
                clean_step_execution_id,
                normalized_domain,
                json.dumps(payload_original),
                json.dumps(payload_override),
                clean_reason,
                clean_created_by,
                bool(active),
            ),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def list_job_decision_overrides(
    job_id: str,
    *,
    conn,
    table_exists,
    normalize_domain=normalize_override_domain,
    sql_job_id_eq: str,
    sql_and: str,
    active_only: bool = False,
    domain: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if not table_exists("job_decision_overrides"):
        return []

    where = [sql_job_id_eq]
    params: list[Any] = [job_id]
    if active_only:
        where.append("active = true")
    if domain:
        where.append("domain = %s")
        params.append(normalize_domain(domain))
    params.append(max(1, min(int(limit or 200), 2000)))

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT *
            FROM job_decision_overrides
            WHERE {sql_and.join(where)}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]
