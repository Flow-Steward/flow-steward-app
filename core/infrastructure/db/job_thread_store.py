"""Reusable helpers for jobs addressed by LangGraph ``thread_id``."""

from __future__ import annotations

import json
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime


def get_job_context_by_thread_id(conn, thread_id: str) -> dict[str, Any] | None:
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, project_id, meta FROM jobs WHERE thread_id = %s",
            (str(thread_id or "").strip(),),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_job_meta_by_thread_id(conn, thread_id: str) -> dict[str, Any] | None:
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT meta FROM jobs WHERE thread_id = %s",
            (str(thread_id or "").strip(),),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def update_job_status_by_thread_id(
    conn,
    *,
    thread_id: str,
    status: str,
    commit: bool = False,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = %s WHERE thread_id = %s",
            (str(status or "").strip(), str(thread_id or "").strip()),
        )
        rowcount = int(cur.rowcount or 0)
    if commit:
        conn.commit()
    return rowcount


def update_job_meta_by_thread_id(
    conn,
    *,
    thread_id: str,
    meta: Any,
    commit: bool = False,
) -> int:
    payload = meta
    if not isinstance(payload, str):
        payload = json.dumps(payload)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET meta = %s WHERE thread_id = %s",
            (payload, str(thread_id or "").strip()),
        )
        rowcount = int(cur.rowcount or 0)
    if commit:
        conn.commit()
    return rowcount
