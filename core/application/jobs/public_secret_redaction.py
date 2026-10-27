"""Small deterministic secret redactor for token-visible workflow contracts."""

from __future__ import annotations

from core.infrastructure.security.secret_detection import (
    contains_detected_secret,
    redact_detected_secrets,
)


def redact_public_secrets(value: str) -> str:
    return redact_detected_secrets(str(value or ""))


def contains_public_secret(value: str) -> bool:
    text = str(value or "")
    return contains_detected_secret(text) or redact_public_secrets(text) != text


__all__ = ["contains_public_secret", "redact_public_secrets"]
