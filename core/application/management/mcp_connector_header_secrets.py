"""Encrypted storage for MCP connector request headers.

Header values (API keys, bearer tokens, tenant ids) are never persisted on the
connector row. Each value is AES-GCM encrypted into ``secret_refs`` and the row
keeps only ``header_secret_refs_jsonb = {"refs": {header_name: ref}}``.

A ``ref`` is one of two shapes:

* a string: the connector's own generation key ``mcp_connector:<id>:header:<gen>:<i>``,
  a value typed into the connector and stored project-scoped;
* an object ``{"catalog_ref": <storage key>, "prefix": <literal>}``: a stored secret
  chosen from the catalog (ADR 0057). The value stays under the catalog secret's own
  account-scoped storage key; the header sent is ``prefix + value``, so
  ``Authorization: Bearer <token>`` is the secret plus the prefix ``"Bearer "``.
  Which secret a header uses is recorded in ``catalog_secret_bindings``
  (``core.application.secrets.reference_consumers``); the object is its projection.

The prefix lives next to the storage key inside ``refs`` rather than in another
column: the row's CHECK allows only the ``refs`` key at the top level, the connector
has no general settings column (``oauth_metadata_jsonb`` is OAuth state and is reset
with it), and the sync fingerprint and the connector-step receiver digest both hash
the whole ``header_secret_refs_jsonb``, so a changed prefix is a changed
configuration exactly like a changed key. The prefix is a literal, never a secret.

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
#: Key of a catalog choice's storage key inside one ``refs`` entry.
CATALOG_REF_FIELD = "catalog_ref"
#: Key of the literal sent before a catalog value (``"Bearer "``).
CATALOG_PREFIX_FIELD = "prefix"
#: The consumer kind of an MCP connector in ``catalog_secret_bindings``.
MCP_CONNECTOR_CONSUMER_KIND = "mcp_connector"


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


def _stored_refs(row: dict[str, Any]) -> dict[str, Any]:
    stored = row.get(HEADER_SECRET_REFS_FIELD)
    refs = stored.get("refs") if isinstance(stored, dict) else None
    return refs if isinstance(refs, dict) else {}


def catalog_ref_entry(storage_key: str, prefix: str = "") -> dict[str, str]:
    """The ``refs`` entry of a header that uses a catalog secret."""
    return {CATALOG_REF_FIELD: _text(storage_key), CATALOG_PREFIX_FIELD: str(prefix or "")}


def connector_header_typed_refs(row: dict[str, Any]) -> dict[str, str]:
    """``{header_name: ref_key}`` of the headers whose values were typed into the connector."""
    return {
        _text(name): _text(ref)
        for name, ref in _stored_refs(row).items()
        if _text(name) and isinstance(ref, str) and _text(ref)
    }


def connector_header_catalog_refs(row: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """``{header_name: (storage key, prefix)}`` of the headers that use a catalog secret."""
    chosen: dict[str, tuple[str, str]] = {}
    for name, ref in _stored_refs(row).items():
        if not _text(name) or not isinstance(ref, dict):
            continue
        storage_key = _text(ref.get(CATALOG_REF_FIELD))
        if storage_key:
            chosen[_text(name)] = (storage_key, str(ref.get(CATALOG_PREFIX_FIELD) or ""))
    return chosen


def connector_header_secret_refs(row: dict[str, Any]) -> dict[str, str]:
    """Return ``{header_name: ref_key}`` for every stored header, typed or chosen."""
    refs = connector_header_typed_refs(row)
    for name, (storage_key, _prefix) in connector_header_catalog_refs(row).items():
        refs[name] = storage_key
    return refs


def connector_header_secret_choices(conn: Any, row: dict[str, Any]) -> dict[str, dict[str, str]]:
    """``{header_name: {"secret_id", "prefix"}}`` for the view: which headers use a stored
    secret. Names, ids and the literal prefix only; never a value."""
    chosen = connector_header_catalog_refs(row)
    if not chosen:
        return {}
    from core.application.secrets import reference_consumers

    with conn.cursor() as cur:
        secret_ids = reference_consumers.bound_fields(
            cur,
            account_id=_text(row.get("account_id")),
            kind=MCP_CONNECTOR_CONSUMER_KIND,
            consumer_id=_text(row.get("connector_id")),
        )
    return {
        name: {"secret_id": secret_ids.get(name, ""), "prefix": prefix}
        for name, (_key, prefix) in sorted(chosen.items())
    }


def delete_connector_header_secret_refs(
    conn: Any,
    *,
    connector_id: str,
    account_id: str,
    project_id: str,
    tenant_id: str,
    ref_keys: list[str],
) -> None:
    """Delete some of the connector's own typed values; never a key outside its prefix."""
    prefix = header_secret_ref_prefix(connector_id)
    owned = sorted({_text(key) for key in ref_keys if _text(key).startswith(prefix)})
    if not owned:
        return
    with conn.cursor() as cur:
        for ref_key in owned:
            cur.execute(
                """DELETE FROM secret_refs
                    WHERE account_id = %s AND project_id = %s AND tenant_id = %s
                      AND ref_key = %s""",
                (_text(account_id), _text(project_id), _text(tenant_id), ref_key),
            )


