"""Shared helpers for ephemeral workflow and step test runs."""

from __future__ import annotations

from typing import Any

from core.infrastructure.security.prompt_safety import redact_sensitive_structure

STATE_PREFIX = "state."


def _text(value: Any) -> str:
    return str(value or "").strip()


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def redact_test_run_payload(value: Any) -> Any:
    return _jsonable(redact_sensitive_structure(value))


def _coerce_update(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return dict(result)
    update = getattr(result, "update", None)
    return dict(update) if isinstance(update, dict) else {}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
