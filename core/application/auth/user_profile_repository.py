"""Persistence helpers for authenticated user profile routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def auth_email_change_tables_available(bl: Any) -> bool:
    if not bl:
        return False
    table_exists = getattr(bl, "_table_exists", None)
    if callable(table_exists):
        try:
            return bool(table_exists("users")) and bool(table_exists("auth_email_change_requests"))
        except Exception:
            return False
    return True


def _commit_if_possible(bl: Any) -> None:
    commit = getattr(getattr(bl, "conn", None), "commit", None)
    if callable(commit):
        commit()


def _normalize_row(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if isinstance(row, (tuple, list)):
        return {column: row[idx] if idx < len(row) else None for idx, column in enumerate(columns)}
    return {}


def load_user_timezone(bl: Any, user_id: Any) -> str:
    with bl.conn.cursor() as cur:
        cur.execute(
            "SELECT timezone FROM users WHERE id = %s LIMIT 1", (str(user_id or "").strip(),)
        )
        row = cur.fetchone()
    if isinstance(row, dict):
        return str(row.get("timezone") or "").strip()
    if isinstance(row, (tuple, list)):
        return str((row[0] if row else "") or "").strip()
    return ""


def load_user_locale(bl: Any, user_id: Any) -> str:
    with bl.conn.cursor() as cur:
        cur.execute("SELECT locale FROM users WHERE id = %s LIMIT 1", (str(user_id or "").strip(),))
        row = cur.fetchone()
    if isinstance(row, dict):
        return str(row.get("locale") or "").strip()
    if isinstance(row, (tuple, list)):
        return str((row[0] if row else "") or "").strip()
    return ""


def update_user_display_name(bl: Any, user_id: Any, display_name: str) -> None:
    with bl.conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET display_name = %s WHERE id = %s",
            (str(display_name or "").strip(), str(user_id or "").strip()),
        )
    _commit_if_possible(bl)


def get_pending_email_change(
    bl: Any,
    *,
    user_id: Any,
    real_dict_cursor_kwargs: Callable[[], dict[str, Any]],
) -> dict[str, Any] | None:
    with bl.conn.cursor(**real_dict_cursor_kwargs()) as cur:
        cur.execute(
            """
            SELECT new_email, expires_at, created_at
            FROM auth_email_change_requests
            WHERE user_id = %s
              AND used_at IS NULL
              AND expires_at > now()
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (str(user_id or "").strip(),),
        )
        row = cur.fetchone()
    payload = _normalize_row(row, ("new_email", "expires_at", "created_at"))
    return payload or None


def create_email_change_request(
    bl: Any,
    *,
    user_id: Any,
    current_email: str,
    new_email: str,
    token_hash: str,
    expires_in_minutes: int,
    requested_ip: str,
    real_dict_cursor_kwargs: Callable[[], dict[str, Any]],
) -> Any:
    with bl.conn.cursor(**real_dict_cursor_kwargs()) as cur:
        cur.execute(
            """
            UPDATE auth_email_change_requests
            SET
                used_at = now(),
                invalidated_reason = 'superseded'
            WHERE user_id = %s
              AND used_at IS NULL
              AND expires_at > now()
            """,
            (str(user_id or "").strip(),),
        )
        cur.execute(
            """
            INSERT INTO auth_email_change_requests (
                user_id,
                current_email,
                new_email,
                token_hash,
                expires_at,
                requested_ip
            ) VALUES (%s, %s, %s, %s, now() + make_interval(mins => %s), %s)
            RETURNING id, expires_at
            """,
            (
                str(user_id or "").strip(),
                str(current_email or "").strip(),
                str(new_email or "").strip(),
                str(token_hash or "").strip(),
                int(expires_in_minutes),
                str(requested_ip or "").strip(),
            ),
        )
        inserted_row = cur.fetchone()
    _commit_if_possible(bl)
    return inserted_row


