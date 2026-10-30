"""Actual loop publication, Invoke validation and Template rendering share saved selectors."""

from __future__ import annotations

from copy import deepcopy

import pytest

from core.application.orchestration.for_each_runtime import publish_item, select_items
from core.application.orchestration.run_cursor import ForEachFrame, RunCursor, begin_step
from core.application.orchestration.template_step_runtime import execute_template_step
from tests._step_run_state import prime, step_run_state
from tests.unit.test_invoke_workflow_step_node import (
    _application_submission_repositories as _application_submission_repositories,
)
from tests.unit.test_invoke_workflow_step_node import (
    _base_state,
    _capture_append_messages,
    _FakeLogger,
    _install_runtime_stubs,
)
from tests.unit.test_loop_selector_contract import (
    ORDERS,
    as_bundle,
    body_write_before,
    child_workflow,
    invoke_workflow,
    template_workflow,
)


def batch_state(wf):
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    step = wf["steps"][1]
    update = control_flow_step_node(
        step_run_state(
            step, orders=[{"id": n} for n in range(3)], workflow_data={}, agent_messages=[]
        )
    )
    assert update["agent_messages"][-1]["status"] == "pass"
    frame = ForEachFrame("orders_loop", "batch-pass")
    published = select_items(wf["for_each"][0], frame, update)
    assert published.failure is None
    return {**update, **published.updates}, frame


@pytest.mark.parametrize(
    "variant,reason,message",
    [
        ("optional", "value_missing", "'size' is undefined"),
        ("required", "input_missing", "Required input 'size' has no value at state.b.size"),
        ("default_required", "input_missing", "Required input 'size' has no value at state.b.size"),
        ("local", "value_missing", "'list object' has no attribute 'size'"),
    ],
)
def test_three_template_variants_keep_distinct_runtime_failure(variant, reason, message):
    wf = template_workflow(
        required={"optional": False, "required": True, "default_required": None, "local": True}[
            variant
        ]
    )
    config = wf["steps"][2]["override"]["template"]
    if variant == "local":
        config["inputs"] = [{"name": "b", "type": "array", "source": "state.b", "required": True}]
        config["text"] = "{{ b.size }}"
    stored = deepcopy(config)
    state, _ = batch_state(wf)
    result = execute_template_step(config, state)
    assert config == stored
    assert result["failure_reason"] == reason, result
    assert result["errors"][0]["message"] == message
    assert result["inputs"] == ({"b": [{"id": 0}]} if variant == "local" else {})
    if reason == "value_missing":
        assert result["errors"][0]["line"] == 1


def test_batch_last_chunk_indices_and_length_are_real_values():
    wf = template_workflow()
    wf["steps"][1]["override"]["batch"]["batch_size"] = 2
    state, frame = batch_state(wf)
    for index, size in enumerate([2, 1]):
        frame.item_index = index
        state.update(publish_item(wf["for_each"][0], frame, state).updates)
        for source in ["state.b[0]", "state.b.0"]:
            result = execute_template_step(
                {
                    "text": "{{ value.id }}",
                    "inputs": [{"name": "value", "type": "object", "source": source}],
                    "result_target": "state.text",
                },
                state,
            )
            assert result["ok"], result
            assert result["text"] == str(index * 2), result
        result = execute_template_step(
            {
                "text": "{{ b | length }}",
                "inputs": [{"name": "b", "type": "array", "source": "state.b"}],
                "result_target": "state.text",
            },
            state,
        )
        assert result["text"] == str(size)


@pytest.mark.parametrize(
    "body,expected", [({"id": 7}, "missing/False"), ({"body": {"id": 7}}, "{'id': 7}/True")]
)
def test_unknown_http_body_member_remains_runtime_defined_or_missing(body, expected):
    result = execute_template_step(
        {
            "text": "{{ value | default('missing') }}/{{ value is defined }}",
            "inputs": [
                {"name": "value", "type": "json", "required": False, "source": "state.user.body"}
            ],
            "result_target": "state.text",
        },
        {"user": body},
    )
    assert result["ok"], result
    assert result["text"] == expected, result


