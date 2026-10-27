"""Direct outputs require the contract of their actual successful producer."""

from __future__ import annotations

from core.application.orchestration.agent_messages import build_agent_message
from core.application.registry.workflow_contract import WorkflowInterface
from core.application.registry.workflow_contract_service import WorkflowInterfaceService


def interface(*, required=True):
    return WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "trigger_channels": ["ui", "parent_workflow"],
                "expected_outputs": [{"name": "summary", "type": "string", "required": required}],
            }
        }
    )


def message(artifacts, **kwargs):
    return build_agent_message(
        from_agent="unrelated",
        to_agent="orchestrator",
        skill="diagnose",
        status="pass",
        correlation_id="diagnostic",
        step_id="diagnostic",
        step_execution_id="00000000-0000-4000-8000-000000000001",
        attempt=1,
        published_at="2026-10-24T00:00:00+00:00",
        artifacts=artifacts,
        **kwargs,
    )


def test_valid_unrelated_message_cannot_satisfy_required_output():
    result = WorkflowInterfaceService().validate_outputs(
        interface(),
        {"agent_messages": [message({"summary": "diagnostic, not a result"})]},
    )
    assert result.outputs == {}
    assert [error["code"] for error in result.errors] == ["workflow.output.required"]


def test_untaken_optional_writer_does_not_return_unrelated_agent_diagnostic(monkeypatch):
    from types import SimpleNamespace

    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.orchestration.agents.base import AgentOutput
    from core.application.orchestration.control_flow_step_node import control_flow_step_node
    from core.application.orchestration.graph.nodes import wrap_agent_as_node
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from core.application.registry.contract_validation import validate_workflow_readiness
    from core.application.registry.registry_manager import RegistryManager
    from core.application.registry.skill_registry import SkillRegistry
    from tests._step_run_state import prime, step_run_state
    from tests._workflow_definitions import edge, linear_workflow, node

    workflow = linear_workflow(
        {
            "step_id": "choose",
            "step_kind": "if",
            "override": {
                "if": {"condition": {"selector": "state.task_summary", "operator": "truthy"}}
            },
        },
        {
            "step_id": "writer",
            "step_kind": "template",
            "override": {
                "template": {"text": "real result", "result_target": "state.outputs.summary"}
            },
        },
        {
            "step_id": "diagnostic",
            "step_kind": "agent",
            "agent_id": "unrelated",
            "skill_id": "diagnose",
        },
        account_id=DEFAULT_ACCOUNT_ID,
        settings={
            "callable": {
                "enabled": True,
                "allow_empty_inputs": True,
                "trigger_channels": ["ui"],
                "expected_outputs": [{"name": "summary", "type": "string", "required": False}],
            }
        },
    )
    workflow["settings"]["execution_graph"] = {
        "schema_version": 1,
        "entry": node("choose"),
        "edges": [
            edge("choose", node("writer"), "true"),
            edge("choose", node("diagnostic"), "false"),
            edge("writer", node("done")),
            edge("diagnostic", node("done")),
        ],
    }
    schema = {"type": "object", "properties": {}, "required": []}
    skill = {
        "skill_id": "diagnose",
        "display_name": "Diagnose",
        "enabled": True,
        "account_id": DEFAULT_ACCOUNT_ID,
        "input_schema": schema,
        "output_schema": {
            "type": "object",
            "properties": {"detail": {"type": "string"}},
            "required": ["detail"],
        },
        "_inputs": [],
        "_outputs": [{"name": "detail", "type": "string", "required": True}],
    }
    agent_row = {
        "agent_id": "unrelated",
        "display_name": "Unrelated",
        "enabled": True,
        "_skills": [{"skill_id": "diagnose"}],
        "_tools": [],
        "_inputs": [],
        "_outputs": [],
    }
    store = SimpleNamespace(
        get_workflow=lambda _: workflow,
        get_agent=lambda _: agent_row,
        get_skill=lambda _: skill,
        list_skills=lambda: [skill],
        get_agent_skills=lambda _: agent_row["_skills"],
        get_agent_tools=lambda _: [],
        get_agent_inputs=lambda _: [],
        get_agent_outputs=lambda _: [],
        get_agent_own_outputs=lambda _: [],
        get_skill_inputs=lambda _: [],
        get_skill_outputs=lambda _: skill["_outputs"],
        get_skill_tools=lambda _: [],
        get_skill_contract=lambda _: {
            "all_fields": skill["_outputs"],
            "required_fields": ["detail"],
        },
    )
    skills = SkillRegistry(store)
    monkeypatch.setattr(
        RegistryManager, "instance", lambda: SimpleNamespace(db_store=store, skills=skills)
    )
    readiness = validate_workflow_readiness(store, workflow["workflow_id"])
    assert readiness["valid"], readiness
    state = step_run_state(
        workflow["steps"][0], *workflow["steps"][1:], account_id=DEFAULT_ACCOUNT_ID, task_summary=""
    )
    state.update(prime({**workflow, "execution_graph": workflow["settings"]["execution_graph"]}))
    state.update(control_flow_step_node(state))
    assert state["agent_messages"][-1]["artifacts"]["_outcome"] == {"name": "false"}
    transition = orchestrator_hub_node(state)
    state.update(transition.update)
    assert state["current_step_id"] == "diagnostic"

    class DiagnosticAgent:
        skill_registry = skills

        def execute(self, _input):
            return AgentOutput(
                status="pass",
                artifacts={
                    "summary": "unrelated diagnostic",
                    "detail": "Declared diagnostic result",
                },
                summary="Diagnosed",
            )

    manifest = SimpleNamespace(
        agent_id="unrelated",
        display_name="Unrelated",
        skills=["diagnose"],
        config={},
        timeout_minutes=0,
        outputs=[],
    )
    state.update(wrap_agent_as_node(DiagnosticAgent(), manifest)(state))
    assert state["agent_messages"][-1]["status"] == "pass"
    assert state["agent_messages"][-1]["artifacts"]["summary"] == "unrelated diagnostic"
    from core.application.orchestration.output_contract_receipt import (
        OutputProducerContext,
        verified_output_contract,
    )

    context = OutputProducerContext.for_state(state)
    receipt_contract = verified_output_contract(state["agent_messages"][-1], context)
    assert receipt_contract is not None
    assert [row["name"] for row in receipt_contract.rows] == ["detail"]
    result = WorkflowInterfaceService().validate_outputs(
        interface(required=False), state, producer_context=context
    )
    assert result.ok
    assert result.outputs == {}
    declared_interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "trigger_channels": ["ui"],
                "expected_outputs": [{"name": "detail", "type": "string", "required": True}],
            }
        }
    )
    declared = WorkflowInterfaceService().validate_outputs(
        declared_interface, state, producer_context=context
    )
    assert declared.ok
    assert declared.outputs == {"detail": "Declared diagnostic result"}
