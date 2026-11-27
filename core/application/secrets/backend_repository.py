"""SQL for secret backend configurations and remote locators (ADR 0066 amendment 2026-11-26).

Every function takes a cursor from the caller's transaction and is scoped by
account. Values never pass through here; a configuration holds non-secret settings
only and a locator names where a backend keeps one value.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from core.application.secrets.repository import _row, _rows

CONFIG_COLUMNS = (
    "id",
    "account_id",
    "backend_kind",
    "name",
    "non_secret_config",
    "enabled",
    "created_at",
    "created_by",
    "updated_at",
    "updated_by",
)
_CONFIG_SELECT = """
    SELECT c.id::text, c.account_id::text, c.backend_kind, c.name, c.non_secret_config,
           c.enabled, c.created_at, c.created_by, c.updated_at, c.updated_by
      FROM secret_backend_configs AS c
"""
LOCATOR_COLUMNS = ("ref_key", "config_id", "backend_kind", "locator", "secret_id")


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (str, bytes, bytearray)) and value:
        decoded = json.loads(value)
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _config(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row or not row.get("id"):
        return None
    row["non_secret_config"] = _json_object(row.get("non_secret_config"))
    row["enabled"] = bool(row.get("enabled"))
    return row


def list_configs(cur: Any, *, account_id: str) -> list[dict[str, Any]]:
    cur.execute(
        _CONFIG_SELECT + " WHERE c.account_id::text = %s ORDER BY c.name, c.id",
        (str(account_id),),
    )
    return [row for row in (_config(item) for item in _rows(cur, CONFIG_COLUMNS)) if row]


def get_config(
    cur: Any, *, account_id: str, config_id: str, for_update: bool = False
) -> dict[str, Any] | None:
    cur.execute(
        _CONFIG_SELECT
        + " WHERE c.id::text = %s AND c.account_id::text = %s"
        + (" FOR UPDATE" if for_update else ""),
        (str(config_id), str(account_id)),
    )
    return _config(_row(cur, CONFIG_COLUMNS))


def insert_config(
    cur: Any,
    *,
    account_id: str,
    backend_kind: str,
    name: str,
    non_secret_config: dict[str, Any],
    enabled: bool,
    actor: str,
) -> dict[str, Any]:
    cur.execute(
        """
        INSERT INTO secret_backend_configs AS c (
            id, account_id, backend_kind, name, non_secret_config, enabled,
            created_by, updated_by
        ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
        RETURNING c.id::text, c.account_id::text, c.backend_kind, c.name,
                  c.non_secret_config, c.enabled, c.created_at, c.created_by,
                  c.updated_at, c.updated_by
        """,
        (
            str(uuid.uuid4()),
            str(account_id),
            backend_kind,
            name,
            json.dumps(non_secret_config, sort_keys=True),
            bool(enabled),
            actor,
            actor,
        ),
    )
    row = _config(_row(cur, CONFIG_COLUMNS))
    assert row is not None
    return row


def update_config(
    cur: Any,
    *,
    account_id: str,
    config_id: str,
    name: str,
    non_secret_config: dict[str, Any],
    enabled: bool,
    actor: str,
) -> dict[str, Any] | None:
    cur.execute(
        """
        UPDATE secret_backend_configs AS c
           SET name = %s, non_secret_config = %s::jsonb, enabled = %s,
               updated_at = now(), updated_by = %s
         WHERE c.id::text = %s AND c.account_id::text = %s
        RETURNING c.id::text, c.account_id::text, c.backend_kind, c.name,
                  c.non_secret_config, c.enabled, c.created_at, c.created_by,
                  c.updated_at, c.updated_by
        """,
        (
            name,
            json.dumps(non_secret_config, sort_keys=True),
            bool(enabled),
            actor,
            str(config_id),
            str(account_id),
        ),
    )
    return _config(_row(cur, CONFIG_COLUMNS))


def get_locator(cur: Any, *, account_id: str, ref_key: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT l.ref_key, l.backend_config_id::text, l.backend_kind, l.locator, NULL::text
          FROM secret_backend_locators AS l
         WHERE l.account_id::text = %s AND l.ref_key = %s
        """,
        (str(account_id), str(ref_key)),
    )
    row = _row(cur, LOCATOR_COLUMNS)
    if not row or not row.get("config_id"):
        return None
    row["locator"] = _json_object(row.get("locator"))
    return row


