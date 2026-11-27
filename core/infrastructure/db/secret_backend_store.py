"""Read the remote locator of one stored-secret reference (ADR 0066 amendment 2026-11-26).

`secret_backend_locators` maps an account-qualified reference to the secret backend
configuration that keeps its value and the backend's own locator. The stored-secret
reader asks here before it reads; the admin service in ``core.application.secrets``
writes the rows.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

_LOCATOR_COLUMNS = ("backend_kind", "config_id", "locator", "config", "enabled")


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (str, bytes, bytearray)) and value:
        decoded = json.loads(value)
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def get_secret_locator(conn: Any, *, account_id: str, ref_key: str) -> dict[str, Any] | None:
    """The backend configuration and locator of `ref_key` in `account_id`; ``None`` when local."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.backend_kind, l.backend_config_id::text, l.locator,
                   c.non_secret_config, c.enabled
              FROM secret_backend_locators AS l
              JOIN secret_backend_configs AS c
                ON c.id = l.backend_config_id AND c.account_id = l.account_id
             WHERE l.account_id = %s AND l.ref_key = %s
            """,
            (str(account_id), str(ref_key)),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        values = list(row.values())
    elif isinstance(row, (tuple, list)):
        values = list(row)
    else:
        return None
    if len(values) < len(_LOCATOR_COLUMNS):
        return None
    record = dict(zip(_LOCATOR_COLUMNS, values, strict=False))
    return {
        "backend_kind": str(record["backend_kind"] or ""),
        "config_id": str(record["config_id"] or ""),
        "locator": _json_object(record["locator"]),
        "config": _json_object(record["config"]),
        "enabled": bool(record["enabled"]),
    }


__all__ = ["get_secret_locator"]
