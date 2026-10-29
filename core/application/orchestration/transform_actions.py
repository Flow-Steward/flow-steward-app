"""Deterministic transform actions and their typed configuration catalog."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from core.application.orchestration import standard_tool_tokens as _tokens
from core.application.orchestration.date_actions import build_date_actions
from core.application.orchestration.transform_action_contract import TransformAction
from core.application.orchestration.transform_selectors import (
    MISSING as _MISSING,
)
from core.application.orchestration.transform_selectors import (
    json_get as _json_get,
)
from core.application.orchestration.transform_values import (
    _coerce_value,
    _money_decimal,
    _money_text,
)
from core.infrastructure import (
    standard_array,
    standard_json,
    standard_money,
    standard_numbers,
    standard_text,
)
from core.infrastructure.html_text import html_to_text
from core.infrastructure.rounding import SUPPORTED_ROUNDING_MODES

TRANSFORM_ACTION_CATALOG_VERSION = 1


def _action_text_trim(value: Any, _params: dict[str, Any]) -> Any:
    return standard_text.trim(str(value))


def _action_html_to_text(value: Any, _params: dict[str, Any]) -> str:
    return html_to_text(str(value))


def _action_text_upper(value: Any, _params: dict[str, Any]) -> Any:
    return standard_text.upper(str(value))


def _action_text_lower(value: Any, _params: dict[str, Any]) -> Any:
    return standard_text.lower(str(value))


def _action_text_title_case(value: Any, _params: dict[str, Any]) -> Any:
    return standard_text.title_case(str(value))


def _action_text_replace(value: Any, params: dict[str, Any]) -> Any:
    return standard_text.replace(
        str(value), str(params.get("find") or ""), str(params.get("replace_with") or "")
    )


def _action_text_append(value: Any, params: dict[str, Any]) -> Any:
    append_value = params.get("append_value")
    if isinstance(append_value, (dict, list)):
        append_value = json.dumps(append_value, ensure_ascii=False)
    return standard_text.append(f"{value}", f"{'' if append_value is None else append_value}")


def _action_text_length(value: Any, _params: dict[str, Any]) -> Any:
    return standard_text.length(str(value))


def _action_text_pad(value: Any, params: dict[str, Any]) -> Any:
    width = int(_coerce_value(params.get("width"), "number"))
    fill_char = str(params.get("fill_char", " ") or " ")
    if len(fill_char) != 1:
        raise ValueError("fill_char must contain exactly one character")
    side = str(params.get("side", "left") or "left").strip().lower()
    if side not in {"left", "right"}:
        raise ValueError("side must be left or right")
    return standard_text.pad(str(value), width, fill_char, side)


def _number_param(params: dict[str, Any], key: str) -> float:
    value = _coerce_value(params.get(key), "number")
    return float(value)


def _action_number_coerce(value: Any, _params: dict[str, Any]) -> Any:
    return _coerce_value(value, "number")


def _action_number_add(value: Any, params: dict[str, Any]) -> Any:
    return standard_numbers.add(_coerce_value(value, "number"), _number_param(params, "operand"))


def _action_number_subtract(value: Any, params: dict[str, Any]) -> Any:
    return standard_numbers.subtract(
        _coerce_value(value, "number"), _number_param(params, "operand")
    )


def _action_number_multiply(value: Any, params: dict[str, Any]) -> Any:
    return standard_numbers.multiply(
        _coerce_value(value, "number"), _number_param(params, "factor")
    )


def _action_number_divide(value: Any, params: dict[str, Any]) -> Any:
    divisor = _number_param(params, "divisor")
    if divisor == 0:
        raise ValueError("divisor must not be zero")
    return standard_numbers.divide(_coerce_value(value, "number"), divisor)


def _action_number_round(value: Any, params: dict[str, Any]) -> Any:
    decimals = int(_coerce_value(params.get("decimals", 0), "number"))
    return standard_numbers.round_number(
        _coerce_value(value, "number"), decimals, params.get("mode")
    )


def _action_number_floor(value: Any, _params: dict[str, Any]) -> Any:
    return standard_numbers.floor(_coerce_value(value, "number"))


def _action_number_ceil(value: Any, _params: dict[str, Any]) -> Any:
    return standard_numbers.ceil(_coerce_value(value, "number"))


def _money_param(params: dict[str, Any], key: str) -> Decimal:
    return _money_decimal(params.get(key))


def _action_money_coerce(value: Any, _params: dict[str, Any]) -> Any:
    return _money_text(value)


def _action_money_add(value: Any, params: dict[str, Any]) -> Any:
    return standard_money.add(_money_decimal(value), _money_param(params, "operand"))


def _action_money_subtract(value: Any, params: dict[str, Any]) -> Any:
    return standard_money.subtract(_money_decimal(value), _money_param(params, "operand"))


def _action_money_multiply(value: Any, params: dict[str, Any]) -> Any:
    return standard_money.multiply(_money_decimal(value), _money_param(params, "factor"))


def _action_money_round(value: Any, params: dict[str, Any]) -> Any:
    decimals = int(_coerce_value(params.get("decimals", 2), "number"))
    if decimals < 0:
        raise ValueError("decimals must be non-negative")
    return standard_money.round_money(_money_decimal(value), decimals, params.get("mode"))


def _action_boolean_coerce(value: Any, _params: dict[str, Any]) -> Any:
    return _coerce_value(value, "boolean")


def _action_array_length(value: Any, _params: dict[str, Any]) -> Any:
    return standard_array.length(_coerce_value(value, "array"))


def _action_array_first(value: Any, params: dict[str, Any]) -> Any:
    return standard_array.first(_coerce_value(value, "array"), params.get("default"))


def _action_array_at(value: Any, params: dict[str, Any]) -> Any:
    items = _coerce_value(value, "array")
    index = int(_coerce_value(params.get("index"), "number"))
    return standard_array.at(items, index, params.get("default"))


def _action_array_compact(value: Any, _params: dict[str, Any]) -> Any:
    return standard_array.compact(_coerce_value(value, "array"))


def _action_array_join_text(value: Any, params: dict[str, Any]) -> Any:
    separator = str(params.get("separator") or "")
    return standard_array.join_text(_coerce_value(value, "array"), separator)


def _action_json_pick(value: Any, params: dict[str, Any]) -> Any:
    return _json_get(value, str(params.get("path") or "").strip())


def _action_json_omit(value: Any, params: dict[str, Any]) -> Any:
    raw_paths = params.get("paths")
    paths = raw_paths if isinstance(raw_paths, list) else [raw_paths]
    return standard_json.omit(value, paths)


def _action_json_get(value: Any, params: dict[str, Any]) -> Any:
    default = params.get("default", _MISSING)
    return _json_get(value, str(params.get("path") or "").strip(), default)


def _action_json_set(value: Any, params: dict[str, Any]) -> Any:
    return standard_json.set_value(
        value, str(params.get("path") or "").strip(), params.get("new_value")
    )


def _action_json_keys(value: Any, _params: dict[str, Any]) -> Any:
    return standard_json.keys(value)


def _action_json_values(value: Any, _params: dict[str, Any]) -> Any:
    return standard_json.values(value)


def _action_json_stringify(value: Any, _params: dict[str, Any]) -> Any:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _action_map_values(value: Any, params: dict[str, Any]) -> Any:
    mapping = params.get("mapping")
    if not isinstance(mapping, dict):
        raise ValueError("mapping must be an object")
    keys = [value, str(value)]
    for key in keys:
        if key in mapping:
            return mapping[key]
    return value if bool(params.get("keep_unmapped", True)) else None


def _schema(properties: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


def _source_param(*types: str) -> dict[str, Any]:
    return {"literal": True, "selector": True, "current": True, "types": list(types or ("json",))}


def _build_action_registry() -> dict[str, TransformAction]:
    actions = [
        TransformAction(
            "text.html_to_text",
            "text",
            "HTML to text",
            "scalar",
            "text",
            "text",
            _schema({}),
            (),
            {},
            _action_html_to_text,
            tool_id=_tokens.TEXT,
            tool_action_id="html_to_text",
        ),
        TransformAction(
            "text.trim",
            "text",
            "Trim",
            "scalar",
            "text",
            "text",
            _schema({}),
            (),
            {},
            _action_text_trim,
            tool_id=_tokens.TEXT,
            tool_action_id="trim",
        ),
        TransformAction(
            "text.upper",
            "text",
            "Uppercase",
            "scalar",
            "text",
            "text",
            _schema({}),
            (),
            {},
            _action_text_upper,
            tool_id=_tokens.TEXT,
            tool_action_id="upper",
        ),
        TransformAction(
            "text.lower",
            "text",
            "Lowercase",
            "scalar",
            "text",
            "text",
            _schema({}),
            (),
            {},
            _action_text_lower,
            tool_id=_tokens.TEXT,
            tool_action_id="lower",
        ),
        TransformAction(
            "text.title_case",
            "text",
            "Title Case",
            "scalar",
            "text",
            "text",
            _schema({}),
            (),
            {},
            _action_text_title_case,
            tool_id=_tokens.TEXT,
            tool_action_id="title_case",
        ),
        TransformAction(
            "text.replace",
            "text",
            "Replace",
            "scalar",
            "text",
            "text",
            _schema({"find": {"type": "string"}, "replace_with": {"type": "string"}}),
            ("find", "replace_with"),
            {},
            _action_text_replace,
            tool_id=_tokens.TEXT,
            tool_action_id="replace",
        ),
        TransformAction(
            "text.append",
            "text",
            "Append",
            "scalar",
            "text",
            "text",
            _schema({"append_value": {"type": "json"}}),
            ("append_value",),
            {"append_value": _source_param("text", "number", "boolean", "json")},
            _action_text_append,
            tool_id=_tokens.TEXT,
            tool_action_id="append",
        ),
        TransformAction(
            "text.length",
            "text",
            "Length",
            "scalar",
            "text",
            "number",
            _schema({}),
            (),
            {},
            _action_text_length,
            tool_id=_tokens.TEXT,
            tool_action_id="length",
        ),
        TransformAction(
            "text.pad",
            "text",
            "Pad",
            "scalar",
            "text",
            "text",
            _schema(
                {
                    "width": {"type": "number"},
                    "fill_char": {"type": "string"},
                    "side": {"type": "string", "enum": ["left", "right"]},
                }
            ),
            ("width",),
            {},
            _action_text_pad,
            tool_id=_tokens.TEXT,
            tool_action_id="pad",
        ),
        TransformAction(
            "number.coerce",
            "number",
            "Coerce Number",
            "scalar",
            "json",
            "number",
            _schema({}),
            (),
            {},
            _action_number_coerce,
            tool_id=_tokens.NUMBER,
            tool_action_id="coerce",
        ),
        TransformAction(
            "number.add",
            "number",
            "Add",
            "scalar",
            "number",
            "number",
            _schema({"operand": {"type": "number"}}),
            ("operand",),
            {"operand": _source_param("number")},
            _action_number_add,
            tool_id=_tokens.NUMBER,
            tool_action_id="add",
        ),
        TransformAction(
            "number.subtract",
            "number",
            "Subtract",
            "scalar",
            "number",
            "number",
            _schema({"operand": {"type": "number"}}),
            ("operand",),
            {"operand": _source_param("number")},
            _action_number_subtract,
            tool_id=_tokens.NUMBER,
            tool_action_id="subtract",
        ),
        TransformAction(
            "number.multiply",
            "number",
            "Multiply",
            "scalar",
            "number",
            "number",
            _schema({"factor": {"type": "number"}}),
            ("factor",),
            {"factor": _source_param("number")},
            _action_number_multiply,
            tool_id=_tokens.NUMBER,
            tool_action_id="multiply",
        ),
        TransformAction(
            "number.divide",
            "number",
            "Divide",
            "scalar",
            "number",
            "number",
            _schema({"divisor": {"type": "number"}}),
            ("divisor",),
            {"divisor": _source_param("number")},
            _action_number_divide,
            tool_id=_tokens.NUMBER,
            tool_action_id="divide",
        ),
        TransformAction(
            "number.round",
            "number",
            "Round",
            "scalar",
            "number",
            "number",
            _schema(
                {
                    "decimals": {"type": "number"},
                    "mode": {"type": "string", "enum": list(SUPPORTED_ROUNDING_MODES)},
                }
            ),
            (),
            {"decimals": _source_param("number")},
            _action_number_round,
            tool_id=_tokens.NUMBER,
            tool_action_id="round",
        ),
        TransformAction(
            "number.floor",
            "number",
            "Floor",
            "scalar",
            "number",
            "number",
            _schema({}),
            (),
            {},
            _action_number_floor,
            tool_id=_tokens.NUMBER,
            tool_action_id="floor",
        ),
        TransformAction(
            "number.ceil",
            "number",
            "Ceil",
            "scalar",
            "number",
            "number",
            _schema({}),
            (),
            {},
            _action_number_ceil,
            tool_id=_tokens.NUMBER,
            tool_action_id="ceil",
        ),
        TransformAction(
            "money.coerce",
            "money",
            "Coerce Money",
            "scalar",
            "json",
            "money",
            _schema({}),
            (),
            {},
            _action_money_coerce,
            tool_id=_tokens.MONEY,
            tool_action_id="coerce",
        ),
        TransformAction(
            "money.add",
            "money",
            "Add Money",
            "scalar",
            "money",
            "money",
            _schema({"operand": {"type": "money"}}),
            ("operand",),
            {"operand": _source_param("money")},
            _action_money_add,
            tool_id=_tokens.MONEY,
            tool_action_id="add",
        ),
        TransformAction(
            "money.subtract",
            "money",
            "Subtract Money",
            "scalar",
            "money",
            "money",
            _schema({"operand": {"type": "money"}}),
            ("operand",),
            {"operand": _source_param("money")},
            _action_money_subtract,
            tool_id=_tokens.MONEY,
            tool_action_id="subtract",
        ),
        TransformAction(
            "money.multiply",
            "money",
            "Multiply Money",
            "scalar",
            "money",
            "money",
            _schema({"factor": {"type": "money"}}),
            ("factor",),
            {"factor": _source_param("money")},
            _action_money_multiply,
            tool_id=_tokens.MONEY,
            tool_action_id="multiply",
        ),
        TransformAction(
            "money.round",
            "money",
            "Round Money",
            "scalar",
            "money",
            "money",
            _schema(
                {
                    "decimals": {"type": "number"},
                    "mode": {"type": "string", "enum": list(SUPPORTED_ROUNDING_MODES)},
                }
            ),
            (),
            {"decimals": _source_param("number")},
            _action_money_round,
            tool_id=_tokens.MONEY,
            tool_action_id="round",
        ),
        TransformAction(
            "boolean.coerce",
            "boolean",
            "Coerce Boolean",
            "scalar",
            "json",
            "boolean",
            _schema({}),
            (),
            {},
            _action_boolean_coerce,
            tool_id="std.boolean",
            tool_action_id="coerce",
        ),
        TransformAction(
            "array.length",
            "array",
            "Array Length",
            "array",
            "array",
            "number",
            _schema({}),
            (),
            {},
            _action_array_length,
            tool_id=_tokens.ARRAY,
            tool_action_id="length",
        ),
        TransformAction(
            "array.first",
            "array",
            "First Item",
            "array",
            "array",
            "json",
            _schema({"default": {"type": "json"}}),
            (),
            {"default": _source_param("json")},
            _action_array_first,
            tool_id=_tokens.ARRAY,
            tool_action_id="first",
        ),
        TransformAction(
            "array.at",
            "array",
            "Item At Index",
            "array",
            "array",
            "json",
            _schema({"index": {"type": "number"}, "default": {"type": "json"}}),
            ("index",),
            {"default": _source_param("json")},
            _action_array_at,
            tool_id=_tokens.ARRAY,
            tool_action_id="at",
        ),
        TransformAction(
            "array.compact",
            "array",
            "Compact",
            "array",
            "array",
            "array",
            _schema({}),
            (),
            {},
            _action_array_compact,
            tool_id=_tokens.ARRAY,
            tool_action_id="compact",
        ),
        TransformAction(
            "array.join_text",
            "array",
            "Join Text",
            "array",
            "array",
            "text",
            _schema({"separator": {"type": "string"}}),
            ("separator",),
            {},
            _action_array_join_text,
            tool_id=_tokens.ARRAY,
            tool_action_id="join_text",
        ),
        TransformAction(
            "json.pick",
            "json",
            "Pick Path",
            "json",
            "json",
            "json",
            _schema({"path": {"type": "string"}}),
            ("path",),
            {},
            _action_json_pick,
            tool_id=_tokens.JSON,
            tool_action_id="pick",
        ),
        TransformAction(
            "json.omit",
            "json",
            "Omit Paths",
            "json",
            "object",
            "object",
            _schema({"paths": {"type": "array"}}),
            ("paths",),
            {},
            _action_json_omit,
            tool_id=_tokens.JSON,
            tool_action_id="omit",
        ),
        TransformAction(
            "json.get",
            "json",
            "Get Path",
            "json",
            "json",
            "json",
            _schema({"path": {"type": "string"}, "default": {"type": "json"}}),
            ("path",),
            {"default": _source_param("json")},
            _action_json_get,
            tool_id=_tokens.JSON,
            tool_action_id="get",
        ),
        TransformAction(
            "json.set",
            "json",
            "Set Path",
            "json",
            "object",
            "object",
            _schema({"path": {"type": "string"}, "new_value": {"type": "json"}}),
            ("path", "new_value"),
            {"new_value": _source_param("json")},
            _action_json_set,
            tool_id=_tokens.JSON,
            tool_action_id="set",
        ),
        TransformAction(
            "json.keys",
            "json",
            "Object Keys",
            "json",
            "object",
            "array",
            _schema({}),
            (),
            {},
            _action_json_keys,
            tool_id=_tokens.JSON,
            tool_action_id="keys",
        ),
        TransformAction(
            "json.values",
            "json",
            "Object Values",
            "json",
            "object",
            "array",
            _schema({}),
            (),
            {},
            _action_json_values,
            tool_id=_tokens.JSON,
            tool_action_id="values",
        ),
        TransformAction(
            "json.stringify",
            "json",
            "Stringify",
            "json",
            "json",
            "text",
            _schema({}),
            (),
            {},
            _action_json_stringify,
            tool_id=_tokens.JSON,
            tool_action_id="stringify",
        ),
        TransformAction(
            "map.values",
            "map",
            "Map Values",
            "json",
            "json",
            "json",
            _schema(
                {
                    "mapping": {"type": "object"},
                    "keep_unmapped": {"type": "boolean"},
                }
            ),
            ("mapping",),
            {},
            _action_map_values,
            tool_id="std.map",
            tool_action_id="values",
        ),
    ]
    return {action.action_id: action for action in actions}


_ACTIONS = _build_action_registry()
_ACTIONS.update(build_date_actions())


def list_transform_action_contracts() -> list[dict[str, Any]]:
    return [
        action.contract() for action in sorted(_ACTIONS.values(), key=lambda item: item.action_id)
    ]


def get_transform_action_contract(action_id: str) -> dict[str, Any] | None:
    action = _ACTIONS.get(str(action_id or "").strip())
    return action.contract() if action else None
