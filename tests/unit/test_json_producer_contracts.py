"""Unknown JSON producers may supply scalars; declared objects stay objects."""

from types import SimpleNamespace

import pytest

from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
from core.application.registry.contract_validation import (
    validate_invoke_workflow_step,
    validate_workflow_readiness,
)
from core.application.registry.io_resolver import IOResolver
from core.application.registry.workflow_contract import WorkflowInterface
from core.application.registry.workflow_contract_service import WorkflowInterfaceService
from tests._workflow_definitions import linear_workflow, stop_step


def _db(*workflows):
    rows = {row["workflow_id"]: row for row in workflows}
    tools = builtin_standard_tool_manifests()
    return SimpleNamespace(
        get_workflow=lambda workflow_id: rows.get(workflow_id),
        get_tool=lambda tool_id: tools.get(tool_id),
        get_agent_inputs=lambda _agent: [],
        get_agent_outputs=lambda _agent: [],
        get_skill_inputs=lambda _skill: [],
        get_skill_outputs=lambda _skill: [],
    )


def _pick_step():
    return {
        "step_id": "pick_leaf",
        "step_kind": "connector",
        "connector_id": "connector.std.json",
        "input_mapping_jsonb": {"value": 'literal:{"name":"Hello-World"}', "path": "literal:name"},
        "output_mapping_jsonb": {"result": "state.picked_leaf"},
        "override": {
            "connector": {
                "function_name": "pick",
                "input_mapping": {
                    "value": 'literal:{"name":"Hello-World"}',
                    "path": "literal:name",
                },
                "output_mapping": {"result": "state.picked_leaf"},
            }
        },
    }


@pytest.mark.parametrize("field_type", ["string", "number", "boolean"])
def test_real_json_pick_can_produce_required_scalar_callable_output(field_type):
    workflow = linear_workflow(
        _pick_step(),
        stop_step("done"),
        workflow_id="json_leaf",
        settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "expected_outputs": [{"name": "picked_leaf", "type": field_type, "required": True}],
            }
        },
    )
    result = validate_workflow_readiness(_db(workflow), "json_leaf")
    assert result["valid"] is True, result["errors"]


def _invoke_step(selector):
    return {
        "step_id": "invoke",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "child",
                "input_mapping": {"leaf": selector},
                "output_mapping": {},
            }
        },
    }


def _child(field_type):
    return linear_workflow(
        stop_step("done"),
        workflow_id="child",
        enabled=True,
        settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "expected_inputs": [{"name": "leaf", "type": field_type, "required": True}],
            }
        },
    )


@pytest.mark.parametrize("field_type", ["string", "number", "boolean"])
@pytest.mark.parametrize(
    "selector", ["state.picked_leaf", "message.step.pick_leaf.artifacts.picked_leaf"]
)
def test_real_json_pick_can_map_to_typed_child_input(field_type, selector):
    step = _invoke_step(selector)
    parent = linear_workflow(_pick_step(), step, stop_step("done"), workflow_id="parent")
    result = validate_invoke_workflow_step(
        _db(parent, _child(field_type)),
        workflow_id="parent",
        step=step,
        path_prefix="workflow.steps.invoke",
    )
    assert result["valid"] is True, result["errors"]


@pytest.mark.parametrize(
    "selector",
    ['literal:{"name":"known object"}', 'literal:["known array"]', 'literal:"known string"'],
)
def test_known_nonnumeric_literal_cannot_map_to_numeric_child_input(selector):
    step = _invoke_step(selector)
    parent = linear_workflow(step, stop_step("done"), workflow_id="parent")
    result = validate_invoke_workflow_step(
        _db(parent, _child("number")),
        workflow_id="parent",
        step=step,
        path_prefix="workflow.steps.invoke",
    )
    assert result["valid"] is False
    assert "invoke.input_mapping_type_mismatch" in {issue["code"] for issue in result["errors"]}


@pytest.mark.parametrize(
    "field_type",
    [
        "text",
        "string",
        "paragraph",
        "select",
        "number",
        "integer",
        "boolean",
        "object",
        "array",
        "list",
    ],
)
def test_generic_json_producer_is_statically_compatible_with_json_value_types(field_type):
    assert IOResolver._check_type_compat(field_type, "json") is True


