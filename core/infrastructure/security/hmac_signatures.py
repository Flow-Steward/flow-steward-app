"""The one HMAC-SHA256 primitive for verifying signed third-party payloads.

Both places that verify someone else's signature use this module: webhook
ingress, which streams a request body it must not buffer, and the OAuth callback,
which signs a short canonical string. That is why the incremental signer is
exported alongside the one-shot digest — a single helper that only took bytes
would have forced the webhook path to hold an entire body in memory, and a second
implementation is exactly what this module exists to prevent.

Deliberately dependency-free, so importing it does not pull the HTTP layer into
roles that have none.
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["hmac_digest_matches", "hmac_sha256_hex", "hmac_sha256_signer"]


def hmac_sha256_signer(*, secret: str) -> hmac.HMAC:
    """An HMAC-SHA256 accumulator to feed a payload into piece by piece."""
    return hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)


def hmac_sha256_hex(*, secret: str, payload: bytes) -> str:
    """The hex HMAC-SHA256 of one byte payload under one secret."""
    signer = hmac_sha256_signer(secret=secret)
    signer.update(payload)
    return signer.hexdigest()


def hmac_digest_matches(expected: str, provided: str) -> bool:
    """Constant-time comparison of two encoded digests.

    Emptiness and length are settled first because a comparison between digests
    of different lengths cannot be a match anyway; what matters is that the
    comparison which does run never short-circuits on content.
    """
    if not expected or not provided or len(expected) != len(provided):
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
