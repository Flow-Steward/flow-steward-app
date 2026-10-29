"""Text primitives preserve each consumer's existing normalization contract."""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    ("operation", "args", "expected"),
    [
        ("trim", (" \tŽąsis\n",), "Žąsis"),
        ("upper", (" straße ",), " STRASSE "),
        ("lower", (" ŽĄSIS ",), " žąsis "),
        ("title_case", ("o'NEILL foo-BAR  1ABC\tŽĄSIS",), "O'neill Foo-bar  1ABC\tŽąsis"),
        ("replace", ("aba", "", "."), ".a.b.a."),
        ("replace", ("a.b.a", ".", "$"), "a$b$a"),
        ("append", (" a ", " b "), " a  b "),
        ("length", ("a\u0301😀",), 3),
        ("pad", ("😀", 3, "0", "left"), "00😀"),
        ("pad", ("abc", -1, "0", "right"), "abc"),
        ("pad", ("a", 3, ".", "right"), "a.."),
    ],
)
def test_text_primitives_accept_normalized_strings(operation, args, expected):
    text = importlib.import_module("core.infrastructure.standard_text")

    assert getattr(text, operation)(*args) == expected


@pytest.mark.parametrize(
    ("action", "value", "params", "expected"),
    [
        ("trim", None, {}, "None"),
        ("upper", False, {}, "FALSE"),
        ("upper", " straße ", {}, " STRASSE "),
        ("lower", " ŽĄSIS ", {}, " žąsis "),
        ("lower", None, {}, "none"),
        ("title_case", "foo-BAR  1ABC", {}, "Foo-bar  1ABC"),
        ("replace", "aba", {"find": 0, "replace_with": False}, "aba"),
        ("replace", " a ", {"find": " ", "replace_with": "_"}, "_a_"),
        ("append", "x", {"append_value": {"ž": [False]}}, 'x{"ž": [false]}'),
        ("append", None, {"append_value": None}, "None"),
        ("length", [1, 2], {}, 6),
        ("pad", "a", {"width": "3.9", "fill_char": "0", "side": " RIGHT "}, "a00"),
        ("pad", "a", {"width": -1}, "a"),
    ],
)
def test_transform_keeps_text_coercion_and_parameter_defaults(action, value, params, expected):
    from core.application.orchestration import transform_actions

    assert getattr(transform_actions, f"_action_text_{action}")(value, params) == expected


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"width": 3, "fill_char": "ab"}, "fill_char must contain exactly one character"),
        ({"width": 3, "side": "middle"}, "side must be left or right"),
    ],
)
def test_transform_keeps_pad_validation(params, message):
    from core.application.orchestration.transform_actions import _action_text_pad

    with pytest.raises(ValueError, match=message):
        _action_text_pad("a", params)


def test_saved_connector_text_contracts_keep_null_and_collection_behavior():
    from core.application.orchestration.tools.standard_tools import length, lower, upper

    assert upper.invoke({"value": None}) == {"result": ""}
    assert lower.invoke({"value": None}) == {"result": ""}
    assert upper.invoke({"value": " straße "}) == {"result": " STRASSE "}
    assert length.invoke({"value": [1, 2]}) == {"result": 2}
    assert length.invoke({"value": {"a": 1}}) == {"result": 1}
    assert length.invoke({"value": None}) == {"result": 0}


@pytest.mark.parametrize(
    ("action", "value", "params", "expected"),
    [
        ("text.trim", " a ", {}, "a"),
        ("text.upper", None, {}, None),
        ("text.length", "a\u0301😀", {}, 3),
        ("text.title_case", "foo-BAR", {}, "Foo-Bar"),
        ("text.replace", " a ", {"find": " ", "replace_with": "_"}, "_ _a_ _"),
        ("text.append", "a", {"append_value": " b "}, "ab"),
        ("text.pad", "a", {"width": 3, "fill_char": "0", "side": "LEFT"}, "a00"),
    ],
)
def test_tabular_keeps_native_text_and_parameter_normalization(action, value, params, expected):
    import polars as pl

    from core.infrastructure.tabular_feed.actions import (
        _rule_expression,
        _shared_action_to_legacy_rule,
    )

    frame = pl.DataFrame({"value": [value]}, schema={"value": pl.String})
    rule = _shared_action_to_legacy_rule({"action": action, "target": "value", "params": params})

    assert frame.select(_rule_expression(frame, rule)).to_dicts() == [{"value": expected}]
