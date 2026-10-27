"""Real deterministic writers survive graph checkpoint channels and final validation."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from core.application.orchestration.control_flow_step_node import control_flow_step_node
from core.application.orchestration.graph.nodes import execute_step_node
from core.application.orchestration.state import OrchestratorState
from core.application.registry.contract_validation import validate_workflow_readiness
from core.application.registry.workflow_contract import WorkflowInterface
from core.application.registry.workflow_contract_service import (
    WorkflowInterfaceService,
    collect_runtime_outputs,
)
from tests._step_run_state import step_run_state
from tests._workflow_definitions import linear_workflow

ALIASES = [
    "state.summary",
    "summary",
    "state.outputs.summary",
    "outputs.summary",
    "state.workflow_data.summary",
    "state.workflow_data.outputs.summary",
]


def workflow(kind, target, text):
    config = (
        {"text": text, "result_target": target}
        if kind == "template"
        else {"target": target, "value": text}
    )
    return linear_workflow(
        {"step_id": "write", "step_kind": kind, "override": {kind: config}},
        *(
            [
                {
                    "step_id": "executable",
                    "step_kind": "template",
                    "override": {"template": {"text": "Executed", "result_target": "state.note"}},
                }
            ]
            if kind == "set_variable"
            else []
        ),
        settings={
            "callable": {
                "enabled": True,
                "allow_empty_inputs": True,
                "trigger_channels": ["ui", "parent_workflow"],
                "expected_outputs": [{"name": "summary", "type": "string", "required": True}],
            }
        },
    )


@pytest.mark.parametrize("kind", ["template", "set_variable"])
@pytest.mark.parametrize("target", ALIASES)
def test_actual_writer_readiness_and_checkpoint_output_parity(kind, target):
    _assert_writer(kind, target, "Task33 greeting ✓")


def _assert_writer(kind, target, text):
    wf = workflow(kind, target, text)
    readiness = validate_workflow_readiness(
        SimpleNamespace(get_workflow=lambda _: wf), wf["workflow_id"]
    )
    assert readiness["valid"], readiness
    writer = control_flow_step_node if kind == "set_variable" else execute_step_node
    graph = StateGraph(OrchestratorState)
    graph.add_node("writer", writer)
    graph.add_edge(START, "writer")
    graph.add_edge("writer", END)
    compiled = graph.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": uuid4().hex}}
    compiled.invoke(step_run_state(wf["steps"][0], *wf["steps"][1:], workflow_data={}), config)
    committed = compiled.get_state(config).values
    assert "summary" not in committed
    assert committed["agent_messages"][-1]["status"] == "pass"
    interface = WorkflowInterface.from_settings(wf["settings"])
    assert collect_runtime_outputs(committed, interface=interface) == ({"summary": text}, {})
    validated = WorkflowInterfaceService().validate_outputs(interface, committed)
    assert validated.ok, validated.errors
    assert validated.outputs == {"summary": text}
    return committed


def test_long_template_uses_durable_mirror_without_diagnostic_patch():
    from core.application.orchestration.template_step_runtime import MESSAGE_TEXT_MAX_CHARS

    text = "x" * (MESSAGE_TEXT_MAX_CHARS + 1)
    committed = _assert_writer("template", "state.summary", text)
    assert "_state_patch" not in committed["agent_messages"][-1]["artifacts"]


@pytest.mark.parametrize(
    "target", ["state.customer.summary", "state.workflow_data.customer.summary", "state.other"]
)
def test_readiness_does_not_invent_public_output_from_nested_or_other_writer(target):
    wf = workflow("template", target, "nested")
    result = validate_workflow_readiness(
        SimpleNamespace(get_workflow=lambda _: wf), wf["workflow_id"]
    )
    assert "workflow.callable.output_not_produced" in {row["code"] for row in result["errors"]}


def test_wrong_declared_type_and_missing_or_invalid_runtime_value_are_distinct():
    wf = workflow("set_variable", "state.outputs.summary", {"object": True})
    result = validate_workflow_readiness(
        SimpleNamespace(get_workflow=lambda _: wf), wf["workflow_id"]
    )
    assert "workflow.callable.output_type_mismatch" in {row["code"] for row in result["errors"]}
    interface = WorkflowInterface.from_settings(wf["settings"])
    service = WorkflowInterfaceService()
    assert [row["code"] for row in service.validate_outputs(interface, {}).errors] == [
        "workflow.output.required"
    ]
    assert [
        row["code"]
        for row in service.validate_outputs(
            interface, {"outputs": {"summary": {"object": True}}}
        ).errors
    ] == ["workflow.output.invalid_type"]
