"""Connector publication exercises shared primitives and Builder discovery contracts."""

from __future__ import annotations

import importlib

import pytest

CASES = [
    ("std.text", "trim", {"value": " a "}, "a"),
    ("std.text", "title_case", {"value": "foo-BAR  1ABC"}, "Foo-bar  1ABC"),
    ("std.text", "replace", {"value": "aba", "find": "a", "replace_with": "$"}, "$b$"),
    ("std.text", "append", {"value": "x", "append_value": {"ž": False}}, 'x{"ž": false}'),
    ("std.text", "pad", {"value": "a", "width": 3, "side": "right", "fill_char": "0"}, "a00"),
    ("std.numbers", "coerce", {"value": " 3 "}, 3),
    ("std.numbers", "add", {"value": 2, "operand": 3}, 5.0),
    ("std.numbers", "subtract", {"value": 2, "operand": 3}, -1.0),
    ("std.numbers", "multiply", {"value": 2, "factor": 3}, 6.0),
    ("std.numbers", "divide", {"value": 9, "divisor": 2}, 4.5),
    ("std.numbers", "floor", {"value": -1.2}, -2),
    ("std.numbers", "ceil", {"value": -1.2}, -1),
    ("std.money", "coerce", {"value": "1.20"}, "1.20"),
    ("std.money", "add", {"value": "1.20", "operand": "0.03"}, "1.23"),
    ("std.money", "subtract", {"value": "1.20", "operand": "0.03"}, "1.17"),
    ("std.money", "multiply", {"value": "1.20", "factor": "2"}, "2.40"),
    ("std.money", "round", {"value": "1.225", "decimals": 2, "mode": "half_even"}, "1.22"),
    ("std.boolean", "coerce", {"value": "off"}, False),
    ("std.array", "length", {"value": [0, None, False]}, 3),
    ("std.array", "first", {"value": [], "default": "empty"}, "empty"),
    ("std.array", "at", {"value": [1], "index": 1, "default": None}, None),
    ("std.array", "compact", {"value": [0, None, False, ""]}, [0, False, ""]),
    ("std.array", "join_text", {"value": ["a", None, 2], "separator": ":"}, "a::2"),
    (
        "std.json",
        "omit",
        {"value": {"a": [{"x": 1, "y": 2}]}, "paths": ["a[0].x"]},
        {"a": [{"y": 2}]},
    ),
    ("std.json", "get", {"value": {"a.b": [False]}, "path": '["a.b"][0]'}, False),
    ("std.json", "set", {"value": {}, "path": "a.b", "new_value": 1}, {"a": {"b": 1}}),
    ("std.json", "keys", {"value": {"a": 1, "b": 2}}, ["a", "b"]),
    ("std.json", "values", {"value": {"a": 1, "b": 2}}, [1, 2]),
]


def _tool(tool_id, operation):
    from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests

    manifest = builtin_standard_tool_manifests()[tool_id]
    assert operation in {fn["name"] for fn in manifest["interface"]["functions"]}
    return getattr(importlib.import_module(manifest["implementation"]["module"]), operation)


def _invoke(tool_id, operation, arguments):
    from core.application.orchestration.tools.tool_resolver import invoke_manifest_tool
    from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
    from core.application.registry.tool_manifest import ToolManifest

    manifest = ToolManifest.model_validate(builtin_standard_tool_manifests()[tool_id])
    return invoke_manifest_tool(manifest, function_name=operation, arguments=arguments)


@pytest.mark.parametrize(("tool_id", "operation", "arguments", "expected"), CASES)
def test_published_connector_actions_execute_shared_semantics(
    tool_id, operation, arguments, expected
):
    assert _invoke(tool_id, operation, arguments) == {"result": expected}


