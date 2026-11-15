"""Locked configuration-only updates of an existing project connection."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.psycopg_runtime import sql

JsonEquality = Callable[[tuple[str, ...], Any], bool]
ConfigTransform = Callable[[dict[str, Any], JsonEquality], dict[str, Any]]


def _patch_expression(assignments: dict[str, Any]) -> tuple[Any, list[Any]]:
    expression: Any = sql.SQL("connection_config")
    parameters: list[Any] = []
    created_parents: set[tuple[str, ...]] = set()
    for path, value in assignments.items():
        parts = tuple(path.split("."))
        for depth in range(1, len(parts)):
            parent = parts[:depth]
            if parent in created_parents:
                continue
            created_parents.add(parent)
            expression = sql.SQL(
                "jsonb_set({}, %s::text[], "
                "COALESCE(connection_config #> %s::text[], '{{}}'::jsonb), true)"
            ).format(expression)
            parameters.extend([list(parent), list(parent)])
        expression = sql.SQL("jsonb_set({}, %s::text[], %s::jsonb, true)").format(expression)
        parameters.extend([list(parts), json.dumps(value, allow_nan=False)])
    return expression, parameters


def mutate_locked_connection_config(
    conn: Any,
    *,
    connection_id: str,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str,
    transform: ConfigTransform,
) -> bool:
    """Transform current config on the caller's transaction-pinned connection.

    The caller validates the patched view and returns path/value assignments.
    Equality and persistence use the original JSONB, avoiding a Python numeric
    round trip for unassigned values. The caller owns the transaction.
    """
    scope = (connection_id, account_id, project_id, extension_id, extension_install_id)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT connection_config, connection_type, ingress_config
              FROM project_connections
             WHERE connection_id = %s AND account_id = %s AND project_id = %s
               AND extension_id = %s AND extension_install_id = %s
             FOR UPDATE
            """,
            scope,
        )
        existing = cur.fetchone()
        if existing is None:
            return False

        def jsonb_equal(path: tuple[str, ...], right: Any) -> bool:
            cur.execute(
                """
                SELECT (connection_config #> %s::text[]) = %s::jsonb AS matches
                  FROM project_connections
                 WHERE connection_id = %s AND account_id = %s AND project_id = %s
                   AND extension_id = %s AND extension_install_id = %s
                """,
                (list(path), json.dumps(right, allow_nan=False), *scope),
            )
            return bool(cur.fetchone()["matches"])

        assignments = transform(dict(existing), jsonb_equal)
        expression, parameters = _patch_expression(assignments)
        cur.execute(
            sql.SQL("""
            UPDATE project_connections
               SET connection_config = {}, updated_at = now()
             WHERE connection_id = %s AND account_id = %s AND project_id = %s
               AND extension_id = %s AND extension_install_id = %s
             RETURNING connection_id
            """).format(expression),
            (*parameters, *scope),
        )
        return cur.fetchone() is not None
