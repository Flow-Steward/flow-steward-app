"""Allocate dataset and connection identifiers against the owning store."""

from __future__ import annotations

import re
import uuid

_DATASET_ID_SAFE_CHARS_PATTERN = re.compile(r"[^a-z0-9_.-]+")
_CONNECTION_ID_SAFE_CHARS_PATTERN = re.compile(r"[^a-z0-9_.-]+")


def _dataset_id_from_display_name(display_name: str) -> str:
    base = _DATASET_ID_SAFE_CHARS_PATTERN.sub("_", display_name.strip().lower())
    base = base.strip("_.-")
    if not base:
        base = "dataset"
    if not base[0].isalpha():
        base = f"dataset_{base}"
    return base[:80].strip("_.-") or "dataset"


def _generate_dataset_id(db, display_name: str) -> str:
    base = _dataset_id_from_display_name(display_name)
    candidate = base
    suffix = 2
    while db.get_dataset(candidate, include_trashed=True):
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _connection_id_from_display_name(display_name: str) -> str:
    base = _CONNECTION_ID_SAFE_CHARS_PATTERN.sub("_", display_name.strip().lower())
    base = base.strip("_.-") or "connection"
    if not base[0].isalpha():
        base = f"connection_{base}"
    return f"conn_{base[:72].strip('_.-') or 'connection'}"


def _generate_connection_id(db, display_name: str) -> str:
    if _workspace_data_connection_id_uses_uuid(db):
        return str(uuid.uuid4())
    base = _connection_id_from_display_name(display_name)
    get_connection = getattr(db, "get_workspace_data_connection", None)
    if not callable(get_connection):
        return f"{base}_{uuid.uuid4().hex[:8]}"
    candidate = base
    suffix = 2
    while get_connection(candidate, include_trashed=True):
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _workspace_data_connection_id_uses_uuid(db) -> bool:
    cursor_factory = getattr(db, "_cursor", None)
    if not callable(cursor_factory):
        return False
    with cursor_factory() as cur:
        cur.execute(
            """
            SELECT data_type, udt_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'workspace_data_connections'
              AND column_name = 'connection_id'
            """
        )
        row = cur.fetchone()
    if not row:
        return False
    if isinstance(row, dict):
        data_type = str(row.get("data_type") or "").strip().lower()
        udt_name = str(row.get("udt_name") or "").strip().lower()
    else:
        data_type = str(row[0] or "").strip().lower()
        udt_name = str(row[1] or "").strip().lower()
    return data_type == "uuid" or udt_name == "uuid"


def _query_template_id() -> str:
    return str(uuid.uuid4())
