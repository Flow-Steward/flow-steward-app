"""The bounded preview is the only report form allowed outside declared outputs.

ADR 0059 decision 11: events, proofs, step previews and model-visible
projections carry kind, version, title, summary text and counts — never
datasets, rows or component values.
"""

from __future__ import annotations

import copy
import json

from core.application.workflow_documents import (
    compile_report,
    replace_report_output,
    report_preview,
)

MARKER = "ROW-MARKER-7f3a"


def _large_report(definition: dict, declared: list, values: dict) -> dict:
    values["regions"] = [
        {
            "region": f"{MARKER}-{index}",
            "orders": index,
            "revenue": index * 1.5,
            "margin": 0.1,
            "manager": "m",
        }
        for index in range(1000)
    ]
    return compile_report(definition, declared, values)


def test_preview_has_counts_and_no_rows(definition: dict, declared: list, values: dict) -> None:
    report = _large_report(definition, declared, values)
    preview = report_preview(report)
    assert preview == {
        "kind": "fs.report",
        "schema_version": 1,
        "title": "Weekly sales",
        "summary_text": report["summary_text"],
        "component_count": 10,
        "datasets": [{"id": "regions", "row_count": 1000}, {"id": "returns", "row_count": 0}],
    }
    assert MARKER not in json.dumps(preview)
    assert len(json.dumps(preview)) < 5_000


def test_preview_of_a_broken_value_leaks_nothing() -> None:
    broken = {"kind": "fs.report", "title": MARKER * 100, "datasets": "x", "rows": [MARKER]}
    preview = report_preview(broken)
    assert MARKER not in json.dumps(preview)
    assert report_preview([MARKER]) == {"report_omitted": True, "value_type": "array"}
    assert report_preview(MARKER) == {"report_omitted": True, "value_type": "string"}


def test_replace_report_output_keeps_the_canonical_outputs(
    definition: dict, declared: list, values: dict
) -> None:
    report = _large_report(definition, declared, values)
    outputs = {"sales_report": report, "note": "kept"}
    original = copy.deepcopy(outputs)
    projected = replace_report_output(outputs, "sales_report")
    assert outputs == original
    assert projected["note"] == "kept"
    assert projected["sales_report"]["datasets"][0]["row_count"] == 1000
    assert MARKER not in json.dumps(projected)


def test_replace_without_a_selection_is_a_no_op() -> None:
    outputs = {"a": 1}
    assert replace_report_output(outputs, None) is outputs
    assert replace_report_output(outputs, "missing") is outputs
    assert replace_report_output("text", "a") == "text"
