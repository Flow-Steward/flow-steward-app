"""Export credential wiring without exporting credential values or local records."""

from __future__ import annotations

import re
from typing import Any

from core.application.orchestration.state_paths import StatePathError, parse_state_path
from core.application.orchestration.step_templating import _TEMPLATE_RE
from core.application.security.credential_redaction import token_count_key
from core.infrastructure.security.secret_reference import is_secret_reference

_SENSITIVE_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "bearer",
    "credential",
    "credentials",
    "api_key",
    "access_key",
    "private_key",
    "client_secret",
)
_AUTHENTICATION_FIELDS = frozenset({"authorization", "proxy_authorization", "cookie", "set_cookie"})
_AUTHENTICATION_SCHEMES = ("Bearer ", "Basic ", "Token ")
_DROPPED_SENSITIVE_KEYS = frozenset({"default_secret_ref", "sample_file"})
_REDACTED = "[redacted]"
# Limit unquoted segments to names; quoted mapping keys retain their canonical
# spelling. The runtime parser below remains authoritative for path validity.
_PORTABLE_NAME = re.compile(r"[\w-]+")
_PORTABLE_INDEX = re.compile(r"\[\s*\d+\s*\]")
_PORTABLE_SINGLE_KEY = re.compile(r"\[\s*'(?:[^'\\]|\\.)*'\s*\]")
_PORTABLE_DOUBLE_KEY = re.compile(r'\[\s*"(?:[^"\\]|\\.)*"\s*\]')


def _portable_path_syntax(selector: str) -> bool:
    if not selector.startswith("state."):
        return False
    match = _PORTABLE_NAME.match(selector, len("state."))
    if match is None:
        return False
    position = match.end()
    while position < len(selector):
        if selector[position] == ".":
            match = _PORTABLE_NAME.match(selector, position + 1)
        else:
            match = next(
                (
                    m
                    for pattern in (_PORTABLE_INDEX, _PORTABLE_SINGLE_KEY, _PORTABLE_DOUBLE_KEY)
                    if (m := pattern.match(selector, position))
                ),
                None,
            )
        if match is None:
            return False
        position = match.end()
    return True


def _sensitive_key(key: Any) -> bool:
    normalized = token_count_key(str(key or ""))
    compact = normalized.replace("_", "")
    return (
        normalized in _AUTHENTICATION_FIELDS
        or normalized.endswith("_authorization")
        or any(marker.replace("_", "") in compact for marker in _SENSITIVE_KEY_MARKERS)
    )


def _portable_state_read(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip().startswith("state."):
        return False
    selector = value.strip()
    if not _portable_path_syntax(selector):
        return False
    try:
        return bool(parse_state_path(selector))
    except StatePathError:
        return False


def _portable_reference(value: Any) -> bool:
    if isinstance(value, dict):
        return set(value) == {"source"} and _portable_state_read(value["source"])
    if not isinstance(value, str):
        return False
    template = value.strip()
    for scheme in _AUTHENTICATION_SCHEMES:
        if template.casefold().startswith(scheme.casefold()):
            template = template[len(scheme) :].strip()
            break
    match = _TEMPLATE_RE.fullmatch(template)
    return match is not None and _portable_state_read(match.group(1).strip())


def _redact_sensitive(value: Any, *, key: str = "") -> Any:
    # Opaque runtime references can contain ciphertext or local record ids.
    # They are not authored expressions and must never travel in a template.
    if is_secret_reference(value):
        return _REDACTED
    if _sensitive_key(key):
        if _portable_reference(value):
            return dict(value) if isinstance(value, dict) else value
        return _REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): _redact_sensitive(item_value, key=str(item_key))
            for item_key, item_value in value.items()
            if str(item_key) not in _DROPPED_SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value
