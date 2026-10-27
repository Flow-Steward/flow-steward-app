"""PostgreSQL schema introspection adapter."""

from __future__ import annotations

from typing import Any

from core.infrastructure.db import psycopg_runtime


def introspect_postgres_schema(dsn: str, schema_name: str = "public") -> dict[str, Any]:
    conn = psycopg_runtime.connect(dsn)
    try:
        tables_payload: list[dict[str, Any]] = []
        foreign_keys_payload: list[dict[str, Any]] = []
        result = {
            "db_type": "postgres",
            "schema": schema_name,
            "tables": tables_payload,
            "foreign_keys": foreign_keys_payload,
        }
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """,
                (schema_name,),
            )
            tables = [row["table_name"] for row in cur.fetchall()]

            for table in tables:
                cur.execute(
                    """
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (schema_name, table),
                )
                columns = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.table_schema = %s
                      AND tc.table_name = %s
                      AND tc.constraint_type = 'PRIMARY KEY'
                    ORDER BY kcu.ordinal_position
                    """,
                    (schema_name, table),
                )
                primary_key = [row["column_name"] for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname = %s AND tablename = %s
                    ORDER BY indexname
                    """,
                    (schema_name, table),
                )
                indexes = [dict(row) for row in cur.fetchall()]

                tables_payload.append(
                    {
                        "name": table,
                        "columns": columns,
                        "primary_key": primary_key,
                        "indexes": indexes,
                    }
                )

            cur.execute(
                """
                SELECT
                  tc.table_name AS source_table,
                  kcu.column_name AS source_column,
                  ccu.table_name AS target_table,
                  ccu.column_name AS target_column,
                  tc.constraint_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                  ON ccu.constraint_name = tc.constraint_name
                 AND ccu.table_schema = tc.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = %s
                ORDER BY tc.table_name, kcu.column_name
                """,
                (schema_name,),
            )
            foreign_keys_payload[:] = [dict(row) for row in cur.fetchall()]

        return result
    finally:
        conn.close()
