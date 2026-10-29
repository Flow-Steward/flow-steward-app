"""Application date adapters and the standard transform action schemas."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any

from core.application.orchestration import standard_tool_tokens as tokens
from core.application.orchestration.date_action_support import get_date_lexicons
from core.application.orchestration.transform_action_contract import TransformAction
from core.infrastructure.date_operations import (
    date_operation_locales,
    execute_date_operation,
    validate_date_operation_params,
)
from core.infrastructure.date_types import DateLexicon


def execute_date_action(
    action_id: str,
    value: object,
    params: Mapping[str, object],
    *,
    lexicons: Mapping[str, DateLexicon] | None = None,
) -> object:
    """Execute with strict application locales, or an already trusted immutable mapping."""
    if lexicons is None:
        locale, output_locale = date_operation_locales(params)
        lexicons = get_date_lexicons((locale, output_locale))
    return execute_date_operation(action_id, value, params, lexicons=lexicons)


def validate_date_action_params(action_id: str, params: Mapping[str, object]) -> None:
    """Validate literal format syntax during transform configuration validation."""
    validate_date_operation_params(action_id, params)


def _string(**extra: Any) -> dict[str, Any]:
    return {"type": "string", **extra}


def build_date_actions() -> dict[str, TransformAction]:
    """Return the fifteen date actions for the existing catalog composition point."""
    parser = {
        "input_format": _string(maxLength=128),
        "locale": _string(default="en"),
        "century": {"type": "integer", "enum": [1900, 2000], "default": 2000},
        "dst_policy": _string(enum=["error", "first"], default="error"),
        "timezone": _string(),
    }
    specs: tuple[tuple[str, str, str, dict[str, Any], tuple[str, ...]], ...] = (
        ("parse", "text", "text", {}, ()),
        ("to_utc", "text", "text", {}, ()),
        ("to_timezone", "text", "text", {"output_format": _string(maxLength=128)}, ("timezone",)),
        (
            "format",
            "text",
            "text",
            {"output_format": _string(maxLength=128), "output_locale": _string()},
            ("output_format",),
        ),
        (
            "add",
            "text",
            "text",
            {
                "amount": {"type": "integer"},
                "unit": _string(enum=["minutes", "hours", "days", "weeks", "months", "years"]),
            },
            ("amount", "unit"),
        ),
        (
            "diff",
            "text",
            "number",
            {
                "from": _string(),
                "unit": _string(enum=["minutes", "hours", "days", "months", "years"]),
            },
            ("from", "unit"),
        ),
        (
            "part",
            "text",
            "number",
            {
                "part": _string(
                    enum=["year", "month", "day", "weekday", "iso_week", "hour", "minute"]
                )
            },
            ("part",),
        ),
        (
            "start_of",
            "text",
            "text",
            {"unit": _string(enum=["day", "week", "month", "year"])},
            ("unit",),
        ),
        (
            "end_of",
            "text",
            "text",
            {"unit": _string(enum=["day", "week", "month", "year"])},
            ("unit",),
        ),
        ("compare", "text", "number", {"to": _string()}, ("to",)),
        ("interval_make", "text", "object", {"end": _string()}, ("end",)),
        (
            "interval_duration",
            "object",
            "number",
            {"unit": _string(enum=["minutes", "hours", "days"], default="minutes")},
            (),
        ),
        ("interval_intersect_all", "array", "object", {}, ()),
        (
            "interval_slots",
            "object",
            "array",
            {
                "duration_minutes": {"type": "integer", "minimum": 1},
                "step_minutes": {"type": "integer", "minimum": 1},
                "max_slots": {"type": "integer", "minimum": 1},
                "align_minutes": {"type": "integer", "minimum": 1},
                "align_timezone": _string(default="UTC"),
            },
            ("duration_minutes", "step_minutes", "max_slots"),
        ),
        (
            "combine",
            "text",
            "text",
            {
                "time": _string(),
                "date_format": _string(maxLength=128),
                "time_format": _string(maxLength=128),
            },
            ("time",),
        ),
    )
    actions = {}
    for name, input_type, output_type, extras, required in specs:
        properties = {**parser, **extras} if input_type == "text" else extras
        if name == "combine":
            properties.pop("input_format")
        sources = {
            key: {
                "literal": True,
                "selector": True,
                "current": True,
                "types": ["number" if key == "amount" else "text"],
            }
            for key in ("timezone", "from", "to", "end", "time", "amount")
            if key in properties
        }
        action_id = f"date.{name}"
        actions[action_id] = TransformAction(
            action_id,
            "date",
            name.replace("_", " ").title(),
            "array" if input_type == "array" else "json" if input_type == "object" else "scalar",
            input_type,
            output_type,
            {"type": "object", "properties": properties, "additionalProperties": False},
            required,
            sources,
            partial(execute_date_action, action_id),
            tool_id=tokens.DATE,
            tool_action_id=name,
        )
    return actions
