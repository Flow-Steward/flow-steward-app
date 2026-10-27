from __future__ import annotations

import contextlib
from typing import Any

from core.infrastructure.config.runtime_config import CONFIG
from core.infrastructure.db.platform_stores import (
    PlatformConnectionStore,
    resolve_observability_dsn,
)


def _observability_dsn() -> str:
    return resolve_observability_dsn(
        CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or ""
    )


def _column_exists(bl: Any, table_name: str, column_name: str) -> bool:
    checker = getattr(bl, "_column_exists", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(str(table_name or "").strip(), str(column_name or "").strip()))
    except Exception:
        return False


def list_recent_runs_rows(
    *,
    project_name: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    dsn = _observability_dsn()
    if not dsn:
        raise RuntimeError("Database not configured")
    bl = PlatformConnectionStore(dsn)
    try:
        with bl.conn.cursor() as cur:
            query = (
                "SELECT j.id, p.name AS project_name, j.task_id, j.task_summary, "
                "j.status, j.started_at, j.finished_at "
                "FROM jobs j JOIN projects p ON p.id = j.project_id"
            )
            conditions: list[str] = []
            params: list[object] = []
            if _column_exists(bl, "jobs", "deleted_at"):
                conditions.append("j.deleted_at IS NULL")
            if _column_exists(bl, "projects", "deleted_at"):
                conditions.append("p.deleted_at IS NULL")
            if project_name:
                conditions.append("p.name = %s")
                params.append(project_name)
            if status:
                conditions.append("j.status = %s")
                params.append(status)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY j.started_at DESC LIMIT %s"
            params.append(max(1, min(int(limit or 20), 200)))
            cur.execute(query, params)
            cols = [item[0] for item in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        with contextlib.suppress(Exception):
            bl.conn.close()