@pytest.mark.parametrize(
    "expected,source",
    [
        ("string", "object"),
        ("number", "object"),
        ("boolean", "object"),
        ("string", "array"),
        ("number", "array"),
        ("number", "string"),
        ("file", "json"),
        ("artifact_handle", "json"),
        ("secret", "json"),
        ("unknown", "json"),
    ],
)
def test_precise_and_non_json_types_are_not_widened(expected, source):
    assert IOResolver._check_type_compat(expected, source) is False


def test_public_json_alias_remains_object_in_input_and_child_output_maps():
    workflow = linear_workflow(
        stop_step("done"),
        workflow_id="public_json",
        settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "expected_inputs": [{"name": "payload", "type": "json"}],
                "expected_outputs": [{"name": "result", "type": "json"}],
            },
        },
    )
    resolver = IOResolver(_db(workflow))
    available = resolver._build_available_type_map("public_json", "done")
    assert available["state.inputs.payload"] == "object"
    assert resolver._get_callable_output_types("public_json") == {"result": "object"}
    assert IOResolver._check_type_compat("string", available["state.inputs.payload"]) is False


@pytest.mark.parametrize(
    "field_type,value,valid",
    [
        ("string", "Hello-World", True),
        ("number", 42, True),
        ("boolean", False, True),
        ("string", {}, False),
        ("string", [], False),
        ("string", None, False),
        ("number", {}, False),
        ("number", "not a number", False),
        ("number", True, False),
        ("number", None, False),
        ("object", "scalar", False),
        ("array", {}, False),
        ("json", "scalar", False),
    ],
)
def test_required_runtime_output_still_validates_actual_json_leaf(field_type, value, valid):
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "output_mode": "variables",
                "expected_outputs": [{"name": "picked_leaf", "type": field_type, "required": True}],
            }
        }
    )
    result = WorkflowInterfaceService().validate_outputs(
        interface, {"outputs": {"picked_leaf": value}}
    )
    assert result.ok is valid
    if valid:
        assert result.outputs["picked_leaf"] == value
    else:
        assert result.errors


@pytest.mark.parametrize("producer", ["agent", "skill"])
def test_generic_json_declared_producer_keeps_scalar_compatibility_without_resolver(producer):
    from core.application.registry.contract_validation_workflow import (
        _collect_workflow_output_types,
    )

    workflow = linear_workflow(
        {"step_id": "produce", "step_kind": "agent", f"{producer}_id": "author"}
    )
    db = _db()
    setattr(db, f"get_{producer}_outputs", lambda _owner: [{"name": "leaf", "type": "json"}])
    produced = _collect_workflow_output_types(db, workflow)
    assert "json" in produced["leaf"]


def _set_variable(value, style):
    spec = (
        {"assignments": {"state.assigned": value}}
        if style == "assignments"
        else {"target": "state.assigned", "value": value}
    )
    return {"step_id": "assign", "step_kind": "set_variable", "override": {"set_variable": spec}}


@pytest.mark.parametrize("style", ["assignments", "target"])
@pytest.mark.parametrize("expected", ["string", "number"])
@pytest.mark.parametrize(
    "value",
    [
        {"name": "known object"},
        {"source": "state"},
        {"source": "state.routing_signals"},
        {"source": "state.reentry_context"},
        {"source": "state.agent_messages"},
    ],
)
def test_known_object_assignment_cannot_satisfy_scalar_callable_output(style, expected, value):
    workflow = linear_workflow(
        _set_variable(value, style),
        stop_step("done"),
        workflow_id="parent",
        settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "expected_outputs": [{"name": "assigned", "type": expected, "required": True}],
            },
        },
    )
    result = validate_workflow_readiness(_db(workflow), "parent")
    assert result["valid"] is False
    assert "workflow.callable.output_type_mismatch" in {issue["code"] for issue in result["errors"]}


