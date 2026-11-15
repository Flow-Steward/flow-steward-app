"""Bounded, provider-neutral raw-body callback authentication contract."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from typing import Any

from fastapi import Request

from core.application.extensions.semver import compare_semver

RAW_BODY_AUTH_TYPE = "raw_body_hmac_sha256"
RAW_BODY_PLATFORM_MIN = "1.9.0"
_HEADER_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
_DIGEST_RE = re.compile(r"[0-9a-fA-F]{64}")
_NONCE_RE = re.compile(r"[A-Za-z0-9._~+/=\-]{1,512}")
_DIGEST_BYTES = 32
_BASE64_DIGEST_LENGTH = 44
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
        "signature_encoding",
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
    if auth.get("signature_encoding", "hex") not in ("hex", "base64"):
        errors.append(("signature_encoding", "signature_encoding must be hex or base64"))
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
    if "verification" in auth:
        errors.extend(_verification_auth_errors(auth["verification"], methods=methods))
    return errors


def _verification_auth_errors(value: Any, *, methods: list[str]) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    if "GET" not in methods:
        errors.append(("verification", "callback verification requires GET in methods"))
    if not isinstance(value, dict):
        return [*errors, ("verification", "verification must be an object")]
    if value.keys() - {
        "response",
        "response_field",
        "token_parameter",
        "challenge_parameter",
        "required_parameters",
    }:
        errors.append(("verification", "Unknown callback verification field"))
    response = value.get("response", "echo")
    if response not in ("echo", "hmac"):
        errors.append(("verification.response", "verification.response must be echo or hmac"))
    name_fields = ["challenge_parameter"]
    if response != "hmac" or "token_parameter" in value:
        name_fields.append("token_parameter")
    if response == "hmac" or "response_field" in value:
        name_fields.append("response_field")
    for field in name_fields:
        name = value.get(field)
        if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
            errors.append(
                (
                    f"verification.{field}",
                    f"{field} must be an ASCII identifier of at most 64 characters",
                )
            )
    query_names = [value.get("token_parameter"), value.get("challenge_parameter")]
    if "token_parameter" in value and query_names[0] == query_names[1]:
        errors.append(("verification", "token and challenge parameter names must be distinct"))
    required = value.get("required_parameters", {})
    if not isinstance(required, dict) or len(required) > 8:
        return [
            *errors,
            (
                "verification.required_parameters",
                "required_parameters must be a map of at most eight literals",
            ),
        ]
    for name, literal in required.items():
        if (
            not isinstance(name, str)
            or _NAME_RE.fullmatch(name) is None
            or name in query_names
            or not _bounded_value(literal)
        ):
            errors.append(
                (
                    "verification.required_parameters",
                    "Required parameters must have disjoint ASCII names and nonempty literals of at most 512 UTF-8 bytes",
                )
            )
    return errors


def is_hmac_verification(auth: dict[str, Any]) -> bool:
    """Whether the shared connection key supplies a JSON verification proof."""
    verification = auth.get("verification")
    return isinstance(verification, dict) and verification.get("response", "echo") == "hmac"


def verification_uses_token(auth: dict[str, Any]) -> bool:
    """Keep existing registration secrets except for explicitly tokenless HMAC."""
    return not (is_hmac_verification(auth) and "token_parameter" not in auth["verification"])


def callback_signature(value: str, auth: dict[str, Any]) -> bytes | None:
    """Strictly decode one digest with legacy syntax only for the host preset."""
    prefix = auth.get("signature_prefix", "")
    if not isinstance(prefix, str):
        return None
    if auth.get("_legacy_compatibility") is True:
        value = value.strip()
        if value.lower().startswith(prefix.lower()):
            value = value[len(prefix) :].strip()
    else:
        if not value.startswith(prefix):
            return None
        value = value[len(prefix) :]
    encoding = auth.get("signature_encoding", "hex")
    if encoding == "hex":
        return bytes.fromhex(value) if _DIGEST_RE.fullmatch(value) else None
    if encoding != "base64" or len(value) != _BASE64_DIGEST_LENGTH:
        return None
    try:
        digest = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(digest) != _DIGEST_BYTES or base64.b64encode(digest).decode("ascii") != value:
        return None
    return digest


def callback_hmac_digest(secret: str, message: bytes) -> bytes:
    """The shared raw-body and verification HMAC primitive, with opaque key bytes."""
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def encode_callback_signature(digest: bytes, auth: dict[str, Any]) -> str | None:
    """Use the body signature's exact prefix and declared encoding for the proof."""
    prefix = auth.get("signature_prefix", "")
    if not isinstance(prefix, str):
        return None
    encoding = auth.get("signature_encoding", "hex")
    if encoding == "hex":
        encoded = digest.hex()
    elif encoding == "base64":
        encoded = base64.b64encode(digest).decode("ascii")
    else:
        return None
    return prefix + encoded


def callback_body_is_json_object(raw_body: bytes) -> bool:
    """Keep dispatchable HMAC-mode bodies disjoint from the ASCII nonce language."""
    try:
        return isinstance(json.loads(raw_body.decode("utf-8")), dict)
    except (ValueError, RecursionError):
        return False


def verification_challenge(request: Request, auth: dict[str, Any], expected: str) -> str | None:
    """Return an authenticated bounded challenge without invoking extension code."""
    verification = auth.get("verification")
    if not isinstance(verification, dict) or _verification_auth_errors(
        verification, methods=["GET"]
    ):
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
    challenge = values.get(verification["challenge_parameter"], "")
    if not challenge or (is_hmac_verification(auth) and _NONCE_RE.fullmatch(challenge) is None):
        return None
    if verification_uses_token(auth):
        token = values.get(verification["token_parameter"], "")
        if (
            not token
            or not expected
            or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
        ):
            return None
    return challenge
