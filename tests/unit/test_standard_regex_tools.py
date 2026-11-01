"""Deterministic regex actions expose stable values and bounded execution."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.application.orchestration.tools import standard_tools
from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
from core.application.registry.tool_manifest import ToolManifest


def invoke(name, **arguments):
    return getattr(standard_tools, name).invoke(arguments)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("regex_find", "A12"), ("regex_find_all", ["A12", "B34"])],
)
def test_search_returns_full_matches_even_with_capture_groups(name, expected):
    assert invoke(name, value="A12 B34", pattern=r"([A-Z])(\d+)") == {"result": expected}


@pytest.mark.parametrize(("name", "expected"), [("regex_find", None), ("regex_find_all", [])])
def test_no_match_has_stable_result(name, expected):
    assert invoke(name, value="abc", pattern=r"\d+") == {"result": expected}


@pytest.mark.parametrize(
    ("name", "expected"),
    [("regex_replace", "12-A B34"), ("regex_replace_all", "12-A 34-B")],
)
def test_replace_first_or_all_with_group_references(name, expected):
    assert invoke(name, value="A12 B34", pattern=r"([A-Z])(\d+)", replacement=r"\g<2>-\g<1>") == {
        "result": expected
    }


def test_search_flags_and_zero_width_matches():
    assert invoke("regex_find_all", value="Ab\naB", pattern="^ab", flags="im") == {
        "result": ["Ab", "aB"]
    }
    assert invoke("regex_find_all", value="ab", pattern="") == {"result": ["", "", ""]}


def test_replace_no_match_preserves_input():
    assert invoke("regex_replace_all", value="abc", pattern=r"\d", replacement="x") == {
        "result": "abc"
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"value": "private-value", "pattern": "["},
        {"value": "private-value", "pattern": "x", "flags": "g"},
    ],
)
def test_bad_pattern_or_flags_rejected_without_input_leak(arguments):
    with pytest.raises(ValueError, match="regex_") as error:
        invoke("regex_find", **arguments)
    assert "private-value" not in str(error.value)


def test_invalid_replacement_rejected():
    with pytest.raises(ValueError, match="regex_invalid_replacement"):
        invoke("regex_replace_all", value="a", pattern="a", replacement=r"\g<missing>")


@pytest.mark.parametrize(
    "arguments",
    [{"value": "a" * (1_048_576 + 1), "pattern": "a"}, {"value": "a", "pattern": "a" * 4097}],
)
def test_oversized_search_rejected(arguments):
    with pytest.raises(ValueError, match=r"regex_.*limit"):
        invoke("regex_find", **arguments)


def test_find_all_rejects_excess_matches_instead_of_truncating():
    with pytest.raises(ValueError, match="regex_match_limit"):
        invoke("regex_find_all", value="x" * 1001, pattern="x")
    assert len(invoke("regex_find_all", value="x" * 1000, pattern="x")["result"]) == 1000


def test_replace_all_rejects_output_expansion():
    with pytest.raises(ValueError, match="regex_output_limit"):
        invoke("regex_replace_all", value="a" * 300, pattern="a", replacement="b" * 4096)


def test_pathological_pattern_times_out():
    with pytest.raises(ValueError, match="regex_timeout"):
        invoke("regex_find", value="a" * 100_000 + "!", pattern=r"(a+)+$")


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("regex_find", {"value": "contact a@b.test", "pattern": r"\S+@\S+"}, "a@b.test"),
        ("regex_find_all", {"value": "a1 b2", "pattern": r"\w\d"}, ["a1", "b2"]),
        ("regex_find", {"value": "abc", "pattern": r"\d+"}, None),
        ("regex_find_all", {"value": "abc", "pattern": r"\d+"}, []),
        ("regex_replace", {"value": "a1 a2", "pattern": r"\d", "replacement": "X"}, "aX a2"),
        ("regex_replace_all", {"value": "a1 a2", "pattern": r"\d", "replacement": "X"}, "aX aX"),
    ],
)
def test_regex_is_discoverable_and_runs_through_connector(name, arguments, expected):
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    manifest = ToolManifest.model_validate(builtin_standard_tool_manifests()["std.text"])
    function = next(fn for fn in manifest.interface.functions if fn.name == name)
    assert function.display_name
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _: manifest))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger",
            return_value=None,
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "connector.std.text",
                "override": {
                    "connector": {
                        "function_name": name,
                        "input_mapping": {key: "state." + key for key in arguments},
                    }
                },
                "output_mapping_jsonb": {"result": "state.extracted"},
            },
            state=arguments,
        )
    assert result["ok"] is True, result
    assert result["state_patch"]["extracted"] == expected


def test_invalid_replacement_rejected_even_without_a_match():
    with pytest.raises(ValueError, match="regex_invalid_replacement"):
        invoke("regex_replace_all", value="b", pattern="a", replacement=r"\g<missing>")


def test_oversized_replacement_rejected():
    with pytest.raises(ValueError, match="regex_replacement_limit"):
        invoke("regex_replace", value="a", pattern="a", replacement="b" * 4097)


def test_connector_routes_invalid_pattern_as_error():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    manifest = ToolManifest.model_validate(builtin_standard_tool_manifests()["std.text"])
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _: manifest))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger",
            return_value=None,
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "connector.std.text",
                "override": {
                    "connector": {
                        "function_name": "regex_find",
                        "input_mapping": {"value": "state.value", "pattern": "state.pattern"},
                    }
                },
                "output_mapping_jsonb": {"result": "state.extracted"},
            },
            state={"value": "private-value", "pattern": "["},
        )
    assert result["ok"] is False
    assert result["detail"] == "regex_invalid_pattern"


@pytest.mark.parametrize(
    "name", ["regex_find", "regex_find_all", "regex_replace", "regex_replace_all"]
)
def test_reverse_pattern_rejected_to_preserve_source_order(name):
    arguments = {"value": "aba", "pattern": "(?r)a"}
    if "replace" in name:
        arguments["replacement"] = "X"
    with pytest.raises(ValueError, match="regex_reverse_unsupported"):
        invoke(name, **arguments)


def test_literal_escape_replacement_on_large_match_is_not_an_expansion():
    assert invoke("regex_replace", value="a" * 600_000, pattern="a+", replacement=r"\n") == {
        "result": "\n"
    }


def test_large_named_capture_replacement_is_bounded():
    with pytest.raises(ValueError, match="regex_output_limit"):
        invoke("regex_replace", value="a" * 600_000, pattern="(?P<x>a+)", replacement=r"\g<x>\g<x>")


def test_zero_width_replacement_preserves_adjacent_nonempty_matches():
    assert invoke("regex_replace_all", value="ab", pattern="x*", replacement="-") == {
        "result": "-a-b-"
    }


@pytest.mark.parametrize("pattern", ["(?u)a", "(?au)a"])
def test_incompatible_flags_are_normalized(pattern):
    with pytest.raises(ValueError, match="regex_invalid_pattern"):
        invoke("regex_find", value="a", pattern=pattern, flags="a")


@pytest.mark.parametrize(
    "pattern",
    [
        "a{1000000}",
        "(?:ab){1000000}",
        "(?:a{1000}){1000}",
        "(?x)a{ 1000000 }",
        "(?x)a{1000000 #comment\n}",
        "(?x)#[\na{1000000}",
    ],
)
def test_oversized_repeat_compilation_is_rejected_before_engine_allocation(pattern):
    with pytest.raises(ValueError, match="regex_compile_limit"):
        invoke("regex_find", value="a", pattern=pattern)


@pytest.mark.parametrize("name", ["regex_find", "regex_find_all", "regex_replace_all"])
def test_inverted_engine_spans_rejected(name):
    arguments = {"value": "ab", "pattern": r"(?=ab\K)"}
    if "replace" in name:
        arguments["replacement"] = "X"
    with pytest.raises(ValueError, match="regex_unsupported_span"):
        invoke(name, **arguments)


@pytest.mark.parametrize(
    "name", ["regex_find", "regex_find_all", "regex_replace", "regex_replace_all"]
)
def test_overlong_supported_flags_are_rejected_before_regex_matching(name):
    arguments = {"value": "a", "pattern": "a", "flags": "i" * 1000}
    if "replace" in name:
        arguments["replacement"] = "x"
    with pytest.raises(ValueError, match="regex_flags_limit"):
        invoke(name, **arguments)


def test_duplicate_flags_are_rejected():
    with pytest.raises(ValueError, match="regex_invalid_flags"):
        invoke("regex_find", value="a", pattern="a", flags="ii")


def test_all_supported_flags_remain_available_together():
    assert invoke("regex_find", value="A\nB", pattern="^a.b$", flags="imsxa") == {"result": "A\nB"}