def test_new_operations_survive_registry_schema_and_are_visible_to_builder():
    from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
    from core.application.registry.tool_manifest import ToolManifest
    from core.application.workflows.discovery.capability_contracts import tool_operations

    seen = set()
    for tool_id, raw in builtin_standard_tool_manifests().items():
        saved = ToolManifest.model_validate(raw).to_registry_payload()
        assert saved["config"]["agent_exposure"] == "connector_only"
        discovered = {fn["operation_id"] for fn in tool_operations(saved)}
        declared = {fn["name"] for fn in raw["interface"]["functions"]}
        assert discovered == declared
        for fn in raw["interface"]["functions"]:
            assert callable(getattr(_tool(tool_id, fn["name"]), "invoke", None))
        seen.update((tool_id, operation) for operation in discovered)
    assert {(tool_id, operation) for tool_id, operation, _, _ in CASES} <= seen


@pytest.mark.parametrize(
    ("tool_id", "operation", "arguments", "message"),
    [
        ("std.numbers", "divide", {"value": 1, "divisor": -0.0}, "divisor must not be zero"),
        ("std.money", "round", {"value": "1", "decimals": -1}, "decimals must be non-negative"),
        ("std.array", "at", {"value": [1], "index": -1}, "index must be non-negative"),
        ("std.array", "length", {"value": "abc"}, "str is not an array"),
        ("std.json", "get", {"value": {}, "path": "a"}, "path 'a' is missing"),
        (
            "std.text",
            "pad",
            {"value": "a", "width": 3, "fill_char": "xx"},
            "fill_char must contain exactly one character",
        ),
    ],
)
def test_new_connector_validation_is_not_silently_relaxed(tool_id, operation, arguments, message):
    tool = _tool(tool_id, operation)
    with pytest.raises(ValueError, match=message):
        tool.invoke(arguments)


def test_json_and_array_operations_do_not_mutate_the_input():
    value = {"a": [{"x": 1, "y": 2}]}
    assert _tool("std.json", "omit").invoke({"value": value, "paths": ["a[0].x"]}) == {
        "result": {"a": [{"y": 2}]}
    }
    assert value == {"a": [{"x": 1, "y": 2}]}
    items = [{"x": 1}]
    selected = _tool("std.array", "first").invoke({"value": items})["result"]
    selected["x"] = 2
    assert items == [{"x": 1}]


DATE_CASES = [
    # Date-only inputs retain their precision; explicit datetimes retain theirs.
    ("parse", {"value": "2026-10-29T00:00:00"}, "2026-10-29T00:00:00"),
    ("add", {"value": "2026-10-29T00:00:00", "amount": 1, "unit": "days"}, "2026-10-30T00:00:00"),
    ("start_of", {"value": "2026-10-29T17:00:00", "unit": "month"}, "2026-10-01T00:00:00"),
    ("end_of", {"value": "2026-10-29T17:00:00", "unit": "month"}, "2026-11-01T00:00:00"),
    ("parse", {"value": "2026-10-29"}, "2026-10-29"),
    ("to_utc", {"value": "2026-10-29T12:00:00+02:00"}, "2026-10-29T10:00:00Z"),
    (
        "to_timezone",
        {"value": "2026-10-29T12:00:00Z", "timezone": "Europe/Vilnius"},
        "2026-10-29T14:00:00+02:00",
    ),
    ("format", {"value": "2026-10-29", "output_format": "DD.MM.YYYY"}, "29.10.2026"),
    ("add", {"value": "2026-10-29", "amount": 1, "unit": "days"}, "2026-10-30"),
    ("diff", {"value": "2026-10-30", "from": "2026-10-29", "unit": "days"}, 1),
    ("part", {"value": "2026-10-29", "part": "weekday"}, 4),
    ("start_of", {"value": "2026-10-29", "unit": "month"}, "2026-10-01"),
    ("end_of", {"value": "2026-10-29", "unit": "month"}, "2026-11-01"),
    ("compare", {"value": "2026-10-29T10:00:00Z", "to": "2026-10-29T12:00:00+02:00"}, 0),
    (
        "interval_make",
        {"value": "2026-10-29T10:00:00Z", "end": "2026-10-29T11:00:00Z"},
        {"empty": False, "start": "2026-10-29T10:00:00Z", "end": "2026-10-29T11:00:00Z"},
    ),
    (
        "interval_duration",
        {"value": {"empty": False, "start": "2026-10-29T10:00:00Z", "end": "2026-10-29T11:00:00Z"}},
        60,
    ),
    (
        "interval_intersect_all",
        {
            "value": [
                {"empty": False, "start": "2026-10-29T10:00:00Z", "end": "2026-10-29T11:00:00Z"}
            ]
        },
        {"empty": False, "start": "2026-10-29T10:00:00Z", "end": "2026-10-29T11:00:00Z"},
    ),
    (
        "interval_slots",
        {
            "value": {
                "empty": False,
                "start": "2026-10-29T10:00:00Z",
                "end": "2026-10-29T11:00:00Z",
            },
            "duration_minutes": 30,
            "step_minutes": 30,
            "max_slots": 2,
        },
        [
            {"empty": False, "start": "2026-10-29T10:00:00Z", "end": "2026-10-29T10:30:00Z"},
            {"empty": False, "start": "2026-10-29T10:30:00Z", "end": "2026-10-29T11:00:00Z"},
        ],
    ),
    ("combine", {"value": "2026-10-29", "time": "17:00"}, "2026-10-29T17:00:00"),
]


