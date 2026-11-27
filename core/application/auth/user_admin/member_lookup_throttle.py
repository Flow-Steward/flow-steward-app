"""Limits on finding and inviting people, shared by every process (ADR 0085).

A candidate search and an invitation each tell an administrator a little about who
uses the installation, and an invitation sends a message to someone. Both are counted
in ``public.auth_rate_limits`` (the store the sign-in and OAuth limiters share), so
the limit holds across every API process and worker:

- per administrator: candidate searches and invitations;
- per account: candidate searches and invitations of all its administrators;
- per recipient: invitations to one email address, from every account.

The count is taken in the caller's transaction: an invitation that is refused or
rolled back counts nothing. When the store is unavailable the request fails closed.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.application.management.errors import ServiceError

#: Refusal of a lookup or an invitation over a limit.
LOOKUP_THROTTLED_MESSAGE = "Too many searches or invitations. Try again in a few minutes."

CANDIDATE_LOOKUP_ACTION = "account_member_candidates"
INVITATION_ACTION = "account_member_invitation"

#: Candidate searches (one per settled keystroke) per administrator and per account.
CANDIDATE_LOOKUP_MAX = 60
CANDIDATE_LOOKUP_ACCOUNT_MAX = 200
#: Invitations per administrator and per account.
INVITATION_MAX = 20
INVITATION_ACCOUNT_MAX = 50
LOOKUP_WINDOW_SECONDS = 600
#: Invitations to one address from the whole installation, per day.
INVITATION_RECIPIENT_MAX = 5
INVITATION_RECIPIENT_WINDOW_SECONDS = 86_400

_KEY_PREFIX = "account-members"


class LookupThrottledError(ServiceError):
    """Searched or invited too often; ``retry_after`` seconds."""

    status_code = 429

    def __init__(self, retry_after: int) -> None:
        super().__init__(LOOKUP_THROTTLED_MESSAGE)
        self.retry_after = max(1, int(retry_after))


@dataclass(frozen=True, slots=True)
class ThrottleLimit:
    """At most ``max_attempts`` attempts of ``key`` in ``window_seconds``."""

    key: str
    max_attempts: int
    window_seconds: int


def _text(value: Any) -> str:
    return str(value or "").strip()


def candidate_limits(*, admin_user_id: str, account_id: str) -> list[ThrottleLimit]:
    """The limits one candidate search counts against."""
    action = CANDIDATE_LOOKUP_ACTION
    return [
        ThrottleLimit(
            f"{_KEY_PREFIX}:{action}:admin:{_text(admin_user_id) or 'unknown'}",
            CANDIDATE_LOOKUP_MAX,
            LOOKUP_WINDOW_SECONDS,
        ),
        ThrottleLimit(
            f"{_KEY_PREFIX}:{action}:account:{_text(account_id) or 'unknown'}",
            CANDIDATE_LOOKUP_ACCOUNT_MAX,
            LOOKUP_WINDOW_SECONDS,
        ),
    ]


def invitation_limits(
    *, admin_user_id: str, account_id: str, recipient: str
) -> list[ThrottleLimit]:
    """The limits one invitation counts against (``recipient`` is the normalized address)."""
    action = INVITATION_ACTION
    return [
        ThrottleLimit(
            f"{_KEY_PREFIX}:{action}:admin:{_text(admin_user_id) or 'unknown'}",
            INVITATION_MAX,
            LOOKUP_WINDOW_SECONDS,
        ),
        ThrottleLimit(
            f"{_KEY_PREFIX}:{action}:account:{_text(account_id) or 'unknown'}",
            INVITATION_ACCOUNT_MAX,
            LOOKUP_WINDOW_SECONDS,
        ),
        ThrottleLimit(
            f"{_KEY_PREFIX}:{action}:recipient:{_text(recipient).lower() or 'unknown'}",
            INVITATION_RECIPIENT_MAX,
            INVITATION_RECIPIENT_WINDOW_SECONDS,
        ),
    ]


def _attempts(row: Any) -> list[float]:
    if row is None:
        return []
    value = row.get("attempts_jsonb") if isinstance(row, Mapping) else row[0]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = []
    out: list[float] = []
    for item in value or ():
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


def consume(cur: Any, limits: Iterable[ThrottleLimit], *, now: float | None = None) -> None:
    """Count one attempt against every limit in the caller's transaction.

    Raises :class:`LookupThrottledError` (nothing counted) when any limit is full.
    """
    observed = float(time.time() if now is None else now)
    ordered = sorted({limit.key: limit for limit in limits}.values(), key=lambda item: item.key)
    states: dict[str, list[float]] = {}
    retry_after = 0
    for limit in ordered:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (limit.key,))
        cur.execute(
            "SELECT attempts_jsonb FROM public.auth_rate_limits WHERE key = %s FOR UPDATE",
            (limit.key,),
        )
        cutoff = observed - float(limit.window_seconds)
        attempts = [value for value in _attempts(cur.fetchone()) if value > cutoff]
        states[limit.key] = attempts
        if len(attempts) >= limit.max_attempts:
            oldest = min(attempts)
            retry_after = max(
                retry_after,
                int(max(1, math.ceil(oldest + float(limit.window_seconds) - observed))),
            )
    if retry_after:
        raise LookupThrottledError(retry_after)
    for key, attempts in states.items():
        attempts.append(observed)
        cur.execute(
            """
            INSERT INTO public.auth_rate_limits (key, attempts_jsonb, locked_until, updated_at)
            VALUES (%s, %s::jsonb, NULL, now())
            ON CONFLICT (key) DO UPDATE
            SET attempts_jsonb = EXCLUDED.attempts_jsonb,
                locked_until = NULL,
                updated_at = now()
            """,
            (key, json.dumps(attempts)),
        )


#: How services count (tests pass an in-memory one).
Throttle = Callable[[Any, list[ThrottleLimit]], None]


__all__ = [
    "CANDIDATE_LOOKUP_ACCOUNT_MAX",
    "CANDIDATE_LOOKUP_ACTION",
    "CANDIDATE_LOOKUP_MAX",
    "INVITATION_ACCOUNT_MAX",
    "INVITATION_ACTION",
    "INVITATION_MAX",
    "INVITATION_RECIPIENT_MAX",
    "INVITATION_RECIPIENT_WINDOW_SECONDS",
    "LOOKUP_THROTTLED_MESSAGE",
    "LOOKUP_WINDOW_SECONDS",
    "LookupThrottledError",
    "Throttle",
    "ThrottleLimit",
    "candidate_limits",
    "consume",
    "invitation_limits",
]
