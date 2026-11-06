"""Interface ``report_output`` selection and the report boundary (ADR 0059 decisions 4-5)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from core.application.registry.workflow_contract import WorkflowInterface
from core.application.registry.workflow_contract_service import WorkflowInterfaceService

SNAPSHOT = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "workflow_reports"
        / "sales_report_snapshot.json"
    ).read_text(encoding="utf-8")
)


def _interface(**callable_overrides: Any) -> WorkflowInterface:
    callable_obj: dict[str, Any] = {
        "enabled": True,
        "trigger_channels": ["api", "mcp"],
        "expected_outputs": [
            {"name": "sales_report", "type": "object", "required": True},
            {"name": "note", "type": "string", "required": False},
        ],
        "report_output": "sales_report",
        **callable_overrides,
    }
    return WorkflowInterface.from_settings({"callable": callable_obj}, steps=[])


def _codes(interface: WorkflowInterface) -> set[str]:
    return {row["code"] for row in interface.definition_issues()}


def test_a_valid_selection_has_no_issues_and_is_dumped() -> None:
    interface = _interface()
    assert not {code for code in _codes(interface) if "report_output" in code}
    assert interface.dump()["report_output"] == "sales_report"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"report_output": "missing"}, "workflow.interface.report_output_unknown"),
        (
            {"expected_outputs": [{"name": "sales_report", "type": "string", "required": True}]},
            "workflow.interface.report_output_not_object",
        ),
        (
            {"expected_outputs": [{"name": "sales_report", "type": "object", "required": False}]},
            "workflow.interface.report_output_not_required",
        ),
        ({"enabled": False}, "workflow.interface.report_output_disabled"),
    ],
)
def test_selection_problems_are_reported(overrides: dict[str, Any], code: str) -> None:
    issues = _interface(**overrides).definition_issues()
    assert any(
        row["code"] == code and row["path"] == "workflow.callable.report_output" for row in issues
    )


def test_public_output_schema_is_the_exact_snapshot_schema() -> None:
    schema = WorkflowInterfaceService().output_json_schema(_interface())
    report_schema = schema["properties"]["sales_report"]
    assert report_schema["$id"] == "urn:flow-steward:workflow-report:snapshot:1"
    assert schema["properties"]["note"] == {"type": "string"}


def _validate(state: dict[str, Any]) -> Any:
    return WorkflowInterfaceService().validate_outputs(_interface(), state)


def test_a_valid_snapshot_in_state_passes_unchanged() -> None:
    result = _validate({"outputs": {"sales_report": copy.deepcopy(SNAPSHOT)}})
    assert result.ok, result.errors
    assert result.outputs["sales_report"] == SNAPSHOT


def test_a_json_string_is_not_coerced_into_a_report() -> None:
    result = _validate({"outputs": {"sales_report": json.dumps(SNAPSHOT)}})
    assert not result.ok
    error = result.errors[0]
    assert error["code"] == "workflow.output.report_invalid"
    assert error["details"]["errors"][0]["code"] == "report.snapshot_not_object"
    assert error["details"]["errors"][0]["path"] == "outputs.sales_report"


def test_an_invalid_snapshot_reports_paths_not_values() -> None:
    broken = copy.deepcopy(SNAPSHOT)
    broken["datasets"][0]["rows"][0][1] = "CONFIDENTIAL-1200"
    result = _validate({"outputs": {"sales_report": broken}})
    assert not result.ok
    nested = result.errors[0]["details"]["errors"]
    assert any(row["path"] == "outputs.sales_report.datasets[0].rows[0][1]" for row in nested)
    assert "CONFIDENTIAL-1200" not in json.dumps(result.errors)


def test_a_message_patch_never_becomes_the_report() -> None:
    message = {
        "status": "pass",
        "artifacts": {"_state_patch": {"outputs": {"sales_report": copy.deepcopy(SNAPSHOT)}}},
    }
    result = _validate({"outputs": {}, "agent_messages": [message]})
    assert not result.ok
    assert result.errors[0]["code"] == "workflow.output.required"
