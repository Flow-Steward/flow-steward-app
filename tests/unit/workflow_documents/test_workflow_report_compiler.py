"""The pure report compiler (schema v1 sections 2.3, 4 and 10)."""

from __future__ import annotations

import json
import math

import pytest

from core.application.workflow_documents import ReportError, compile_report


def _issues(definition: dict, declared: list, values: dict) -> list[tuple[str, str]]:
    with pytest.raises(ReportError) as caught:
        compile_report(definition, declared, values)
    return [(item["code"], item["path"]) for item in caught.value.issues]


def _bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def test_fixture_compiles_to_the_frozen_snapshot(
    definition: dict, declared: list, values: dict, snapshot: dict
) -> None:
    compiled = compile_report(definition, declared, values)
    assert compiled == snapshot
    assert _bytes(compiled) == _bytes(compile_report(definition, declared, values))


def test_compiler_leaves_its_arguments_untouched(
    definition: dict, declared: list, values: dict
) -> None:
    before = (_bytes(definition), _bytes(values))
    compile_report(definition, declared, values)
    assert (_bytes(definition), _bytes(values)) == before


def test_zero_false_and_null_are_values(definition: dict, declared: list, values: dict) -> None:
    compiled = compile_report(definition, declared, values)
    metrics = {c["id"]: c["value"] for c in compiled["components"] if c["type"] == "metric"}
    assert metrics["orders"] == 0
    assert metrics["refund_rate"] is None
    regions = compiled["datasets"][0]
    assert regions["fields"] == ["region", "revenue", "margin"]
    assert ["APAC", 0, None] in regions["rows"]


def test_unread_source_columns_are_never_stored(
    definition: dict, declared: list, values: dict
) -> None:
    compiled = compile_report(definition, declared, values)
    encoded = _bytes(compiled).decode("utf-8")
    for row in values["regions"]:
        assert str(row["manager"]) not in encoded


def test_generated_summary_when_none_is_authored(
    definition: dict, declared: list, values: dict
) -> None:
    definition.pop("summary")
    compiled = compile_report(definition, declared, values)
    assert compiled["summary_text"] == "\n".join(
        [
            "Weekly sales",
            "Revenue: 48210.5 EUR (vs previous week: 12.3%)",
            "Orders: 0",
            "Refund rate: —",
            "Revenue by region: 3 rows",
            "Revenue by region: 3 points",
            "Returns: 0 rows",
        ]
    )


def test_generated_summary_is_bounded_and_marked(declared: list) -> None:
    metrics = [
        {
            "id": f"m{index}",
            "type": "metric",
            "label": f"Metric number {index:03d} " + "x" * 40,
            "value": {"input": "kpis", "path": "n"},
            "format": {"type": "number"},
        }
        for index in range(100)
    ]
    definition = {
        "schema_version": 1,
        "title": "Many metrics",
        "locale": "en-US",
        "time_zone": "UTC",
        "components": metrics,
    }
    compiled = compile_report(definition, declared, {"kpis": {"n": 1}})
    text = compiled["summary_text"]
    assert len(text) <= 4000
    assert text.splitlines()[-1].endswith("more items not shown]")


def test_missing_value_without_default(definition: dict, declared: list, values: dict) -> None:
    del values["kpis"]["refund_rate"]
    assert ("report.binding_missing", "components[3].value") in _issues(
        definition, declared, values
    )


def test_number_held_as_string_is_a_type_error(
    definition: dict, declared: list, values: dict
) -> None:
    values["kpis"]["revenue"] = "48210.50"
    assert ("report.value_type_mismatch", "components[1].value") in _issues(
        definition, declared, values
    )


def test_object_where_a_scalar_is_expected(definition: dict, declared: list, values: dict) -> None:
    values["kpis"]["revenue"] = {"amount": 1}
    assert ("report.binding_not_scalar", "components[1].value") in _issues(
        definition, declared, values
    )


@pytest.mark.parametrize(
    ("number", "code"),
    [(math.nan, "report.number_not_finite"), (2**53, "report.number_out_of_range")],
)
def test_numbers_a_browser_cannot_hold(
    definition: dict, declared: list, values: dict, number: float, code: str
) -> None:
    values["kpis"]["revenue"] = number
    assert (code, "components[1].value") in _issues(definition, declared, values)


def test_dataset_shape_errors(definition: dict, declared: list, values: dict) -> None:
    values["regions"][0] = ["EMEA"]
    values["regions"][1].pop("revenue")
    values["regions"][2]["margin"] = [0.1]
    issues = _issues(definition, declared, values)
    assert ("report.dataset_row_not_object", "datasets[0].rows[0]") in issues
    assert ("report.dataset_field_missing", "datasets[0].rows[1].revenue") in issues
    assert ("report.dataset_cell_not_scalar", "datasets[0].rows[2].margin") in issues


def test_dataset_that_is_not_an_array(definition: dict, declared: list, values: dict) -> None:
    declared[1]["type"] = "json"
    values["regions"] = {"rows": []}
    assert ("report.dataset_not_array", "datasets[0].source") in _issues(
        definition, declared, values
    )


def test_too_many_rows_fail_without_truncation(
    definition: dict, declared: list, values: dict
) -> None:
    row = dict(values["regions"][0])
    values["regions"] = [dict(row) for _ in range(1001)]
    assert ("report.rows_too_many", "datasets[0].rows") in _issues(definition, declared, values)


def test_empty_dataset_is_valid(definition: dict, declared: list, values: dict) -> None:
    values["regions"] = []
    compiled = compile_report(definition, declared, values)
    assert compiled["datasets"][0]["rows"] == []


def test_path_into_a_list_reads_an_index(declared: list) -> None:
    definition = {
        "schema_version": 1,
        "title": "Totals",
        "locale": "en-US",
        "time_zone": "UTC",
        "components": [
            {
                "id": "first",
                "type": "metric",
                "label": "First total",
                "value": {"input": "regions", "path": "0.total"},
                "format": {"type": "number"},
            }
        ],
    }
    compiled = compile_report(definition, declared, {"regions": [{"total": 7}]})
    assert compiled["components"][0]["value"] == 7


def test_authored_summary_longer_than_the_limit_fails(
    definition: dict, declared: list, values: dict
) -> None:
    definition["summary"] = [{"text": "x" * 5000}]
    assert ("report.summary_too_long", "summary") in _issues(definition, declared, values)


def test_errors_do_not_echo_report_data(definition: dict, declared: list, values: dict) -> None:
    values["kpis"]["revenue"] = "CONFIDENTIAL-48210"
    with pytest.raises(ReportError) as caught:
        compile_report(definition, declared, values)
    assert "CONFIDENTIAL-48210" not in json.dumps(caught.value.issues)
