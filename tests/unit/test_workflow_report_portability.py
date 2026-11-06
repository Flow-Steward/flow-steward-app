"""A report Template step and the report output survive export and import (ADR 0059)."""

from __future__ import annotations

import copy

import yaml

from core.application.workflow_documents.report_examples import TEMPLATE_REPORT_EXAMPLE
from core.application.workflows.workflow_portability import WorkflowPortabilityService
from tests.unit.test_workflow_portability import _manual_workflow, _resolved_db

CALLABLE = {
    "enabled": True,
    "trigger_channels": ["api", "mcp"],
    "expected_inputs": [],
    "expected_outputs": [{"name": "sales_report", "type": "object", "required": True}],
    "report_output": "sales_report",
}


def _report_workflow(workflow_id: str) -> dict:
    workflow = _manual_workflow(workflow_id)
    workflow["steps"] = [
        {
            "step_id": "build_report",
            "step_kind": "template",
            "group_id": None,
            "for_each_id": None,
            "lane_id": None,
            "override": {"template": copy.deepcopy(TEMPLATE_REPORT_EXAMPLE)},
        },
        {"step_id": "done", "step_kind": "stop_success"},
    ]
    workflow["settings"] = {
        "callable": copy.deepcopy(CALLABLE),
        "execution_graph": {
            "schema_version": 1,
            "entry": {"kind": "step", "id": "build_report"},
            "edges": [
                {
                    "source": {"kind": "step", "id": "build_report"},
                    "outcome": {"name": "success"},
                    "target": {"kind": "step", "id": "done"},
                    "rework": False,
                }
            ],
        },
    }
    return workflow


def test_the_report_definition_and_selection_round_trip_unchanged() -> None:
    exported = WorkflowPortabilityService(
        _resolved_db(workflows={"weekly": _report_workflow("weekly")})
    ).export_yaml("weekly")
    bundle_workflow = yaml.safe_load(exported)["workflows"][0]
    exported_step = next(
        step for step in bundle_workflow["steps"] if step["step_id"] == "build_report"
    )
    assert exported_step["override"]["template"] == TEMPLATE_REPORT_EXAMPLE

    import_db = _resolved_db()
    WorkflowPortabilityService(import_db).apply_import(exported, target_workflow_id="weekly_copy")
    imported = import_db.workflows["weekly_copy"]
    step = next(row for row in imported["steps"] if row["step_id"] == "build_report")
    assert step["override"]["template"] == TEMPLATE_REPORT_EXAMPLE
    assert imported["settings"]["callable"]["report_output"] == "sales_report"
    assert imported["settings"]["callable"]["expected_outputs"][0]["name"] == "sales_report"
