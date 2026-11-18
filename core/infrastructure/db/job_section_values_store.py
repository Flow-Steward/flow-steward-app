"""One batch read of the values a section's history table shows (ADR 0075)."""

from __future__ import annotations

from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime

_READ_SQL = """
SELECT j.id::text AS job_id,
       -- The accepted request's bounded, display-safe projection; older jobs kept
       -- their inputs in meta directly.
       COALESCE(j.meta -> 'invocation' -> 'mapped_inputs', j.meta -> 'inputs') AS inputs,
       j.meta -> 'workflow_outputs' -> 'outputs' AS outputs,
       j.meta -> 'submitted_by' AS submitted_by
  FROM jobs j
  JOIN projects p ON p.id = j.project_id
 WHERE j.id::text = ANY(%s)
   AND p.account_id = %s
"""


def read_job_section_payloads(
    conn: Any, *, job_ids: list[str], account_id: str
) -> dict[str, dict[str, Any]]:
    """Accepted inputs, final outputs and author of one page of jobs, in one query.

    The page was already selected under the caller's scope; the account check
    keeps this read from reaching past it even if called with foreign ids.
    """
    ids = sorted({str(job_id) for job_id in job_ids if str(job_id or "").strip()})
    if not ids:
        return {}
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(_READ_SQL, (ids, str(account_id)))
        return {
            str(row["job_id"]): {
                "inputs": row.get("inputs") if isinstance(row.get("inputs"), dict) else {},
                "outputs": row.get("outputs") if isinstance(row.get("outputs"), dict) else {},
                "submitted_by": row.get("submitted_by")
                if isinstance(row.get("submitted_by"), dict)
                else None,
            }
            for row in cur.fetchall()
        }


__all__ = ["read_job_section_payloads"]
