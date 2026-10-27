"""What the LLM provider panel can say about a connection without showing secrets.

Two facts, kept on the install's existing ``config_jsonb``:

- which declared provider secrets have a saved value, and when each was last
  saved (names and times only; values are read only to tell empty from set and
  never leave this module);
- the outcome of the last model discovery against the provider: ``ok`` or the
  safe error category (``authentication_error``, ``timeout``…), with its time.

A save that changes the key, the settings or the runtime endpoint clears the
last check, because it no longer describes what is saved.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from core.application.settings import llm_provider_extension_repository as store

LAST_CHECK_KEY = "llm_provider_last_check"
SAVED_SECRETS_KEY = "llm_provider_saved_secrets"  # pragma: allowlist secret - a key name
CHECK_OK = "ok"


def record_last_check(
    conn: Any, *, account_id: str, extension_install_id: str, status: str
) -> bool:
    """Store the outcome of a discovery check (``ok`` or a safe error category)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extension_installs
               SET config_jsonb = jsonb_set(
                       COALESCE(config_jsonb, '{}'::jsonb),
                       '{llm_provider_last_check}',
                       jsonb_build_object('status', %s::text, 'checked_at', now()),
                       true
                   )
             WHERE account_id = %s AND install_id = %s
            """,
            (str(status), str(account_id), str(extension_install_id)),
        )
        return bool(cur.rowcount)


def clear_last_check(conn: Any, *, account_id: str, extension_install_id: str) -> bool:
    """Forget the last check: what it checked is no longer what is saved."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extension_installs
               SET config_jsonb = COALESCE(config_jsonb, '{}'::jsonb) - 'llm_provider_last_check'
             WHERE account_id = %s AND install_id = %s
            """,
            (str(account_id), str(extension_install_id)),
        )
        return bool(cur.rowcount)


def record_saved_secrets(
    conn: Any, *, account_id: str, extension_install_id: str, names: Iterable[str]
) -> bool:
    """Stamp the save time of each secret name just saved (names only)."""
    saved = sorted({str(name).strip() for name in names if str(name).strip()})
    if not saved:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extension_installs
               SET config_jsonb = jsonb_set(
                       COALESCE(config_jsonb, '{}'::jsonb),
                       '{llm_provider_saved_secrets}',
                       CASE
                           WHEN jsonb_typeof(config_jsonb -> 'llm_provider_saved_secrets') = 'object'
                           THEN config_jsonb -> 'llm_provider_saved_secrets'
                           ELSE '{}'::jsonb
                       END
                       || (SELECT jsonb_object_agg(name, now()) FROM unnest(%s::text[]) AS name),
                       true
                   )
             WHERE account_id = %s AND install_id = %s
            """,
            (saved, str(account_id), str(extension_install_id)),
        )
        return bool(cur.rowcount)


def read_install_config(conn: Any, *, account_id: str, extension_install_id: str) -> dict:
    """The install's current ``config_jsonb``, read fresh on this connection."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT config_jsonb FROM extension_installs WHERE account_id = %s AND install_id = %s",
            (str(account_id), str(extension_install_id)),
        )
        row = cur.fetchone()
    value = None
    if isinstance(row, dict):
        value = row.get("config_jsonb")
    elif row:
        value = row[0]
    if isinstance(value, str):
        value = json.loads(value or "{}")
    return dict(value) if isinstance(value, dict) else {}


def declared_secret_names(contract: Any) -> list[str]:
    names = [
        *(getattr(contract, "required_provider_secrets", None) or []),
        *(getattr(contract, "optional_provider_secrets", None) or []),
    ]
    return list(dict.fromkeys(str(name).strip() for name in names if str(name).strip()))


def saved_secret_names(conn: Any, *, account_id: str, extension_install_id: str) -> set[str]:
    """Names of the stored credentials that hold a non-empty value."""
    from core.application.auth.secrets import decrypt_secret

    _ref, ciphertext = store.get_install_current_credential_snapshot(
        conn, account_id=account_id, extension_install_id=extension_install_id
    )
    if not ciphertext:
        return set()
    try:
        decoded = json.loads(decrypt_secret(ciphertext))
    except Exception:
        return set()
    if not isinstance(decoded, dict):
        return set()
    return {str(name) for name, value in decoded.items() if str(value or "").strip()}


def connection_status_payload(
    *,
    install_id: str,
    contract: Any,
    install_config: Any,
    saved_names: set[str],
) -> dict[str, Any]:
    """The panel's view: saved secret names with times, and the last check."""
    config = install_config if isinstance(install_config, dict) else {}
    saved_times = config.get(SAVED_SECRETS_KEY)
    saved_times = saved_times if isinstance(saved_times, dict) else {}
    last_check = config.get(LAST_CHECK_KEY)
    last_check = last_check if isinstance(last_check, dict) else {}
    status = str(last_check.get("status") or "").strip()
    return {
        "install_id": install_id,
        "saved_secrets": [
            {"name": name, "saved_at": str(saved_times.get(name) or "") or None}
            for name in declared_secret_names(contract)
            if name in saved_names
        ],
        "last_check": (
            {"status": status, "checked_at": str(last_check.get("checked_at") or "") or None}
            if status
            else None
        ),
    }


__all__ = [
    "CHECK_OK",
    "LAST_CHECK_KEY",
    "SAVED_SECRETS_KEY",
    "clear_last_check",
    "connection_status_payload",
    "declared_secret_names",
    "read_install_config",
    "record_last_check",
    "record_saved_secrets",
    "saved_secret_names",
]
