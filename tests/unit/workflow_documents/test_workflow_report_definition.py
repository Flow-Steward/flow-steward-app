"""Authoring-time ReportDefinition validation (schema v1 sections 3 and 7.1)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from core.application.workflow_documents import validate_report_definition


def _codes(issues: list[dict[str, str]]) -> list[tuple[str, str]]:
    return [(item["code"], item["path"]) for item in issues]


def test_fixture_definition_is_valid(definition: dict, declared: list) -> None:
    assert validate_report_definition(definition, declared) == []


def _metric(definition: dict) -> dict:
    return definition["components"][1]


CASES: list[tuple[str, Callable[[dict], Any], tuple[str, str]]] = [
    (
        "unknown component type",
        lambda d: d["components"][0].__setitem__("type", "gauge"),
        ("report.component_type_unknown", "components[0].type"),
    ),
    (
        "currency without a code",
        lambda d: _metric(d).__setitem__("format", {"type": "currency"}),
        ("report.property_required", "components[1].format"),
    ),
    (
        "malformed currency code",
        lambda d: _metric(d)["format"].__setitem__("currency", "EURO"),
        ("report.value_invalid", "components[1].format.currency"),
    ),
    (
        "unknown ISO 4217 code",
        lambda d: _metric(d)["format"].__setitem__("currency", "ABC"),
        ("report.currency_invalid", "components[1].format.currency"),
    ),
    (
        "undeclared dataset",
        lambda d: d["components"][5].__setitem__("dataset", "orders"),
        ("report.dataset_unknown", "components[5].dataset"),
    ),
    (
        "percent category",
        lambda d: d["components"][6]["category"].__setitem__("format", {"type": "percent"}),
        ("report.format_not_allowed", "components[6].category.format"),
    ),
    (
        "unknown Template input",
        lambda d: _metric(d)["value"].__setitem__("input", "totals"),
        ("report.binding_input_unknown", "components[1].value.input"),
    ),
    (
        "wildcard path",
        lambda d: _metric(d)["value"].__setitem__("path", "revenue[*]"),
        ("report.binding_path_invalid", "components[1].value.path"),
    ),
    (
        "state selector in a binding",
        lambda d: _metric(d)["value"].__setitem__("path", "state.outputs.kpis"),
        ("report.binding_path_invalid", "components[1].value.path"),
    ),
    (
        "style on a metric",
        lambda d: _metric(d).__setitem__("style", "heading"),
        ("report.property_unknown", "components[1].style"),
    ),
    (
        "raw html",
        lambda d: d["components"][0].__setitem__("html", "<b>x</b>"),
        ("report.property_unknown", "components[0].html"),
    ),
    (
        "schema version 2",
        lambda d: d.__setitem__("schema_version", 2),
        ("report.schema_version_unsupported", "schema_version"),
    ),
    (
        "unknown locale",
        lambda d: d.__setitem__("locale", "xx-XX"),
        ("report.locale_invalid", "locale"),
    ),
    (
        "unknown time zone",
        lambda d: d.__setitem__("time_zone", "Mars/Olympus"),
        ("report.time_zone_invalid", "time_zone"),
    ),
    (
        "fraction digits out of order",
        lambda d: _metric(d)["format"].update(
            {"minimum_fraction_digits": 3, "maximum_fraction_digits": 1}
        ),
        ("report.fraction_digits_invalid", "components[1].format"),
    ),
    (
        "unused dataset",
        lambda d: d["datasets"].append({"id": "spare", "source": {"input": "regions"}}),
        ("report.dataset_unused", "datasets[2].id"),
    ),
    (
        "default that does not fit the format",
        lambda d: _metric(d)["value"].__setitem__("default", "zero"),
        ("report.value_type_mismatch", "components[1].value.default"),
    ),
]


@pytest.mark.parametrize(
    ("change", "expected"), [(c[1], c[2]) for c in CASES], ids=[c[0] for c in CASES]
)
def test_invalid_definition_names_code_and_field(
    definition: dict, declared: list, change: Callable[[dict], Any], expected: tuple[str, str]
) -> None:
    change(definition)
    assert expected in _codes(validate_report_definition(definition, declared))


def test_duplicate_component_id_anywhere_in_the_tree(definition: dict, declared: list) -> None:
    section = definition["components"][7]
    section["children"][0]["id"] = "region_table"
    codes = [item["code"] for item in validate_report_definition(definition, declared)]
    assert "report.id_duplicate" in codes


def test_depth_beyond_four_is_rejected(definition: dict, declared: list) -> None:
    leaf: dict = {"id": "deep", "type": "text", "style": "paragraph", "segments": [{"text": "x"}]}
    for level in range(4):
        leaf = {"id": f"s{level}", "type": "section", "title": "S", "children": [leaf]}
    definition["components"].append(leaf)
    codes = [item["code"] for item in validate_report_definition(definition, declared)]
    assert "report.depth_exceeded" in codes


@pytest.mark.parametrize(
    ("input_type", "binding", "dataset", "possible"),
    [
        ("text", {}, False, True),
        ("text", {"path": "a"}, False, False),
        ("object", {}, False, False),
        ("object", {"path": "a"}, False, True),
        ("array", {"path": "0.total"}, False, True),
        ("json", {}, False, True),
        ("object", {}, True, False),
        ("object", {"path": "rows"}, True, True),
        ("array", {}, True, True),
        ("number", {}, True, False),
        ("json", {"path": "rows"}, True, True),
    ],
)
def test_binding_against_declared_input_type(
    input_type: str, binding: dict, dataset: bool, possible: bool
) -> None:
    declared = [{"name": "src", "type": input_type, "required": True}]
    source = {"input": "src", **binding}
    if dataset:
        definition = {
            "schema_version": 1,
            "title": "T",
            "locale": "en-US",
            "time_zone": "UTC",
            "datasets": [{"id": "rows", "source": source}],
            "components": [
                {
                    "id": "tbl",
                    "type": "table",
                    "caption": "C",
                    "dataset": "rows",
                    "columns": [
                        {"id": "a", "field": "a", "label": "A", "format": {"type": "text"}}
                    ],
                    "empty_text": "none",
                }
            ],
        }
    else:
        definition = {
            "schema_version": 1,
            "title": "T",
            "locale": "en-US",
            "time_zone": "UTC",
            "components": [
                {
                    "id": "t",
                    "type": "text",
                    "style": "paragraph",
                    "segments": [{"value": source, "format": {"type": "text"}}],
                }
            ],
        }
    codes = [item["code"] for item in validate_report_definition(definition, declared)]
    assert ("report.binding_type_impossible" in codes) is not possible


def test_oversized_definition_is_rejected_before_the_schema(
    definition: dict, declared: list
) -> None:
    definition["components"][0]["segments"] = [{"text": "й" * 9_000} for _ in range(5)]
    issues = validate_report_definition(definition, declared)
    assert [item["code"] for item in issues] == ["report.definition_too_large"]


def test_messages_do_not_echo_values(definition: dict, declared: list) -> None:
    _metric(definition)["value"]["default"] = "SECRET-LOOKING-DEFAULT"
    issues = validate_report_definition(definition, declared)
    assert issues
    assert all("SECRET-LOOKING-DEFAULT" not in item["message"] for item in issues)
