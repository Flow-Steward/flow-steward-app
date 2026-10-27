"""Persistence helpers for extension-runtime project connection lookups."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _row_to_dict(row: Any, description: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        payload = dict(row)
    else:
        columns = [col[0] if hasattr(col, "__getitem__") else col.name for col in description or []]
        payload = dict(zip(columns, row, strict=False))

    for key, value in list(payload.items()):
        if isinstance(value, datetime):
            payload[key] = value.timestamp()
    return payload


def list_extension_runtime_connections(
    db: Any,
    *,
    account_id: str = "",
    install_id: str,
    connection_id: str = "",
    project_id: str | None = None,
    enabled_only: bool = True,
) -> list[dict[str, Any]]:
    normalized_install_id = _normalize_text(install_id)
    if not normalized_install_id:
        return []

    conditions: list[str] = [
        "connection_kind = 'extension_runtime'",
        "extension_install_id = %s",
    ]
    params: list[Any] = [normalized_install_id]
    normalized_account_id = _normalize_text(account_id)
    if normalized_account_id:
        conditions.insert(0, "account_id = %s")
        params.insert(0, normalized_account_id)
    normalized_connection_id = _normalize_text(connection_id)
    if normalized_connection_id:
        conditions.append("connection_id = %s")
        params.append(normalized_connection_id)
    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(str(project_id))
    if enabled_only:
        conditions.append("enabled = true")

    list_query = "\n".join(
        [
            "SELECT connection_id, project_id, extension_id, account_id,",
            "       display_name, connection_kind, connection_type, environment,",
            "       credential_revision, extension_install_id, runtime_mode, remote_base_url,",
            "       health_status, runtime_status, last_seen_ok_at, last_error,",
            "       enabled, connection_config, secret_refs, connection_routes,",
            "       ingress_config, workflow_mappings, tags, created_at, updated_at",
            "FROM project_connections",
            "WHERE ",
            " AND ".join(conditions),
            "ORDER BY created_at DESC",
        ]
    )

    with db.cursor() as cur:
        cur.execute(list_query, tuple(params))
        rows = cur.fetchall() or []
        description = cur.description
    return [_row_to_dict(row, description) for row in rows]


def list_extension_runtime_connection_counts(
    db: Any,
    *,
    account_id: str,
    install_ids: list[str],
    enabled_only: bool = True,
) -> dict[str, int]:
    normalized_install_ids = [
        _normalize_text(item) for item in install_ids if _normalize_text(item)
    ]
    if not normalized_install_ids:
        return {}

    placeholders = ", ".join(["%s"] * len(normalized_install_ids))
    conditions = [
        "account_id = %s",
        "connection_kind = 'extension_runtime'",
        f"extension_install_id IN ({placeholders})",
    ]
    params: list[Any] = [_normalize_text(account_id), *normalized_install_ids]
    if enabled_only:
        conditions.append("enabled = true")

    count_query = "\n".join(
        [
            "SELECT extension_install_id, COUNT(*) AS connection_count",
            "FROM project_connections",
            "WHERE ",
            " AND ".join(conditions),
            "GROUP BY extension_install_id",
        ]
    )

    with db.cursor() as cur:
        cur.execute(count_query, tuple(params))
        rows = cur.fetchall() or []
        description = cur.description

    counts: dict[str, int] = {}
    for row in rows:
        payload = _row_to_dict(row, description)
        install_id = _normalize_text(payload.get("extension_install_id"))
        if install_id:
            counts[install_id] = int(payload.get("connection_count") or 0)
    return counts
