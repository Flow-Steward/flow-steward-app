"""Local credential classification; no platform runtime or local secret records."""

from __future__ import annotations

import re
from typing import Any

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
# Accept only complete generated state reads; never arbitrary template expressions.
_PORTABLE_PATH = re.compile(
    r"""state\.[\w-]+(?:\.[\w-]+|\[\s*(?:\d+|'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")\s*\])*"""
)


def _sensitive_key(key: Any) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    compact = normalized.replace("_", "")
    return any(
        part in _AUTHENTICATION_FIELDS or part.endswith("_authorization")
        for part in normalized.split(".")
    ) or any(marker.replace("_", "") in compact for marker in _SENSITIVE_KEY_MARKERS)


def _portable_state_read(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip().startswith("state."):
        return False
    selector = value.strip()
    return _PORTABLE_PATH.fullmatch(selector) is not None


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
    match = re.fullmatch(r"\{\{\s*(state\..*?)\s*\}\}", template)
    return match is not None and _portable_state_read(match.group(1).strip())
