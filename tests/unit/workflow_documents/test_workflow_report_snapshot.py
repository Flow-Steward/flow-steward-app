"""Snapshot validation at the compiler exit and the workflow boundary (section 5.1)."""

from __future__ import annotations

import json

import pytest

from core.application.workflow_documents import validate_report_snapshot


def _codes(value: object) -> list[tuple[str, str]]:
    return [(item["code"], item["path"]) for item in validate_report_snapshot(value)]


def test_fixture_snapshot_is_valid(snapshot: dict) -> None:
    assert validate_report_snapshot(snapshot) == []


def test_a_json_string_is_not_a_report(snapshot: dict) -> None:
    assert _codes(json.dumps(snapshot)) == [("report.snapshot_not_object", "")]


@pytest.mark.parametrize(
    ("key", "value", "code"),
    [
        ("kind", "fs.other", "report.kind_invalid"),
        ("schema_version", 2, "report.schema_version_unsupported"),
        ("schema_version", True, "report.schema_version_unsupported"),
    ],
)
def test_dispatch_on_kind_and_version(snapshot: dict, key: str, value: object, code: str) -> None:
    snapshot[key] = value
    assert [item[0] for item in _codes(snapshot)] == [code]


def test_row_with_the_wrong_number_of_cells(snapshot: dict) -> None:
    snapshot["datasets"][0]["rows"][0] = snapshot["datasets"][0]["rows"][0][:2]
    assert ("report.row_shape_invalid", "datasets[0].rows[0]") in _codes(snapshot)


def test_field_not_stored_and_field_not_read(snapshot: dict) -> None:
    snapshot["datasets"][0]["fields"][2] = "profit"
    codes = _codes(snapshot)
    assert ("report.dataset_field_unused", "datasets[0].fields[2]") in codes
    assert any(code == "report.dataset_field_unknown" for code, _path in codes)


def test_cell_that_breaks_its_format(snapshot: dict) -> None:
    snapshot["datasets"][0]["rows"][0][1] = "1200.50"
    assert ("report.value_type_mismatch", "datasets[0].rows[0][1]") in _codes(snapshot)


def test_bad_datetime_value(snapshot: dict) -> None:
    def find(node: object) -> dict | None:
        if isinstance(node, dict):
            if node.get("format", {}).get("type") == "datetime" and "value" in node:
                return node
            for item in node.values():
                hit = find(item)
                if hit is not None:
                    return hit
        if isinstance(node, list):
            for item in node:
                hit = find(item)
                if hit is not None:
                    return hit
        return None

    segment = find(snapshot["components"])
    assert segment is not None
    segment["value"] = "2026-02-30T10:00:00Z"
    assert any(code == "report.date_invalid" for code, _path in _codes(snapshot))


def test_oversized_snapshot(snapshot: dict) -> None:
    rows = snapshot["datasets"][0]["rows"]
    snapshot["datasets"][0]["rows"] = [["x" * 1200, 1, 0.1] for _ in range(1000)]
    assert rows
    assert [item[0] for item in _codes(snapshot)] == ["report.snapshot_too_large"]


def test_issue_list_is_capped(snapshot: dict) -> None:
    snapshot["datasets"][0]["rows"] = [["EMEA", "bad", "bad"] for _ in range(200)]
    codes = [code for code, _path in _codes(snapshot)]
    assert len(codes) == 101
    assert codes[-1] == "report.more_issues"
