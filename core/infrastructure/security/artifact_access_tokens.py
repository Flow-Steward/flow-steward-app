"""Self-contained tokens that let the platform serve an artifact it cannot presign.

AC17-R123. An extension subprocess reaches its artifacts through a URL the host
hands it. Object storage mints that URL itself; a filesystem backend has no URL to
mint, so its grants came out as ``file://`` and were refused -- correctly, since a
path is not a capability. The platform therefore serves the object itself, and this
is what authorizes that request.

It cannot be a session: the subprocess environment is an allowlist carrying no
credential, and deliberately so. It cannot be the platform grant signature either --
that signature exists so the *extension* can tell a real grant from an invented one,
and verifying it requires the whole descriptor, which the request does not carry. So
the token is self-contained: everything the endpoint must check travels inside it,
signed, and nothing is looked up.

It is narrow on purpose. One object, one method, one short window. A token for a
read cannot write, a token for one artifact cannot name another, and an expired one
is refused without the endpoint knowing anything about the artifact at all.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

# Distinct from the platform grant's context so a signature minted for one purpose
# can never be presented as the other, even though both derive from FS_SECRET_KEY.
_CONTEXT = b"flow-steward-artifact-access-token-v1\0"
_DEFAULT_TTL_SECONDS = 600
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 3600
# Every shape of unreadable token gets the same words. A caller learns that its
# token did not work and nothing about which part of it we could not read.
_MALFORMED = "Artifact access token is malformed"

HEADER_NAME = "x-platform-artifact-access"


class ArtifactAccessTokenError(ValueError):
    """The token is absent, malformed, expired, or not for what was asked."""


def _signing_key() -> bytes | None:
    secret = str(_runtime_env.get("FS_SECRET_KEY") or "").strip()
    if not secret:
        return None
    return hashlib.sha256(_CONTEXT + secret.encode("utf-8")).digest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_artifact_access_token(
    *,
    object_id: str,
    kind: str,
    method: str,
    now: datetime | None = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> str:
    """Return a token for exactly this object, kind and method, or "" with no key."""
    key = _signing_key()
    if key is None:
        return ""
    issued_at = (now or datetime.now(UTC)).astimezone(UTC)
    ttl = max(_MIN_TTL_SECONDS, min(int(ttl_seconds), _MAX_TTL_SECONDS))
    claims = {
        "version": 1,
        "object_id": str(object_id or "").strip(),
        "kind": str(kind or "").strip().lower(),
        "method": str(method or "").strip().upper(),
        "expires_at": (issued_at + timedelta(seconds=ttl)).isoformat().replace("+00:00", "Z"),
    }
    if not claims["object_id"] or not claims["kind"] or not claims["method"]:
        raise ArtifactAccessTokenError("An artifact access token needs an object, kind and method")
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(signature)}"


def verify_artifact_access_token(
    token: str,
    *,
    object_id: str,
    kind: str,
    method: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the claims when the token authorizes exactly this request.

    Every mismatch raises the same error type with a reason that names the condition
    and never the expected value: an endpoint that says which artifact a token was
    meant for has answered a question the caller was not entitled to ask.
    """
    key = _signing_key()
    if key is None:
        raise ArtifactAccessTokenError("Artifact access tokens are unavailable")
    raw = str(token or "").strip()
    encoded_payload, _, encoded_signature = raw.partition(".")
    if not encoded_payload or not encoded_signature:
        raise ArtifactAccessTokenError(_MALFORMED)
    try:
        payload = _unb64url(encoded_payload)
        signature = _unb64url(encoded_signature)
    except (ValueError, TypeError) as exc:
        raise ArtifactAccessTokenError(_MALFORMED) from exc
    if not hmac.compare_digest(signature, hmac.new(key, payload, hashlib.sha256).digest()):
        raise ArtifactAccessTokenError("Artifact access token is not valid")
    try:
        claims = json.loads(payload.decode("utf-8"))
    except ValueError as exc:  # UnicodeDecodeError is one
        raise ArtifactAccessTokenError(_MALFORMED) from exc
    if not isinstance(claims, dict) or claims.get("version") != 1:
        raise ArtifactAccessTokenError(_MALFORMED)

    expected = {
        "object_id": str(object_id or "").strip(),
        "kind": str(kind or "").strip().lower(),
        "method": str(method or "").strip().upper(),
    }
    for field, wanted in expected.items():
        if str(claims.get(field) or "") != wanted:
            raise ArtifactAccessTokenError("Artifact access token is not for this request")
    try:
        expires_at = datetime.fromisoformat(
            str(claims.get("expires_at") or "").replace("Z", "+00:00")
        ).astimezone(UTC)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ArtifactAccessTokenError("Artifact access token has no usable expiry") from exc
    if (now or datetime.now(UTC)).astimezone(UTC) >= expires_at:
        raise ArtifactAccessTokenError("Artifact access token has expired")
    return dict(claims)


__all__ = [
    "HEADER_NAME",
    "ArtifactAccessTokenError",
    "issue_artifact_access_token",
    "verify_artifact_access_token",
]
