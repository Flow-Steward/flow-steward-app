"""Archived summaries describe pinned interfaces without exposing configuration values."""

import copy
import json
from unittest.mock import patch
from uuid import uuid4

import pytest

from tests.unit.test_job_definition_view import _view


def _row(settings, *, protected=False, steps=()):
    return {
        "id": "pinned-snapshot",
        "workflow_id": "archived-workflow",
        "revision": 4,
        "schema_version": 1,
        "definition_jsonb": {
            "protected": protected,
            "settings": settings,
            "steps": list(steps),
            "groups": [],
            "execution_graph": {"schema_version": 1, "entry": None, "edges": []},
        },
    }


def test_pinned_summary_whitelists_names_and_modes_without_values_or_live_lookup():
    reference = str(uuid4())
    schedule_expression = "0 9 * * *"
    row = _row(
        {
            "callable": {
                "trigger_channels": ["api", "parent_workflow"],
                "input_mode": "fields",
                "expected_inputs": [
                    {"name": " customer ", "required": True, "default": "private-value"},
                    {
                        "name": "credential",
                        "type": "secret",
                        "default_secret_ref": reference,
                        "description": "private-description",
                    },
                ],
                "output_mode": "variables_and_artifacts",
                "expected_outputs": [{"name": "result", "default": "private-output"}],
                # Declared before External changes were removed: never summarized.
                "side_effects": [{"kind": "send_email", "target": "private-recipient"}],
            },
            "schedule": {"cron": schedule_expression},
        },
        steps=[
            {
                "step_id": "render",
                "display_name": "Render report",
                "step_kind": "file_from_base64",
                "override": {
                    "file_from_base64": {
                        "source": "state.inputs.private_body",
                        "result_target": "state.private_variable",
                    },
                    "returned_files": [{"result": "file", "key": "report"}],
                },
            }
        ],
    )
    original = copy.deepcopy(row)
    with patch(
        "core.application.registry.workflow_contract_service."
        "WorkflowInterfaceService.interface_for_workflow_id",
        side_effect=AssertionError("An archived contract must not fetch a live workflow"),
    ):
        view = _view(row)
    assert view.available is True
    assert view.model_dump().get("contract_summary") == {
        "inputs": [
            {"name": "customer", "required": True},
            {"name": "credential", "required": False},
        ],
        "outputs": ["result"],
        "artifacts": ["report"],
        "trigger_channels": ["api", "parent_workflow"],
        "input_mode": "fields",
        "output_mode": "variables_and_artifacts",
    }
    serialized = json.dumps(view.model_dump())
    assert "private-" not in serialized
    assert reference not in serialized
    assert schedule_expression not in serialized
    assert row == original


@pytest.mark.parametrize(
    ("schema", "inputs"),
    [
        ({"type": "string", "default": "private-scalar"}, []),
        ({"type": "array", "items": {"type": "string", "default": "private-item"}}, []),
        (
            {
                "type": "object",
                "properties": {
                    "account": {"type": "string", "default": "private-default"},
                    "limit": {"type": "integer", "description": "private-description"},
                },
                "required": ["account"],
            },
            [{"name": "account", "required": True}, {"name": "limit", "required": False}],
        ),
    ],
)
def test_pinned_json_schema_has_named_fields_only_without_losing_unnamed_contract(schema, inputs):
    view = _view(_row({"callable": {"input_mode": "json_schema", "input_schema": schema}}))
    summary = view.model_dump().get("contract_summary")
    assert summary is not None
    assert summary["inputs"] == inputs
    assert summary["input_mode"] == "json_schema"
    assert "private-" not in json.dumps(summary)
    assert "input_schema" not in summary


@pytest.mark.parametrize("settings", [{}, {"callable": {"input_mode": "passthrough"}}])
def test_valid_empty_contract_is_available_not_unknown(settings):
    summary = _view(_row(settings)).model_dump().get("contract_summary")
    assert summary == {
        "inputs": [],
        "outputs": [],
        "artifacts": [],
        "trigger_channels": [],
        "input_mode": "passthrough" if settings else "fields",
        "output_mode": "none",
    }


@pytest.mark.parametrize("settings", [None, "bad-json", [], {"callable": True}])
def test_malformed_or_missing_old_settings_have_unknown_summary(settings):
    row = _row(settings)
    if settings is None:
        row["definition_jsonb"].pop("settings")
    payload = _view(row).model_dump()
    assert payload["available"] is True
    assert "contract_summary" in payload
    assert payload["contract_summary"] is None


def test_unknown_protection_never_discloses_contract_names():
    row = _row({"callable": {"expected_inputs": [{"name": "private-name"}]}})
    row["definition_jsonb"].pop("protected")
    payload = _view(row).model_dump()
    assert payload["available"] is False
    assert payload.get("contract_summary") is None
    assert "private-name" not in json.dumps(payload)


def test_protected_definition_keeps_its_public_contract_and_redacts_defaults():
    payload = _view(
        _row(
            {"callable": {"expected_inputs": [{"name": "customer", "default": "private-value"}]}},
            protected=True,
        )
    ).model_dump()
    assert payload["available"] is True
    assert payload["protected"] is True
    assert "private-value" not in json.dumps(payload)


def test_summary_uses_canonical_derived_output_mode():
    summary = (
        _view(_row({"callable": {"output_mode": "none", "expected_outputs": [{"name": "result"}]}}))
        .model_dump()
        .get("contract_summary")
    )
    assert summary is not None
    assert summary["output_mode"] == "variables"
    assert summary["outputs"] == ["result"]
