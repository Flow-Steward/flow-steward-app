"""Shared schema inspection helpers for Flow Steward observability storage."""

from __future__ import annotations


def table_exists(*, conn, table_name: str) -> bool:
    normalized_table = str(table_name or "").strip()
    if not normalized_table:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{normalized_table}",))
        row = cur.fetchone()
        return bool(row and row[0])


def column_exists(
    *,
    conn,
    cache: dict[tuple[str, str], bool],
    table_name: str,
    column_name: str,
) -> bool:
    table = str(table_name or "").strip()
    column = str(column_name or "").strip()
    if not table or not column:
        return False

    key = (table, column)
    if key in cache:
        return bool(cache[key])

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                  AND column_name = %s
            )
            """,
            (table, column),
        )
        row = cur.fetchone()
        exists = bool(row and row[0])
        cache[key] = exists
        return exists
