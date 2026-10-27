"""Infrastructure-owned symmetric codec for encrypted secret storage."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.infrastructure.config.runtime_env import runtime_env

_ENV_KEY = "ENCRYPTION_KEY"
_logger = logging.getLogger(__name__)


def _master_key() -> bytes:
    raw = runtime_env.get(_ENV_KEY, "").strip()
    if raw:
        return hashlib.sha256(raw.encode()).digest()
    _logger.error("ENCRYPTION_KEY is not set; encrypted secret storage is unavailable.")
    raise RuntimeError("ENCRYPTION_KEY is required for encrypted secret storage")


def derive_secret_storage_key(
    context: bytes,
    master_key: bytes | None = None,
) -> bytes:
    """Derive a domain-separated 256-bit key from the canonical encryption root."""
    if not isinstance(context, bytes) or not context:
        raise ValueError("secret storage key context must be non-empty bytes")
    root_key = master_key or _master_key()
    return hmac.new(root_key, context, hashlib.sha256).digest()


def encrypt_secret(plaintext: str, master_key: bytes | None = None) -> str:
    key = master_key or _master_key()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(ciphertext_b64: str, master_key: bytes | None = None) -> str:
    key = master_key or _master_key()
    raw = base64.b64decode(ciphertext_b64)
    plaintext = AESGCM(key).decrypt(raw[:12], raw[12:], None)
    return plaintext.decode("utf-8")


def secret_value_fingerprint(plaintext: str, master_key: bytes | None = None) -> str:
    """Return a stable, keyed, non-revealing identity for idempotency only."""
    root_key = master_key or _master_key()
    digest_key = hmac.new(
        root_key,
        b"flow-steward.workflow-secret-fingerprint.v1",
        hashlib.sha256,
    ).digest()
    digest = hmac.new(digest_key, str(plaintext).encode(), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


__all__ = [
    "decrypt_secret",
    "derive_secret_storage_key",
    "encrypt_secret",
    "secret_value_fingerprint",
]