def list_catalog_locators(cur: Any, *, account_id: str) -> list[dict[str, Any]]:
    """The catalog secrets of the account whose values a backend keeps."""
    cur.execute(
        """
        SELECT l.ref_key, l.backend_config_id::text, l.backend_kind, l.locator, s.id::text
          FROM secret_backend_locators AS l
          JOIN catalog_secrets AS s
            ON s.account_id = l.account_id AND s.storage_ref_key = l.ref_key
         WHERE l.account_id::text = %s
         ORDER BY s.name, s.id
        """,
        (str(account_id),),
    )
    rows = _rows(cur, LOCATOR_COLUMNS)
    for row in rows:
        row["locator"] = _json_object(row.get("locator"))
    return rows


def insert_locator(
    cur: Any,
    *,
    account_id: str,
    ref_key: str,
    config_id: str,
    backend_kind: str,
    locator: dict[str, Any],
    actor: str,
) -> None:
    cur.execute(
        """
        INSERT INTO secret_backend_locators (
            account_id, ref_key, backend_config_id, backend_kind, locator, created_by
        ) VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        """,
        (
            str(account_id),
            ref_key,
            str(config_id),
            backend_kind,
            json.dumps(locator, sort_keys=True),
            actor,
        ),
    )


def locator_holder(
    cur: Any,
    *,
    account_id: str,
    config_id: str,
    locator: dict[str, Any],
    exclude_ref_key: str | None = None,
) -> str | None:
    """The reference already kept at ``locator`` of ``config_id``, if any.

    A stored locator that contains every key of ``locator`` (a backend may have added
    a version to it) names the same remote secret.
    """
    cur.execute(
        """
        SELECT l.ref_key
          FROM secret_backend_locators AS l
         WHERE l.account_id::text = %s
           AND l.backend_config_id::text = %s
           AND l.locator @> %s::jsonb
           AND (%s::text IS NULL OR l.ref_key <> %s::text)
         LIMIT 1
        """,
        (
            str(account_id),
            str(config_id),
            json.dumps(locator, sort_keys=True),
            exclude_ref_key,
            exclude_ref_key,
        ),
    )
    row = cur.fetchone()
    if not row:
        return None
    return str(row["ref_key"] if isinstance(row, dict) else row[0])


def update_locator(cur: Any, *, account_id: str, ref_key: str, locator: dict[str, Any]) -> None:
    cur.execute(
        """
        UPDATE secret_backend_locators
           SET locator = %s::jsonb
         WHERE account_id::text = %s AND ref_key = %s
        """,
        (json.dumps(locator, sort_keys=True), str(account_id), ref_key),
    )


def delete_locator(cur: Any, *, account_id: str, ref_key: str) -> None:
    """Forget where a backend keeps the value. The remote value itself is never touched."""
    cur.execute(
        "DELETE FROM secret_backend_locators WHERE account_id::text = %s AND ref_key = %s",
        (str(account_id), ref_key),
    )


def delete_local_value(cur: Any, *, account_id: str, ref_key: str) -> None:
    cur.execute(
        "DELETE FROM secret_refs WHERE account_id::text = %s AND ref_key = %s",
        (str(account_id), ref_key),
    )


__all__ = [
    "CONFIG_COLUMNS",
    "LOCATOR_COLUMNS",
    "delete_local_value",
    "delete_locator",
    "get_config",
    "get_locator",
    "insert_config",
    "insert_locator",
    "list_catalog_locators",
    "list_configs",
    "locator_holder",
    "update_config",
    "update_locator",
]
