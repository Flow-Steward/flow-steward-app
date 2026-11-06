"""The Template step in report mode (ADR 0059, schema v1 section 1)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from core.application.orchestration.template_step_runtime import (
    execute_template_step,
    validate_template_config,
)
from core.application.registry.builtin_step_kind_profiles import builtin_step_kind_profiles
from core.application.workflow_documents.report_examples import TEMPLATE_REPORT_EXAMPLE

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "workflow_reports"
MARKER = "ROW-MARKER-91c2"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _report_config() -> dict[str, Any]:
    inputs = _fixture("sales_report_inputs.json")
    return {
        "format": "report",
        "inputs": inputs["declared_inputs"],
        "report": _fixture("sales_report_definition.json"),
        "result_target": "state.outputs.sales_report",
    }


def _state() -> dict[str, Any]:
    return {"outputs": copy.deepcopy(_fixture("sales_report_inputs.json")["values"])}


def _codes(config: dict[str, Any]) -> list[tuple[str, str]]:
    issues = validate_template_config(config, path_prefix="steps[0].override.template")
    return [(item["code"], item["path"]) for item in issues]


def test_the_report_configuration_and_the_published_example_are_valid() -> None:
    assert _codes(_report_config()) == []
    assert _codes(copy.deepcopy(TEMPLATE_REPORT_EXAMPLE)) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda c: c.pop("report"),
            ("workflow.step_template_report_missing", "steps[0].override.template.report"),
        ),
        (
            lambda c: c.__setitem__("text", ""),
            ("workflow.step_template_text_not_allowed", "steps[0].override.template.text"),
        ),
        (
            lambda c: c.update({"format": "markdown", "text": "x"}),
            ("workflow.step_template_report_not_allowed", "steps[0].override.template.report"),
        ),
        (
            lambda c: c["report"]["components"][1]["value"].__setitem__("input", "totals"),
            (
                "report.binding_input_unknown",
                "steps[0].override.template.report.components[1].value.input",
            ),
        ),
    ],
)
def test_report_configuration_issues_carry_the_authoring_path(change, expected) -> None:
    config = _report_config()
    change(config)
    assert expected in _codes(config)


def test_a_text_template_keeps_working_unchanged() -> None:
    config = {
        "text": "Hello {{ name }}",
        "inputs": [{"name": "name", "type": "text", "required": True, "source": "state.n"}],
        "result_target": "state.outputs.greeting",
    }
    assert _codes(config) == []
    assert execute_template_step(config, {"n": "Ada"})["text"] == "Hello Ada"


def test_the_step_compiles_the_frozen_snapshot() -> None:
    result = execute_template_step(_report_config(), _state())
    assert result["ok"] is True
    assert result["report"] == _fixture("sales_report_snapshot.json")


def test_a_bad_value_fails_with_report_errors_and_no_values() -> None:
    state = _state()
    state["outputs"]["kpis"]["revenue"] = f"{MARKER}-48210.50"
    result = execute_template_step(_report_config(), state)
    assert result["ok"] is False
    assert result["failure_reason"] == "report_invalid"
    assert any(
        error["code"] == "report.value_type_mismatch"
        and error["path"] == "report.components[1].value"
        for error in result["errors"]
    )
    assert MARKER not in json.dumps(result["errors"])


def _run_node(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    from core.application.orchestration.graph.nodes import execute_step_node
    from tests._step_run_state import step_run_state

    return execute_step_node(
        step_run_state(
            {
                "step_id": "build_report",
                "display_name": "Build report",
                "step_kind": "template",
                "override": {"template": config},
            },
            **state,
        )
    )


def test_the_node_writes_the_snapshot_object_and_a_bounded_message() -> None:
    state = _state()
    state["outputs"]["regions"] = [
        {"region": f"{MARKER}-{index}", "orders": 1, "revenue": 1.0, "margin": 0.1, "manager": "m"}
        for index in range(1000)
    ]
    update = _run_node(_report_config(), state)

    report = update["outputs"]["sales_report"]
    assert isinstance(report, dict)
    assert report["kind"] == "fs.report"
    assert len(report["datasets"][0]["rows"]) == 1000
    message = update["agent_messages"][-1]
    assert message["status"] == "pass"
    artifacts = message["artifacts"]
    assert "_state_patch" not in artifacts
    assert artifacts["template"]["report"]["datasets"][0]["row_count"] == 1000
    assert artifacts["output_payload"]["format"] == "report"
    assert MARKER not in json.dumps(artifacts["template"])
    assert MARKER not in json.dumps(artifacts["output_payload"])
    assert MARKER not in message["summary"]


def test_a_failed_report_writes_nothing() -> None:
    state = _state()
    del state["outputs"]["kpis"]["refund_rate"]
    update = _run_node(_report_config(), state)
    assert "sales_report" not in update.get("outputs", {})
    message = update["agent_messages"][-1]
    assert message["status"] == "fail"
    assert message["artifacts"]["template"]["failure_reason"] == "report_invalid"


def test_the_step_profile_publishes_the_report_vocabulary() -> None:
    profile = next(p for p in builtin_step_kind_profiles() if p["step_kind_key"] == "template")
    schema = profile["config_schema_jsonb"]["properties"]["template"]
    assert "report" in schema["properties"]["format"]["enum"]
    assert schema["properties"]["report"]["$id"] == "urn:flow-steward:workflow-report:definition:1"
    assert any(example.get("format") == "report" for example in schema["examples"])
