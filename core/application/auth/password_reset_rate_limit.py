"""Reusable password reset throttling helpers."""

from __future__ import annotations

import time
from typing import Any


def password_reset_throttle_key(client_ip: str, action: str, subject: str = "") -> str:
    normalized_action = str(action or "request").strip().lower() or "request"
    normalized_ip = str(client_ip or "").strip() or "unknown"
    normalized_subject = str(subject or "").strip().lower()
    if normalized_subject:
        return f"{normalized_action}:subject:{normalized_subject}:ip:{normalized_ip}"
    return f"{normalized_action}:ip:{normalized_ip}"


def _password_reset_throttle_keys(client_ip: str, action: str, subject: str = "") -> list[str]:
    subject_key = password_reset_throttle_key(client_ip, action, subject)
    aggregate_key = password_reset_throttle_key(client_ip, action)
    if subject_key == aggregate_key:
        return [aggregate_key]
    return [subject_key, aggregate_key]


def password_reset_throttle_check(
    client_ip: str,
    *,
    action: str = "request",
    subject: str = "",
    attempts_store: dict[str, dict[str, Any]],
    attempts_lock,
    rate_window_seconds: int,
    max_attempts: int,
    lockout_seconds: int,
    now_fn=time.time,
) -> tuple[bool, int]:
    """Return whether the password reset action is currently throttled."""
    now = now_fn()
    keys = _password_reset_throttle_keys(client_ip, action, subject)
    with attempts_lock:
        for key in keys:
            state = attempts_store.get(key) or {}
            locked_until = float(state.get("locked_until") or 0.0)
            if locked_until > now:
                return True, int(max(1, locked_until - now))
            if locked_until > 0:
                continue
            attempts = [
                float(ts)
                for ts in (state.get("attempts") or [])
                if now - float(ts) <= rate_window_seconds
            ]
            if len(attempts) >= max_attempts:
                return True, int(lockout_seconds)
    return False, 0


def password_reset_throttle_record(
    client_ip: str,
    *,
    action: str = "request",
    subject: str = "",
    attempts_store: dict[str, dict[str, Any]],
    attempts_lock,
    rate_window_seconds: int,
    max_attempts: int,
    lockout_seconds: int,
    now_fn=time.time,
) -> None:
    """Record a password reset action for future throttling decisions."""
    now = now_fn()
    keys = _password_reset_throttle_keys(client_ip, action, subject)
    with attempts_lock:
        for key in keys:
            state = attempts_store.get(key) or {}
            locked_until = float(state.get("locked_until") or 0.0)
            if locked_until > 0 and locked_until <= now:
                # Previous lockout expired — start fresh so the window resets.
                state = {}
            attempts = [
                float(ts)
                for ts in (state.get("attempts") or [])
                if now - float(ts) <= rate_window_seconds
            ]
            attempts.append(now)
            new_state: dict[str, Any] = {"attempts": attempts}
            if len(attempts) >= max_attempts:
                new_state["locked_until"] = now + lockout_seconds
            attempts_store[key] = new_state
