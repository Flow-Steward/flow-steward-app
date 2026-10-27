"""Host-owned runtime session cache for project connections."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from core.application.auth.secrets import decrypt_secret, encrypt_secret
from core.infrastructure.db import project_connection_session_store


def connection_session_scope_hash(scope: dict[str, Any] | None = None) -> str:
    payload = {str(key): value for key, value in (scope or {}).items() if value not in (None, "")}
    if not payload:
        return "default"
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _connection_identity(connection: dict[str, Any]) -> tuple[str, str, str, int]:
    connection_id = str(connection.get("connection_id") or "").strip()
    account_id = str(connection.get("account_id") or "").strip()
    project_id = str(connection.get("project_id") or "").strip()
    try:
        credential_revision = max(1, int(connection.get("credential_revision") or 1))
    except (TypeError, ValueError):
        credential_revision = 1
    if not connection_id:
        raise ValueError("connection_id is required")
    if not account_id:
        raise ValueError("account_id is required")
    if not project_id:
        raise ValueError("project_id is required")
    return connection_id, account_id, project_id, credential_revision


def _encrypt_session_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(dict(payload or {}), sort_keys=True, separators=(",", ":"), default=str)
    return encrypt_secret(encoded)


def _decrypt_session_payload(encrypted_payload: str) -> dict[str, Any]:
    if not str(encrypted_payload or "").strip():
        return {}
    decoded = decrypt_secret(str(encrypted_payload))
    try:
        parsed = json.loads(decoded)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def get_connection_session(
    conn: Any,
    *,
    connection: dict[str, Any],
    scope: dict[str, Any] | None = None,
    subject_key: str = "",
) -> dict[str, Any] | None:
    connection_id, account_id, project_id, credential_revision = _connection_identity(connection)
    row = project_connection_session_store.get_valid_connection_session(
        conn,
        connection_id=connection_id,
        account_id=account_id,
        project_id=project_id,
        scope_hash=connection_session_scope_hash(scope),
        subject_key=str(subject_key or "").strip(),
        credential_revision=credential_revision,
    )
    if not row:
        return None
    return {
        "payload": _decrypt_session_payload(str(row.get("encrypted_session_payload") or "")),
        "expires_at": row.get("expires_at"),
        "refresh_after": row.get("refresh_after"),
        "scope_hash": row.get("scope_hash"),
        "subject_key": row.get("subject_key"),
        "credential_revision": row.get("credential_revision"),
    }


def upsert_connection_session(
    conn: Any,
    *,
    connection: dict[str, Any],
    session_payload: dict[str, Any],
    expires_at: float,
    refresh_after: float | None = None,
    scope: dict[str, Any] | None = None,
    subject_key: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    connection_id, account_id, project_id, credential_revision = _connection_identity(connection)
    row = project_connection_session_store.upsert_connection_session(
        conn,
        connection_id=connection_id,
        account_id=account_id,
        project_id=project_id,
        scope_hash=connection_session_scope_hash(scope),
        subject_key=str(subject_key or "").strip(),
        credential_revision=credential_revision,
        encrypted_session_payload=_encrypt_session_payload(session_payload),
        expires_at=float(expires_at),
        refresh_after=refresh_after,
        metadata_jsonb=json.dumps(dict(metadata or {}), sort_keys=True, default=str),
    )
    return {
        "session_id": str(row.get("session_id") or "").strip(),
        "connection_id": connection_id,
        "expires_at": row.get("expires_at"),
        "refresh_after": row.get("refresh_after"),
        "scope_hash": row.get("scope_hash"),
        "subject_key": row.get("subject_key"),
        "credential_revision": row.get("credential_revision"),
    }


def invalidate_connection_sessions(conn: Any, *, connection_id: str) -> int:
    return project_connection_session_store.invalidate_connection_sessions(
        conn,
        connection_id=str(connection_id or "").strip(),
    )


def acquire_connection_session_refresh_lock(
    conn: Any,
    *,
    connection: dict[str, Any],
    scope: dict[str, Any] | None = None,
    subject_key: str = "",
    lease_owner: str = "",
    lease_ttl_seconds: int = 30,
) -> dict[str, Any] | None:
    connection_id, account_id, project_id, credential_revision = _connection_identity(connection)
    owner = str(lease_owner or "").strip() or f"worker:{uuid.uuid4().hex}"
    row = project_connection_session_store.acquire_connection_session_refresh_lease(
        conn,
        connection_id=connection_id,
        account_id=account_id,
        project_id=project_id,
        scope_hash=connection_session_scope_hash(scope),
        subject_key=str(subject_key or "").strip(),
        credential_revision=credential_revision,
        lease_owner=owner,
        lease_expires_at=time.time() + max(1, int(lease_ttl_seconds or 30)),
    )
    if not row:
        return None
    return {
        "lease_owner": owner,
        "lease_expires_at": row.get("lease_expires_at"),
        "scope_hash": row.get("scope_hash"),
        "subject_key": row.get("subject_key"),
        "credential_revision": row.get("credential_revision"),
    }


def release_connection_session_refresh_lock(
    conn: Any,
    *,
    connection: dict[str, Any],
    lock: dict[str, Any],
) -> int:
    connection_id, _account_id, _project_id, credential_revision = _connection_identity(connection)
    return project_connection_session_store.release_connection_session_refresh_lease(
        conn,
        connection_id=connection_id,
        scope_hash=str(lock.get("scope_hash") or "default"),
        subject_key=str(lock.get("subject_key") or "").strip(),
        credential_revision=int(lock.get("credential_revision") or credential_revision),
        lease_owner=str(lock.get("lease_owner") or "").strip(),
    )
