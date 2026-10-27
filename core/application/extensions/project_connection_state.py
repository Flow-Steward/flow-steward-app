"""State a partial project-connection write must carry across unchanged.

`upsert_project_connection` overwrites every column with what it is given, so a
write that means to change one partition of a connection has to hand back all
the others explicitly. Omitting a field here is how saving a token would erase
the operator's channels, or saving an inbound setting would drop a secret ref.
"""

from __future__ import annotations

import copy
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def nested_get(payload: dict[str, Any], path: str, default: Any = None) -> Any:
    parts = [segment for segment in str(path or "").split(".") if segment]
    if not parts:
        return default
    cursor: Any = payload
    for part in parts:
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor.get(part)
    return cursor


def nested_set(payload: dict[str, Any], path: str, value: Any) -> None:
    parts = [segment for segment in str(path or "").split(".") if segment]
    if not parts:
        return
    cursor = payload
    for part in parts[:-1]:
        next_value = cursor.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[part] = next_value
        cursor = next_value
    cursor[parts[-1]] = value


def carried_connection_fields(
    existing: dict[str, Any] | None,
    *,
    extension_install_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Every upsert argument, as the row currently holds it.

    A caller replaces only the partition it owns on top of this. A row that does
    not exist yet is carried as a disabled connection: only a successful
    project-bound secret save makes a connection usable.
    """
    row = _as_dict(existing)
    return {
        "connection_id": _text(row.get("connection_id")),
        "account_id": _text(account_id) or _text(row.get("account_id")) or None,
        "connection_kind": _text(row.get("connection_kind")) or "extension",
        "display_name": _text(row.get("display_name")),
        "connection_type": _text(row.get("connection_type")),
        "environment": _text(row.get("environment")),
        "credential_revision": row.get("credential_revision"),
        "extension_install_id": _text(extension_install_id)
        or _text(row.get("extension_install_id")),
        "runtime_mode": _text(row.get("runtime_mode")),
        "remote_base_url": _text(row.get("remote_base_url")),
        "health_status": _text(row.get("health_status")),
        "runtime_status": _text(row.get("runtime_status")),
        "last_seen_ok_at": row.get("last_seen_ok_at"),
        "last_error": _text(row.get("last_error")),
        "enabled": bool(row.get("enabled", False)),
        "connection_config": copy.deepcopy(_as_dict(row.get("connection_config"))),
        "secret_refs": copy.deepcopy(_as_dict(row.get("secret_refs"))),
        "connection_routes": copy.deepcopy(_as_list(row.get("connection_routes"))),
        "ingress_config": copy.deepcopy(_as_dict(row.get("ingress_config"))),
        "workflow_mappings": copy.deepcopy(_as_dict(row.get("workflow_mappings"))),
        "tags": [_text(tag) for tag in _as_list(row.get("tags")) if _text(tag)],
    }


__all__ = ["carried_connection_fields", "nested_get", "nested_set"]
