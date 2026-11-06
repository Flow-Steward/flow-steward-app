"""Plain number rendering is exact over every finite double (schema v1 section 4.1)."""

from __future__ import annotations

import sys

import pytest

from core.application.orchestration.template_step_runtime import execute_template_step
from core.application.workflow_documents import compile_report
from core.application.workflow_documents.report_formats import plain_number

LARGEST = sys.float_info.max
SMALLEST = 5e-324


@pytest.mark.parametrize(
    ("value", "fmt", "expected"),
    [
        (1e30, {"type": "number", "maximum_fraction_digits": 2}, "1" + "0" * 30),
        (2.5, {"type": "number", "maximum_fraction_digits": 0}, "2"),
        (3.5, {"type": "number", "maximum_fraction_digits": 0}, "4"),
        (0.125, {"type": "number", "maximum_fraction_digits": 2}, "0.12"),
        (0.125, {"type": "percent", "maximum_fraction_digits": 1}, "12.5"),
        (0.07, {"type": "percent"}, "7"),
        (21500.0, {"type": "number"}, "21500"),
        (7, {"type": "number", "minimum_fraction_digits": 2}, "7.00"),
        (-0.0, {"type": "number"}, "0"),
        (-0.001, {"type": "number", "maximum_fraction_digits": 2}, "0"),
        (SMALLEST, {"type": "number", "maximum_fraction_digits": 6}, "0"),
        (2**53 - 1, {"type": "currency", "currency": "EUR"}, str(2**53 - 1)),
    ],
)
def test_plain_numbers(value: float, fmt: dict, expected: str) -> None:
    assert plain_number(value, fmt) == expected


@pytest.mark.parametrize(
    "fmt",
    [
        {"type": "number"},
        {"type": "number", "maximum_fraction_digits": 6},
        {"type": "percent", "maximum_fraction_digits": 6},
        {"type": "currency", "currency": "EUR", "maximum_fraction_digits": 2},
    ],
)
@pytest.mark.parametrize("value", [LARGEST, -LARGEST, SMALLEST, 1e-300])
def test_the_extremes_render_without_error_or_exponent(value: float, fmt: dict) -> None:
    text = plain_number(value, fmt)
    assert "E" not in text
    assert "e" not in text
    if abs(value) >= 1:
        assert len(text.lstrip("-").split(".")[0]) >= 309


def _metric_definition(fmt: dict) -> dict:
    return {
        "schema_version": 1,
        "title": "Extremes",
        "locale": "en-US",
        "time_zone": "UTC",
        "components": [
            {
                "id": "big",
                "type": "metric",
                "label": "Big",
                "value": {"input": "kpis", "path": "n"},
                "format": fmt,
            }
        ],
    }


DECLARED = [{"name": "kpis", "type": "object", "required": True}]


@pytest.mark.parametrize("value", [1e30, LARGEST, -LARGEST, SMALLEST])
def test_the_compiler_summarises_extreme_values(value: float) -> None:
    fmt = {"type": "percent", "maximum_fraction_digits": 2}
    snapshot = compile_report(_metric_definition(fmt), DECLARED, {"kpis": {"n": value}})
    assert snapshot["components"][0]["value"] == value
    assert snapshot["summary_text"].startswith("Extremes\nBig: ")


def test_the_template_step_builds_a_report_with_a_huge_value() -> None:
    config = {
        "format": "report",
        "inputs": [{"name": "kpis", "type": "object", "required": True, "source": "state.kpis"}],
        "report": _metric_definition({"type": "number", "maximum_fraction_digits": 2}),
        "result_target": "state.outputs.report",
    }
    result = execute_template_step(config, {"kpis": {"n": 1e30}})
    assert result["ok"] is True, result.get("errors")
    assert "1" + "0" * 30 in result["report"]["summary_text"]
