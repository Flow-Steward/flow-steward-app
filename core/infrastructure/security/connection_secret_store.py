"""Infrastructure-owned encrypted project-connection secret persistence."""

from __future__ import annotations

from typing import Any

from core.infrastructure.db import model_provider_store
from core.infrastructure.security.secret_codec import decrypt_secret, encrypt_secret


def normalize_connection_secret_refs(value: Any) -> dict[str, str]:
    """Return the canonical secret-name to ref-key mapping."""
    if isinstance(value, list):
        return {
            secret_name: secret_name for item in value if (secret_name := str(item or "").strip())
        }
    if not isinstance(value, dict):
        return {}
    refs = value.get("refs")
    if not isinstance(refs, dict):
        refs = value if all(not isinstance(item, (dict, list)) for item in value.values()) else {}
    return {
        str(name or "").strip(): str(ref_key or "").strip()
        for name, ref_key in refs.items()
        if str(name or "").strip() and str(ref_key or "").strip()
    }


def connection_secret_ref_key(connection_id: str, secret_name: str) -> str:
    normalized_connection_id = str(connection_id or "").strip()
    normalized_secret_name = str(secret_name or "").strip()
    if not normalized_connection_id:
        raise ValueError("connection_id is required")
    if not normalized_secret_name:
        raise ValueError("secret_name is required")
    return f"project_connection:{normalized_connection_id}:{normalized_secret_name}"


def connection_secret_ref_map(connection_id: str, secrets: dict[str, Any]) -> dict[str, Any]:
    refs = {
        str(secret_name or "").strip(): connection_secret_ref_key(connection_id, secret_name)
        for secret_name, value in (secrets or {}).items()
        if str(secret_name or "").strip() and value is not None
    }
    return {"refs": refs} if refs else {}


def save_connection_secret(
    conn: Any,
    *,
    account_id: str,
    connection_id: str,
    secret_name: str,
    secret_value: str,
) -> str:
    ref_key = connection_secret_ref_key(connection_id, secret_name)
    model_provider_store.upsert_secret_ref(
        conn,
        account_id=str(account_id or "").strip(),
        ref_key=ref_key,
        encrypted_value=encrypt_secret(str(secret_value or "")),
    )
    return ref_key


def save_connection_secrets(
    conn: Any,
    *,
    account_id: str,
    connection_id: str,
    secrets: dict[str, Any],
) -> dict[str, Any]:
    refs: dict[str, str] = {}
    for secret_name, secret_value in (secrets or {}).items():
        normalized_name = str(secret_name or "").strip()
        if not normalized_name or secret_value is None:
            continue
        refs[normalized_name] = save_connection_secret(
            conn,
            account_id=account_id,
            connection_id=connection_id,
            secret_name=normalized_name,
            secret_value=str(secret_value),
        )
    return {"refs": refs} if refs else {}


def resolve_connection_secrets(
    conn: Any,
    *,
    account_id: str,
    secret_refs: Any,
) -> dict[str, str]:
    values: dict[str, str] = {}
    for secret_name, ref_key in normalize_connection_secret_refs(secret_refs).items():
        ciphertext = model_provider_store.get_secret_ref_ciphertext(
            conn,
            account_id=str(account_id or "").strip(),
            ref_key=ref_key,
        )
        if ciphertext:
            values[secret_name] = decrypt_secret(ciphertext)
    return values


def delete_connection_secret_refs(
    conn: Any,
    *,
    account_id: str,
    connection_id: str,
) -> int:
    normalized_account_id = str(account_id or "").strip()
    normalized_connection_id = str(connection_id or "").strip()
    if not normalized_account_id:
        raise ValueError("account_id is required")
    if not normalized_connection_id:
        raise ValueError("connection_id is required")
    ref_prefix = f"project_connection:{normalized_connection_id}:"
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM secret_refs
             WHERE account_id::text = %s
               AND left(ref_key, length(%s)) = %s
            """,
            (normalized_account_id, ref_prefix, ref_prefix),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


__all__ = [
    "connection_secret_ref_key",
    "connection_secret_ref_map",
    "delete_connection_secret_refs",
    "normalize_connection_secret_refs",
    "resolve_connection_secrets",
    "save_connection_secret",
    "save_connection_secrets",
]
