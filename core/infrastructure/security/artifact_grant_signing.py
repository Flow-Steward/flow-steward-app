"""Host-issued signatures for narrowly scoped private artifact upload grants."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_CONTEXT = b"flow-steward-platform-artifact-grant-v1\0"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def artifact_grant_claims(descriptor: dict[str, Any]) -> dict[str, Any]:
    access = dict(descriptor.get("access") or {})
    upload_url = str(access.get("upload_url") or "").strip()
    method = "PUT" if upload_url else "GET"
    granted_url = upload_url or str(access.get("download_url") or "").strip()
    return {
        "version": 1,
        "artifact_id": str(descriptor.get("artifact_id") or "").strip(),
        "artifact_kind": str(descriptor.get("artifact_kind") or "artifact").strip(),
        "binding_key": str(descriptor.get("binding_key") or "").strip(),
        "content_type": str(descriptor.get("content_type") or "").strip(),
        "scope": dict(descriptor.get("scope") or {}),
        "method": method,
        "upload_url": granted_url,
        "max_size_bytes": int(access.get("max_size_bytes") or 0),
        "required_headers": dict(access.get("required_headers") or {}),
        "expires_at": str(access.get("expires_at") or "").strip(),
        "single_use": bool(access.get("single_use")),
    }


def canonical_artifact_grant_claims(descriptor: dict[str, Any]) -> bytes:
    return json.dumps(
        artifact_grant_claims(descriptor),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _private_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    secret = str(_runtime_env.get("FS_SECRET_KEY") or "").strip()
    if not secret:
        return None
    seed = hashlib.sha256(_CONTEXT + secret.encode("utf-8")).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def artifact_grant_verification_key() -> str:
    key = _private_key()
    return _b64url(key.public_key().public_bytes_raw()) if key is not None else ""


def sign_platform_artifact_grant(
    descriptor: dict[str, Any],
    *,
    now: datetime | None = None,
    ttl_seconds: int = 600,
) -> dict[str, Any]:
    key = _private_key()
    if key is None:
        return dict(descriptor)
    signed = copy.deepcopy(descriptor)
    access = dict(signed.get("access") or {})
    access["expires_at"] = (
        ((now or datetime.now(UTC)) + timedelta(seconds=max(60, min(int(ttl_seconds), 3600))))
        .isoformat()
        .replace("+00:00", "Z")
    )
    access["single_use"] = bool(access.get("upload_url"))
    signed["access"] = access
    access["platform_grant"] = {
        "version": 1,
        "signature": _b64url(key.sign(canonical_artifact_grant_claims(signed))),
    }
    return signed


__all__ = [
    "artifact_grant_claims",
    "artifact_grant_verification_key",
    "canonical_artifact_grant_claims",
    "sign_platform_artifact_grant",
]
