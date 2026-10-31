"""SQL for the five inbound OAuth tables (ADR 0047 decision 11).

Every function takes a connection that the service obtained from
:func:`core.infrastructure.db.db_pool.transaction_connection`, so a use case is one
transaction. Time is always passed in by the caller (the service clock), never read
from ``now()``, so lifetimes are testable and one decision uses one instant.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from core.application.auth.inbound_oauth_policy import (
    INBOUND_OAUTH_SETTING_KEY,
    INBOUND_OAUTH_TABLES,
    LAST_USED_WRITE_INTERVAL,
)

_GRANT_COLUMNS = (
    "g.id, g.user_id, g.account_id, g.client_id, g.client_name_snapshot, g.resource, "
    "g.preset, g.scopes_jsonb, g.allowed_project_ids_jsonb, g.allowed_workflow_ids_jsonb, "
    "g.created_at, g.expires_at, g.last_used_at, g.revoked_at, g.revoked_reason"
)


def _rows(cur: Any) -> list[dict[str, Any]]:
    fetched = cur.fetchall()
    if not fetched:
        return []
    if isinstance(fetched[0], dict):
        return [dict(row) for row in fetched]
    names = [column[0] for column in cur.description]
    return [dict(zip(names, row, strict=True)) for row in fetched]


def _row(cur: Any) -> dict[str, Any] | None:
    fetched = cur.fetchone()
    if fetched is None:
        return None
    if isinstance(fetched, dict):
        return dict(fetched)
    names = [column[0] for column in cur.description]
    return dict(zip(names, fetched, strict=True))


def json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


def get_client(conn: Any, client_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT client_id, client_name, redirect_uris_jsonb, metadata_sha256, fetched_at, "
            "cache_expires_at, first_seen_at, last_used_at FROM oauth_clients "
            "WHERE client_id = %s",
            (client_id,),
        )
        return _row(cur)


def upsert_client(
    conn: Any,
    *,
    client_id: str,
    client_name: str,
    redirect_uris: Sequence[str],
    metadata_sha256: str,
    fetched_at: datetime,
    cache_expires_at: datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO oauth_clients
                (client_id, client_name, redirect_uris_jsonb, metadata_sha256, fetched_at,
                 cache_expires_at, first_seen_at)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s)
            ON CONFLICT (client_id) DO UPDATE
            SET client_name = EXCLUDED.client_name,
                redirect_uris_jsonb = EXCLUDED.redirect_uris_jsonb,
                metadata_sha256 = EXCLUDED.metadata_sha256,
                fetched_at = EXCLUDED.fetched_at,
                cache_expires_at = EXCLUDED.cache_expires_at
            """,
            (
                client_id,
                client_name,
                json.dumps(list(redirect_uris)),
                metadata_sha256,
                fetched_at,
                cache_expires_at,
                fetched_at,
            ),
        )


def touch_client(conn: Any, client_id: str, now: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_clients SET last_used_at = %s WHERE client_id = %s "
            "AND (last_used_at IS NULL OR last_used_at < %s)",
            (now, client_id, now - LAST_USED_WRITE_INTERVAL),
        )


# ---------------------------------------------------------------------------
# Pending authorizations and codes
# ---------------------------------------------------------------------------


