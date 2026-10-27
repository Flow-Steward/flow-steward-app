"""CSRF and origin validation — browser-specific, requires Request."""

from __future__ import annotations

import hmac
from urllib.parse import SplitResult, urlparse

from core.application.auth.config import (
    AUTH_BOOTSTRAP_TOKEN_HEADER,
    AUTH_CSRF_COOKIE_NAME,
    AUTH_CSRF_HEADER_NAME,
    env_flag,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def auth_cookie_secure() -> bool:
    return env_flag("FS_AUTH_COOKIE_SECURE", True)


def trusted_auth_origins() -> set[str]:

    configured = str(_runtime_env.get("FS_AUTH_TRUSTED_ORIGINS", "") or "").strip()
    if not configured:
        return set()
    return {item.strip().rstrip("/").lower() for item in configured.split(",") if item.strip()}


def _compose_origin(scheme: str, netloc: str) -> str:
    normalized_scheme = str(scheme or "").strip().lower()
    normalized_netloc = str(netloc or "").strip().lower()
    if not normalized_scheme or not normalized_netloc:
        return ""
    return SplitResult(normalized_scheme, normalized_netloc, "", "", "").geturl()


def origin_host(origin_str: str) -> str:
    text = str(origin_str or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except Exception:
        return ""
    scheme = str(parsed.scheme or "").strip().lower()
    netloc = str(parsed.netloc or "").strip().lower()
    if not scheme or not netloc:
        return ""
    return _compose_origin(scheme, netloc)


def expected_request_origin(request) -> str:
    host = str(request.headers.get("host") or "").strip().lower()
    if not host:
        return ""
    origin_hint = str(request.headers.get("origin") or "").strip().lower()
    if origin_hint.startswith("https://"):
        scheme = "https"
    elif origin_hint.startswith("http://"):
        scheme = "http"
    else:
        scheme = "https" if auth_cookie_secure() else "http"
    return _compose_origin(scheme, host)


def same_origin_request(request) -> bool:
    trusted = trusted_auth_origins()
    expected = expected_request_origin(request)
    if expected:
        trusted.add(expected)
    origin = origin_host(request.headers.get("origin") or "")
    if origin:
        return origin in trusted
    referer = str(request.headers.get("referer") or "").strip()
    referer_origin = origin_host(referer)
    if referer_origin:
        return referer_origin in trusted
    return False


def csrf_request_valid(request) -> bool:
    header_value = str(request.headers.get(AUTH_CSRF_HEADER_NAME) or "").strip()
    cookie_value = str(request.cookies.get(AUTH_CSRF_COOKIE_NAME) or "").strip()
    if header_value and cookie_value and hmac.compare_digest(header_value, cookie_value):
        return True
    return same_origin_request(request)


def bootstrap_request_allowed(request) -> tuple[bool, str]:
    if not env_flag("FS_AUTH_BOOTSTRAP_ENABLED", True):
        return False, "Bootstrap endpoint is disabled by configuration"

    required_token = str(_runtime_env.get("FS_AUTH_BOOTSTRAP_TOKEN", "") or "").strip()
    if required_token:
        from core.application.auth.bootstrap_tokens import bootstrap_token_request_valid

        provided = str(request.headers.get(AUTH_BOOTSTRAP_TOKEN_HEADER) or "").strip()
        return bootstrap_token_request_valid(
            provided,
            required=required_token,
            expires_at=str(_runtime_env.get("FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT", "") or ""),
        )

    if env_flag("FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY", True):
        client_host = str((request.client.host if request.client else "") or "").strip().lower()
        if client_host not in {"127.0.0.1", "::1", "localhost"}:
            return False, "Bootstrap is restricted to localhost requests"
    return True, ""
