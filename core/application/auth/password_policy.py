"""Shared password rules for local human accounts."""

from __future__ import annotations

from datetime import UTC, datetime
from math import ceil
from unicodedata import category

PASSWORD_MAX_AGE_DAYS = 365
PASSWORD_WARNING_DAYS = 14


def password_policy_error(password: str, min_length: int, *, field: str = "password") -> str:
    minimum = max(12, min_length)
    if len(password) < minimum:
        return f"{field} must be at least {minimum} characters"
    if not (
        any(character.isupper() for character in password)
        and any(character.islower() for character in password)
        and any(character.isdecimal() for character in password)
        and any(category(character)[0] in "PS" for character in password)
    ):
        return f"{field} must include uppercase and lowercase letters, a number, and a special character"
    return ""


def password_expires_in_days(changed_at: object, *, now: datetime | None = None) -> int | None:
    if not isinstance(changed_at, datetime):
        return None
    changed_at = changed_at if changed_at.tzinfo is not None else changed_at.replace(tzinfo=UTC)
    elapsed_seconds = ((now or datetime.now(UTC)) - changed_at).total_seconds()
    return max(0, ceil(PASSWORD_MAX_AGE_DAYS - elapsed_seconds / 86_400))
