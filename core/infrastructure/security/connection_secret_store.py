"""Infrastructure-owned encrypted project-connection secret persistence."""

from __future__ import annotations

from typing import Any

from core.infrastructure.db import model_provider_store
from core.infrastructure.security.secret_codec import decrypt_secret, encrypt_secret
from core.infrastructure.security.stored_secrets import (
    SecretReadContext,
    StoredSecretReader,
    stored_secret_reader,
)

#: Names the OAuth grant lifecycle owns inside a connection's secret map.
OAUTH_GRANT_SECRET_PREFIX = "_oauth_"  # pragma: allowlist secret - a name prefix, not a value


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


def is_oauth_grant_secret_name(secret_name: Any) -> bool:
    """Whether a connection secret belongs to the OAuth grant lifecycle, not static credentials."""
    return str(secret_name or "").strip().startswith(OAUTH_GRANT_SECRET_PREFIX)


def resolve_connection_secrets(
    conn: Any,
    *,
    account_id: str,
    secret_refs: Any,
    reader: StoredSecretReader | None = None,
) -> dict[str, str]:
    """Resolve a connection's static credentials through the host-selected reader.

    Every configured reference must produce a value: a reference with nothing
    behind it raises `StoredSecretMissing` rather than dropping the field, and a
    stored empty string is a value. Whether a field is required at all is the
    connection contract's question, not this one's. OAuth grant values are not
    static credentials and never reach the reader; they stay on
    `read_local_connection_secrets`.
    """
    store = reader or stored_secret_reader(conn)
    context = SecretReadContext.for_account(account_id)
    return {
        secret_name: store.read(context, ref_key)
        for secret_name, ref_key in normalize_connection_secret_refs(secret_refs).items()
        if not is_oauth_grant_secret_name(secret_name)
    }


def read_local_connection_secrets(
    conn: Any,
    *,
    account_id: str,
    secret_refs: Any,
) -> dict[str, str]:
    """Read connection secrets from the local encrypted store with the OAuth lifecycle's semantics.

    OAuth grant persistence writes, refreshes and revokes these values locally
    and treats a missing one as absent. It deliberately does not go through the
    stored-secret reader.
    """
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
               AND NOT EXISTS (
                   SELECT 1 FROM catalog_secrets AS catalog
                    WHERE catalog.account_id = secret_refs.account_id
                      AND catalog.storage_ref_key = secret_refs.ref_key
               )
            """,
            (normalized_account_id, ref_prefix, ref_prefix),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


__all__ = [
    "OAUTH_GRANT_SECRET_PREFIX",
    "connection_secret_ref_key",
    "connection_secret_ref_map",
    "delete_connection_secret_refs",
    "is_oauth_grant_secret_name",
    "normalize_connection_secret_refs",
    "read_local_connection_secrets",
    "resolve_connection_secrets",
    "save_connection_secret",
    "save_connection_secrets",
]