def mark_email_change_delivery_failed(
    bl: Any,
    *,
    token_hash: str,
    real_dict_cursor_kwargs: Callable[[], dict[str, Any]],
) -> None:
    with bl.conn.cursor(**real_dict_cursor_kwargs()) as cur:
        cur.execute(
            """
            UPDATE auth_email_change_requests
            SET
                used_at = now(),
                invalidated_reason = 'delivery_failed'
            WHERE token_hash = %s
              AND used_at IS NULL
            """,
            (str(token_hash or "").strip(),),
        )
    _commit_if_possible(bl)


def get_email_change_verification_request(
    bl: Any,
    *,
    token_hash: str,
    real_dict_cursor_kwargs: Callable[[], dict[str, Any]],
) -> dict[str, Any] | None:
    with bl.conn.cursor(**real_dict_cursor_kwargs()) as cur:
        cur.execute(
            """
            SELECT
                e.id,
                e.user_id,
                e.current_email,
                e.new_email,
                e.expires_at,
                u.username,
                u.is_active
            FROM auth_email_change_requests e
            JOIN users u ON u.id = e.user_id
            WHERE e.token_hash = %s
              AND e.used_at IS NULL
              AND e.expires_at > now()
            LIMIT 1
            """,
            (str(token_hash or "").strip(),),
        )
        row = cur.fetchone()
    payload = _normalize_row(
        row,
        ("id", "user_id", "current_email", "new_email", "expires_at", "username", "is_active"),
    )
    return payload or None


def mark_email_change_taken(
    bl: Any,
    *,
    request_id: str,
    real_dict_cursor_kwargs: Callable[[], dict[str, Any]],
) -> None:
    with bl.conn.cursor(**real_dict_cursor_kwargs()) as cur:
        cur.execute(
            """
            UPDATE auth_email_change_requests
            SET
                used_at = now(),
                invalidated_reason = 'email_taken'
            WHERE id = %s
              AND used_at IS NULL
            """,
            (str(request_id),),
        )
    _commit_if_possible(bl)


def apply_email_change(
    bl: Any,
    *,
    user_id: Any,
    new_email: str,
    request_id: str,
    real_dict_cursor_kwargs: Callable[[], dict[str, Any]],
) -> None:
    with bl.conn.cursor(**real_dict_cursor_kwargs()) as cur:
        cur.execute(
            "UPDATE users SET username = %s WHERE id = %s",
            (str(new_email or "").strip(), str(user_id or "").strip()),
        )
        cur.execute(
            """
            UPDATE auth_email_change_requests
            SET
                used_at = now(),
                verified_at = now(),
                invalidated_reason = 'verified'
            WHERE id = %s
              AND used_at IS NULL
            """,
            (str(request_id),),
        )
    _commit_if_possible(bl)


def update_user_timezone(
    bl: Any,
    *,
    user_id: Any,
    timezone: str,
    only_if_empty: bool = False,
) -> None:
    query = "UPDATE users SET timezone = %s WHERE id = %s"
    if only_if_empty:
        query = (
            "UPDATE users SET timezone = %s WHERE id = %s AND (timezone IS NULL OR timezone = '')"
        )
    with bl.conn.cursor() as cur:
        cur.execute(query, (str(timezone or "").strip(), str(user_id or "").strip()))
    _commit_if_possible(bl)
    column_cache = getattr(bl, "_column_exists_cache", None)
    if isinstance(column_cache, dict):
        column_cache[("users", "timezone")] = True


def update_user_locale(
    bl: Any,
    *,
    user_id: Any,
    locale: str,
) -> None:
    with bl.conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET locale = %s WHERE id = %s",
            (str(locale or "").strip(), str(user_id or "").strip()),
        )
    _commit_if_possible(bl)
    column_cache = getattr(bl, "_column_exists_cache", None)
    if isinstance(column_cache, dict):
        column_cache[("users", "locale")] = True
