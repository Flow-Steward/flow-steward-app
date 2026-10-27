"""Credential-safe copies of registry changelog response payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from core.application.security.credential_redaction import redact

#: A path segment: a dotted key, a list index, or a quoted business id such as
#: ``steps["fetch.order"]`` that may itself contain dots.
_PATH_TOKEN = re.compile(r'\[(?:\d+|"(?:[^"\\]|\\.)*")\]|[^.\[\]]+')


def redact_changelog_payload(payload: object) -> Any:
    """Project a response without changing durable or restore snapshots."""
    return redact(payload)


def _redact_diff_value(path: str, value: object) -> object:
    if value is None:
        # Preserve the missing side of an added/removed change.
        return None
    keys = [token for token in _PATH_TOKEN.findall(path) if not token.startswith("[")]
    wrapped = value
    for key in reversed(keys):
        wrapped = {key: wrapped}
    projected = redact(wrapped)
    for key in keys:
        if not isinstance(projected, Mapping):
            # A sensitive ancestor masks the entire subtree, even a safe leaf.
            return projected
        projected = projected[key]
    return projected


def redact_changelog_diff(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply nested credential policy to flattened diff paths and values."""
    projected = cast(dict[str, Any], redact(payload))
    changes = projected.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            path = str(change.get("path") or "")
            for side in ("from", "to"):
                if side in change:
                    change[side] = _redact_diff_value(path, change[side])
    return projected
