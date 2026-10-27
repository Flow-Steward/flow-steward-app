"""Symmetric encryption helpers for secret storage (AES-256-GCM).

The master key is sourced from the ``ENCRYPTION_KEY`` environment variable.
If the variable is not set, encrypted secret storage fails closed instead of
deriving a reusable key from broader application secrets.
"""

from __future__ import annotations

from core.infrastructure.security.secret_codec import (
    decrypt_secret as decrypt_secret,
)
from core.infrastructure.security.secret_codec import (
    encrypt_secret as encrypt_secret,
)


def mask_secret(value: str, visible: int = 4) -> str:
    """Return a masked version of a secret string (e.g. ``sk-****abcd``)."""
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]


__all__ = ["decrypt_secret", "encrypt_secret", "mask_secret"]