def unbind_connector_catalog_secrets(conn: Any, *, connector_id: str, account_id: str) -> None:
    """Remove every catalog binding of one connector (it goes to Trash and keeps no refs).

    Purging the row cascades the bindings through their foreign key; a trashed row
    stays, so its bindings are removed here with its ref map. The secrets and their
    values are never touched.
    """
    with conn.cursor() as cur:
        cur.execute(
            """DELETE FROM catalog_secret_bindings
                WHERE consumer_kind = %s AND mcp_connector_id = %s AND account_id::text = %s""",
            (MCP_CONNECTOR_CONSUMER_KIND, _text(connector_id), _text(account_id)),
        )


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
    """Delete every header secret generation of one connector, orphans included.

    Only keys under the connector's own prefix are deleted: a catalog secret a header
    uses keeps its own storage key, so it is never removed here.
    """
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


def _missing_header(connector_id: str, name: str) -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "MCP connector header secret is missing; re-enter the connector headers",
        payload={
            "error": "mcp_connector_header_secret_missing",
            "connector_id": connector_id,
            "header": name,
        },
    )


def _resolve_catalog_header(
    conn: Any, *, account_id: str, connector_id: str, name: str, storage_key: str, prefix: str
) -> str:
    """``prefix + value`` of a catalog secret, read account-scoped; fail closed."""
    from core.application.secrets import reference_consumers
    from core.infrastructure.security.stored_secrets import (
        SecretReadContext,
        StoredSecretError,
        stored_secret_reader,
    )

    if storage_key.startswith(header_secret_ref_prefix(connector_id)):
        raise _missing_header(connector_id, name)
    with conn.cursor() as cur:
        is_catalog = reference_consumers.is_catalog_storage_key(
            cur, account_id=account_id, ref_key=storage_key
        )
    if not is_catalog:
        raise _missing_header(connector_id, name)
    try:
        value = stored_secret_reader(conn).read(
            SecretReadContext.for_account(account_id), storage_key
        )
    except StoredSecretError as exc:
        raise _missing_header(connector_id, name) from exc
    if not value:
        raise _missing_header(connector_id, name)
    return f"{prefix}{value}"


def resolve_connector_headers(conn: Any, row: dict[str, Any]) -> dict[str, str]:
    """Decrypt a stored connector's headers; fail closed when a secret is missing.

    A typed value is read from the connector's own project-scoped key. A header that
    uses a catalog secret is read through the stored-secret reader with the
    connector's account, only when the key is a catalog secret's storage key of that
    account, and is sent as ``prefix + value``.

    A row that still carries transient plaintext ``headers`` (validation and
    request-scoped rows built in memory) is returned as-is.
    """
    plaintext = row.get("headers")
    if isinstance(plaintext, dict):
        return {_text(name): _text(value) for name, value in plaintext.items() if _text(name)}
    refs = connector_header_typed_refs(row)
    chosen = connector_header_catalog_refs(row)
    if not refs and not chosen:
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
            raise _missing_header(connector_id, name)
        headers[name] = decrypt_secret(encrypted_value)
    for name, (storage_key, header_prefix) in chosen.items():
        headers[name] = _resolve_catalog_header(
            conn,
            account_id=account_id,
            connector_id=connector_id,
            name=name,
            storage_key=storage_key,
            prefix=header_prefix,
        )
    return headers
