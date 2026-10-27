"""Authored Agent inputs survive declared graph channels and reach model messages."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from core.application.orchestration.control_flow_step_node import control_flow_step_node
from core.application.orchestration.graph.nodes import load_agent_implementation, wrap_agent_as_node
from core.application.orchestration.graph.nodes_contracts import _resolve_required_input
from core.application.orchestration.run_cursor import RunCursor, begin_step
from core.application.orchestration.state import OrchestratorState
from tests._step_run_state import pinned_definition, prime, step_run_state
from tests.unit import test_agent_instruction_transport as instruction_transport

authored_runtime = instruction_transport.authored_runtime
BASE_TEXT = instruction_transport.BASE_TEXT
SYSTEM_TEXT = instruction_transport.SYSTEM_TEXT

REQUEST = "Report request Привет ✓"


def _graph_run(runtime, monkeypatch, *, required=True, explicit=False, request=REQUEST):
    runtime.store.save_skill(
        {
            **runtime.skill_payload,
            "expected_inputs": [{"name": "request", "type": "text", "required": required}],
        }
    )
    runtime.registry.reload_all()
    manifest = runtime.registry.agents.get_agent_or_raise(runtime.agent_id)
    agent = load_agent_implementation(manifest, runtime.registry.tools, runtime.registry.skills)
    monkeypatch.setattr(agent, "_log_prompt_request", lambda *_a, **_k: None)
    monkeypatch.setattr(agent, "_log_prompt_response", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "core.application.orchestration.agents.common.publish_agent_message", lambda *_a, **_k: True
    )
    set_step = {
        "step_id": "supply",
        "step_kind": "set_variable",
        "override": {"set_variable": {"target": "state.request", "value": request}},
    }
    agent_step = {
        "step_id": "write",
        "step_kind": "agent",
        "agent_id": runtime.agent_id,
        "skill_id": runtime.skill_id,
    }
    if explicit:
        agent_step["input_mapping_jsonb"] = {"request": "state.request"}
    initial = step_run_state(
        set_step, agent_step, **runtime.state, workflow_data={}, materialized_inputs={}
    )
    observed = []

    def supply(state):
        result = control_flow_step_node(state)
        result["run_cursor"] = begin_step(RunCursor.load(state["run_cursor"]), "write").dump()
        return result

    wrapped = wrap_agent_as_node(agent, manifest)

    def execute(state):
        observed.append(dict(state))
        return wrapped(state)

    builder = StateGraph(OrchestratorState)
    builder.add_node("supply", supply)
    builder.add_node("write", execute)
    builder.add_edge(START, "supply")
    builder.add_edge("supply", "write")
    builder.add_edge("write", END)
    graph = builder.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "input-channel"}}
    graph.invoke(initial, config)
    committed = graph.get_state(config).values
    assert "request" not in observed[0]
    assert observed[0]["workflow_data"]["request"] == request
    assert committed["workflow_data"]["request"] == request
    return runtime.model.calls, committed


def test_required_natural_input_survives_real_graph_channels(authored_runtime, monkeypatch):
    calls, final = _graph_run(authored_runtime, monkeypatch)
    assert calls, final.get("agent_messages")
    assert len(calls) == 1


def test_natural_input_reaches_actual_provider_messages(authored_runtime, monkeypatch):
    # Optional contract isolates delivery from the independently failing required-input gate.
    calls, _final = _graph_run(authored_runtime, monkeypatch, required=False)
    assert len(calls) == 1
    assert SYSTEM_TEXT in calls[0][0].content
    human = next(message for message in calls[0] if message.type == "human")
    assert BASE_TEXT in human.content
    assert REQUEST in human.content


def test_explicit_input_mapping_reaches_actual_provider_messages(authored_runtime, monkeypatch):
    calls, _final = _graph_run(authored_runtime, monkeypatch, explicit=True)
    assert len(calls) == 1
    assert REQUEST in next(message for message in calls[0] if message.type == "human").content


@pytest.mark.parametrize(
    "state,field,expected",
    [
        (
            {
                "request": "direct",
                "materialized_inputs": {"request": "mapped"},
                "workflow_data": {"request": "mirror"},
            },
            "request",
            "direct",
        ),
        (
            {
                "request": None,
                "materialized_inputs": {"request": "mapped"},
                "workflow_data": {"request": "mirror"},
            },
            "request",
            "mapped",
        ),
        (
            {"materialized_inputs": {"request": None}, "workflow_data": {"request": "mirror"}},
            "request",
            None,
        ),
        (
            {"materialized_inputs": {"request": False}, "workflow_data": {"request": "mirror"}},
            "request",
            False,
        ),
        ({"workflow_data": {"request": 0}}, "request", 0),
        ({"workflow_data": {"request": False}}, "request", False),
        ({"workflow_data": {"request": ""}}, "request", ""),
        ({"workflow_data": {"request": None}}, "request", None),
        ({"workflow_data": {"rows": [{"value": "nested"}]}}, "rows[0].value", "nested"),
        ({"request": None, "workflow_data": {"request": "mirror"}}, "request", "message"),
        ({}, "request", "message"),
    ],
)
def test_required_input_precedence_and_canonical_mirror_selectors(state, field, expected):
    assert (
        _resolve_required_input(field, state, [{"artifacts": {"request": "message"}}]) == expected
    )


def test_supplied_step_input_passes_actual_readiness_and_reaches_only_target(
    authored_runtime, monkeypatch
):
    from uuid import uuid4

    from core.entrypoints.admin.workflows_projection import _get_step_test_run_validation
    from tests._workflow_definitions import linear_workflow
    from tests.unit.test_workflow_test_run_routes_async import _client, _close

    runtime = authored_runtime
    runtime.store.save_skill(
        {
            **runtime.skill_payload,
            "expected_inputs": [{"name": "request", "type": "text", "required": True}],
            "expected_outputs": [{"name": "answer", "type": "text", "required": False}],
        }
    )
    runtime.registry.reload_all()
    workflow_id = "isolated_" + uuid4().hex
    step = {
        "step_id": "compile",
        "step_kind": "agent",
        "agent_id": runtime.agent_id,
        "skill_id": runtime.skill_id,
    }
    workflow = linear_workflow(
        {
            "step_id": "unselected",
            "step_kind": "set_variable",
            "override": {
                "set_variable": {"target": "state.unselected_value", "value": "Must stay absent"}
            },
        },
        step,
        workflow_id=workflow_id,
        account_id=runtime.state["account_id"],
    )
    runtime.store.save_workflow(workflow)
    captured = []
    try:
        validation = _get_step_test_run_validation(runtime.store, workflow_id, "compile")
        assert any(
            issue.get("issue_type") == "step_required_input_unavailable"
            and issue.get("input_name") == "request"
            for issue in validation["errors"]
        ), validation
        assert all(
            issue.get("issue_type") == "step_required_input_unavailable"
            for issue in validation["errors"]
        ), validation
        client, _projects, _jobs = _client(
            account_id=runtime.state["account_id"],
            enqueue=lambda **kwargs: captured.append(kwargs) or 41,
            workflow_override=runtime.store.get_workflow(workflow_id),
            step_validation=lambda *_args: _get_step_test_run_validation(
                runtime.store, workflow_id, "compile"
            ),
            step_contract_preview={
                "unresolved_required": [
                    {"name": "request", "type": "text", "required": True, "source": ""}
                ]
            },
        )
        try:
            response = client.post(
                f"/api/workflows/{workflow_id}/steps/compile/test-run",
                json={
                    "project_id": "project-1",
                    "task_summary": "Only the selected step",
                    "task_envelope": {"inputs": {"request": REQUEST}},
                },
            )
            assert response.status_code == 202, response.text
            assert len(captured) == 1
            assert captured[0]["extra_state"]["run_cursor"]["step_id"] == "compile"
            from core.application.orchestration.agents.prompt_agent import PromptAgent
            from core.application.orchestration.graph.nodes import execute_step_node
            from core.application.workflows.platform_test_run_runtime import _build_initial_state

            state = _build_initial_state(
                workflow_id=workflow_id,
                project_id="project-1",
                task_summary="Only selected input",
                task_source="step_test_run",
                project_name="Report project",
                project_config=captured[0]["project_config"],
                task_id="isolated-input",
                task_envelope=captured[0]["extra_state"]["task_envelope"],
                extra_state={
                    **captured[0]["extra_state"],
                    **prime(pinned_definition(*workflow["steps"])),
                },
            )
            monkeypatch.setattr(PromptAgent, "_log_prompt_request", lambda *_a, **_k: None)
            monkeypatch.setattr(PromptAgent, "_log_prompt_response", lambda *_a, **_k: None)
            monkeypatch.setattr(
                "core.application.orchestration.agents.common.publish_agent_message",
                lambda *_a, **_k: True,
            )
            result = execute_step_node(state)
            assert "unselected_value" not in state.get("workflow_data", {})
            assert result["agent_messages"][-1]["step_id"] == "compile"
            assert len(runtime.model.calls) == 1
            assert (
                REQUEST
                in next(
                    message for message in runtime.model.calls[0] if message.type == "human"
                ).content
            )
            assert SYSTEM_TEXT in runtime.model.calls[0][0].content
            assert (
                BASE_TEXT
                in next(
                    message for message in runtime.model.calls[0] if message.type == "human"
                ).content
            )
            missing = client.post(
                f"/api/workflows/{workflow_id}/steps/compile/test-run",
                json={
                    "project_id": "project-1",
                    "task_summary": "Missing value",
                    "task_envelope": {"inputs": {}},
                },
            )
            assert missing.status_code in {400, 422}
            assert len(captured) == 1
        finally:
            _close(client)
    finally:
        runtime.conn.execute(
            "DELETE FROM registry_workflows WHERE workflow_id = %s", (workflow_id,)
        )


@pytest.mark.parametrize("supplied", [0, False])
def test_natural_falsy_inputs_remain_present(authored_runtime, monkeypatch, supplied):
    calls, _state = _graph_run(authored_runtime, monkeypatch, request=supplied)
    assert len(calls) == 1
    human = next(message for message in calls[0] if message.type == "human")
    assert ('"request": 0' if type(supplied) is int else '"request": false') in human.content


@pytest.mark.parametrize("supplied", [None, "", "   "])
def test_empty_natural_required_inputs_still_refuse_before_provider(
    authored_runtime, monkeypatch, supplied
):
    calls, state = _graph_run(authored_runtime, monkeypatch, request=supplied)
    assert calls == []
    assert any(
        "Missing required input 'request'" in str(message.get("errors"))
        for message in state["agent_messages"]
    )


def test_authoring_accepts_the_same_exact_upstream_request_write(authored_runtime):
    from uuid import uuid4

    from core.application.registry.io_resolver import IOResolver
    from tests._workflow_definitions import linear_workflow

    runtime = authored_runtime
    runtime.store.save_skill(
        {
            **runtime.skill_payload,
            "expected_inputs": [{"name": "request", "type": "text", "required": True}],
        }
    )
    workflow_id = "producer_" + uuid4().hex
    runtime.store.save_workflow(
        linear_workflow(
            {
                "step_id": "supply",
                "step_kind": "set_variable",
                "override": {"set_variable": {"target": "state.request", "value": REQUEST}},
            },
            {
                "step_id": "write",
                "step_kind": "agent",
                "agent_id": runtime.agent_id,
                "skill_id": runtime.skill_id,
            },
            workflow_id=workflow_id,
            account_id=runtime.state["account_id"],
        )
    )
    try:
        resolver = IOResolver(runtime.store)
        assert "request" in {
            row["name"] for row in resolver.get_step_state_outputs(workflow_id, "supply")
        }
        assert not any(
            row.get("input_name") == "request" and row.get("severity") == "error"
            for row in resolver.validate_step_io(workflow_id, "write")
        )
    finally:
        runtime.conn.execute(
            "DELETE FROM registry_workflows WHERE workflow_id = %s", (workflow_id,)
        )


@pytest.mark.parametrize(
    "field,mirror",
    [
        ("project_config", {}),
        ("project_config", {"project_config": {"business_value": "mirror"}}),
        ("state.project_config", {"project_config": {"business_value": "mirror"}}),
        (
            "workflow_data.project_config",
            {
                "project_config": {"business_value": "mirror"},
                "workflow_data": {"project_config": {"business_value": "nested mirror"}},
            },
        ),
    ],
)
def test_automatic_input_copy_does_not_export_runtime_configuration(
    authored_runtime, field, mirror
):
    from core.application.orchestration.graph.nodes_contracts import (
        _materialize_agent_step_input_mapping,
    )

    runtime = authored_runtime
    runtime.store.save_skill(
        {
            **runtime.skill_payload,
            "expected_inputs": [{"name": field, "type": "object", "required": False}],
        }
    )
    runtime.registry.reload_all()
    state = step_run_state(
        {
            "step_id": "write",
            "step_kind": "agent",
            "agent_id": runtime.agent_id,
            "skill_id": runtime.skill_id,
        },
        **runtime.state,
        workflow_data=mirror,
        materialized_inputs={},
    )
    _materialize_agent_step_input_mapping(state)
    assert field not in state["materialized_inputs"]


def test_automatic_mirrored_business_inputs_keep_precedence_and_sensitive_provenance(
    authored_runtime,
):
    from core.application.orchestration.agents.prompt_builder import PromptBuilder
    from core.application.orchestration.graph.nodes_contracts import (
        _materialize_agent_step_input_mapping,
    )

    runtime = authored_runtime
    fields = ["request", "rows[0].title", "customer.password"]
    runtime.store.save_skill(
        {
            **runtime.skill_payload,
            "expected_inputs": [
                {"name": field, "type": "text", "required": False} for field in fields
            ],
        }
    )
    runtime.registry.reload_all()
    business_value = "sensitive-business-input"
    state = step_run_state(
        {
            "step_id": "write",
            "step_kind": "agent",
            "agent_id": runtime.agent_id,
            "skill_id": runtime.skill_id,
        },
        **runtime.state,
        request="Direct request",
        workflow_data={
            "request": "Mirrored request",
            "rows": [{"title": "Nested business title"}],
            "customer": {"password": business_value},
        },
        materialized_inputs={},
    )
    _materialize_agent_step_input_mapping(state)
    assert state["materialized_inputs"]["request"] == "Direct request"
    assert state["materialized_inputs"]["rows[0].title"] == "Nested business title"
    assert state["materialized_inputs"]["customer.password"] == business_value
    assert "customer.password" in state["materialized_input_sensitive_keys"]
    assert "sensitive-business-input" not in PromptBuilder._build_materialized_inputs_context(state)
