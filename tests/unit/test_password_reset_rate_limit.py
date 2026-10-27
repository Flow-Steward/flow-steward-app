from __future__ import annotations

import threading

from core.application.auth.config import (
    AUTH_PASSWORD_RESET_LOCKOUT_SECONDS,
    AUTH_PASSWORD_RESET_MAX_ATTEMPTS,
    AUTH_PASSWORD_RESET_RATE_WINDOW_SECONDS,
)
from core.application.auth.password_reset_rate_limit import (
    password_reset_throttle_check,
    password_reset_throttle_record,
)


def test_password_reset_throttle_is_scoped_by_action() -> None:
    attempts_store: dict[str, dict[str, float]] = {}
    attempts_lock = threading.Lock()
    client_ip = "127.0.0.1"

    for _ in range(AUTH_PASSWORD_RESET_MAX_ATTEMPTS):
        password_reset_throttle_record(
            client_ip,
            action="request",
            attempts_store=attempts_store,
            attempts_lock=attempts_lock,
            rate_window_seconds=AUTH_PASSWORD_RESET_RATE_WINDOW_SECONDS,
            max_attempts=AUTH_PASSWORD_RESET_MAX_ATTEMPTS,
            lockout_seconds=AUTH_PASSWORD_RESET_LOCKOUT_SECONDS,
        )

    throttled_request, retry_after_request = password_reset_throttle_check(
        client_ip,
        action="request",
        attempts_store=attempts_store,
        attempts_lock=attempts_lock,
        rate_window_seconds=AUTH_PASSWORD_RESET_RATE_WINDOW_SECONDS,
        max_attempts=AUTH_PASSWORD_RESET_MAX_ATTEMPTS,
        lockout_seconds=AUTH_PASSWORD_RESET_LOCKOUT_SECONDS,
    )
    throttled_verify, retry_after_verify = password_reset_throttle_check(
        client_ip,
        action="verify",
        attempts_store=attempts_store,
        attempts_lock=attempts_lock,
        rate_window_seconds=AUTH_PASSWORD_RESET_RATE_WINDOW_SECONDS,
        max_attempts=AUTH_PASSWORD_RESET_MAX_ATTEMPTS,
        lockout_seconds=AUTH_PASSWORD_RESET_LOCKOUT_SECONDS,
    )

    assert throttled_request is True
    assert retry_after_request > 0
    assert throttled_verify is False
    assert retry_after_verify == 0


def test_password_reset_request_throttle_blocks_rotated_subject() -> None:
    attempts_store: dict[str, dict[str, float]] = {}
    attempts_lock = threading.Lock()
    client_ip = "127.0.0.1"

    for _ in range(AUTH_PASSWORD_RESET_MAX_ATTEMPTS):
        password_reset_throttle_record(
            client_ip,
            action="request",
            subject="first-user",
            attempts_store=attempts_store,
            attempts_lock=attempts_lock,
            rate_window_seconds=AUTH_PASSWORD_RESET_RATE_WINDOW_SECONDS,
            max_attempts=AUTH_PASSWORD_RESET_MAX_ATTEMPTS,
            lockout_seconds=AUTH_PASSWORD_RESET_LOCKOUT_SECONDS,
        )

    throttled_first, retry_after_first = password_reset_throttle_check(
        client_ip,
        action="request",
        subject="first-user",
        attempts_store=attempts_store,
        attempts_lock=attempts_lock,
        rate_window_seconds=AUTH_PASSWORD_RESET_RATE_WINDOW_SECONDS,
        max_attempts=AUTH_PASSWORD_RESET_MAX_ATTEMPTS,
        lockout_seconds=AUTH_PASSWORD_RESET_LOCKOUT_SECONDS,
    )
    throttled_second, retry_after_second = password_reset_throttle_check(
        client_ip,
        action="request",
        subject="second-user",
        attempts_store=attempts_store,
        attempts_lock=attempts_lock,
        rate_window_seconds=AUTH_PASSWORD_RESET_RATE_WINDOW_SECONDS,
        max_attempts=AUTH_PASSWORD_RESET_MAX_ATTEMPTS,
        lockout_seconds=AUTH_PASSWORD_RESET_LOCKOUT_SECONDS,
    )

    assert throttled_first is True
    assert retry_after_first > 0
    assert throttled_second is True
    assert retry_after_second > 0