def insert_authorization(
    conn: Any,
    *,
    client_id: str,
    redirect_uri: str,
    resource: str,
    code_challenge: str,
    state: str | None,
    requested_scopes: Sequence[str],
    browser_binding_hash: str,
    created_at: datetime,
    expires_at: datetime,
) -> str:
    authorization_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO oauth_authorizations
                (id, client_id, redirect_uri, resource, code_challenge, state,
                 requested_scopes_jsonb, browser_binding_hash, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            """,
            (
                authorization_id,
                client_id,
                redirect_uri,
                resource,
                code_challenge,
                state,
                json.dumps(list(requested_scopes)),
                browser_binding_hash,
                created_at,
                expires_at,
            ),
        )
    return authorization_id


_AUTHORIZATION_COLUMNS = (
    "a.id, a.client_id, a.redirect_uri, a.resource, a.code_challenge, a.state, "
    "a.requested_scopes_jsonb, a.browser_binding_hash, a.created_at, a.expires_at, "
    "a.decided_at, a.decision, a.user_id, a.account_id, a.preset, a.scopes_jsonb, "
    "a.allowed_project_ids_jsonb, a.grant_expires_at, a.code_expires_at, "
    "a.code_consumed_at, a.grant_id, c.client_name, c.redirect_uris_jsonb"
)


def get_authorization(conn: Any, authorization_id: str, *, for_update: bool) -> dict | None:
    lock = " FOR UPDATE OF a" if for_update else ""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_AUTHORIZATION_COLUMNS} FROM oauth_authorizations a "  # nosec B608
            "JOIN oauth_clients c ON c.client_id = a.client_id "
            f"WHERE a.id = %s{lock}",
            (authorization_id,),
        )
        return _row(cur)


def record_decision(
    conn: Any,
    *,
    authorization_id: str,
    decision: str,
    decided_at: datetime,
    user_id: str,
    account_id: str,
    preset: str | None = None,
    scopes: Sequence[str] | None = None,
    allowed_project_ids: Sequence[str] | None = None,
    grant_expires_at: datetime | None = None,
    code_hash: str | None = None,
    code_expires_at: datetime | None = None,
) -> bool:
    """Mark an undecided, unexpired pending row decided; False when it was not."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE oauth_authorizations
            SET decided_at = %s, decision = %s, user_id = %s, account_id = %s, preset = %s,
                scopes_jsonb = %s::jsonb, allowed_project_ids_jsonb = %s::jsonb,
                grant_expires_at = %s, code_hash = %s, code_expires_at = %s
            WHERE id = %s AND decided_at IS NULL AND expires_at > %s
            """,
            (
                decided_at,
                decision,
                user_id,
                account_id,
                preset,
                json.dumps(list(scopes)) if scopes is not None else None,
                json.dumps(list(allowed_project_ids)) if allowed_project_ids is not None else None,
                grant_expires_at,
                code_hash,
                code_expires_at,
                authorization_id,
                decided_at,
            ),
        )
        return int(cur.rowcount or 0) == 1


def consume_code(conn: Any, code_hash: str, now: datetime) -> dict | None:
    """Atomically consume a code: the first caller gets the row, every later one ``None``."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE oauth_authorizations
            SET code_consumed_at = %s
            WHERE code_hash = %s AND code_consumed_at IS NULL
            RETURNING id, client_id, redirect_uri, resource, code_challenge, user_id,
                      account_id, preset, scopes_jsonb, allowed_project_ids_jsonb,
                      grant_expires_at, code_expires_at, decided_at
            """,
            (now, code_hash),
        )
        return _row(cur)


def find_code(conn: Any, code_hash: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, grant_id, code_consumed_at, client_id FROM oauth_authorizations "
            "WHERE code_hash = %s",
            (code_hash,),
        )
        return _row(cur)


def set_authorization_grant(conn: Any, authorization_id: str, grant_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_authorizations SET grant_id = %s WHERE id = %s",
            (grant_id, authorization_id),
        )


# ---------------------------------------------------------------------------
# Grants and tokens
# ---------------------------------------------------------------------------


def insert_grant(
    conn: Any,
    *,
    user_id: str,
    account_id: str,
    client_id: str,
    client_name_snapshot: str,
    resource: str,
    preset: str,
    scopes: Sequence[str],
    allowed_project_ids: Sequence[str],
    created_at: datetime,
    expires_at: datetime,
) -> str:
    grant_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO oauth_grants
                (id, user_id, account_id, client_id, client_name_snapshot, resource, preset,
                 scopes_jsonb, allowed_project_ids_jsonb, allowed_workflow_ids_jsonb,
                 created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, '[]'::jsonb, %s, %s)
            """,
            (
                grant_id,
                user_id,
                account_id,
                client_id,
                client_name_snapshot,
                resource,
                preset,
                json.dumps(list(scopes)),
                json.dumps(list(allowed_project_ids)),
                created_at,
                expires_at,
            ),
        )
    return grant_id


def insert_access_token(
    conn: Any,
    *,
    token_hash: str,
    grant_id: str,
    scopes: Sequence[str],
    created_at: datetime,
    expires_at: datetime,
) -> str:
    token_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_access_tokens (id, token_hash, grant_id, scopes_jsonb, "
            "created_at, expires_at) VALUES (%s, %s, %s, %s::jsonb, %s, %s)",
            (token_id, token_hash, grant_id, json.dumps(list(scopes)), created_at, expires_at),
        )
    return token_id


def insert_refresh_token(
    conn: Any,
    *,
    token_hash: str,
    grant_id: str,
    parent_id: str | None,
    created_at: datetime,
    expires_at: datetime,
) -> str:
    token_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_refresh_tokens (id, token_hash, grant_id, parent_id, "
            "created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (token_id, token_hash, grant_id, parent_id, created_at, expires_at),
        )
    return token_id


def lock_grant(conn: Any, grant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_GRANT_COLUMNS} FROM oauth_grants g WHERE g.id = %s FOR UPDATE",  # nosec B608
            (grant_id,),
        )
        return _row(cur)


def refresh_token_grant_id(conn: Any, token_hash: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT grant_id FROM oauth_refresh_tokens WHERE token_hash = %s",
            (token_hash,),
        )
        row = _row(cur)
    return str(row["grant_id"]) if row else None


