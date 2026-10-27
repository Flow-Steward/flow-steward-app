"""Login rate limiting with shared-store support and in-memory fallback."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Protocol, overload

from core.application.auth.config import (
    AUTH_LOGIN_LOCKOUT_SECONDS,
    AUTH_LOGIN_MAX_ATTEMPTS,
    AUTH_LOGIN_RATE_WINDOW_SECONDS,
)

_auth_login_lock = threading.Lock()
_auth_login_attempts: dict[str, dict[str, Any]] = {}
_AUTH_RATE_LIMIT_TABLE = "auth_rate_limits"


class ClearUserLoginFailures(Protocol):
    @overload
    def __call__(self, username: str) -> None: ...

    @overload
    def __call__(self, username: str, *, bl: Any) -> None: ...

    def __call__(self, username: str, *, bl: Any = None) -> None: ...


def _auth_login_keys(username: str, client_ip: str) -> list[str]:
    normalized_user = str(username or "").strip().lower()
    normalized_ip = str(client_ip or "").strip()
    keys: list[str] = []
    if normalized_user:
        keys.append(f"user:{normalized_user}")
    if normalized_ip:
        keys.append(f"ip:{normalized_ip}")
    if not keys:
        keys.append("ip:unknown")
    return keys


def _auth_login_subject_keys(username: str) -> list[str]:
    normalized_user = str(username or "").strip().lower()
    return [f"user:{normalized_user}"] if normalized_user else []


def _conn_commit(conn: Any) -> None:
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def _conn_rollback(conn: Any) -> None:
    rollback = getattr(conn, "rollback", None)
    if callable(rollback):
        rollback()


def _shared_rate_limit_store_available(bl: Any) -> bool:
    if not bl or not getattr(bl, "conn", None):
        return False
    table_exists = getattr(bl, "_table_exists", None)
    if not callable(table_exists):
        return False
    try:
        return table_exists(_AUTH_RATE_LIMIT_TABLE) is True
    except Exception:
        return False


def _row_value(row: Any, key: str, index: int) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[index]
    except Exception:
        return None


def _normalize_attempts(value: Any, now: float) -> list[float]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = []
    return [float(ts) for ts in (value or []) if now - float(ts) <= AUTH_LOGIN_RATE_WINDOW_SECONDS]


def _lock_db_rate_key(cur: Any, key: str) -> None:
    try:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
    except Exception:
        return


def _load_db_rate_state(cur: Any, key: str, now: float) -> dict[str, Any]:
    cur.execute(
        """
        SELECT
            attempts_jsonb,
            EXTRACT(EPOCH FROM locked_until) AS locked_until_epoch
        FROM public.auth_rate_limits
        WHERE key = %s
        FOR UPDATE
        """,
        (key,),
    )
    row = cur.fetchone()
    if not row:
        return {"attempts": [], "locked_until": 0.0}
    return {
        "attempts": _normalize_attempts(_row_value(row, "attempts_jsonb", 0), now),
        "locked_until": float(_row_value(row, "locked_until_epoch", 1) or 0.0),
    }


def _store_db_rate_state(
    cur: Any,
    key: str,
    *,
    attempts: list[float],
    locked_until: float,
) -> None:
    locked_until_param = float(locked_until) if locked_until > 0 else None
    cur.execute(
        """
        INSERT INTO public.auth_rate_limits (key, attempts_jsonb, locked_until, updated_at)
        VALUES (
            %s,
            %s::jsonb,
            CASE
                WHEN %s::double precision IS NULL THEN NULL
                ELSE to_timestamp(%s::double precision)
            END,
            now()
        )
        ON CONFLICT (key) DO UPDATE
        SET
            attempts_jsonb = EXCLUDED.attempts_jsonb,
            locked_until = EXCLUDED.locked_until,
            updated_at = now()
        """,
        (key, json.dumps(attempts), locked_until_param, locked_until_param),
    )


def _auth_login_lock_state_db(bl: Any, keys: list[str], now: float) -> tuple[bool, int]:
    lock_until = 0.0
    with bl.conn.cursor() as cur:
        for key in keys:
            _lock_db_rate_key(cur, key)
            state = _load_db_rate_state(cur, key, now)
            locked_until = float(state.get("locked_until") or 0.0)
            if locked_until > now:
                lock_until = max(lock_until, locked_until)
    _conn_commit(bl.conn)
    if lock_until > now:
        return True, int(max(1, lock_until - now))
    return False, 0


def _auth_login_record_failure_db(bl: Any, keys: list[str], now: float) -> tuple[bool, int]:
    lock_until = 0.0
    with bl.conn.cursor() as cur:
        for key in keys:
            _lock_db_rate_key(cur, key)
            state = _load_db_rate_state(cur, key, now)
            attempts = list(state.get("attempts") or [])
            attempts.append(now)
            existing_lock_until = float(state.get("locked_until") or 0.0)
            if existing_lock_until > now:
                key_lock_until = existing_lock_until
            elif len(attempts) >= AUTH_LOGIN_MAX_ATTEMPTS:
                key_lock_until = now + AUTH_LOGIN_LOCKOUT_SECONDS
            else:
                key_lock_until = 0.0
            _store_db_rate_state(
                cur,
                key,
                attempts=attempts,
                locked_until=key_lock_until,
            )
            lock_until = max(lock_until, key_lock_until)
    _conn_commit(bl.conn)
    if lock_until > now:
        return True, int(max(1, lock_until - now))
    return False, 0


def _auth_login_clear_failures_db(bl: Any, keys: list[str]) -> None:
    with bl.conn.cursor() as cur:
        for key in keys:
            _lock_db_rate_key(cur, key)
            cur.execute("DELETE FROM public.auth_rate_limits WHERE key = %s", (key,))
    _conn_commit(bl.conn)


def auth_login_lock_state(username: str, client_ip: str, *, bl: Any = None) -> tuple[bool, int]:
    """Return whether the submitted login subject is currently locked out.

    The IP-wide limiter remains authoritative when another failed attempt is
    recorded, preventing username spraying. It is deliberately not a preflight
    lock: otherwise junk attempts behind a shared proxy deny correct logins for
    every unrelated user on that proxy.
    """
    del client_ip
    now = time.time()
    keys = _auth_login_subject_keys(username)
    if not keys:
        return False, 0
    if _shared_rate_limit_store_available(bl):
        try:
            return _auth_login_lock_state_db(bl, keys, now)
        except Exception:
            _conn_rollback(getattr(bl, "conn", None))
            return True, AUTH_LOGIN_LOCKOUT_SECONDS
    lock_until = 0.0
    with _auth_login_lock:
        for user_key in keys:
            state = _auth_login_attempts.get(user_key) or {}
            locked_until = float(state.get("locked_until") or 0.0)
            if locked_until > now:
                lock_until = max(lock_until, locked_until)
    if lock_until > now:
        return True, int(max(1, lock_until - now))
    return False, 0


def auth_login_record_failure(username: str, client_ip: str, *, bl: Any = None) -> tuple[bool, int]:
    now = time.time()
    keys = _auth_login_keys(username, client_ip)
    if _shared_rate_limit_store_available(bl):
        try:
            return _auth_login_record_failure_db(bl, keys, now)
        except Exception:
            _conn_rollback(getattr(bl, "conn", None))
            return True, AUTH_LOGIN_LOCKOUT_SECONDS
    lock_until = 0.0
    with _auth_login_lock:
        for key in keys:
            state = _auth_login_attempts.get(key) or {}
            attempts = [
                float(ts)
                for ts in (state.get("attempts") or [])
                if now - float(ts) <= AUTH_LOGIN_RATE_WINDOW_SECONDS
            ]
            attempts.append(now)
            new_state: dict[str, Any] = {"attempts": attempts}
            existing_lock_until = float(state.get("locked_until") or 0.0)
            if existing_lock_until > now:
                new_state["locked_until"] = existing_lock_until
            elif len(attempts) >= AUTH_LOGIN_MAX_ATTEMPTS:
                new_state["locked_until"] = now + AUTH_LOGIN_LOCKOUT_SECONDS
            _auth_login_attempts[key] = new_state
            lock_until = max(lock_until, float(new_state.get("locked_until") or 0.0))
    if lock_until > now:
        return True, int(max(1, lock_until - now))
    return False, 0


def auth_login_clear_failures(username: str, client_ip: str, *, bl: Any = None) -> None:
    keys = _auth_login_keys(username, client_ip)
    if _shared_rate_limit_store_available(bl):
        try:
            _auth_login_clear_failures_db(bl, keys)
            return
        except Exception:
            _conn_rollback(getattr(bl, "conn", None))
    with _auth_login_lock:
        for key in keys:
            _auth_login_attempts.pop(key, None)


def auth_login_clear_user_failures(username: str, *, bl: Any = None) -> None:
    normalized_user = str(username or "").strip().lower()
    if not normalized_user:
        return
    user_key = f"user:{normalized_user}"
    if _shared_rate_limit_store_available(bl):
        try:
            _auth_login_clear_failures_db(bl, [user_key])
            return
        except Exception:
            _conn_rollback(getattr(bl, "conn", None))
    with _auth_login_lock:
        _auth_login_attempts.pop(user_key, None)
