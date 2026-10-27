"""First-party data-connection actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.postgres_schema_introspection import introspect_postgres_schema

SUPPORTED_PLATFORM_POSTGRES_ACTIONS = frozenset(
    {"test_connection", "validate_connection", "introspect_schema", "preview", "infer"}
)


def _failed(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "result": {}, "errors": [{"code": code, "message": message}]}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _values(secret_refs: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = dict(secret_refs or {})
    values = payload.get("values")
    return dict(values) if isinstance(values, Mapping) else {}


def _postgres_dsn(
    connection: Mapping[str, Any], secret_refs: Mapping[str, Any] | None
) -> tuple[str, str]:
    config = connection.get("connection_config_jsonb") or connection.get("connection_config") or {}
    config = dict(config) if isinstance(config, Mapping) else {}
    values = _values(secret_refs)
    host = _text(config.get("host"))
    database = _text(config.get("database") or config.get("dbname"))
    user = _text(config.get("user") or config.get("username"))
    password = _text(values.get("password") or config.get("password"))
    if not host or not database or not user or not password:
        return "", _text(config.get("schema") or config.get("db_schema") or "public")
    port = _text(config.get("port") or "5432")
    sslmode = _text(config.get("sslmode"))
    authority = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    query = f"?sslmode={quote(sslmode, safe='')}" if sslmode else ""
    return f"postgresql://{authority}/{quote(database, safe='')}{query}", _text(
        config.get("schema") or config.get("db_schema") or "public"
    )


def _connection_failure(exc: Exception) -> dict[str, Any]:
    text = str(exc).lower()
    if "password authentication" in text or "authentication failed" in text:
        return _failed("connection_authentication_failed", "Database authentication failed.")
    return _failed("connection_failed", "Unable to connect to the database.")


def _snapshot_from_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    schema_name = _text(schema.get("schema") or "public")
    resources = []
    for table in schema.get("tables") or []:
        if not isinstance(table, Mapping):
            continue
        name = _text(table.get("name"))
        if not name:
            continue
        resources.append(
            {
                "resource_id": f"{schema_name}.{name}",
                "columns": [
                    dict(column)
                    for column in table.get("columns") or []
                    if isinstance(column, Mapping)
                ],
                "primary_key": list(table.get("primary_key") or []),
                "indexes": [
                    dict(index)
                    for index in table.get("indexes") or []
                    if isinstance(index, Mapping)
                ],
            }
        )
    return {"resources": resources, "metadata": {"engine": "postgres"}}


def run_platform_connection_action(
    connection: Mapping[str, Any],
    action_id: str,
    *,
    secret_refs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if action_id not in SUPPORTED_PLATFORM_POSTGRES_ACTIONS:
        return _failed("unsupported_connection_action", "Connection action is not supported.")
    dsn, schema_name = _postgres_dsn(connection, secret_refs)
    if not dsn:
        return _failed(
            "connection_configuration_invalid", "Database connection configuration is incomplete."
        )
    try:
        if action_id in {"test_connection", "validate_connection"}:
            with psycopg_runtime.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return {"ok": True, "result": {"connected": True}, "errors": []}
        snapshot = _snapshot_from_schema(introspect_postgres_schema(dsn, schema_name=schema_name))
        if action_id == "preview":
            return {"ok": True, "result": {"preview": snapshot}, "errors": []}
        if action_id == "infer":
            return {
                "ok": True,
                "result": {"inference": {"schema_snapshot": snapshot}},
                "errors": [],
            }
        return {"ok": True, "result": {"schema_snapshot": snapshot}, "errors": []}
    except Exception as exc:
        return _connection_failure(exc)
