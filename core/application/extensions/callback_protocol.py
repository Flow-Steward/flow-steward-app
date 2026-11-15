"""Bounded, provider-neutral raw-body callback authentication contract."""

from __future__ import annotations

import hmac
import re
from typing import Any

from fastapi import Request

from core.application.extensions.semver import compare_semver

RAW_BODY_AUTH_TYPE = "raw_body_hmac_sha256"
RAW_BODY_PLATFORM_MIN = "1.9.0"
_HEADER_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
_DIGEST_RE = re.compile(r"[0-9a-fA-F]{64}")
_MAX_QUERY_BYTES = 8192
_MAX_QUERY_PAIRS = 16
_MAX_VALUE_BYTES = 512


def normalize_callback_auth(auth: dict[str, Any]) -> dict[str, Any]:
    """Expand the legacy preset at compile and installed-declaration boundaries."""
    payload = dict(auth)
    auth_type = str(auth.get("type") or "none").strip().lower() or "none"
    payload["type"] = auth_type
    if auth_type == "hub_signature":
        return {
            "type": RAW_BODY_AUTH_TYPE,
            "signature_header": str(auth.get("signature_header") or "").strip(),
            "signature_prefix": "sha256=",
            "secret_name": str(auth.get("secret_name") or "").strip(),
            "verification": {
                "token_parameter": "hub.verify_token",
                "challenge_parameter": "hub.challenge",
                "required_parameters": {"hub.mode": "subscribe"},
            },
            "_legacy_compatibility": True,
        }
    if auth_type == RAW_BODY_AUTH_TYPE:
        payload.setdefault("signature_prefix", "")
        if isinstance(payload.get("secret_name"), str):
            payload["secret_name"] = payload["secret_name"].strip()
    return payload


def _bounded_value(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_VALUE_BYTES
    except UnicodeError:
        return False


def raw_body_auth_errors(
    auth: dict[str, Any],
    *,
    connection_scoped: bool,
    methods: list[str],
    secret_names: set[str],
    platform_min: str,
) -> list[tuple[str, str]]:
    """Validate authored generic fields; internal compatibility flags are forbidden."""
    errors: list[tuple[str, str]] = []
    for field in auth.keys() - {
        "type",
        "signature_header",
        "signature_prefix",
        "secret_name",
        "verification",
    }:
        errors.append((str(field), "Unknown raw-body callback auth field"))
    header = auth.get("signature_header")
    if not isinstance(header, str) or _HEADER_RE.fullmatch(header) is None:
        errors.append(
            (
                "signature_header",
                "signature_header must be an ASCII HTTP token of at most 128 characters",
            )
        )
    prefix = auth.get("signature_prefix", "")
    if (
        not isinstance(prefix, str)
        or len(prefix) > 64
        or any(ord(char) < 32 or ord(char) > 126 for char in prefix)
    ):
        errors.append(
            ("signature_prefix", "signature_prefix must be at most 64 printable ASCII characters")
        )
    secret_name = auth.get("secret_name")
    if (
        not isinstance(secret_name, str)
        or _NAME_RE.fullmatch(secret_name.strip()) is None
        or secret_name.strip() not in secret_names
    ):
        errors.append(("secret_name", "secret_name must name a declared owning connection secret"))
    if not connection_scoped:
        errors.append(("", "raw-body callback auth requires connection_scoped=true"))
    try:
        supported = (
            compare_semver(platform_min, RAW_BODY_PLATFORM_MIN, field_name="platform_min") >= 0
        )
    except ValueError:
        supported = False
    if not supported:
        errors.append(
            (
                "",
                "raw-body callback auth requires runtime.compatibility.platform_min 1.9.0 or later",
            )
        )
    if "verification" not in auth:
        return errors
    verification = auth["verification"]
    if "GET" not in methods:
        errors.append(("verification", "callback verification requires GET in methods"))
    if not isinstance(verification, dict):
        errors.append(("verification", "verification must be an object"))
        return errors
    if verification.keys() - {"token_parameter", "challenge_parameter", "required_parameters"}:
        errors.append(("verification", "Unknown callback verification field"))
    names = [verification.get(field) for field in ("token_parameter", "challenge_parameter")]
    if (
        any(not isinstance(name, str) or _NAME_RE.fullmatch(name) is None for name in names)
        or names[0] == names[1]
    ):
        errors.append(
            (
                "verification",
                "token and challenge parameters must be distinct ASCII identifiers of at most 64 characters",
            )
        )
    required = verification.get("required_parameters", {})
    if not isinstance(required, dict) or len(required) > 8:
        errors.append(
            (
                "verification.required_parameters",
                "required_parameters must be a map of at most eight literals",
            )
        )
        return errors
    for name, value in required.items():
        if (
            not isinstance(name, str)
            or _NAME_RE.fullmatch(name) is None
            or name in names
            or not _bounded_value(value)
        ):
            errors.append(
                (
                    "verification.required_parameters",
                    "Required parameters must have disjoint ASCII names and nonempty literals of at most 512 UTF-8 bytes",
                )
            )
    return errors


def callback_signature(value: str, auth: dict[str, Any]) -> str | None:
    """Decode one digest, preserving only the host-derived preset's legacy syntax."""
    prefix = auth.get("signature_prefix", "")
    if auth.get("_legacy_compatibility") is True:
        value = value.strip()
        if value.lower().startswith(prefix.lower()):
            value = value[len(prefix) :].strip()
    else:
        if not value.startswith(prefix):
            return None
        value = value[len(prefix) :]
    return value.lower() if _DIGEST_RE.fullmatch(value) else None


def verification_challenge(request: Request, auth: dict[str, Any], expected: str) -> str | None:
    """Return an authenticated bounded challenge without invoking extension code."""
    verification = auth.get("verification")
    if not isinstance(verification, dict) or not expected:
        return None
    if len(request.scope.get("query_string", b"")) > _MAX_QUERY_BYTES:
        return None
    pairs = list(request.query_params.multi_items())
    if not pairs or len(pairs) > _MAX_QUERY_PAIRS:
        return None
    values: dict[str, str] = {}
    for name, value in pairs:
        if (
            name in values
            or not name
            or len(name.encode("utf-8")) > 64
            or not _bounded_value(value)
        ):
            return None
        values[name] = value.strip() if auth.get("_legacy_compatibility") is True else value
    for name, literal in verification.get("required_parameters", {}).items():
        if values.get(name) != literal:
            return None
    token = values.get(verification.get("token_parameter"), "")
    challenge = values.get(verification.get("challenge_parameter"), "")
    if (
        not token
        or not challenge
        or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
    ):
        return None
    return challenge
