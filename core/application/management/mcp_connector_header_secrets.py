"""Encrypted storage for MCP connector request headers.

Header values (API keys, bearer tokens, tenant ids) are never persisted on the
connector row. Each value is AES-GCM encrypted into ``secret_refs`` and the row
keeps only ``header_secret_refs_jsonb = {"refs": {header_name: ref_key}}``.

Every save writes a fresh ref generation, so a changed value always changes the
row-level ref map that sync fingerprints and binding comparisons observe.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.application.auth.secrets import decrypt_secret, encrypt_secret
from core.application.management.errors import ServiceUnavailableError
from core.infrastructure.db import model_provider_store

HEADER_SECRET_REFS_FIELD = "header_secret_refs_jsonb"  # pragma: allowlist secret


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lineage(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        _text(row.get("connector_id")),
        _text(row.get("account_id")),
        _text(row.get("project_id")),
        _text(row.get("tenant_id")),
    )


def header_secret_ref_prefix(connector_id: str) -> str:
    normalized = _text(connector_id)
    if not normalized:
        raise ValueError("MCP connector_id is required for header secret refs")
    return f"mcp_connector:{normalized}:header:"


def connector_header_secret_refs(row: dict[str, Any]) -> dict[str, str]:
    """Return the ``{header_name: ref_key}`` map stored on a connector row."""
    stored = row.get(HEADER_SECRET_REFS_FIELD)
    refs = stored.get("refs") if isinstance(stored, dict) else None
    if not isinstance(refs, dict):
        return {}
    return {_text(name): _text(ref) for name, ref in refs.items() if _text(name) and _text(ref)}


def connector_header_names(row: dict[str, Any]) -> dict[str, str]:
    """Header names known for a row, as a name-keyed dict for name-only validation."""
    plaintext = row.get("headers")
    if isinstance(plaintext, dict):
        return {_text(name): "" for name in plaintext if _text(name)}
    return dict.fromkeys(connector_header_secret_refs(row), "")


def connector_has_headers(row: dict[str, Any]) -> bool:
    plaintext = row.get("headers")
    if isinstance(plaintext, dict) and any(_text(name) for name in plaintext):
        return True
    return bool(connector_header_secret_refs(row))


def delete_connector_header_secrets(
    conn: Any,
    *,
    connector_id: str,
    account_id: str,
    project_id: str,
    tenant_id: str,
) -> None:
    """Delete every header secret generation of one connector, orphans included."""
    prefix = header_secret_ref_prefix(connector_id)
    with conn.cursor() as cur:
        cur.execute(
            """DELETE FROM secret_refs
                WHERE account_id = %s AND project_id = %s AND tenant_id = %s
                  AND left(ref_key, %s) = %s""",
            (_text(account_id), _text(project_id), _text(tenant_id), len(prefix), prefix),
        )


def save_connector_header_secrets(
    conn: Any,
    *,
    connector_id: str,
    account_id: str,
    project_id: str,
    tenant_id: str,
    headers: dict[str, Any] | None,
) -> dict[str, Any]:
    """Replace the connector's header secrets; return the row's ref map payload.

    Run inside the transaction that writes the connector row so the row and its
    secrets commit or roll back together.
    """
    delete_connector_header_secrets(
        conn,
        connector_id=connector_id,
        account_id=account_id,
        project_id=project_id,
        tenant_id=tenant_id,
    )
    generation = uuid.uuid4().hex[:12]
    prefix = header_secret_ref_prefix(connector_id)
    refs: dict[str, str] = {}
    for index, (name, value) in enumerate(sorted((headers or {}).items())):
        header_name = _text(name)
        if not header_name:
            continue
        ref_key = f"{prefix}{generation}:{index}"
        model_provider_store.upsert_secret_ref(
            conn,
            account_id=_text(account_id),
            project_id=_text(project_id),
            tenant_id=_text(tenant_id),
            ref_key=ref_key,
            encrypted_value=encrypt_secret(_text(value)),
        )
        refs[header_name] = ref_key
    return {"refs": refs} if refs else {}


def resolve_connector_headers(conn: Any, row: dict[str, Any]) -> dict[str, str]:
    """Decrypt a stored connector's headers; fail closed when a secret is missing.

    A row that still carries transient plaintext ``headers`` (validation and
    request-scoped rows built in memory) is returned as-is.
    """
    plaintext = row.get("headers")
    if isinstance(plaintext, dict):
        return {_text(name): _text(value) for name, value in plaintext.items() if _text(name)}
    refs = connector_header_secret_refs(row)
    if not refs:
        return {}
    connector_id, account_id, project_id, tenant_id = _lineage(row)
    prefix = header_secret_ref_prefix(connector_id)
    headers: dict[str, str] = {}
    for name, ref_key in refs.items():
        encrypted_value = ""
        if ref_key.startswith(prefix):
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT encrypted_value FROM secret_refs
                        WHERE account_id::text = %s AND project_id::text = %s
                          AND tenant_id::text = %s AND ref_key = %s
                        LIMIT 1""",
                    (account_id, project_id, tenant_id, ref_key),
                )
                db_row = cur.fetchone()
            if isinstance(db_row, dict):
                encrypted_value = _text(db_row.get("encrypted_value"))
            elif isinstance(db_row, (tuple, list)) and db_row:
                encrypted_value = _text(db_row[0])
        if not encrypted_value:
            raise ServiceUnavailableError(
                "MCP connector header secret is missing; re-enter the connector headers",
                payload={
                    "error": "mcp_connector_header_secret_missing",
                    "connector_id": connector_id,
                    "header": name,
                },
            )
        headers[name] = decrypt_secret(encrypted_value)
    return headers
