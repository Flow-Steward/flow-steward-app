"""Opaque idempotency keys shared by every transport that starts a run or decides."""

from core.application.management.errors import ServiceValidationError


def validate_idempotency_key(value: str | None) -> str:
    if value is None:
        return ""
    normalized = str(value).strip()
    if (
        not normalized
        or len(normalized) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        message = "Idempotency-Key must be a non-empty opaque value of at most 255 characters"
        raise ServiceValidationError(
            message,
            status_code=422,
            payload={"error": message, "code": "invalid_idempotency_key"},
        )
    return normalized


__all__ = ["validate_idempotency_key"]