def consume_refresh_token(conn: Any, token_hash: str, now: datetime) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_refresh_tokens SET consumed_at = %s "
            "WHERE token_hash = %s AND consumed_at IS NULL "
            "RETURNING id, grant_id, expires_at",
            (now, token_hash),
        )
        return _row(cur)


def revoke_grant(conn: Any, grant_id: str, *, reason: str, now: datetime) -> bool:
    """Revoke once; a second revocation keeps the first reason and answers False."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_grants SET revoked_at = %s, revoked_reason = %s "
            "WHERE id = %s AND revoked_at IS NULL",
            (now, reason, grant_id),
        )
        return int(cur.rowcount or 0) == 1


def grant_for_token(conn: Any, token_hash: str) -> dict | None:
    """The grant an access or refresh token belongs to, with the token kind."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT grant_id, 'access' AS kind FROM oauth_access_tokens WHERE token_hash = %s "
            "UNION ALL "
            "SELECT grant_id, 'refresh' AS kind FROM oauth_refresh_tokens WHERE token_hash = %s "
            "LIMIT 1",
            (token_hash, token_hash),
        )
        return _row(cur)


def user_account_state(conn: Any, user_id: str, account_id: str) -> dict | None:
    """Live user/account/membership state for the exchange and refresh checks."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.is_active AS user_active,
                   COALESCE(a.is_active, false) AS account_active,
                   EXISTS (
                       SELECT 1 FROM user_account_memberships m
                       WHERE m.user_id = u.id AND m.account_id = %s
                   ) AS is_member
            FROM users u
            LEFT JOIN accounts a ON a.id = %s
            WHERE u.id = %s AND u.deleted_at IS NULL
            """,
            (account_id, account_id, user_id),
        )
        return _row(cur)


def oauth_enabled_for_account(conn: Any, account_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT setting_value_jsonb FROM system_settings "
            "WHERE account_id = %s AND setting_key = %s",
            (account_id, INBOUND_OAUTH_SETTING_KEY),
        )
        row = _row(cur)
    value = (row or {}).get("setting_value_jsonb")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    return bool(isinstance(value, dict) and value.get("enabled") is True)


def set_oauth_enabled_for_account(
    conn: Any, account_id: str, *, enabled: bool, updated_by: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO system_settings (account_id, setting_key, setting_value_jsonb, updated_by)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (account_id, setting_key) DO UPDATE
            SET setting_value_jsonb = EXCLUDED.setting_value_jsonb,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            (account_id, INBOUND_OAUTH_SETTING_KEY, json.dumps({"enabled": enabled}), updated_by),
        )


def resolve_access_token(conn: Any, token_hash: str, now: datetime) -> dict | None:
    """One query: token, live grant, active user, active account, membership, switch.

    Revocation, grant expiry, access-token expiry, account switch, user state and
    membership are all checked here on every use (decisions 9, 16); nothing is cached.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.id AS token_id, t.scopes_jsonb AS token_scopes_jsonb,
                   t.expires_at AS token_expires_at,
                   g.id AS grant_id, g.user_id, g.account_id, g.client_id, g.resource,
                   g.allowed_project_ids_jsonb, g.allowed_workflow_ids_jsonb,
                   g.expires_at AS grant_expires_at, g.last_used_at AS grant_last_used_at,
                   u.username, u.display_name,
                   COALESCE(r.roles, ARRAY[]::text[]) AS roles
            FROM oauth_access_tokens t
            JOIN oauth_grants g ON g.id = t.grant_id
            JOIN users u ON u.id = g.user_id AND u.is_active AND u.deleted_at IS NULL
            JOIN accounts a ON a.id = g.account_id AND a.is_active
            JOIN system_settings s
              ON s.account_id = g.account_id AND s.setting_key = %s
             AND s.setting_value_jsonb @> '{"enabled": true}'::jsonb
            LEFT JOIN LATERAL (
                SELECT array_agg(ro.key ORDER BY ro.key) AS roles
                FROM user_roles ur JOIN roles ro ON ro.id = ur.role_id
                WHERE ur.user_id = g.user_id
            ) r ON true
            WHERE t.token_hash = %s
              AND t.expires_at > %s
              AND g.revoked_at IS NULL
              AND g.expires_at > %s
              AND EXISTS (
                  SELECT 1 FROM user_account_memberships m
                  WHERE m.user_id = g.user_id AND m.account_id = g.account_id
              )
            """,
            (INBOUND_OAUTH_SETTING_KEY, token_hash, now, now),
        )
        row = _row(cur)
        if row is None:
            return None
        cur.execute(
            "UPDATE oauth_grants SET last_used_at = %s WHERE id = %s "
            "AND (last_used_at IS NULL OR last_used_at < %s)",
            (now, row["grant_id"], now - LAST_USED_WRITE_INTERVAL),
        )
    return row


