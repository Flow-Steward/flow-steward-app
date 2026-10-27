from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.psycopg_runtime import sql as psycopg_sql

from .search_timeout import (
    SearchQueryTimeoutError,
    is_postgres_query_cancellation,
    remaining_search_timeout_ms,
)

J_PROJECT_ID_VALUE = "j.project_id"
P_ACCOUNT_ID_S_SQL = "p.account_id = %s"
VALUE_AND_SQL = " AND "


logger = logging.getLogger(__name__)

_EVENT_DETAIL_TEXT_SQL = """
COALESCE(
    NULLIF(BTRIM(e.detail->>'message'), ''),
    NULLIF(BTRIM(e.detail->>'error'), ''),
    NULLIF(BTRIM(e.detail->>'reason'), ''),
    NULLIF(BTRIM(e.detail->>'summary'), ''),
    NULLIF(BTRIM(e.detail->>'code'), ''),
    ''
)
"""


def _trim(value: Any, *, limit: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _isoformat(value: Any) -> str | None:
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    text = str(value or "").strip()
    return text or None


def _like_pattern(value: str) -> str:
    escaped = str(value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _project_scope_sql(column: str, project_ids: Sequence[str] | None) -> tuple[str, list[Any]]:
    if project_ids is None:
        return "", []
    normalized = [str(project_id).strip() for project_id in project_ids if str(project_id).strip()]
    if not normalized:
        return " AND 1 = 0", []
    placeholders = ", ".join(["%s"] * len(normalized))
    return f" AND {column} = ANY(ARRAY[{placeholders}]::uuid[])", normalized


def _sql_fragment(fragment: str) -> psycopg_sql.SQL:
    return psycopg_sql.SQL(fragment)


def _sql_join(fragments: Sequence[str], *, separator: str) -> psycopg_sql.Composable:
    return psycopg_sql.SQL(separator).join(
        _sql_fragment(fragment.strip()) for fragment in fragments if fragment.strip()
    )


def _render_sql(query: str | psycopg_sql.Composable) -> str:
    if isinstance(query, str):
        return query
    obj = getattr(query, "_obj", None)
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return "".join(_render_sql(part) for part in obj)
    wrapped = getattr(query, "_wrapped", None)
    if isinstance(wrapped, str):
        return wrapped
    if isinstance(wrapped, list):
        return "".join(_render_sql(part) for part in wrapped)
    return str(query)


def _execute_rows(
    conn: Any,
    sql: str | psycopg_sql.Composable,
    params: Sequence[Any],
) -> list[dict[str, Any]]:
    rendered_sql = _render_sql(sql)
    timeout_ms = remaining_search_timeout_ms()
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        if timeout_ms is not None:
            cur.execute(f"SET statement_timeout = {timeout_ms}")
        try:
            cur.execute(rendered_sql, tuple(params))
            return [dict(row) for row in cur.fetchall()]
        except Exception as exc:
            if is_postgres_query_cancellation(exc):
                raise SearchQueryTimeoutError from exc
            raise
        finally:
            if timeout_ms is not None:
                with suppress(Exception):
                    cur.execute("SET statement_timeout = 0")


def _execute_total(
    conn: Any,
    sql: str | psycopg_sql.Composable,
    params: Sequence[Any],
) -> int:
    rows = _execute_rows(conn=conn, sql=sql, params=params)
    if not rows:
        return 0
    try:
        return max(0, int((rows[0] or {}).get("total") or 0))
    except Exception:
        return 0


def _rollback_quietly(conn: Any) -> None:
    with suppress(Exception):
        conn.rollback()


def _column_exists(bl: Any, table: str, column: str) -> bool:
    checker = getattr(bl, "_column_exists", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(table, column))
    except Exception:
        return False


def _append_soft_delete_conditions(
    *,
    conditions: list[str],
    filters: Mapping[str, Any],
) -> None:
    if bool(filters.get("_jobs_has_deleted_at")):
        conditions.append("j.deleted_at IS NULL")
    if bool(filters.get("_projects_has_deleted_at")):
        conditions.append("p.deleted_at IS NULL")


def _append_tenant_conditions(
    *,
    conditions: list[str],
    params: list[Any],
    filters: Mapping[str, Any],
) -> None:
    tenant_id = str(filters.get("tenant_id") or "").strip() or None
    if not tenant_id:
        return
    if bool(filters.get("_jobs_has_tenant_id")):
        conditions.append("j.tenant_id = %s")
        params.append(tenant_id)
        return
    if bool(filters.get("_projects_has_tenant_id")):
        conditions.append("p.tenant_id = %s")
        params.append(tenant_id)
