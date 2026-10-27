"""Installer adapter for the shared credential-redaction policy."""

from __future__ import annotations

from core.application.security.credential_redaction import redact_text


def redact_installer_text(value: str) -> str:
    return redact_text(str(value))