def list_user_grants(conn: Any, user_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_GRANT_COLUMNS} FROM oauth_grants g "  # nosec B608
            "WHERE g.user_id = %s ORDER BY g.created_at DESC LIMIT 500",
            (user_id,),
        )
        return _rows(cur)


def get_user_grant(conn: Any, grant_id: str, user_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_GRANT_COLUMNS} FROM oauth_grants g "  # nosec B608
            "WHERE g.id = %s AND g.user_id = %s FOR UPDATE",
            (grant_id, user_id),
        )
        return _row(cur)


def count_grant_tokens(conn: Any, grant_id: str) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM oauth_access_tokens WHERE grant_id = %s) AS access, "
            "(SELECT count(*) FROM oauth_refresh_tokens WHERE grant_id = %s) AS refresh",
            (grant_id, grant_id),
        )
        row = _row(cur) or {}
    return {"access": int(row.get("access") or 0), "refresh": int(row.get("refresh") or 0)}


def storage_available(conn: Any) -> bool:
    """All five tables exist (one round trip; the readiness ``storage_missing`` check)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(to_regclass('public.' || name)) AS present "
            "FROM unnest(%s::text[]) AS name",
            (list(INBOUND_OAUTH_TABLES),),
        )
        row = _row(cur) or {}
    return int(row.get("present") or 0) == len(INBOUND_OAUTH_TABLES)


def any_account_enabled(conn: Any) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 AS enabled FROM system_settings WHERE setting_key = %s "
            "AND setting_value_jsonb @> '{\"enabled\": true}'::jsonb LIMIT 1",
            (INBOUND_OAUTH_SETTING_KEY,),
        )
        return _row(cur) is not None


def accessible_project_ids(conn: Any, account_id: str, project_ids: Sequence[str]) -> set[str]:
    """The subset of ``project_ids`` that are live projects of ``account_id``."""
    wanted = [project_id for project_id in project_ids if is_uuid(project_id)]
    if not wanted:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text AS id FROM projects WHERE id = ANY(%s::uuid[]) "
            "AND account_id = %s AND deleted_at IS NULL",
            (wanted, account_id),
        )
        return {str(row["id"]) for row in _rows(cur)}


def consume_rate_limit(
    conn: Any,
    keys: Sequence[str],
    *,
    now_epoch: float,
    window_seconds: int,
    max_attempts: int,
) -> int:
    """Sliding-window limiter on the shared ``auth_rate_limits`` store.

    Returns 0 and records the attempt when every key has capacity, otherwise the
    number of seconds until the oldest attempt of the fullest key leaves the window.
    """
    cutoff = now_epoch - window_seconds
    states: dict[str, list[float]] = {}
    retry_after = 0
    with conn.cursor() as cur:
        for key in sorted(set(keys)):
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
            cur.execute(
                "SELECT attempts_jsonb FROM auth_rate_limits WHERE key = %s FOR UPDATE",
                (key,),
            )
            row = _row(cur) or {}
            raw = row.get("attempts_jsonb") or []
            if isinstance(raw, str):
                raw = json.loads(raw)
            attempts = [float(value) for value in raw if float(value) > cutoff]
            states[key] = attempts
            if len(attempts) >= max_attempts:
                retry_after = max(retry_after, int(min(attempts) + window_seconds - now_epoch) + 1)
        if retry_after:
            return retry_after
        for key, attempts in states.items():
            attempts.append(now_epoch)
            cur.execute(
                """
                INSERT INTO auth_rate_limits (key, attempts_jsonb, locked_until, updated_at)
                VALUES (%s, %s::jsonb, NULL, now())
                ON CONFLICT (key) DO UPDATE
                SET attempts_jsonb = EXCLUDED.attempts_jsonb, updated_at = now()
                """,
                (key, json.dumps(attempts)),
            )
    return 0


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value or ""))
    except ValueError:
        return False
    return True


__all__ = [
    "accessible_project_ids",
    "any_account_enabled",
    "consume_code",
    "consume_rate_limit",
    "consume_refresh_token",
    "find_code",
    "get_authorization",
    "get_client",
    "get_user_grant",
    "grant_for_token",
    "insert_access_token",
    "insert_authorization",
    "insert_grant",
    "insert_refresh_token",
    "is_uuid",
    "json_list",
    "list_user_grants",
    "lock_grant",
    "oauth_enabled_for_account",
    "record_decision",
    "refresh_token_grant_id",
    "resolve_access_token",
    "revoke_grant",
    "set_authorization_grant",
    "set_oauth_enabled_for_account",
    "storage_available",
    "touch_client",
    "upsert_client",
    "user_account_state",
]
