"""Extension callback declaration, token references and canonical URL policies."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import Request

from core.application.auth.serve_auth_runtime import public_base_url
from core.application.extensions.lifecycle_descriptor_io import _allow_private_remote_urls
from core.application.extensions.oauth_contract import is_canonical_https_origin
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

CALLBACK_TOKEN_BYTES = 32


CALLBACK_SECRET_BYTES = 32


CALLBACK_TOKEN_REDACTION = "<redacted>"


CALLBACK_SHARED_CREDENTIAL_REF_PREFIX = "extension_callback"


CALLBACK_CAPABILITY_REF_SUFFIX = "capability_token"


class OAuthCallbackPublicBaseURLUnavailable(ValueError):
    """No trustworthy canonical public callback origin is configured."""

    def __init__(self, message: str, *, reason: str = "unavailable") -> None:
        super().__init__(message)
        self.reason = (
            reason
            if reason in {"missing", "invalid", "unresolvable", "disallowed"}
            else "unavailable"
        )


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _extension_runtime_id(extension: dict[str, Any] | None) -> str:
    payload = _as_dict(extension)
    compiled = _as_dict(payload.get("compiled_manifest"))
    return (
        _normalize_text(payload.get("extension_key"))
        or _normalize_text(payload.get("extension_id"))
        or _normalize_text(compiled.get("extension_id"))
    )


def generate_callback_token() -> str:
    return secrets.token_urlsafe(CALLBACK_TOKEN_BYTES)


def generate_callback_secret() -> str:
    return secrets.token_urlsafe(CALLBACK_SECRET_BYTES)


def callback_token_hash(raw_token: str) -> str:
    return hashlib.sha256(_normalize_text(raw_token).encode("utf-8")).hexdigest()


def callback_secret_ref(registration_id: str) -> str:
    normalized_id = _normalize_text(registration_id)
    return f"{CALLBACK_SHARED_CREDENTIAL_REF_PREFIX}:{normalized_id}:shared_secret"


def callback_capability_ref(registration_id: str) -> str:
    normalized_id = _normalize_text(registration_id)
    return (
        f"{CALLBACK_SHARED_CREDENTIAL_REF_PREFIX}:{normalized_id}:{CALLBACK_CAPABILITY_REF_SUFFIX}"
    )


def _callbacks_from_extension(extension: dict | None) -> list[dict[str, Any]]:
    payload = _as_dict(extension)
    manifest = _as_dict(payload.get("compiled_manifest"))
    callbacks = payload.get("callbacks") or manifest.get("callbacks")
    return [dict(item) for item in _as_list(callbacks) if isinstance(item, dict)]


def find_callback_declaration(
    extension: dict | None,
    callback_id: str,
) -> dict[str, Any] | None:
    normalized_callback_id = _normalize_text(callback_id)
    if not normalized_callback_id:
        return None
    for callback in _callbacks_from_extension(extension):
        if _normalize_text(callback.get("callback_id")) == normalized_callback_id:
            return callback
    return None


def _callback_methods(declaration: dict | None) -> set[str]:
    return {
        _normalize_text(item).upper()
        for item in _as_list(_as_dict(declaration).get("methods"))
        if _normalize_text(item)
    }


def _callback_auth(declaration: dict | None) -> dict[str, Any]:
    auth = _as_dict(_as_dict(declaration).get("auth"))
    auth_type = _normalize_text(auth.get("type")).lower() or "none"
    payload = dict(auth)
    payload["type"] = auth_type
    return payload


def _forwarded_header_base_url(request: Request) -> str:
    forwarded = _normalize_text(request.headers.get("forwarded"))
    if forwarded:
        first_part = forwarded.split(",", 1)[0]
        attrs: dict[str, str] = {}
        for token in first_part.split(";"):
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            attrs[_normalize_text(key).lower()] = _normalize_text(value).strip('"')
        proto = _normalize_text(attrs.get("proto")).lower()
        host = _normalize_text(attrs.get("host"))
        if proto and host:
            return f"{proto}://{host}".rstrip("/")

    proto = _normalize_text(request.headers.get("x-forwarded-proto")).lower()
    host = _normalize_text(request.headers.get("x-forwarded-host"))
    if proto and host:
        return f"{proto}://{host}".rstrip("/")
    return ""


def _configured_callback_base_url() -> str:
    configured = public_base_url().rstrip("/")
    if not configured:
        return ""
    try:
        parsed = urlsplit(configured)
    except Exception:
        return configured
    host = _normalize_text(parsed.hostname).lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return ""
    return configured


def browser_oauth_callback_base_url(request: Request | None = None) -> str:
    """Return the configured canonical origin for extension browser OAuth.

    Browser redirects may use the exact local HTTPS loopback origins shipped by
    the open-source Compose stack. Other private or reserved destinations remain
    governed by the existing remote-URL policy. Request headers never contribute
    to this origin.
    """
    _ = request
    configured = _normalize_text(_runtime_env.get("FS_PUBLIC_BASE_URL"))
    if not is_canonical_https_origin(configured):
        raise OAuthCallbackPublicBaseURLUnavailable(
            "A canonical HTTPS browser OAuth origin (FS_PUBLIC_BASE_URL) must be configured",
            reason="invalid" if configured else "missing",
        )
    try:
        host = _normalize_text(urlsplit(configured).hostname).lower()
    except Exception as exc:
        raise OAuthCallbackPublicBaseURLUnavailable(
            "Configured browser OAuth origin is not allowed"
        ) from exc
    if host not in {"localhost", "127.0.0.1", "::1"} and not _allow_private_remote_urls():
        from core.application.extensions.url_safety import RemoteURLResolutionError

        try:
            from core.application.extensions.service_support import validate_remote_url

            validate_remote_url(configured, purpose="browser OAuth origin")
        except RemoteURLResolutionError as exc:
            raise OAuthCallbackPublicBaseURLUnavailable(
                "Configured browser OAuth origin cannot be resolved",
                reason="unresolvable",
            ) from exc
        except ValueError as exc:
            raise OAuthCallbackPublicBaseURLUnavailable(
                "Configured browser OAuth origin is not allowed",
                reason="disallowed",
            ) from exc
        except Exception as exc:
            raise OAuthCallbackPublicBaseURLUnavailable(
                "Configured browser OAuth origin could not be verified"
            ) from exc
    return configured


def _base_url_from_request(request: Request) -> str:
    configured = _configured_callback_base_url()
    if configured:
        return configured
    forwarded = _forwarded_header_base_url(request)
    if forwarded:
        return forwarded
    direct = str(request.base_url).rstrip("/")
    try:
        parsed = urlsplit(direct)
    except Exception:
        return direct
    if parsed.scheme and parsed.scheme != "https" and _normalize_text(request.headers.get("host")):
        host = _normalize_text(request.headers.get("host"))
        return urlunsplit((parsed.scheme, host, "", "", "")).rstrip("/")
    return str(request.base_url).rstrip("/")


def callback_base_url_for_provider_setup(request: Request | None = None) -> str:
    """Return the callback base URL safe to publish to an external webhook
    provider (the ``configure=true`` path).

    Unlike :func:`_base_url_from_request`, request-derived origins
    (Forwarded / X-Forwarded-Host / Host) are NOT trusted here: an attacker who
    can spoof those headers could otherwise have the callback token URL
    registered against a host they control (host poisoning, CAND-009 / 049). A
    configured canonical public base URL (``FS_PUBLIC_BASE_URL``) is required,
    and it must resolve to a public address.

    Raises ``ValueError`` if no trustworthy canonical base URL is configured.
    """
    _ = request
    configured = _configured_callback_base_url()
    if not configured:
        raise OAuthCallbackPublicBaseURLUnavailable(
            "A canonical public callback base URL (FS_PUBLIC_BASE_URL) must be "
            "configured before publishing callback URLs to an external provider"
        )
    if not _allow_private_remote_urls():
        try:
            from core.application.extensions.service_support import validate_remote_url

            validate_remote_url(configured, purpose="callback base_url")
        except Exception as exc:
            raise OAuthCallbackPublicBaseURLUnavailable(
                "Configured callback base URL is not allowed"
            ) from exc
    return configured


def callback_url(
    *,
    base_url: str,
    extension_id: str,
    callback_id: str,
    callback_token: str,
) -> str:
    return (
        f"{_normalize_text(base_url).rstrip('/')}/webhook/extensions/"
        f"{quote(_normalize_text(extension_id), safe='')}/"
        f"{quote(_normalize_text(callback_id), safe='')}/"
        f"{quote(_normalize_text(callback_token), safe='')}"
    )


def redacted_callback_path(extension_id: str, callback_id: str) -> str:
    return (
        f"/webhook/extensions/{quote(_normalize_text(extension_id), safe='')}/"
        f"{quote(_normalize_text(callback_id), safe='')}/{CALLBACK_TOKEN_REDACTION}"
    )


def redacted_callback_url(*, base_url: str, extension_id: str, callback_id: str) -> str:
    return f"{_normalize_text(base_url).rstrip('/')}{redacted_callback_path(extension_id, callback_id)}"
