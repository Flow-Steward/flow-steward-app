"""Persistence helpers for MCP connector OAuth authorization state."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from core.infrastructure.db.oauth_refresh_lock import advisory_xact_lock


def _row_to_dict(row: Any, description: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        payload = dict(row)
    else:
        columns = [
            col[0] if hasattr(col, "__getitem__") else getattr(col, "name", "")
            for col in (description or [])
        ]
        if not isinstance(row, (tuple, list)) or len(columns) != len(row):
            return {}
        payload = {str(columns[idx]): row[idx] for idx in range(len(columns))}

    for key, value in list(payload.items()):
        if isinstance(value, datetime):
            payload[key] = value.timestamp()
        elif key == "auth_payload_jsonb" and isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                parsed = {}
            payload[key] = parsed if isinstance(parsed, dict) else {}
    return payload


def insert_oauth_state(
    db: Any,
    *,
    state_hash: str,
    connector_id: str,
    account_id: str,
    project_id: str,
    tenant_id: str,
    oauth_credential_revision: int,
    code_verifier_encrypted: str,
    redirect_uri: str,
    return_path: str,
    auth_payload: dict[str, Any],
    expires_at: float,
) -> None:
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mcp_connector_oauth_states (
                state_hash,
                connector_id,
                account_id,
                project_id,
                tenant_id,
                oauth_credential_revision,
                code_verifier_encrypted,
                redirect_uri,
                return_path,
                auth_payload_jsonb,
                expires_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,to_timestamp(%s))
            """,
            (
                str(state_hash),
                str(connector_id),
                str(account_id),
                str(project_id),
                str(tenant_id),
                max(1, int(oauth_credential_revision)),
                str(code_verifier_encrypted),
                str(redirect_uri),
                str(return_path or ""),
                json.dumps(dict(auth_payload or {}), sort_keys=True, default=str),
                float(expires_at),
            ),
        )


def consume_oauth_state(db: Any, *, state_hash: str) -> dict[str, Any] | None:
    """Mark an MCP OAuth state as consumed and return it atomically."""
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE mcp_connector_oauth_states
               SET consumed_at = now(),
                   updated_at = now()
             WHERE state_hash = %s
               AND consumed_at IS NULL
               AND expires_at > now()
            RETURNING state_hash, connector_id, account_id, project_id, tenant_id,
                      oauth_credential_revision,
                      code_verifier_encrypted, redirect_uri, return_path,
                      auth_payload_jsonb, expires_at, consumed_at,
                      created_at, updated_at
            """,
            (str(state_hash),),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def get_oauth_state(db: Any, *, state_hash: str) -> dict[str, Any] | None:
    """Read an active MCP OAuth state without consuming its PKCE material."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT state_hash, connector_id, account_id, project_id, tenant_id,
                   oauth_credential_revision,
                   code_verifier_encrypted, redirect_uri, return_path,
                   auth_payload_jsonb, expires_at, consumed_at,
                   created_at, updated_at
              FROM mcp_connector_oauth_states
             WHERE state_hash = %s
               AND consumed_at IS NULL
               AND expires_at > now()
            """,
            (str(state_hash),),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def delete_expired_oauth_states(db: Any) -> int:
    with db.cursor() as cur:
        cur.execute(
            """
            DELETE FROM mcp_connector_oauth_states
             WHERE expires_at <= now()
                OR consumed_at IS NOT NULL
            """
        )
        return int(getattr(cur, "rowcount", 0) or 0)


def delete_connector_oauth_states(
    db: Any,
    *,
    connector_id: str,
    account_id: str,
    project_id: str,
    tenant_id: str,
) -> int:
    """Remove reusable PKCE material when a connector is moved to Trash."""
    with db.cursor() as cur:
        cur.execute(
            """DELETE FROM mcp_connector_oauth_states
                WHERE connector_id = %s AND account_id = %s
                  AND project_id = %s AND tenant_id = %s""",
            (str(connector_id), str(account_id), str(project_id), str(tenant_id)),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


def refresh_advisory_lock(db: Any, *, lock_name: str):
    return advisory_xact_lock(db, lock_name=lock_name)
