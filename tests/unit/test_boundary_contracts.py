"""Malformed step boundary entry rows remain visible without mutating stored data."""

from copy import deepcopy

import pytest

from core.application.management.query_models_workflow_editor import (
    WorkflowEditorInputDefaultsView,
    WorkflowEditorStepContractPreviewView,
)
from core.application.registry import boundary_contracts
from core.application.registry.io_resolver import IOResolver
from tests._workflow_definitions import linear_workflow


@pytest.mark.parametrize(
    ("inputs", "path", "code"),
    [
        (
            [{}],
            "workflow.steps.normalize.boundary_contract.entry[0].name",
            "contract.name_required",
        ),
        (
            [{"name": "x", "type": "unsupported"}],
            "workflow.steps.normalize.boundary_contract.entry[0].type",
            "contract.invalid_type",
        ),
    ],
)
def test_raw_workflow_detail_projects_canonical_entry_errors_without_mutation(inputs, path, code):
    original = _DB(inputs).workflow
    before = deepcopy(original)
    result = boundary_contracts.project_workflow_boundary_contracts(original)
    step = result["steps"][0]
    assert step["boundary_contract"]["entry"] == []
    assert step["raw_expected_inputs"] == inputs
    assert step["expected_input_issues"][0]["path"] == path
    assert step["expected_input_issues"][0]["code"] == code
    assert original == before


def test_group_entry_rows_are_projected_with_group_paths():
    workflow = _DB([]).workflow
    workflow["groups"] = [{"group_id": "batch", "boundary_contract": {"entry": [{}]}}]
    result = boundary_contracts.project_workflow_boundary_contracts(workflow)
    group = result["groups"][0]
    assert group["boundary_contract"]["entry"] == []
    assert group["expected_input_issues"][0]["path"] == (
        "workflow.groups.batch.boundary_contract.entry[0].name"
    )
    issues = boundary_contracts.validate_workflow_boundary_contracts(workflow)
    assert [issue["path"] for issue in issues] == [
        "workflow.groups.batch.boundary_contract.entry[0].name"
    ]


def test_owner_without_entry_is_left_untouched():
    workflow = linear_workflow({"step_id": "normalize", "boundary_contract": {"exit": ["x"]}})
    result = boundary_contracts.project_workflow_boundary_contracts(workflow)
    assert result["steps"][0] == workflow["steps"][0]
    assert boundary_contracts.validate_workflow_boundary_contracts(workflow) == []


def test_exit_keys_are_trimmed_and_deduplicated_in_authored_order():
    step = {"boundary_contract": {"exit": [" report ", "summary", "report", ""]}}
    assert boundary_contracts.boundary_exit_keys(step) == ["report", "summary"]
    assert boundary_contracts.boundary_exit_keys({"boundary_contract": None}) == []


def test_mixed_projection_retains_complete_valid_contract_metadata():
    retained = {
        "name": "payload",
        "type": "object",
        "required": True,
        "source": "state.inputs.payload",
        "description": "Import payload",
        "contract_id": "inventory_payload",
        "source_id": "original_source",
    }
    workflow = _DB([None, retained]).workflow
    before = deepcopy(workflow)
    result = boundary_contracts.project_workflow_boundary_contracts(workflow)
    assert result["steps"][0]["boundary_contract"]["entry"] == [retained]
    assert workflow == before


@pytest.mark.parametrize("inputs", [[{}], [{"name": "x", "type": "unsupported"}]])
def test_detail_route_exposes_canonical_invalid_object_errors(monkeypatch, inputs):
    from core.entrypoints.admin import workflows_routes_read

    workflow = _DB(inputs).workflow
    before = deepcopy(workflow)
    monkeypatch.setattr(workflows_routes_read, "_request_scope", lambda request: None)
    monkeypatch.setattr(workflows_routes_read, "_get_db", lambda: None)
    monkeypatch.setattr(
        workflows_routes_read,
        "_resolve_active_workflow_scope_target",
        lambda *args: (workflow, None),
    )

    class Portability:
        def redact_protected_workflow(self, value):
            return value

    monkeypatch.setattr(
        workflows_routes_read, "_get_workflow_portability_service", lambda db: Portability()
    )
    result = workflows_routes_read.get_workflow("legacy_inputs", object())
    step = result["steps"][0]
    assert step["boundary_contract"]["entry"] == []
    assert step["raw_expected_inputs"] == inputs
    assert step["expected_input_issues"]
    assert workflow == before


class _DB:
    def __init__(self, inputs):
        self.workflow = linear_workflow(
            {"step_id": "normalize", "boundary_contract": {"entry": inputs}},
            workflow_id="legacy_inputs",
            enabled=False,
        )

    def get_workflow(self, workflow_id):
        return self.workflow if workflow_id == "legacy_inputs" else None


def test_step_input_defaults_preserve_contract_metadata_for_source_repair():
    db = _DB(
        [
            {
                "name": "payload",
                "type": "object",
                "source": "state.inputs.payload",
                "required": True,
                "description": "Import payload",
                "contract_id": "inventory_payload",
            }
        ]
    )
    before = deepcopy(db.workflow)

    payload = IOResolver(db).get_step_input_defaults("legacy_inputs", "normalize")
    result = WorkflowEditorInputDefaultsView.model_validate(payload).model_dump()

    assert payload["step_id"] == "normalize"
    assert payload["group_id"] is None
    assert len(result["current_expected_inputs"]) == 1
    row = result["current_expected_inputs"][0]
    assert {
        key: row.get(key)
        for key in ("name", "type", "source", "required", "description", "contract_id")
    } == {
        "name": "payload",
        "type": "object",
        "source": "state.inputs.payload",
        "required": True,
        "description": "Import payload",
        "contract_id": "inventory_payload",
    }
    assert db.workflow == before


@pytest.mark.parametrize("reader", ["defaults", "preview"])
@pytest.mark.parametrize(
    "inputs", [["message", None, {"name": "valid", "type": "text"}], {}, "message"]
)
def test_inspection_preserves_malformed_evidence_and_structured_issues(reader, inputs):
    db = _DB(inputs)
    before = deepcopy(db.workflow)
    resolver = IOResolver(db)
    if reader == "defaults":
        payload = resolver.get_step_input_defaults("legacy_inputs", "normalize")
        result = WorkflowEditorInputDefaultsView.model_validate(payload).model_dump()
        rows = result["current_expected_inputs"]
    else:
        payload = resolver.resolve_step_contract_preview(
            workflow_id="legacy_inputs", step_id="normalize"
        )
        result = WorkflowEditorStepContractPreviewView.model_validate(payload).model_dump()
        rows = result["custom"]
    assert result["raw_expected_inputs"] == inputs
    issues = result["expected_input_issues"]
    expected_code = (
        "contract.item_not_object" if isinstance(inputs, list) else "contract.items_not_list"
    )
    assert issues[0]["code"] == expected_code
    assert issues[0]["path"].startswith("workflow.steps.normalize.boundary_contract.entry")
    assert [row["name"] for row in rows] == (["valid"] if isinstance(inputs, list) else [])
    assert db.workflow == before
