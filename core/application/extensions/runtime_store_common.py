"""Shared helpers for extension runtime store adapters."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from core.infrastructure.db.psycopg_runtime import sql as psycopg_sql

ACCOUNT_ID_S_SQL = "account_id = %s"


def _row_to_dict(row: Any, description: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    cols = [col[0] if hasattr(col, "__getitem__") else col.name for col in description]
    return dict(zip(cols, row, strict=False))


def _to_timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return time.time()


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return normalized


def _compose_where_clause(conditions: list[Any]) -> Any:
    if not conditions:
        return psycopg_sql.SQL("")
    return psycopg_sql.SQL("WHERE ") + psycopg_sql.SQL(" AND ").join(conditions)


def _placeholders(count: int) -> Any:
    return psycopg_sql.SQL(", ").join(psycopg_sql.Placeholder() for _ in range(count))
