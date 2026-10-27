"""The full value of one field a step produced, for "Show full text".

Job Details shows a bounded preview of every step output, so a generated email
or blog post ends with "… (1027 characters in total)". The step's message keeps
the value itself; this reads it back for one field, masked like every other
step value and capped so one request cannot return an unbounded payload.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.infrastructure.security.prompt_safety import redact_sensitive_structure

#: Largest text returned in full; anything longer is cut with a note.
MAX_FULL_VALUE_CHARS = 200_000

_MISSING = object()
_STATE_PREFIX = "state."


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _resolve(data: Any, path: str) -> Any:
    current = data
    for part in [segment for segment in path.split(".") if segment]:
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def _candidates(artifacts: Mapping[str, Any], field: str) -> list[Any]:
    """Where a step kind keeps the full value of an output it previewed."""
    state_path = field.removeprefix(_STATE_PREFIX)
    http_result = _mapping(artifacts.get("http_request"))
    connector_result = _mapping(
        _mapping(_mapping(artifacts.get("connector_invocation")).get("response")).get("result")
    )
    return [
        _resolve(_mapping(artifacts.get("mapped_outputs")), field),
        _resolve(connector_result, field),
        # An agent stores its answer under its own output names.
        artifacts.get(field, _MISSING) if not field.startswith("_") else _MISSING,
        _resolve(_mapping(artifacts.get("_state_patch")), state_path),
        http_result.get("response_body", _MISSING) if field == "body" else _MISSING,
        # A transform previews what it wrote to its target as "value".
        _resolve(
            _mapping(artifacts.get("_state_patch")),
            str(artifacts.get("result_target") or "").removeprefix(_STATE_PREFIX),
        )
        if field == "value" and str(artifacts.get("result_target") or "").startswith(_STATE_PREFIX)
        else _MISSING,
    ]


def full_step_output_value(message: Mapping[str, Any], field: str) -> dict[str, Any]:
    """``{"found": bool, "value": ...}`` for one output field of a step message."""
    name = str(field or "").strip()
    if not name:
        return {"found": False, "value": None}
    artifacts = _mapping(message.get("artifacts"))
    value = next((item for item in _candidates(artifacts, name) if item is not _MISSING), _MISSING)
    if value is _MISSING:
        return {"found": False, "value": None}
    try:
        value = redact_sensitive_structure(value)
    except Exception:
        return {"found": False, "value": None}
    if isinstance(value, str) and len(value) > MAX_FULL_VALUE_CHARS:
        value = f"{value[:MAX_FULL_VALUE_CHARS]}… ({len(value)} characters in total)"
    return {"found": True, "value": value}


__all__ = ["MAX_FULL_VALUE_CHARS", "full_step_output_value"]
