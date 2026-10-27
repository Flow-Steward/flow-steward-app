"""Persistence helpers for project connection OAuth authorization state."""

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
    connection_id: str,
    account_id: str,
    project_id: str,
    code_verifier_encrypted: str,
    redirect_uri: str,
    return_path: str,
    auth_payload: dict[str, Any],
    expires_at: float,
) -> None:
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO project_connection_oauth_states (
                state_hash,
                connection_id,
                account_id,
                project_id,
                code_verifier_encrypted,
                redirect_uri,
                return_path,
                auth_payload_jsonb,
                expires_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,to_timestamp(%s))
            """,
            (
                str(state_hash),
                str(connection_id),
                str(account_id),
                str(project_id),
                str(code_verifier_encrypted),
                str(redirect_uri),
                str(return_path or ""),
                json.dumps(dict(auth_payload or {}), sort_keys=True, default=str),
                float(expires_at),
            ),
        )


def invalidate_connection_oauth_states(db: Any, *, connection_id: str) -> int:
    """Consume every live callback state for exactly one project connection."""
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE project_connection_oauth_states
               SET consumed_at = now(),
                   updated_at = now()
             WHERE connection_id = %s
               AND consumed_at IS NULL
            """,
            (str(connection_id),),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


def replace_connection_oauth_state(
    db: Any,
    *,
    state_hash: str,
    connection_id: str,
    account_id: str,
    project_id: str,
    code_verifier_encrypted: str,
    redirect_uri: str,
    return_path: str,
    auth_payload: dict[str, Any],
    expires_at: float,
) -> None:
    """Serialize OAuth starts and leave one live state for the connection.

    The caller owns the surrounding transaction. Locking the connection row
    makes concurrent Retry/Reconnect starts execute in a deterministic order.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT connection_id FROM project_connections WHERE connection_id = %s FOR UPDATE",
            (str(connection_id),),
        )
    invalidate_connection_oauth_states(db, connection_id=connection_id)
    insert_oauth_state(
        db,
        state_hash=state_hash,
        connection_id=connection_id,
        account_id=account_id,
        project_id=project_id,
        code_verifier_encrypted=code_verifier_encrypted,
        redirect_uri=redirect_uri,
        return_path=return_path,
        auth_payload=auth_payload,
        expires_at=expires_at,
    )


def consume_oauth_state(db: Any, *, state_hash: str) -> dict[str, Any] | None:
    """Mark an OAuth state as consumed and return it atomically."""
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE project_connection_oauth_states
               SET consumed_at = now(),
                   updated_at = now()
             WHERE state_hash = %s
               AND consumed_at IS NULL
               AND expires_at > now()
            RETURNING state_hash, connection_id, account_id, project_id,
                      code_verifier_encrypted, redirect_uri, return_path,
                      auth_payload_jsonb, expires_at, consumed_at,
                      created_at, updated_at
            """,
            (str(state_hash),),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def get_oauth_state(db: Any, *, state_hash: str) -> dict[str, Any] | None:
    """Read a still-usable OAuth state without consuming it."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT state_hash, connection_id, account_id, project_id,
                   code_verifier_encrypted, redirect_uri, return_path,
                   auth_payload_jsonb, expires_at, consumed_at,
                   created_at, updated_at
              FROM project_connection_oauth_states
             WHERE state_hash = %s
               AND consumed_at IS NULL
               AND expires_at > now()
            """,
            (str(state_hash),),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def has_live_connection_oauth_state(db: Any, *, connection_id: str) -> bool:
    """Return whether a newer authorization flow is live for the connection."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT 1
              FROM project_connection_oauth_states
             WHERE connection_id = %s
               AND consumed_at IS NULL
               AND expires_at > now()
             LIMIT 1
            """,
            (str(connection_id),),
        )
        return cur.fetchone() is not None


def delete_expired_oauth_states(db: Any) -> int:
    with db.cursor() as cur:
        cur.execute(
            """
            DELETE FROM project_connection_oauth_states
             WHERE expires_at <= now()
                OR consumed_at IS NOT NULL
            """
        )
        return int(getattr(cur, "rowcount", 0) or 0)


def refresh_advisory_lock(db: Any, *, lock_name: str):
    return advisory_xact_lock(db, lock_name=lock_name)
