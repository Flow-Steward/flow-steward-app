"""Safety helpers for free-form JSON command inputs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_SENSITIVE_JSON_KEY = re.compile(
    r"(?:api[_-]?key|authorization|bearer|cookie|credential|dsn|passphrase|password|private[_-]?key|secret|session|token)",
    re.IGNORECASE,
)


def validate_bounded_json_object(value: dict[str, Any]) -> dict[str, Any]:
    """Bound execution input/context to the existing 32-level, 256-KiB policy."""
    stack: list[tuple[dict[Any, Any] | list[Any] | tuple[Any, ...], int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > 32:
            raise ValueError("Payload nesting depth must be <= 32 levels")
        children = current.values() if isinstance(current, dict) else current
        for item in children:
            if isinstance(item, (dict, list, tuple)):
                stack.append((item, depth + 1))
    encoded = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > 262_144:
        raise ValueError("Payload JSON size must be <= 262144 bytes")
    return value


def is_sensitive_json_key(key: object) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    if not normalized:
        return False
    if normalized.endswith(("_ref", "_reference", "_id")):
        return False
    if normalized in {"must_change_password", "revoked_sessions"} or normalized.endswith(
        ("_session_count", "_session_total")
    ):
        return False
    return bool(_SENSITIVE_JSON_KEY.search(normalized))


def sensitive_json_paths(value: Any, *, root: str = "$") -> list[str]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key or "").strip()
            child_path = f"{root}.{key_text}" if key_text else root
            if is_sensitive_json_key(key_text):
                paths.append(child_path)
                continue
            paths.extend(sensitive_json_paths(item, root=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(sensitive_json_paths(item, root=f"{root}[{index}]"))
    return paths


def validate_no_sensitive_json_keys(value: dict[str, Any], *, field_name: str) -> dict[str, Any]:
    paths = sensitive_json_paths(value, root=field_name)
    if paths:
        preview = ", ".join(paths[:5])
        suffix = "" if len(paths) <= 5 else f", +{len(paths) - 5} more"
        raise ValueError(
            f"{field_name} must not contain secret-bearing keys ({preview}{suffix}); "
            "use approved secret fields or secret reference keys instead"
        )
    return value
