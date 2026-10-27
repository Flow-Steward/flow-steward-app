"""Helpers for normalizing auth email addresses."""

from __future__ import annotations

import re
from typing import Any

_EMAIL_PATTERN = re.compile(
    r"^(?=.{1,254}$)(?=.{1,64}@)[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)


def normalize_email_address(value: Any, *, field_name: str = "email") -> str:
    """Return a canonical lowercase email or raise ``ValueError``."""

    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if not _EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("Invalid email address")
    return normalized


def normalize_login_identifier(value: Any, *, field_name: str = "email") -> str:
    """Return a canonical lowercase email login identifier."""

    return normalize_email_address(value, field_name=field_name)
