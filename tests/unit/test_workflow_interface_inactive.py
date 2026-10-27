"""Inactive declarations are visible without changing the runtime boundary."""

from __future__ import annotations

from types import ModuleType
from unittest.mock import patch

import pytest

from core.application.registry.contract_validation import validate_workflow_readiness
from core.application.registry.workflow_contract import WorkflowInterface
from core.application.registry.workflow_contract_service import WorkflowInterfaceService
from core.application.workflows.canvas_graph_service import build_workflow_canvas_graph
from tests._workflow_definitions import linear_workflow


class _Store:
    def __init__(self, workflow: dict) -> None:
        self.workflow = workflow

    def get_workflow(self, workflow_id: str) -> dict | None:
        return self.workflow if workflow_id == self.workflow["workflow_id"] else None


def _workflow(**callable_settings) -> dict:
    return linear_workflow(
        {
            "step_id": "render",
            "step_kind": "template",
            "override": {
                "template": {"text": "Ready", "result_target": "state.outputs.summary"},
            },
        },
        settings={"callable": {"enabled": False, "trigger_channels": ["ui"], **callable_settings}},
    )


@pytest.mark.parametrize(
    "declarations",
    [
        {"expected_inputs": [{"name": "request", "type": "string"}]},
        {"expected_outputs": [{"name": "summary", "type": "string", "required": True}]},
        {"input_mode": "json_schema", "input_schema": {"type": "object"}},
    ],
    ids=["inputs", "required-output", "schema"],
)
def test_inactive_declarations_warn_without_refusing_an_otherwise_valid_workflow(declarations):
    workflow = _workflow(**declarations)
    validation = validate_workflow_readiness(_Store(workflow), workflow["workflow_id"])

    assert validation["valid"] is True, validation
    assert validation["errors"] == []
    assert len(validation["warnings"]) == 1
    assert validation["warnings"][0] == {
        "code": "workflow.callable.inactive",
        "message": (
            "Workflow interface is off. Any declared outputs are not returned and "
            "required-output checks are inactive until it is enabled. "
            "Input forms may still validate declared inputs."
        ),
        "path": "workflow.callable.enabled",
        "severity": "warning",
    }


@pytest.mark.parametrize(
    "callable_settings",
    [
        {},
        {"expected_inputs": [], "expected_outputs": []},
        {
            "expected_inputs": [{"name": " ", "type": "string"}],
            "expected_outputs": [{"name": "", "type": "string"}],
        },
        {"input_mode": "json_schema", "input_schema": {}},
        {"enabled": True, "allow_empty_inputs": True},
        {
            "enabled": True,
            "allow_empty_inputs": True,
            "expected_outputs": [
                {"name": "summary", "type": "string", "required": True},
            ],
        },
    ],
)
def test_an_empty_or_enabled_contract_has_no_inactivity_warning(callable_settings):
    workflow = _workflow(**callable_settings)
    validation = validate_workflow_readiness(_Store(workflow), workflow["workflow_id"])
    assert validation["valid"] is True, validation
    assert validation["errors"] == []
    assert validation["warnings"] == []


def test_returned_files_also_warn_through_the_real_canvas_diagnostics_projection():
    workflow = linear_workflow(
        {
            "step_id": "render",
            "step_kind": "file_from_base64",
            "override": {
                "file_from_base64": {"source": "literal:SGk=", "result_target": "state.invoice"},
                "returned_files": [{"result": "file", "key": "invoice"}],
            },
        },
        settings={"callable": {"enabled": False}},
    )
    store = _Store(workflow)
    validation = validate_workflow_readiness(store, workflow["workflow_id"])
    assert validation["valid"] is True, validation
    assert validation["errors"] == []
    graph = build_workflow_canvas_graph(store, workflow["workflow_id"])
    warnings = graph["validation_issues"]
    assert len(warnings) == 1
    assert warnings[0]["code"] == "workflow.callable.inactive"
    assert warnings[0]["scope"] == "workflow"
    assert warnings[0]["severity"] == "warning"
    assert warnings[0]["field_path"] == "workflow.callable.enabled"
    assert (
        warnings[0]["suggested_action"]
        == "Open workflow settings and update the workflow contract."
    )
    assert graph["output_contract"]["returned_files"][0]["name"] == "invoice"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("produced", [False, True])
def test_required_summary_runtime_boundary_preserves_explicit_activation(enabled, produced):
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": enabled,
                "expected_outputs": [
                    {"name": "summary", "type": "string", "required": True},
                ],
            }
        }
    )
    state = {"outputs": {"summary": "Interface ready ✓"}} if produced else {}
    result = WorkflowInterfaceService().validate_outputs(interface, state)
    assert result.ok is (not enabled or produced)
    assert result.outputs == ({"summary": "Interface ready ✓"} if enabled and produced else {})
    assert result.artifacts == {}
    assert result.errors == (
        []
        if not enabled or produced
        else [
            {
                "code": "workflow.output.required",
                "message": "Workflow output 'summary' is required",
                "path": "outputs.summary",
                "severity": "error",
            }
        ]
    )


@pytest.mark.parametrize(
    "enabled,produced,status",
    [
        (False, False, "completed"),
        (False, True, "completed"),
        (True, False, "failed"),
        (True, True, "completed"),
    ],
)
def test_pinned_completion_uses_the_real_required_summary_boundary(enabled, produced, status):
    from core.application.orchestration.run_definition import clear_run_definition_cache
    from core.application.orchestration.validation_node import orchestrator_validation_node
    from tests._step_run_state import pinned_definition, prime

    definition = pinned_definition({"step_id": "done", "step_kind": "stop_success"})
    definition["settings"] = {
        "callable": {
            "enabled": enabled,
            "expected_outputs": [
                {"name": "summary", "type": "string", "required": True},
            ],
        }
    }
    common = ModuleType("core.application.orchestration.agents.common")
    common.emit_event = lambda *args, **kwargs: None
    clear_run_definition_cache()
    try:
        state = {
            **prime(definition),
            "workflow_id": "test_workflow",
            "status": "in_progress",
            "task_id": "summary-run",
            "agent_messages": [],
            "platform_events": [],
            "workflow_terminal": {
                "kind": "stop_success",
                "status": "pass",
                "step_id": "done",
                "step_execution_id": "done-pass",
            },
            "outputs": {"summary": "Interface ready ✓"} if produced else {},
        }
        with patch.dict("sys.modules", {"core.application.orchestration.agents.common": common}):
            result = orchestrator_validation_node(state)
    finally:
        clear_run_definition_cache()
    assert result["status"] == status
    assert result["workflow_outputs"]["outputs"] == (
        {"summary": "Interface ready ✓"} if enabled and produced else {}
    )
    if status == "failed":
        assert result["last_failure_class"] == "workflow_output_contract"
        assert result["workflow_outputs"]["errors"] == [
            {
                "code": "workflow.output.required",
                "message": "Workflow output 'summary' is required",
                "path": "outputs.summary",
                "severity": "error",
            }
        ]
