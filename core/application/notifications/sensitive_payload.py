"""Keep a sensitive notification's body and subject out of everything kept.

An event may carry ``payload_sensitive: true``. The platform's own messages
(a password reset, a welcome note, an email change confirmation) always do,
because their body holds a single-use link; any other event may set it too.

For such an event the body and subject reach only the delivery request to the
extension. Every copy the platform keeps -- the invocation log and audit, the
external-effect ledger, the runtime journal, notification delivery rows, error
text -- holds a placeholder that says how long the value was and what its hash
is, so two copies can still be compared without revealing either.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

SENSITIVE_FLAG = "payload_sensitive"
_PLACEHOLDER_PREFIX = "<redacted"
#: Route fields that carry message text rather than an address.
_ROUTE_TEXT_FIELDS = ("subject",)
#: Below this length a value is not worth scrubbing out of other text.
_MIN_SCRUB_LENGTH = 8
#: Links and token-like words inside a body are scrubbed on their own too, so a
#: single-use token quoted apart from its sentence does not slip through.
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_TOKEN_RE = re.compile(r"[A-Za-z0-9._~-]{16,}")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def is_payload_sensitive(event: dict[str, Any] | None) -> bool:
    return _as_dict(event).get(SENSITIVE_FLAG) is True


def placeholder(value: str) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
    return f"{_PLACEHOLDER_PREFIX} length={len(str(value))} sha256={digest}>"


def _redact_strings(value: Any) -> Any:
    if isinstance(value, str):
        return placeholder(value) if value else value
    if isinstance(value, dict):
        return {key: _redact_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_strings(item) for item in value]
    return value


def redact_event(event: dict[str, Any] | None) -> dict[str, Any]:
    """A copy of *event* that may be kept: a flagged event's text is replaced."""
    payload = _as_dict(event)
    if not is_payload_sensitive(payload):
        return payload
    redacted = dict(payload)
    redacted["payload"] = _redact_strings(_as_dict(payload.get("payload")))
    for key in ("summary", "title", "message"):
        if isinstance(redacted.get(key), str) and redacted[key]:
            redacted[key] = placeholder(redacted[key])
    return redacted


def redact_route(route: dict[str, Any] | None, event: dict[str, Any] | None) -> dict[str, Any]:
    """A copy of a route payload that may be kept alongside a flagged event."""
    payload = _as_dict(route)
    if not is_payload_sensitive(event):
        return payload
    config = _as_dict(payload.get("route_config"))
    for key in _ROUTE_TEXT_FIELDS:
        if isinstance(config.get(key), str) and config[key]:
            config[key] = placeholder(config[key])
    return {**payload, "route_config": config} if "route_config" in payload else payload


def sensitive_values(event: dict[str, Any] | None) -> list[str]:
    """Every text a flagged event carries, longest first; empty when not flagged."""
    payload = _as_dict(event)
    if not is_payload_sensitive(payload):
        return []
    found: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, str):
            if len(value.strip()) >= _MIN_SCRUB_LENGTH:
                found.add(value)
                # A body's lines are scrubbed one by one too, so an excerpt
                # quoted in an error does not slip through.
                for line in value.splitlines():
                    if len(line.strip()) >= _MIN_SCRUB_LENGTH:
                        found.add(line.strip())
                found.update(_URL_RE.findall(value))
                found.update(
                    word for word in _TOKEN_RE.findall(value) if any(c.isdigit() for c in word)
                )
            return
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload.get("payload"))
    for key in ("summary", "title", "message"):
        collect(payload.get(key))
    return sorted(found, key=len, reverse=True)


def scrub(value: Any, event: dict[str, Any] | None) -> Any:
    """*value* with every text of a flagged *event* replaced by its placeholder."""
    values = sensitive_values(event)
    if not values:
        return value

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            text = item
            for secret in values:
                if secret in text:
                    text = text.replace(secret, placeholder(secret))
            return text
        if isinstance(item, dict):
            return {key: walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        return item

    return walk(value)


__all__ = [
    "SENSITIVE_FLAG",
    "is_payload_sensitive",
    "placeholder",
    "redact_event",
    "redact_route",
    "scrub",
    "sensitive_values",
]
