"""Authenticator and recovery-code primitives for local multi-factor authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime

from cryptography.hazmat.primitives.hashes import SHA1
from cryptography.hazmat.primitives.twofactor.totp import TOTP as _Totp

_DIGITS = 6
_PERIOD_SECONDS = 30
_SECRET_BYTES = 20
_RECOVERY_BYTES = 16
_RECOVERY_CODE_COUNT = 10
_RECOVERY_GROUP_LENGTH = 8
_RECOVERY_CODE_PATTERN = re.compile(r"(?:[a-fA-F0-9]{32}|(?:[a-fA-F0-9]{8}-){3}[a-fA-F0-9]{8})")


def _authenticator(secret: str) -> _Totp:
    # HMAC-SHA-1 is the interoperable authenticator default specified by RFC 6238.
    return _Totp(base64.b32decode(secret), _DIGITS, SHA1(), _PERIOD_SECONDS)  # nosec B303


def new_totp_secret() -> str:
    """Return the base32 key expected by authenticator apps."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii")


def provisioning_uri(secret: str, username: str) -> str:
    """Build the URI used to provision a user's authenticator."""
    return _authenticator(secret).get_provisioning_uri(username, "Flow Steward")


def is_totp_code(code: str) -> bool:
    return len(code) == _DIGITS and code.isascii() and code.isdecimal()


def matching_totp_step(secret: str, code: str, *, now: datetime) -> int | None:
    """Return the most recent matching step within one step of server time."""
    if not is_totp_code(code):
        return None
    authenticator = _authenticator(secret)
    current_step = int(now.timestamp()) // _PERIOD_SECONDS
    candidate = code.encode("ascii")
    for step in (current_step + 1, current_step, current_step - 1):
        if step >= 0 and hmac.compare_digest(
            candidate, authenticator.generate(step * _PERIOD_SECONDS)
        ):
            return step
    return None


def new_recovery_codes() -> list[str]:
    """Generate independently random, printable recovery codes."""
    return [_new_recovery_code() for _ in range(_RECOVERY_CODE_COUNT)]


def _new_recovery_code() -> str:
    code = secrets.token_hex(_RECOVERY_BYTES)
    return "-".join(
        code[index : index + _RECOVERY_GROUP_LENGTH]
        for index in range(0, len(code), _RECOVERY_GROUP_LENGTH)
    )


def is_recovery_code(code: str) -> bool:
    return _RECOVERY_CODE_PATTERN.fullmatch(code) is not None


def recovery_code_hash(code: str) -> str:
    """Hash a printable recovery code for lookup without storing the code."""
    normalized = code.replace("-", "").lower()
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()
