"""SQL for a person's AI agent connections and their expiry reminders (ADR 0076 §5).

An AI agent connection is a record that already exists: a personal token the "connect
an AI agent" wizard created (its label starts with ``AI agent · ``) or an inbound
OAuth grant of a cloud client (ADR 0047). Every function takes a connection from
:func:`core.infrastructure.db.db_pool.transaction_connection`; time is passed in by the
caller, never read from ``now()``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

KIND_PERSONAL_TOKEN = "personal_token"
KIND_OAUTH_GRANT = "oauth_grant"

#: Labels of the personal tokens the wizard creates: ``AI agent · <client label>``.
AGENT_TOKEN_LABEL_PREFIX = "AI agent · "
_AGENT_TOKEN_LABEL_PATTERN = "AI agent · %"

_TABLE_OF_KIND = {
    KIND_PERSONAL_TOKEN: "personal_access_tokens",
    KIND_OAUTH_GRANT: "oauth_grants",
}


def _rows(cur: Any) -> list[dict[str, Any]]:
    fetched = cur.fetchall()
    if not fetched:
        return []
    if isinstance(fetched[0], dict):
        return [dict(row) for row in fetched]
    names = [column[0] for column in cur.description]
    return [dict(zip(names, row, strict=True)) for row in fetched]


def _oauth_grants_present(conn: Any) -> bool:
    """Whether the inbound OAuth grants table exists (it is absent before ADR 0047)."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.oauth_grants') IS NOT NULL AS present")
        row = cur.fetchone()
    if row is None:
        return False
    return bool(row.get("present") if isinstance(row, dict) else row[0])


def list_user_connections(conn: Any, user_id: str, *, now: datetime) -> list[dict[str, Any]]:
    """The person's AI agent connections that still work, the soonest to end first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT %s AS kind, pat.id::text AS id, pat.label AS label, "
            "pat.created_at, pat.expires_at, pat.last_used_at "
            "FROM personal_access_tokens pat "
            "WHERE pat.user_id = %s AND pat.revoked = false AND pat.label LIKE %s "
            "AND (pat.expires_at IS NULL OR pat.expires_at > %s) "
            "ORDER BY pat.created_at DESC LIMIT 200",
            (KIND_PERSONAL_TOKEN, user_id, _AGENT_TOKEN_LABEL_PATTERN, now),
        )
        rows = _rows(cur)
    if _oauth_grants_present(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT %s AS kind, g.id::text AS id, g.client_name_snapshot AS label, "
                "g.client_id, g.created_at, g.expires_at, g.last_used_at "
                "FROM oauth_grants g "
                "WHERE g.user_id = %s AND g.revoked_at IS NULL AND g.expires_at > %s "
                "ORDER BY g.created_at DESC LIMIT 200",
                (KIND_OAUTH_GRANT, user_id, now),
            )
            rows.extend(_rows(cur))
    far = datetime.max.replace(tzinfo=now.tzinfo)
    return sorted(rows, key=lambda row: row.get("expires_at") or far)


def list_reminder_candidates(
    conn: Any, *, now: datetime, window: timedelta, limit: int
) -> list[dict[str, Any]]:
    """Connections ending within ``window`` whose owner was not reminded yet.

    A connection that lived ``window`` or less from the start is left out (its owner
    chose that short a life), as are revoked ones and those of inactive or deleted
    people.
    """
    params: tuple[Any, ...] = (now, now + window, window)
    candidates: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT %s AS kind, pat.id::text AS id, pat.label AS label, pat.created_at, "
            "pat.expires_at, u.username, u.display_name, u.locale "
            "FROM personal_access_tokens pat JOIN users u ON u.id = pat.user_id "
            "WHERE pat.revoked = false AND pat.label LIKE %s "
            "AND pat.expiry_reminder_sent_at IS NULL "
            "AND pat.expires_at > %s AND pat.expires_at <= %s "
            "AND pat.expires_at - pat.created_at > %s "
            "AND u.is_active AND u.deleted_at IS NULL "
            "ORDER BY pat.expires_at LIMIT %s",
            (KIND_PERSONAL_TOKEN, _AGENT_TOKEN_LABEL_PATTERN, *params, limit),
        )
        candidates.extend(_rows(cur))
    if _oauth_grants_present(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT %s AS kind, g.id::text AS id, g.client_name_snapshot AS label, "
                "g.created_at, g.expires_at, u.username, u.display_name, u.locale "
                "FROM oauth_grants g JOIN users u ON u.id = g.user_id "
                "WHERE g.revoked_at IS NULL AND g.expiry_reminder_sent_at IS NULL "
                "AND g.expires_at > %s AND g.expires_at <= %s "
                "AND g.expires_at - g.created_at > %s "
                "AND u.is_active AND u.deleted_at IS NULL "
                "ORDER BY g.expires_at LIMIT %s",
                (KIND_OAUTH_GRANT, *params, limit),
            )
            candidates.extend(_rows(cur))
    return sorted(candidates, key=lambda row: row["expires_at"])[:limit]


def claim_reminder(conn: Any, kind: str, connection_id: str, *, now: datetime) -> bool:
    """Mark the reminder of one connection as sent; ``False`` when it already was."""
    table = _TABLE_OF_KIND[kind]
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET expiry_reminder_sent_at = %s "  # nosec B608 - fixed names
            "WHERE id = %s::uuid AND expiry_reminder_sent_at IS NULL RETURNING id",
            (now, connection_id),
        )
        return cur.fetchone() is not None


def release_reminder(conn: Any, kind: str, connection_id: str, *, claimed_at: datetime) -> None:
    """Undo :func:`claim_reminder` when nothing could be sent (no email connection)."""
    table = _TABLE_OF_KIND[kind]
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET expiry_reminder_sent_at = NULL "  # nosec B608 - fixed names
            "WHERE id = %s::uuid AND expiry_reminder_sent_at = %s",
            (connection_id, claimed_at),
        )


__all__ = [
    "AGENT_TOKEN_LABEL_PREFIX",
    "KIND_OAUTH_GRANT",
    "KIND_PERSONAL_TOKEN",
    "claim_reminder",
    "list_reminder_candidates",
    "list_user_connections",
    "release_reminder",
]
