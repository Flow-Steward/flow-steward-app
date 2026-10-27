"""Native empty collections remain typed workflow inputs, not missing values."""

from copy import deepcopy

import pytest

from core.application.orchestration.transform_step_runtime import execute_transform_step
from core.application.registry.workflow_contract import WorkflowInterface
from core.application.registry.workflow_contract_service import WorkflowInterfaceService


def _interface(*fields):
    return WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "input_mode": "fields",
                "strict_inputs": True,
                "trigger_channels": ["mcp", "ui", "api", "parent_workflow"],
                "expected_inputs": list(fields),
                "output_mode": "none",
            }
        }
    )


@pytest.mark.parametrize("channel", ["mcp", "ui", "api", "parent_workflow"])
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize(("field_type", "value"), [("object", {}), ("array", [])])
def test_present_empty_collections_survive_input_validation(channel, required, field_type, value):
    interface = _interface(
        {"name": "payload", "type": field_type, "required": required},
        {"name": "flag", "type": "boolean", "required": True},
        {"name": "count", "type": "number", "required": True},
    )
    inputs = {"payload": deepcopy(value), "flag": False, "count": 0}
    original = deepcopy(inputs)
    result = WorkflowInterfaceService().validate_inputs(interface, inputs, channel=channel)
    assert result.ok, result.errors
    assert result.inputs == original
    assert result.inputs["flag"] is False
    assert type(result.inputs["count"]) is int
    assert inputs == original


@pytest.mark.parametrize(("field_type", "value"), [("boolean", False), ("number", 0)])
def test_false_and_zero_remain_present_required_values(field_type, value):
    interface = _interface({"name": "payload", "type": field_type, "required": True})
    result = WorkflowInterfaceService().validate_inputs(
        interface, {"payload": value}, channel="mcp"
    )
    assert result.ok, result.errors
    assert result.inputs == {"payload": value}
    assert type(result.inputs["payload"]) is type(value)


@pytest.mark.parametrize("field_type", ["object", "array"])
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("present_null", [False, True])
def test_absent_and_null_collections_follow_requiredness(field_type, required, present_null):
    interface = _interface({"name": "payload", "type": field_type, "required": required})
    result = WorkflowInterfaceService().validate_inputs(
        interface, {"payload": None} if present_null else {}, channel="mcp"
    )
    assert result.ok is not required
    assert result.inputs == {}
    if required:
        assert any(error["code"] == "workflow.input.required" for error in result.errors)
    else:
        assert result.errors == []


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("value", ["", " \t\n"])
def test_blank_text_keeps_existing_missing_value_semantics(required, value):
    interface = _interface({"name": "payload", "type": "text", "required": required})
    result = WorkflowInterfaceService().validate_inputs(
        interface, {"payload": value}, channel="mcp"
    )
    assert result.ok is not required
    assert result.inputs == {}
    if required:
        assert any(error["code"] == "workflow.input.required" for error in result.errors)


@pytest.mark.parametrize(("field_type", "value"), [("object", {}), ("array", [])])
@pytest.mark.parametrize("caller_supplied", [False, True])
def test_configured_empty_collection_defaults_stay_fixed(field_type, value, caller_supplied):
    interface = _interface(
        {"name": "payload", "type": field_type, "required": True, "default": deepcopy(value)}
    )
    result = WorkflowInterfaceService().validate_inputs(
        interface, {"payload": deepcopy(value)} if caller_supplied else {}, channel="mcp"
    )
    assert result.ok is not caller_supplied
    if caller_supplied:
        assert any(error["code"] == "workflow.input.override_forbidden" for error in result.errors)
    else:
        assert result.inputs == {"payload": value}


def test_transform_can_consume_a_validated_empty_object():
    interface = _interface({"name": "payload", "type": "object", "required": False})
    validated = WorkflowInterfaceService().validate_inputs(
        interface, {"payload": {}}, channel="mcp"
    )
    assert validated.ok, validated.errors
    result = execute_transform_step(
        config={
            "input": {"source": "state.inputs.payload"},
            "mode": "object",
            "output_mode": "mapped_only",
            "result_target": "state.result",
            "fields": [{"to": "side", "type": "text", "value": {"literal": "left"}, "actions": []}],
        },
        state={"inputs": validated.inputs},
    )
    assert result["ok"], result["errors"]
    assert result["output"] == {"side": "left"}
    assert result["state_patch"]["result"] == {"side": "left"}
