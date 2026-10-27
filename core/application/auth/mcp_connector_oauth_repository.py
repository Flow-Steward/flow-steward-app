"""Scoped MCP OAuth row locks, secret persistence and credential state updates."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any

from core.application.auth.mcp_connector_oauth_policy import (
    _as_text,
    _connector_lineage,
    _oauth_binding_error,
    _oauth_credential_revision,
    _oauth_metadata,
    _refs_map,
    _require_current_oauth_revision,
    _sanitize_oauth_error_message,
    _secret_ref_key,
)
from core.application.auth.oauth_client import OAuthConnectionError
from core.application.auth.secrets import decrypt_secret, encrypt_secret
from core.application.management.mcp_connector_store import McpConnectorStore
from core.infrastructure.db import model_provider_store
from core.infrastructure.db.db_pool import (
    transaction_connection,
)


def _latest_connector_row(conn: Any, row: dict[str, Any]) -> dict[str, Any]:
    try:
        connector_id, account_id, project_id, tenant_id = _connector_lineage(row)
    except OAuthConnectionError:
        return row
    with contextlib.suppress(Exception):
        latest = McpConnectorStore().get_connector_row(
            conn,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
        if latest:
            return latest
    return row


def _locked_connector_row(conn: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Lock the OAuth connector row for a final revision compare-and-write."""
    connector_id, account_id, project_id, tenant_id = _connector_lineage(row)
    cursor_factory = getattr(conn, "cursor", None)
    if not callable(cursor_factory):
        # Explicit in-memory seam only. A real SQL connection must never fall
        # back to the stale caller row after a missing/deleted live row.
        return _latest_connector_row(conn, row)
    with cursor_factory() as cur:
        cur.execute(
            """SELECT * FROM mcp_connectors
                 WHERE connector_id = %s AND account_id = %s
                   AND project_id = %s AND tenant_id = %s
                   AND deleted_at IS NULL
                 FOR UPDATE""",
            (connector_id, account_id, project_id, tenant_id),
        )
        locked = cur.fetchone()
        if not locked:
            raise OAuthConnectionError(
                "MCP connector no longer exists; exchanged tokens were not stored"
            )
        if isinstance(locked, Mapping):
            return dict(locked)
        if isinstance(locked, (tuple, list)):
            return McpConnectorStore.row_to_dict(tuple(locked), cur.description)
    raise OAuthConnectionError("MCP connector row could not be locked for OAuth token storage")


def _oauth_metadata_for_update(conn: Any, row: dict[str, Any]) -> dict[str, Any]:
    latest = _latest_connector_row(conn, row)
    metadata = _oauth_metadata(latest)
    metadata.update(_oauth_metadata(row))
    return metadata


def _require_oauth_credentials_bound_to_connector(
    conn: Any,
    connector_row: dict[str, Any],
    *,
    metadata: dict[str, Any],
    values: dict[str, str],
) -> None:
    problem = _oauth_binding_error(connector_row, metadata=metadata, values=values)
    if not problem:
        return
    _mark_auth_problem_if_current(
        conn,
        connector_row,
        expected_credential_revision=_oauth_credential_revision(connector_row),
        auth_status="auth_required",
        message=problem,
    )
    raise OAuthConnectionError(problem)


def _save_connector_oauth_secrets(
    conn: Any,
    *,
    account_id: str,
    project_id: str,
    tenant_id: str,
    connector_id: str,
    secrets_to_save: dict[str, Any],
) -> dict[str, Any]:
    refs: dict[str, str] = {}
    for secret_name, secret_value in (secrets_to_save or {}).items():
        normalized_name = _as_text(secret_name)
        if not normalized_name or secret_value is None:
            continue
        normalized_value = str(secret_value)
        if normalized_value == "":
            continue
        ref_key = _secret_ref_key(connector_id, normalized_name)
        model_provider_store.upsert_secret_ref(
            conn,
            account_id=_as_text(account_id),
            project_id=_as_text(project_id),
            tenant_id=_as_text(tenant_id),
            ref_key=ref_key,
            encrypted_value=encrypt_secret(normalized_value),
        )
        refs[normalized_name] = ref_key
    return {"refs": refs} if refs else {}


def _resolve_connector_oauth_secrets(conn: Any, row: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    _connector_id, account_id, project_id, tenant_id = _connector_lineage(row)
    for secret_name, ref_key in _refs_map(row).items():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT encrypted_value
                  FROM secret_refs
                 WHERE account_id::text = %s
                   AND project_id::text = %s
                   AND tenant_id::text = %s
                   AND ref_key = %s
                 LIMIT 1
                """,
                (account_id, project_id, tenant_id, ref_key),
            )
            db_row = cur.fetchone()
        encrypted_value = ""
        if isinstance(db_row, dict):
            encrypted_value = _as_text(db_row.get("encrypted_value"))
        elif isinstance(db_row, (tuple, list)) and db_row:
            encrypted_value = _as_text(db_row[0])
        if encrypted_value:
            values[secret_name] = decrypt_secret(encrypted_value)
    return values


def _update_connector_oauth(
    conn: Any,
    connector_id: str,
    *,
    account_id: str,
    project_id: str,
    tenant_id: str,
    updates: dict[str, Any],
) -> None:
    McpConnectorStore().update_connector_row(
        conn,
        connector_id,
        updates,
        account_id=account_id,
        project_id=project_id,
        tenant_id=tenant_id,
    )


def _mark_auth_problem(
    conn: Any,
    row: dict[str, Any],
    *,
    auth_status: str,
    message: str,
) -> None:
    try:
        connector_id, account_id, project_id, tenant_id = _connector_lineage(row)
    except OAuthConnectionError:
        return
    with contextlib.suppress(Exception):
        _update_connector_oauth(
            conn,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            updates={
                "auth_status": auth_status,
                "status": "degraded",
                "oauth_last_error": _sanitize_oauth_error_message(message),
            },
        )


def _mark_auth_problem_if_current(
    conn: Any,
    connector_row: dict[str, Any],
    *,
    expected_credential_revision: int,
    auth_status: str,
    message: str,
) -> None:
    expected_row = {
        **connector_row,
        "oauth_credential_revision": expected_credential_revision,
    }
    with transaction_connection(conn) as transaction_conn:
        locked_row = _locked_connector_row(transaction_conn, expected_row)
        _require_current_oauth_revision(expected_row, locked_row)
        connector_id, account_id, project_id, tenant_id = _connector_lineage(locked_row)
        _update_connector_oauth(
            transaction_conn,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            updates={
                "auth_status": auth_status,
                "status": "degraded",
                "oauth_last_error": _sanitize_oauth_error_message(message),
            },
        )