@pytest.mark.usefixtures("_application_submission_repositories")
@pytest.mark.parametrize(
    "case",
    [
        "two",
        "missing",
        "wrong_type",
        "fields_empty",
        "schema_empty",
        "schema_missing",
        "schema_wrong_type",
        "index_text",
        "index_object",
        "index_wrong_type",
        "callable_collision",
    ],
)
def test_real_loop_mapping_and_child_contract_before_enqueue(monkeypatch, case):
    from core.application.orchestration import invoke_workflow_step_node as node

    child = child_workflow(
        [
            {"name": "order_id", "type": "text", "required": True},
            {"name": "items", "type": "array", "required": True},
            {"name": "active", "type": "boolean", "required": True},
            {"name": "count", "type": "number", "required": True},
        ]
    )
    if case in {"index_object", "index_wrong_type", "callable_collision"}:
        child["settings"]["callable"]["expected_inputs"][0]["type"] = (
            "object" if case == "index_object" else "number"
        )
    if case.startswith("schema_"):
        child["settings"]["callable"].update(
            input_mode="json_schema",
            input_schema={
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "items": {"type": "array"},
                    "active": {"type": "boolean"},
                    "count": {"type": "number"},
                },
                "required": ["order_id", "items", "active", "count"],
            },
        )
    from core.application.workflows.workflow_definition_service import definition_from_workflow
    from tests.unit.test_workflow_authoring_catalog import SCOPE
    from tests.unit.test_workflow_authoring_drafts import DraftDB, service

    db = DraftDB()
    db.workflows["selector_child"] = child
    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService

    child_state = step_run_state(child["steps"][0])
    child_state.update(execute_step_node(child_state))
    assert child_state["agent_messages"][-1]["status"] == "pass"
    contracts = WorkflowInterfaceService()
    final_projection = contracts.validate_outputs(
        contracts.interface_from_definition(child), child_state
    )
    assert final_projection.ok
    assert final_projection.outputs == {"summary": "Shipment assembled"}
    runtime = _install_runtime_stubs(monkeypatch, workflows={"selector_child": child})
    messages = _capture_append_messages(monkeypatch, node)
    logger = _FakeLogger(
        child_job_id=932,
        child_job_extra={"meta": {"workflow_outputs": final_projection.dump()}},
        child_messages=[{"envelope": {"artifacts": {"summary": "stale diagnostic"}}}],
    )
    monkeypatch.setattr(node, "_get_logger", lambda: logger)
    monkeypatch.setattr(node, "_should_execute_child_inline", lambda _parent: False)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda *_args, **_kwargs: ({"id": 932, "status": "completed", "error": ""}, "completed"),
    )
    mapping = {
        "order_id": "state.o.id",
        "items": "state.o.items",
        "active": "state.o.active",
        "count": "state.o.count",
    }
    if case.startswith("index_"):
        mapping["order_id"] = "state.i"
    elif case == "callable_collision":
        mapping = {
            key: value.replace("state.o.", "state.inputs.") for key, value in mapping.items()
        }
    wf = invoke_workflow(mapping)
    if case.startswith("index_"):
        body_write_before(wf, "i", {"source": "state.o.index_value"})
    elif case == "callable_collision":
        wf["for_each"][0]["item_name"] = "inputs"
        wf["settings"]["callable"] = {
            "enabled": True,
            "trigger_channels": ["parent_workflow"],
            "expected_inputs": [{"name": "id", "type": "text", "required": True}],
        }
    text = as_bundle(wf)
    checked = service(db).validate(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert checked["can_save"], checked
    assert checked["readiness"]["valid"], checked
    saved = service(db).save(scope=SCOPE, project_id="project", bundle_yaml=text)
    wf = db.get_workflow(saved["workflow_id"])
    step = wf["steps"][1]
    values = deepcopy(ORDERS)
    if case.startswith("index_"):
        for index, value in enumerate(values):
            value["index_value"] = (
                {"id": index + 7}
                if case == "index_object"
                else index + 7
                if case == "index_wrong_type" and index == 0
                else value["id"]
            )
    elif case == "callable_collision":
        for index, value in enumerate(values):
            value["id"] = index + 7
    if case in {"missing", "schema_missing"}:
        del values[1]["id"]
    elif case in {"wrong_type", "schema_wrong_type"}:
        values[1]["items"] = "not an array"
    elif case in {"fields_empty", "schema_empty"}:
        values[1]["items"] = []
    state = _base_state()
    state.update(prime(definition_from_workflow(wf)))
    state.update(orders=values, workflow_data={}, workflow_id=wf["workflow_id"])
    if case == "callable_collision":
        state["inputs"] = {"id": "stale callable input"}
    frame = ForEachFrame("orders_loop", "invoke-pass")
    state.update(select_items(wf["for_each"][0], frame, state).updates)
    for index in range(2):
        frame.item_index = index
        state.update(publish_item(wf["for_each"][0], frame, state).updates)
        if case.startswith("index_"):
            from core.application.orchestration.control_flow_step_node import control_flow_step_node

            state["run_cursor"] = begin_step(RunCursor(frames=[frame]), "replace").dump()
            replaced = control_flow_step_node(state)
            assert replaced["agent_messages"][-1]["status"] == "pass", replaced
            state.update(replaced)
            assert state["i"] == values[index]["index_value"]
        state["run_cursor"] = begin_step(RunCursor(frames=[frame]), step["step_id"]).dump()
        update = node.invoke_workflow_step_node(state)
        refused_item = index == 1 and case in {
            "missing",
            "wrong_type",
            "fields_empty",
            "schema_missing",
            "schema_wrong_type",
            "index_wrong_type",
        }
        if refused_item:
            expected_error = (
                "input_mapping_failed"
                if case in {"missing", "fields_empty"}
                else "child_input_contract_failed"
            )
            assert messages[-1]["artifacts"]["_invoke_workflow_error"] == expected_error, messages[
                -1
            ]
            assert len(runtime["enqueue_calls"]) == 1
        else:
            assert messages[-1]["status"] == "pass", messages[-1]
            assert update["summary"] == "Shipment assembled"
            assert update["workflow_data"]["summary"] == "Shipment assembled"
        state.update(update)
    payloads = [call["extra_state"]["inputs"] for call in runtime["enqueue_calls"]]
    expected = [
        {
            "order_id": value["index_value"] if case.startswith("index_") else value.get("id"),
            "items": value["items"],
            "active": value["active"],
            "count": value["count"],
            "task_summary": (
                str(value["index_value"]["id"]) if case == "index_object" else "Build release plan"
            ),
        }
        for value in values
    ]
    refused = case in {
        "missing",
        "wrong_type",
        "fields_empty",
        "schema_missing",
        "schema_wrong_type",
        "index_wrong_type",
    }
    assert payloads == (expected[:1] if refused else expected), (payloads, messages)
    assert payloads[0]["active"] is False
    assert payloads[0]["count"] == 0
    assert len([m for m in messages if m["status"] == "pass"]) == (1 if refused else 2)


@pytest.mark.parametrize("body,present", [({"id": 7}, False), ({"body": {"id": 7}}, True)])
def test_transform_unknown_http_member_is_validated_at_runtime(body, present):
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.user.body"},
            "mode": "object",
            "result_target": "state.result",
            "fields": [{"from": "id", "to": "id", "type": "number"}],
        },
        state={"user": body},
    )
    assert result["ok"] is present, result
    if present:
        assert result["state_patch"]["result"] == {"id": 7}
    else:
        assert result["detail"] == "input_source_missing"
        assert result["state_patch"] == {}
