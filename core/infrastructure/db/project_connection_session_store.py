"""Persistence helpers for project connection runtime sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any


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
    return payload


def get_valid_connection_session(
    db: Any,
    *,
    connection_id: str,
    account_id: str,
    project_id: str,
    scope_hash: str,
    subject_key: str,
    credential_revision: int,
) -> dict[str, Any] | None:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT session_id, connection_id, account_id, project_id,
                   scope_hash, subject_key, credential_revision,
                   encrypted_session_payload, expires_at, refresh_after,
                   lease_owner, lease_expires_at, last_error, metadata_jsonb,
                   created_at, updated_at
              FROM project_connection_sessions
             WHERE connection_id = %s
               AND account_id::text = %s
               AND project_id::text = %s
               AND scope_hash = %s
               AND subject_key = %s
               AND credential_revision = %s
               AND encrypted_session_payload <> ''
               AND expires_at IS NOT NULL
               AND expires_at > now()
             LIMIT 1
            """,
            (
                str(connection_id),
                str(account_id),
                str(project_id),
                str(scope_hash),
                str(subject_key),
                int(credential_revision),
            ),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def upsert_connection_session(
    db: Any,
    *,
    connection_id: str,
    account_id: str,
    project_id: str,
    scope_hash: str,
    subject_key: str,
    credential_revision: int,
    encrypted_session_payload: str,
    expires_at: float,
    refresh_after: float | None = None,
    metadata_jsonb: str = "{}",
) -> dict[str, Any]:
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO project_connection_sessions (
                connection_id,
                account_id,
                project_id,
                scope_hash,
                subject_key,
                credential_revision,
                encrypted_session_payload,
                expires_at,
                refresh_after,
                lease_owner,
                lease_expires_at,
                last_error,
                metadata_jsonb
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),to_timestamp(%s),NULL,NULL,'',%s::jsonb)
            ON CONFLICT (connection_id, scope_hash, subject_key, credential_revision)
            DO UPDATE SET
                account_id = EXCLUDED.account_id,
                project_id = EXCLUDED.project_id,
                encrypted_session_payload = EXCLUDED.encrypted_session_payload,
                expires_at = EXCLUDED.expires_at,
                refresh_after = EXCLUDED.refresh_after,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_error = '',
                metadata_jsonb = EXCLUDED.metadata_jsonb,
                updated_at = now()
            RETURNING session_id, connection_id, account_id, project_id,
                      scope_hash, subject_key, credential_revision,
                      encrypted_session_payload, expires_at, refresh_after,
                      lease_owner, lease_expires_at, last_error, metadata_jsonb,
                      created_at, updated_at
            """,
            (
                str(connection_id),
                str(account_id),
                str(project_id),
                str(scope_hash),
                str(subject_key),
                int(credential_revision),
                str(encrypted_session_payload),
                float(expires_at),
                float(refresh_after if refresh_after is not None else expires_at),
                str(metadata_jsonb or "{}"),
            ),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description)


def invalidate_connection_sessions(db: Any, *, connection_id: str) -> int:
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE project_connection_sessions
               SET expires_at = now(),
                   refresh_after = now(),
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   updated_at = now()
             WHERE connection_id = %s
            """,
            (str(connection_id),),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


def acquire_connection_session_refresh_lease(
    db: Any,
    *,
    connection_id: str,
    account_id: str,
    project_id: str,
    scope_hash: str,
    subject_key: str,
    credential_revision: int,
    lease_owner: str,
    lease_expires_at: float,
) -> dict[str, Any] | None:
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO project_connection_sessions (
                connection_id,
                account_id,
                project_id,
                scope_hash,
                subject_key,
                credential_revision,
                encrypted_session_payload,
                expires_at,
                refresh_after,
                lease_owner,
                lease_expires_at,
                last_error
            ) VALUES (%s,%s,%s,%s,%s,%s,'',NULL,NULL,%s,to_timestamp(%s),'')
            ON CONFLICT (connection_id, scope_hash, subject_key, credential_revision)
            DO UPDATE SET
                lease_owner = EXCLUDED.lease_owner,
                lease_expires_at = EXCLUDED.lease_expires_at,
                updated_at = now()
            WHERE project_connection_sessions.lease_owner IS NULL
               OR project_connection_sessions.lease_expires_at IS NULL
               OR project_connection_sessions.lease_expires_at < now()
            RETURNING session_id, connection_id, account_id, project_id,
                      scope_hash, subject_key, credential_revision,
                      encrypted_session_payload, expires_at, refresh_after,
                      lease_owner, lease_expires_at, last_error, metadata_jsonb,
                      created_at, updated_at
            """,
            (
                str(connection_id),
                str(account_id),
                str(project_id),
                str(scope_hash),
                str(subject_key),
                int(credential_revision),
                str(lease_owner),
                float(lease_expires_at),
            ),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def release_connection_session_refresh_lease(
    db: Any,
    *,
    connection_id: str,
    scope_hash: str,
    subject_key: str,
    credential_revision: int,
    lease_owner: str,
) -> int:
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE project_connection_sessions
               SET lease_owner = NULL,
                   lease_expires_at = NULL,
                   updated_at = now()
             WHERE connection_id = %s
               AND scope_hash = %s
               AND subject_key = %s
               AND credential_revision = %s
               AND lease_owner = %s
            """,
            (
                str(connection_id),
                str(scope_hash),
                str(subject_key),
                int(credential_revision),
                str(lease_owner),
            ),
        )
        return int(getattr(cur, "rowcount", 0) or 0)