@pytest.mark.parametrize("style", ["assignments", "target"])
@pytest.mark.parametrize(
    "value,valid",
    [
        ({"name": "known object"}, False),
        ({"source": "not-a-selector"}, False),
        ({"source": "state.inputs.payload", "extra": True}, False),
        ({"source": 42}, False),
        ({"source": "state.inputs.payload"}, True),
    ],
)
def test_object_assignment_and_source_wrapper_have_distinct_child_input_types(style, value, valid):
    step = _invoke_step("state.assigned")
    parent = linear_workflow(
        _set_variable(value, style),
        step,
        stop_step("done"),
        workflow_id="parent",
        settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "expected_inputs": [{"name": "payload", "type": "string"}],
            }
        },
    )
    result = validate_invoke_workflow_step(
        _db(parent, _child("string")),
        workflow_id="parent",
        step=step,
        path_prefix="workflow.steps.invoke",
    )
    assert result["valid"] is valid, result["errors"]
    if not valid:
        assert "invoke.input_mapping_type_mismatch" in {issue["code"] for issue in result["errors"]}


@pytest.mark.parametrize("style", ["assignments", "target"])
@pytest.mark.parametrize(
    "value,expected",
    [
        ({"name": "known object"}, "object"),
        ({"source": "not-a-selector"}, "object"),
        ({"source": "state.inputs.payload", "extra": True}, "object"),
        ({"source": 42}, "object"),
        ({"source": "state.inputs.payload"}, "json"),
        ({"source": " state "}, "object"),
        ({"source": "state.routing_signals"}, "object"),
        ({"source": "state.reentry_context"}, "object"),
        ({"source": "state.agent_messages"}, "array"),
        ({"source": "state.task_summary"}, "text"),
        ([1, 2], "array"),
        (42, "number"),
        (False, "boolean"),
        ("text", "text"),
    ],
)
def test_assignment_preview_and_output_collection_keep_literal_precision(style, value, expected):
    workflow = linear_workflow(_set_variable(value, style), stop_step("done"), workflow_id="parent")
    resolver = IOResolver(_db(workflow))
    preview = resolver.resolve_step_contract_preview(workflow_id="parent", step_id="assign")
    outputs = resolver.get_step_outputs("parent", "assign")
    assert {item["name"]: item["type"] for item in preview["outputs"]}["assigned"] == expected
    assert {item["name"]: item["type"] for item in outputs}["assigned"] == expected


@pytest.mark.parametrize(
    "name,expected",
    [("routing_signals", "object"), ("reentry_context", "object"), ("agent_messages", "array")],
)
def test_known_runtime_collections_cannot_map_to_scalar_child_input(name, expected):
    step = _invoke_step(f"state.{name}")
    parent = linear_workflow(step, stop_step("done"), workflow_id="parent")
    db = _db(parent, _child("number"))
    result = validate_invoke_workflow_step(
        db, workflow_id="parent", step=step, path_prefix="workflow.steps.invoke"
    )
    assert result["valid"] is False
    assert "invoke.input_mapping_type_mismatch" in {issue["code"] for issue in result["errors"]}
    assert (
        IOResolver(db).get_available_type_map_for_step("parent", "invoke")[f"state.{name}"]
        == expected
    )


@pytest.mark.parametrize("style", ["assignments", "target"])
@pytest.mark.parametrize(
    "value,expected", [("state.assigned", "text"), ({"source": "assigned"}, "object")]
)
def test_selector_shaped_literals_are_not_dropped_as_self_passthrough(style, value, expected):
    workflow = linear_workflow(_set_variable(value, style), stop_step("done"), workflow_id="parent")
    outputs = IOResolver(_db(workflow)).get_step_outputs("parent", "assign")
    assert {item["name"]: item["type"] for item in outputs}.get("assigned") == expected


@pytest.mark.parametrize("style", ["assignments", "target"])
@pytest.mark.parametrize(
    "source", ["state", "state.routing_signals", "state.reentry_context", "state.agent_messages"]
)
@pytest.mark.parametrize("expected", ["string", "number"])
def test_known_collection_source_wrapper_cannot_map_to_scalar_child(style, source, expected):
    step = _invoke_step("state.assigned")
    parent = linear_workflow(
        _set_variable({"source": source}, style), step, stop_step("done"), workflow_id="parent"
    )
    result = validate_invoke_workflow_step(
        _db(parent, _child(expected)),
        workflow_id="parent",
        step=step,
        path_prefix="workflow.steps.invoke",
    )
    assert result["valid"] is False
    assert "invoke.input_mapping_type_mismatch" in {issue["code"] for issue in result["errors"]}
