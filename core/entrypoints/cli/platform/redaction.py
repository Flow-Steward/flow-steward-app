"""Compatibility import for the shared application credential redactor."""

from core.application.security.credential_redaction import (
    RedactionContext,
    redact,
    redact_output,
    redact_text,
)

__all__ = ("RedactionContext", "redact", "redact_output", "redact_text")