@pytest.mark.parametrize(("operation", "arguments", "expected"), DATE_CASES)
def test_every_date_connector_uses_the_common_date_adapter(operation, arguments, expected):
    assert _invoke("std.date", operation, arguments) == {"result": expected}


def test_date_connector_schema_preserves_required_parameters_and_free_locale_strings():
    from core.application.orchestration.date_actions import build_date_actions
    from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests

    functions = {
        fn["name"]: fn
        for fn in builtin_standard_tool_manifests()["std.date"]["interface"]["functions"]
    }
    assert set(functions) == {operation for operation, _, _ in DATE_CASES}
    for action in build_date_actions().values():
        parameters = {
            param["name"]: param for param in functions[action.tool_action_id]["parameters"]
        }
        assert set(parameters) == {"value", *action.params_schema["properties"]}
        assert {key for key, param in parameters.items() if param["required"]} == {
            "value",
            *action.required_params,
        }
        if "locale" in parameters:
            assert parameters["locale"]["type"] == "string"
            assert "values" not in parameters["locale"]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"value": "2026-10-29", "output_format": "DD.MM.YYYY"}, "29.10.2026"),
        (
            {
                "value": "29 spalio 2026",
                "input_format": "D MMMM YYYY",
                "output_format": "MMMM YYYY",
                "locale": "lt",
            },
            "spalio 2026",
        ),
        (
            {
                "value": "29 spalio 2026",
                "input_format": "D MMMM YYYY",
                "output_format": "MMMM YYYY",
                "locale": "lt",
                "output_locale": "en",
            },
            "October 2026",
        ),
    ],
)
def test_date_connector_format_uses_default_and_inherited_locales(arguments, expected):
    assert _invoke("std.date", "format", arguments) == {"result": expected}


@pytest.mark.parametrize(
    ("key", "invalid"),
    [
        ("locale", ""),
        ("locale", "de"),
        ("output_locale", ""),
        ("output_locale", "de"),
    ],
)
def test_date_connector_format_keeps_explicit_unknown_locales_strict(key, invalid):
    from core.infrastructure.date_types import DateValueError

    with pytest.raises(DateValueError) as error:
        _invoke(
            "std.date",
            "format",
            {"value": "2026-10-29", "output_format": "MMMM YYYY", key: invalid},
        )
    assert error.value.code == "unknown_locale"


def test_date_connector_rejects_float_century_with_named_error():
    from core.infrastructure.date_types import DateValueError

    with pytest.raises(DateValueError) as error:
        _invoke(
            "std.date",
            "parse",
            {"value": "29/10/26", "input_format": "DD/MM/YY", "century": 2000.0},
        )
    assert error.value.code == "invalid_date"
