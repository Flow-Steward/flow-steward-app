"""Shared abuse guard for paid model-provider verification probes."""

from __future__ import annotations

import json
import math
import time
from typing import Any

from core.application.auth.config import (
    MODEL_PROVIDER_VERIFY_MAX_ATTEMPTS,
    MODEL_PROVIDER_VERIFY_RATE_WINDOW_SECONDS,
)
from core.application.management.errors import ServiceUnavailableError
from core.infrastructure.db.db_pool import transaction_connection


def _rate_limit_keys(*, account_id: str, provider_id: str) -> tuple[str, str]:
    account_key = f"model-provider-verify:account:{account_id}"
    return account_key, f"{account_key}:provider:{provider_id}"


def _row_attempts(row: Any) -> list[Any]:
    if isinstance(row, dict):
        value = row.get("attempts_jsonb")
    else:
        try:
            value = row[0]
        except (IndexError, TypeError):
            value = []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = []
    return list(value or [])


def consume_attempt_window(
    conn: Any,
    *,
    keys: tuple[str, ...] | list[str],
    max_attempts: int,
    window_seconds: float,
    now: float | None = None,
    unavailable_message: str = "Rate limit is unavailable",
) -> tuple[bool, int]:
    """Atomically consume one attempt of every key's sliding window; ``(allowed, retry)``.

    The windows live in ``public.auth_rate_limits``, shared by every process. The
    limiter fails closed (``ServiceUnavailableError``) when its store is unavailable:
    an infrastructure fault must not turn into unlimited attempts.
    """
    observed_at = float(time.time() if now is None else now)
    cutoff = observed_at - float(window_seconds)
    try:
        with (
            transaction_connection(conn) as transaction_conn,
            transaction_conn.cursor() as cur,
        ):
            states: dict[str, list[float]] = {}
            retry_after = 0
            for key in sorted(keys):
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
                cur.execute(
                    "SELECT attempts_jsonb FROM public.auth_rate_limits WHERE key = %s FOR UPDATE",
                    (key,),
                )
                row = cur.fetchone()
                attempts = [float(value) for value in _row_attempts(row) if float(value) > cutoff]
                states[key] = attempts
                if len(attempts) >= max_attempts:
                    oldest = min(attempts)
                    retry_after = max(
                        retry_after,
                        int(max(1, math.ceil(oldest + float(window_seconds) - observed_at))),
                    )
            if retry_after:
                return False, retry_after
            for key, attempts in states.items():
                attempts.append(observed_at)
                cur.execute(
                    """
                    INSERT INTO public.auth_rate_limits
                        (key, attempts_jsonb, locked_until, updated_at)
                    VALUES (%s, %s::jsonb, NULL, now())
                    ON CONFLICT (key) DO UPDATE
                    SET attempts_jsonb = EXCLUDED.attempts_jsonb,
                        locked_until = NULL,
                        updated_at = now()
                    """,
                    (key, json.dumps(attempts)),
                )
    except Exception as exc:
        raise ServiceUnavailableError(
            unavailable_message,
            payload={"code": "rate_limit_unavailable", "message": unavailable_message},
        ) from exc
    return True, 0


def consume_model_provider_verification_limit(
    conn: Any,
    *,
    account_id: str,
    provider_id: str,
    now: float | None = None,
) -> tuple[bool, int]:
    """Atomically consume account/provider verification capacity.

    The limiter deliberately fails closed when its shared store is unavailable:
    an infrastructure fault must not turn into unlimited paid upstream calls.
    """
    try:
        return consume_attempt_window(
            conn,
            keys=_rate_limit_keys(account_id=account_id, provider_id=provider_id),
            max_attempts=MODEL_PROVIDER_VERIFY_MAX_ATTEMPTS,
            window_seconds=MODEL_PROVIDER_VERIFY_RATE_WINDOW_SECONDS,
            now=now,
            unavailable_message="Model provider verification rate limit is unavailable",
        )
    except ServiceUnavailableError as exc:
        raise ServiceUnavailableError(
            "Model provider verification rate limit is unavailable",
            payload={
                "code": "rate_limit_unavailable",
                "message": "Model provider verification is temporarily unavailable",
            },
        ) from exc.__cause__
