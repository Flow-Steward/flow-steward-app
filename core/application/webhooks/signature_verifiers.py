"""Generic, configuration-driven HMAC-SHA256 verification for webhook ingress.

This module is intentionally provider-agnostic: it knows nothing about Stripe,
Slack, GitHub or any commerce platform.  Provider differences are expressed entirely as
configuration (header name, parser, digest encoding, timestamp recipe), so a new
source is onboarded by filling in fields rather than by adding a code branch.

The module is pure application logic — no FastAPI, no database, no request
objects — so it is directly unit-testable and returns deterministic values.
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, BinaryIO

from core.infrastructure.security.hmac_signatures import (
    hmac_digest_matches,
    hmac_sha256_signer,
)

WEBHOOK_SIGNATURE_AUTH_MODE = "webhook_signature"

MAX_SIGNATURE_HEADER_BYTES = 8192
DEFAULT_TOLERANCE_SECONDS = 300
MAX_TOLERANCE_SECONDS = 86400
_BODY_CHUNK_BYTES = 1024 * 1024

SIGNATURE_SOURCES = frozenset({"header"})
SIGNATURE_PARSERS = frozenset({"raw", "prefixed", "comma_kv"})
SIGNATURE_ENCODINGS = frozenset({"hex", "base64"})
TIMESTAMP_SOURCES = frozenset({"none", "header", "signature_param"})
TIMESTAMP_UNITS = frozenset({"seconds", "milliseconds"})
SIGNED_PAYLOAD_SOURCES = frozenset({"raw_body", "timestamp"})

#: The only signed-payload recipes accepted in v1.  Each one contains exactly a
#: single ``raw_body`` part, which is what lets the HTTP route treat every
#: ``webhook_signature`` request as body-dependent without re-interpreting the
#: schema.
SIGNED_PAYLOAD_RECIPES: tuple[tuple[dict[str, str], ...], ...] = (
    ({"source": "raw_body"},),
    ({"source": "timestamp"}, {"literal": "."}, {"source": "raw_body"}),
    (
        {"literal": "v0:"},
        {"source": "timestamp"},
        {"literal": ":"},
        {"source": "raw_body"},
    ),
)

_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")
_FIELD_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_FIELD_VALUE_RE = re.compile(r"^[A-Za-z0-9+/=_.:-]{1,4096}$")
_HEX_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_TIMESTAMP_RE = re.compile(r"^(?a:\d){1,19}$")

_INVALID_SIGNATURE_MESSAGE = "Invalid webhook signature"
_MALFORMED_HEADER_MESSAGE = "Webhook signature header is malformed"


class WebhookSignatureConfigError(ValueError):
    """Raised when a webhook signature verifier configuration is unsupported."""


class _MalformedSignature(ValueError):
    """Internal marker for a request-side (not configuration-side) rejection."""


@dataclass(frozen=True)
class WebhookSignatureVerificationResult:
    """Deterministic verification outcome.

    ``reason`` separates a rejected delivery (``auth_failed`` — the caller should
    answer 401) from a server-side configuration defect (``config_invalid`` — the
    caller should fail closed with a service error).
    """

    ok: bool
    error: str = ""
    reason: str = ""

    @classmethod
    def accepted(cls) -> WebhookSignatureVerificationResult:
        return cls(ok=True)

    @classmethod
    def failed(cls, error: str) -> WebhookSignatureVerificationResult:
        return cls(ok=False, error=error, reason="auth_failed")

    @classmethod
    def misconfigured(cls, error: str) -> WebhookSignatureVerificationResult:
        return cls(ok=False, error=error, reason="config_invalid")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _header_name(value: Any, *, label: str) -> str:
    name = _text(value)
    if not name:
        raise WebhookSignatureConfigError(f"{label} is required")
    if not _HEADER_NAME_RE.match(name):
        raise WebhookSignatureConfigError(f"{label} is not a valid HTTP header name")
    return name


def _normalize_signature(raw: Any) -> dict[str, Any]:
    payload = raw if isinstance(raw, Mapping) else {}
    source = _text(payload.get("source") or "header").lower()
    if source not in SIGNATURE_SOURCES:
        raise WebhookSignatureConfigError("Unsupported webhook signature source")
    parser = _text(payload.get("parser") or "raw").lower()
    if parser not in SIGNATURE_PARSERS:
        raise WebhookSignatureConfigError("Unsupported webhook signature parser")
    encoding = _text(payload.get("encoding") or "hex").lower()
    if encoding not in SIGNATURE_ENCODINGS:
        raise WebhookSignatureConfigError("Unsupported webhook signature encoding")
    normalized: dict[str, Any] = {
        "source": source,
        "header": _header_name(payload.get("header"), label="Webhook signature header"),
        "parser": parser,
        "encoding": encoding,
    }
    if parser == "prefixed":
        prefix = _text(payload.get("prefix"))
        if not prefix:
            raise WebhookSignatureConfigError(
                "Webhook signature prefix is required for the prefixed parser"
            )
        normalized["prefix"] = prefix
    if parser == "comma_kv":
        value_key = _text(payload.get("value_key"))
        if not value_key or not _FIELD_KEY_RE.match(value_key):
            raise WebhookSignatureConfigError(
                "Webhook signature value_key is required for the comma_kv parser"
            )
        normalized["value_key"] = value_key
        normalized["allow_multiple"] = bool(payload.get("allow_multiple", False))
    return normalized


def _normalize_tolerance(raw: Any) -> int:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_TOLERANCE_SECONDS
    not_whole = WebhookSignatureConfigError(
        "Webhook timestamp tolerance_seconds must be a whole number of seconds"
    )
    # `int()` would silently truncate 1.5 to 1, which contradicts the message and
    # would quietly narrow the operator's freshness window.
    if isinstance(raw, bool):
        raise not_whole
    if isinstance(raw, int):
        tolerance = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            raise not_whole
        tolerance = int(raw)
    elif isinstance(raw, str) and raw.strip().isdigit():
        tolerance = int(raw.strip())
    else:
        raise not_whole
    if tolerance <= 0 or tolerance > MAX_TOLERANCE_SECONDS:
        raise WebhookSignatureConfigError(
            f"Webhook timestamp tolerance_seconds must be between 1 and {MAX_TOLERANCE_SECONDS}"
        )
    return tolerance


def _normalize_timestamp(raw: Any) -> dict[str, Any]:
    payload = raw if isinstance(raw, Mapping) else {}
    source = _text(payload.get("source") or "none").lower()
    if source not in TIMESTAMP_SOURCES:
        raise WebhookSignatureConfigError("Unsupported webhook timestamp source")
    if source == "none":
        return {"source": "none"}
    unit = _text(payload.get("unit") or "seconds").lower()
    if unit not in TIMESTAMP_UNITS:
        raise WebhookSignatureConfigError("Unsupported webhook timestamp unit")
    normalized: dict[str, Any] = {
        "source": source,
        "unit": unit,
        "tolerance_seconds": _normalize_tolerance(payload.get("tolerance_seconds")),
    }
    if source == "header":
        normalized["header"] = _header_name(payload.get("header"), label="Webhook timestamp header")
    else:
        key = _text(payload.get("key"))
        if not key or not _FIELD_KEY_RE.match(key):
            raise WebhookSignatureConfigError("Webhook timestamp key is required")
        normalized["key"] = key
    return normalized


def _recipe_key(parts: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    key: list[tuple[str, str]] = []
    for part in parts:
        if "literal" in part:
            key.append(("literal", str(part["literal"])))
        else:
            key.append(("source", str(part["source"])))
    return tuple(key)


_SUPPORTED_RECIPE_KEYS = frozenset(_recipe_key(recipe) for recipe in SIGNED_PAYLOAD_RECIPES)


def _normalize_signed_payload(raw: Any) -> dict[str, Any]:
    payload = raw if isinstance(raw, Mapping) else {}
    raw_parts = payload.get("parts")
    if not isinstance(raw_parts, Sequence) or isinstance(raw_parts, str | bytes):
        raise WebhookSignatureConfigError("Webhook signed_payload.parts must be a list")
    parts: list[dict[str, str]] = []
    for item in raw_parts:
        if not isinstance(item, Mapping):
            raise WebhookSignatureConfigError("Webhook signed_payload parts must be objects")
        keys = set(item)
        if keys == {"source"}:
            source = _text(item.get("source")).lower()
            if source not in SIGNED_PAYLOAD_SOURCES:
                raise WebhookSignatureConfigError("Unsupported webhook signed_payload part")
            parts.append({"source": source})
        elif keys == {"literal"}:
            # Literals are byte-exact: never strip them.
            parts.append({"literal": str(item.get("literal") or "")})
        else:
            raise WebhookSignatureConfigError("Unsupported webhook signed_payload part")
    if sum(1 for part in parts if part.get("source") == "raw_body") != 1:
        raise WebhookSignatureConfigError(
            "Webhook signed_payload must contain exactly one raw_body part"
        )
    if _recipe_key(parts) not in _SUPPORTED_RECIPE_KEYS:
        raise WebhookSignatureConfigError("Unsupported webhook signed_payload recipe")
    return {"parts": parts}


def normalize_webhook_signature_config(
    config: Any,
    *,
    secret_ref: str = "",
) -> dict[str, Any]:
    """Return the canonical stored shape, or raise ``WebhookSignatureConfigError``.

    The same normalizer runs at write time (registry and webhook creation
    service) and at read time (the verifier), so a stored config can never be
    interpreted more loosely than it was accepted.
    """
    payload = config if isinstance(config, Mapping) else {}
    signature = _normalize_signature(payload.get("signature"))
    timestamp = _normalize_timestamp(payload.get("timestamp"))
    signed_payload = _normalize_signed_payload(payload.get("signed_payload"))
    references_timestamp = any(
        part.get("source") == "timestamp" for part in signed_payload["parts"]
    )
    if references_timestamp and timestamp["source"] == "none":
        raise WebhookSignatureConfigError(
            "Webhook signed_payload references a timestamp, so timestamp verification "
            "must be configured"
        )
    if not references_timestamp and timestamp["source"] != "none":
        raise WebhookSignatureConfigError(
            "Webhook timestamp verification requires a signed_payload that includes the timestamp"
        )
    if timestamp["source"] == "signature_param" and signature["parser"] != "comma_kv":
        raise WebhookSignatureConfigError(
            "Webhook timestamp source signature_param requires the comma_kv signature parser"
        )
    normalized: dict[str, Any] = {
        "signature": signature,
        "timestamp": timestamp,
        "signed_payload": signed_payload,
    }
    reference = _text(secret_ref) or _text(payload.get("secret_ref"))
    if reference:
        normalized["secret_ref"] = reference
    return normalized


def webhook_signature_reserved_headers(config: Any) -> set[str]:
    """Lower-cased header names that carry credential material for this config.

    Used to keep a workflow input mapping from reading its own signature or
    timestamp headers back out of the request.
    """
    payload = config if isinstance(config, Mapping) else {}
    signature = payload.get("signature")
    timestamp = payload.get("timestamp")
    reserved = set()
    if isinstance(signature, Mapping):
        header = _text(signature.get("header")).lower()
        if header:
            reserved.add(header)
    if isinstance(timestamp, Mapping):
        header = _text(timestamp.get("header")).lower()
        if header:
            reserved.add(header)
    return reserved


def _header_lookup(headers: Mapping[str, str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for key, value in headers.items():
        lookup[str(key or "").strip().lower()] = str(value or "")
    return lookup


def _parse_comma_kv(value: str) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for chunk in value.split(","):
        field = chunk.strip()
        if not field:
            raise _MalformedSignature(_MALFORMED_HEADER_MESSAGE)
        key, separator, raw_item = field.partition("=")
        if not separator:
            raise _MalformedSignature(_MALFORMED_HEADER_MESSAGE)
        item = raw_item.strip()
        normalized_key = key.strip()
        if not _FIELD_KEY_RE.match(normalized_key) or not _FIELD_VALUE_RE.match(item):
            raise _MalformedSignature(_MALFORMED_HEADER_MESSAGE)
        fields.append((normalized_key, item))
    if not fields:
        raise _MalformedSignature(_MALFORMED_HEADER_MESSAGE)
    return fields


def _parse_signature_header(
    header_value: str,
    signature: Mapping[str, Any],
) -> tuple[list[str], list[tuple[str, str]]]:
    parser = str(signature["parser"])
    if parser == "raw":
        return [header_value], []
    if parser == "prefixed":
        prefix = str(signature["prefix"])
        if not header_value.startswith(prefix):
            raise _MalformedSignature(_MALFORMED_HEADER_MESSAGE)
        return [header_value[len(prefix) :].strip()], []
    fields = _parse_comma_kv(header_value)
    value_key = str(signature["value_key"])
    matches = [item for key, item in fields if key == value_key]
    if not matches:
        raise _MalformedSignature(_MALFORMED_HEADER_MESSAGE)
    if len(matches) > 1 and not bool(signature.get("allow_multiple")):
        raise _MalformedSignature(_MALFORMED_HEADER_MESSAGE)
    return matches, fields


def _resolve_timestamp(
    timestamp: Mapping[str, Any],
    *,
    lookup: Mapping[str, str],
    fields: Sequence[tuple[str, str]],
) -> str:
    if str(timestamp["source"]) == "header":
        raw_value = lookup.get(str(timestamp["header"]).lower())
        if raw_value is None:
            raise _MalformedSignature("Webhook timestamp header is missing")
        return raw_value.strip()
    key = str(timestamp["key"])
    matches = [item for field_key, item in fields if field_key == key]
    if len(matches) != 1:
        raise _MalformedSignature("Webhook timestamp field is missing or ambiguous")
    return matches[0]


def _check_timestamp_freshness(
    timestamp_text: str,
    timestamp: Mapping[str, Any],
    *,
    now: float,
) -> None:
    if not _TIMESTAMP_RE.match(timestamp_text):
        raise _MalformedSignature("Webhook timestamp is malformed")
    divisor = 1000.0 if str(timestamp["unit"]) == "milliseconds" else 1.0
    seconds = int(timestamp_text) / divisor
    tolerance = float(timestamp["tolerance_seconds"])
    drift = now - seconds
    if drift > tolerance:
        raise _MalformedSignature("Webhook timestamp is too old")
    if -drift > tolerance:
        raise _MalformedSignature("Webhook timestamp is too far in the future")


def _update_with_body(
    signer: Any,
    raw_body: bytes,
    raw_body_file: BinaryIO | None,
) -> None:
    if raw_body_file is None:
        signer.update(raw_body or b"")
        return
    try:
        position = raw_body_file.tell()
    except Exception:
        position = 0
    raw_body_file.seek(0)
    while chunk := raw_body_file.read(_BODY_CHUNK_BYTES):
        signer.update(chunk)
    raw_body_file.seek(position)


def _expected_digest(
    *,
    secret: str,
    parts: Sequence[Mapping[str, Any]],
    encoding: str,
    timestamp_text: str,
    raw_body: bytes,
    raw_body_file: BinaryIO | None,
) -> str:
    signer = hmac_sha256_signer(secret=secret)
    for part in parts:
        if "literal" in part:
            signer.update(str(part["literal"]).encode("utf-8"))
        elif part.get("source") == "timestamp":
            signer.update(timestamp_text.encode("utf-8"))
        else:
            _update_with_body(signer, raw_body, raw_body_file)
    if encoding == "base64":
        return base64.b64encode(signer.digest()).decode("ascii")
    return signer.hexdigest()


def _signature_matches(candidates: Sequence[str], expected: str, *, encoding: str) -> bool:
    matched = False
    for candidate in candidates:
        if encoding == "hex":
            if not _HEX_DIGEST_RE.match(candidate):
                continue
            presented = candidate.lower()
        else:
            presented = candidate
        # No early break: every candidate costs the same comparison work.
        matched = hmac_digest_matches(expected, presented) or matched
    return matched


def verify_webhook_signature(
    *,
    headers: Mapping[str, str],
    secret: str,
    config: Mapping[str, Any],
    raw_body: bytes,
    raw_body_file: BinaryIO | None = None,
    now: float | None = None,
) -> WebhookSignatureVerificationResult:
    """Verify one HMAC-SHA256 signed webhook delivery against ``config``.

    ``raw_body_file`` streams a staged body into the digest in chunks so a large
    delivery never has to be buffered in memory; the file position is restored.
    """
    try:
        normalized = normalize_webhook_signature_config(config)
    except WebhookSignatureConfigError as exc:
        return WebhookSignatureVerificationResult.misconfigured(str(exc))
    signing_secret = str(secret or "")
    if not signing_secret.strip():
        return WebhookSignatureVerificationResult.misconfigured(
            "Webhook signing secret is not configured"
        )
    signature = normalized["signature"]
    timestamp = normalized["timestamp"]
    lookup = _header_lookup(headers)
    header_value = lookup.get(str(signature["header"]).lower())
    if header_value is None:
        return WebhookSignatureVerificationResult.failed("Webhook signature header is missing")
    if len(header_value.encode("utf-8", "surrogatepass")) > MAX_SIGNATURE_HEADER_BYTES:
        return WebhookSignatureVerificationResult.failed("Webhook signature header is too large")
    header_value = header_value.strip()
    if not header_value:
        return WebhookSignatureVerificationResult.failed("Webhook signature header is empty")
    try:
        candidates, fields = _parse_signature_header(header_value, signature)
        timestamp_text = ""
        if str(timestamp["source"]) != "none":
            timestamp_text = _resolve_timestamp(timestamp, lookup=lookup, fields=fields)
            _check_timestamp_freshness(
                timestamp_text,
                timestamp,
                now=float(now) if now is not None else time.time(),
            )
    except _MalformedSignature as exc:
        return WebhookSignatureVerificationResult.failed(str(exc))
    expected = _expected_digest(
        secret=signing_secret,
        parts=normalized["signed_payload"]["parts"],
        encoding=str(signature["encoding"]),
        timestamp_text=timestamp_text,
        raw_body=raw_body,
        raw_body_file=raw_body_file,
    )
    if not _signature_matches(candidates, expected, encoding=str(signature["encoding"])):
        return WebhookSignatureVerificationResult.failed(_INVALID_SIGNATURE_MESSAGE)
    return WebhookSignatureVerificationResult.accepted()


__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "MAX_SIGNATURE_HEADER_BYTES",
    "MAX_TOLERANCE_SECONDS",
    "SIGNATURE_ENCODINGS",
    "SIGNATURE_PARSERS",
    "SIGNED_PAYLOAD_RECIPES",
    "TIMESTAMP_SOURCES",
    "TIMESTAMP_UNITS",
    "WEBHOOK_SIGNATURE_AUTH_MODE",
    "WebhookSignatureConfigError",
    "WebhookSignatureVerificationResult",
    "normalize_webhook_signature_config",
    "verify_webhook_signature",
    "webhook_signature_reserved_headers",
]
