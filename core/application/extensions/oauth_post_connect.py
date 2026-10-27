"""Closed provider-neutral post-connect result contract."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, cast

_RESULT_KEYS = {
    "subject",
    "verified_display_name",
    "verified_email",
    "safe_metadata",
    "health",
    "error_code",
}
_HEALTH_VALUES = {"ok", "needs_attention", "authorization_required"}
_NEEDS_ATTENTION_ERRORS = {
    "internal_failure",
    "malformed_response",
    "provider_unavailable",
}
_SAFE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SUBJECT_MAX_BYTES = 512
_DISPLAY_MAX_BYTES = 512
_EMAIL_MAX_BYTES = 320
_METADATA_MAX_ITEMS = 16
_METADATA_KEY_MAX_BYTES = 64
_METADATA_VALUE_MAX_BYTES = 1024
_METADATA_TOTAL_MAX_BYTES = 4096


def _is_basic_email_address(value: str) -> bool:
    parts = value.split("@")
    if len(parts) != 2:
        return False
    local_part, domain = parts
    return bool(
        local_part
        and domain
        and not any(character.isspace() for character in value)
        and "." in domain
        and not domain.startswith(".")
        and not domain.endswith(".")
    )


@dataclass(frozen=True)
class OAuthPostConnectResult:
    subject: str
    verified_display_name: str | None
    verified_email: str | None
    safe_metadata: Mapping[str, str]
    health: Literal["ok", "needs_attention", "authorization_required"]
    error_code: str | None = None


def _invalid() -> ValueError:
    return ValueError("invalid_oauth_post_connect_result")


def _required_text(value: Any, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _invalid()
    normalized = value.strip()
    if (not normalized and not allow_empty) or len(normalized.encode("utf-8")) > maximum:
        raise _invalid()
    return normalized


def _optional_text(value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, maximum=maximum)


def _safe_metadata(value: Any) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping) or len(value) > _METADATA_MAX_ITEMS:
        raise _invalid()
    normalized: dict[str, str] = {}
    total_bytes = 0
    for raw_key, raw_value in value.items():
        key = _required_text(raw_key, maximum=_METADATA_KEY_MAX_BYTES)
        item = _required_text(raw_value, maximum=_METADATA_VALUE_MAX_BYTES, allow_empty=True)
        if _SAFE_KEY_RE.fullmatch(key) is None or key in normalized:
            raise _invalid()
        total_bytes += len(key.encode("utf-8")) + len(item.encode("utf-8"))
        if total_bytes > _METADATA_TOTAL_MAX_BYTES:
            raise _invalid()
        normalized[key] = item
    return MappingProxyType(dict(sorted(normalized.items())))


def parse_oauth_post_connect_result(value: Any) -> OAuthPostConnectResult:
    if not isinstance(value, Mapping) or set(value) - _RESULT_KEYS:
        raise _invalid()
    if "subject" not in value or "health" not in value:
        raise _invalid()
    subject = _required_text(value.get("subject"), maximum=_SUBJECT_MAX_BYTES, allow_empty=True)
    health = value.get("health")
    if not isinstance(health, str) or health not in _HEALTH_VALUES:
        raise _invalid()
    normalized_health = cast(Literal["ok", "needs_attention", "authorization_required"], health)
    display_name = _optional_text(value.get("verified_display_name"), maximum=_DISPLAY_MAX_BYTES)
    email = _optional_text(value.get("verified_email"), maximum=_EMAIL_MAX_BYTES)
    if email is not None and not _is_basic_email_address(email):
        raise _invalid()
    error_code = _optional_text(value.get("error_code"), maximum=64)
    if normalized_health == "ok":
        if not subject or error_code is not None:
            raise _invalid()
    elif normalized_health == "needs_attention":
        if error_code not in _NEEDS_ATTENTION_ERRORS:
            raise _invalid()
    elif error_code != "authorization_required":
        raise _invalid()
    return OAuthPostConnectResult(
        subject=subject,
        verified_display_name=display_name,
        verified_email=email,
        safe_metadata=_safe_metadata(value.get("safe_metadata")),
        health=normalized_health,
        error_code=error_code,
    )


def oauth_post_connect_result_from_invocation(value: Any) -> OAuthPostConnectResult:
    if not isinstance(value, Mapping):
        return OAuthPostConnectResult(
            subject="",
            verified_display_name=None,
            verified_email=None,
            safe_metadata=MappingProxyType({}),
            health="needs_attention",
            error_code="internal_failure",
        )
    if value.get("status") != "succeeded":
        response = value.get("response")
        response = response if isinstance(response, Mapping) else {}
        error_code = response.get("error_code")
        if error_code == "authorization_required":
            return OAuthPostConnectResult(
                subject="",
                verified_display_name=None,
                verified_email=None,
                safe_metadata=MappingProxyType({}),
                health="authorization_required",
                error_code="authorization_required",
            )
        if error_code in {"provider_unavailable", "malformed_response"}:
            return OAuthPostConnectResult(
                subject="",
                verified_display_name=None,
                verified_email=None,
                safe_metadata=MappingProxyType({}),
                health="needs_attention",
                error_code=cast(Literal["provider_unavailable", "malformed_response"], error_code),
            )
        return OAuthPostConnectResult(
            subject="",
            verified_display_name=None,
            verified_email=None,
            safe_metadata=MappingProxyType({}),
            health="needs_attention",
            error_code="internal_failure",
        )
    response = value.get("response")
    payload = response if isinstance(response, Mapping) else {}
    result = payload.get("result")
    if isinstance(result, Mapping):
        payload = result
    try:
        return parse_oauth_post_connect_result(payload)
    except ValueError:
        return OAuthPostConnectResult(
            subject="",
            verified_display_name=None,
            verified_email=None,
            safe_metadata=MappingProxyType({}),
            health="needs_attention",
            error_code="malformed_response",
        )


__all__ = [
    "OAuthPostConnectResult",
    "oauth_post_connect_result_from_invocation",
    "parse_oauth_post_connect_result",
]
