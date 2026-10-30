"""Persisted instructions must reach the real formatted model invocation boundary."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.orchestration.agents.base import AgentInput
from core.application.orchestration.graph.nodes import load_agent_implementation
from core.application.registry.registry_manager import RegistryManager

SYSTEM_TEXT = 'Authored system Привет ✓ {literal_brace} {"quantity": 0}'
BASE_TEXT = "Assigned skill azure apricot ✓ {skill_literal}"
JUNCTION_TEXT = "Agent guidance amber pear ✓"


class RecordingModel:
    """Fake provider transport; prompt formatting and the invocation loop stay real."""

    def __init__(self):
        self.calls = []
        self.responses = []
        self.bound_tools = []

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages, **_kwargs):
        self.calls.append(copy.deepcopy(messages))
        if self.responses:
            return self.responses.pop(0)
        return AIMessage(
            content=json.dumps(
                {
                    "schema_version": "phase_result_v2",
                    "status": "pass",
                    "summary": "Completed summary.",
                    "artifacts": {},
                }
            )
        )


@pytest.fixture
def authored_runtime(audit_db, monkeypatch):
    store, conn, _workflow_id, project_id = audit_db
    suffix = uuid4().hex
    skill_id = f"summary_{suffix}"
    agent_id = f"writer_{suffix}"
    skill_payload = {
        "skill_id": skill_id,
        "display_name": "Summarize report",
        "enabled": True,
        "skill_type": "base",
        "instructions": [BASE_TEXT, "Keep the source order."],
        "expected_inputs": [],
        "expected_outputs": [],
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    store.save_skill(skill_payload)
    store.save_agent(
        {
            "agent_id": agent_id,
            "display_name": "Report writer",
            "category": "ai",
            "enabled": True,
            "skills": [skill_id],
            "tools": [],
            "prompts": {
                "system": SYSTEM_TEXT,
                "skill_prompts": {skill_id: JUNCTION_TEXT},
                "examples": {skill_id: [{"input": "brief", "output": "short summary"}]},
            },
            "config": {"max_iterations": 3, "timeout_minutes": 0},
            "account_id": DEFAULT_ACCOUNT_ID,
        }
    )
    registry = RegistryManager(store.dsn, sync_builtins=False)
    model = RecordingModel()
    monkeypatch.setattr(
        "core.application.orchestration.agents.common.get_chat_model", lambda *_a, **_k: model
    )
    monkeypatch.setattr(
        "core.application.orchestration.agents.common.emit_event", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "core.application.orchestration.agents.prompt_agent.emit_event", lambda *_a, **_k: None
    )
    state = {
        "account_id": DEFAULT_ACCOUNT_ID,
        "project_id": project_id,
        "project_name": "Report project",
        "task_summary": "Summarize the report",
        "project_rules_context": {"rules_excerpt": "Preserve report evidence."},
        "project_config": {
            "project_rules": "Preserve report evidence.",
            "credentials": {"access_token": "private-fixture-value"},
        },
    }

    def run(selected=None, *, agent_id_override=None, step=None):
        manifest = registry.agents.get_agent_or_raise(agent_id_override or agent_id)
        agent = load_agent_implementation(manifest, registry.tools, registry.skills)
        monkeypatch.setattr(agent, "_log_prompt_request", lambda *_a, **_k: None)
        monkeypatch.setattr(agent, "_log_prompt_response", lambda *_a, **_k: None)
        return agent.execute(
            AgentInput(
                task_context={},
                step_context={"_step": step} if step else {},
                agent_config={},
                skill=selected or skill_id,
                state_ref=state,
            )
        )

    try:
        with RegistryManager.thread_local_instance(registry):
            yield SimpleNamespace(
                store=store,
                conn=conn,
                registry=registry,
                model=model,
                state=state,
                skill_id=skill_id,
                agent_id=agent_id,
                skill_payload=skill_payload,
                run=run,
            )
    finally:
        conn.execute("DELETE FROM registry_agents WHERE agent_id = %s", (agent_id,))
        conn.execute("DELETE FROM registry_skills WHERE skill_id = %s", (skill_id,))


def test_persisted_system_prompt_reaches_formatted_model_messages(authored_runtime):
    runtime = authored_runtime
    runtime.run()
    assert runtime.model.calls, "The actual model invocation must be captured"
    system = runtime.model.calls[0][0]
    assert system.type == "system"
    assert SYSTEM_TEXT in system.content
    assert system.content.count(SYSTEM_TEXT) == 1
    assert "## Input Handling" in system.content
    rendered = json.dumps([m.content for m in runtime.model.calls[0]], ensure_ascii=False)
    assert "Preserve report evidence." in rendered
    assert "private-fixture-value" not in rendered


def test_persisted_skill_instructions_reach_formatted_model_messages(authored_runtime):
    runtime = authored_runtime
    runtime.run()
    human = next(message for message in runtime.model.calls[0] if message.type == "human")
    assert BASE_TEXT in human.content
    assert human.content.index(BASE_TEXT) < human.content.index(JUNCTION_TEXT)
    assert "short summary" in human.content
    assert "phase_result_v2" in human.content


def test_denied_selected_skills_stop_before_model_transport(authored_runtime):
    from core.application.management.errors import ServiceScopeNotFoundError

    runtime = authored_runtime
    original_state = dict(runtime.state)
    cases = {
        "unassigned": {},
        "disabled": {"enabled": False},
        "deleted": {},
        "foreign_account": {},
        "foreign_project": {"project_id": str(runtime.state["project_id"])},
        "missing_account": {},
        "missing_project": {"project_id": str(runtime.state["project_id"])},
        "blocked": {"new_runs_blocked": True},
        "unavailable": {"availability_status": "unavailable"},
    }
    observations = {}
    for case, overrides in cases.items():
        runtime.store.save_skill({**runtime.skill_payload, **overrides})
        if case == "deleted":
            runtime.conn.execute(
                "UPDATE registry_skills SET deleted_at = now() WHERE skill_id = %s",
                (runtime.skill_id,),
            )
        runtime.registry.reload_all()
        runtime.state.clear()
        runtime.state.update(original_state)
        if case == "foreign_account":
            runtime.state["account_id"] = str(uuid4())
        elif case == "foreign_project":
            runtime.state["project_id"] = str(uuid4())
        elif case == "missing_account":
            runtime.state.pop("account_id")
        elif case == "missing_project":
            runtime.state.pop("project_id")
        runtime.model.calls.clear()
        try:
            runtime.run("unassigned_summary" if case == "unassigned" else None)
        except ServiceScopeNotFoundError as exc:
            observations[case] = (str(exc), len(runtime.model.calls))
        else:
            observations[case] = ("continued", len(runtime.model.calls))
    assert observations == dict.fromkeys(
        cases, ("Selected skill is unavailable in the current scope.", 0)
    )


def test_hidden_assigned_skill_retains_instructions(authored_runtime):
    from core.application.workflows.discovery.scoped_capability_catalog import (
        ScopedCapabilityCatalog,
        ScopedCapabilityRef,
    )

    runtime = authored_runtime
    runtime.store.save_skill({**runtime.skill_payload, "visible_in_catalog": False})
    runtime.registry.reload_all()
    result = runtime.run()
    assert result.status == "pass"
    human = next(message for message in runtime.model.calls[0] if message.type == "human")
    assert BASE_TEXT in human.content
    catalog = ScopedCapabilityCatalog(runtime.store)
    project_id = str(runtime.state["project_id"])
    capability = ScopedCapabilityRef("skill", runtime.skill_id)
    with pytest.raises(ValueError, match="not visible"):
        catalog.assert_capability_visible(runtime.state, project_id, capability)


def test_normal_refresh_delivers_updated_skill_instructions(authored_runtime):
    runtime = authored_runtime
    updated = "Updated skill violet plum ✓ {new_literal}"
    runtime.store.save_skill({**runtime.skill_payload, "instructions": [updated]})
    runtime.registry.reload_all()
    result = runtime.run()
    assert result.status == "pass"
    human = next(message for message in runtime.model.calls[0] if message.type == "human")
    assert updated in human.content
    assert BASE_TEXT not in human.content


def test_selected_secondary_skill_uses_its_own_instructions(authored_runtime):
    runtime = authored_runtime
    secondary_id = f"secondary_{uuid4().hex}"
    marker = "Secondary instructions copper peach ✓"
    try:
        runtime.store.save_skill(
            {
                **runtime.skill_payload,
                "skill_id": secondary_id,
                "skill_type": "secondary",
                "instructions": [marker],
            }
        )
        agent_row = runtime.store.get_agent(runtime.agent_id)
        runtime.store.save_agent(
            {
                **agent_row,
                "skills": [runtime.skill_id, secondary_id],
                "prompts": {
                    **agent_row["prompts"],
                    "skill_prompts": {
                        runtime.skill_id: JUNCTION_TEXT,
                        secondary_id: "Secondary agent guidance",
                    },
                },
            }
        )
        runtime.registry.reload_all()
        result = runtime.run(secondary_id)
        assert result.status == "pass"
        human = next(message for message in runtime.model.calls[0] if message.type == "human")
        assert marker in human.content
        assert "Secondary agent guidance" in human.content
        assert BASE_TEXT not in human.content
        assert JUNCTION_TEXT not in human.content
    finally:
        runtime.conn.execute(
            "DELETE FROM registry_agent_skills WHERE skill_id = %s", (secondary_id,)
        )
        runtime.conn.execute("DELETE FROM registry_skills WHERE skill_id = %s", (secondary_id,))


def test_instruction_payload_survives_actual_tool_replay(authored_runtime, monkeypatch):
    from langchain_core.tools import tool

    runtime = authored_runtime
    actual_calls = []

    @tool
    def echo_values(quantity: int, approved: bool, note: str) -> str:
        """Echo typed report values without changing their representation."""
        payload = {"quantity": quantity, "approved": approved, "note": note}
        actual_calls.append(payload)
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(
        runtime.registry.tool_resolver,
        "resolve",
        lambda ids: [echo_values] if "echo_values" in ids else [],
    )
    runtime.model.responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "echo_values",
                    "args": {"quantity": 0, "approved": False, "note": "Привет ✓"},
                    "id": "echo-call",
                    "type": "tool_call",
                }
            ],
        )
    ]
    result = runtime.run(step={"override": {"add_tools": ["echo_values"]}})
    assert result.status == "pass"
    assert actual_calls == [{"quantity": 0, "approved": False, "note": "Привет ✓"}]
    assert len(runtime.model.calls) == 2
    replay = runtime.model.calls[1]
    tool_message = next(message for message in replay if message.type == "tool")
    assert json.loads(tool_message.content) == actual_calls[0]
    for messages in runtime.model.calls:
        system = messages[0].content
        human = next(message.content for message in messages if message.type == "human")
        assert system.count(SYSTEM_TEXT) == 1
        assert human.count(BASE_TEXT) == 1
        assert human.count(JUNCTION_TEXT) == 1


def test_visible_projected_skill_delivers_instructions_in_its_scope(authored_runtime):
    from core.application.registry.agent_registry import AgentRegistry
    from core.application.registry.skill_registry import SkillRegistry

    runtime = authored_runtime
    marker = "Projected instructions jade apple ✓"
    projections = [
        {
            "install_id": "sample-install",
            "artifact_id": "summarize",
            "artifact_kind": "skill",
            "projection": {
                "skill_id": "summarize",
                "display_name": "Summarize",
                "instructions": [marker],
                "enabled": True,
            },
        },
        {
            "install_id": "sample-install",
            "artifact_id": "writer",
            "artifact_kind": "agent",
            "projection": {
                "agent_id": "writer",
                "display_name": "Writer",
                "enabled": True,
                "skills": [{"skill_id": "summarize", "skill_prompt": JUNCTION_TEXT}],
                "prompts": {"system": SYSTEM_TEXT},
            },
        },
    ]
    store = SimpleNamespace(
        list_skills=list,
        list_agents=list,
        get_skill=lambda _identifier: None,
        extension_runtime_store=SimpleNamespace(
            list_projections=lambda *, account_id, **_kwargs: (
                projections if account_id == DEFAULT_ACCOUNT_ID else []
            )
        ),
    )
    runtime.registry.skills = SkillRegistry(store)
    runtime.registry.agents = AgentRegistry(store)
    result = runtime.run(
        "extension.sample-install.summarize", agent_id_override="extension.sample-install.writer"
    )
    assert result.status == "pass"
    human = next(message for message in runtime.model.calls[0] if message.type == "human")
    assert marker in human.content
    assert JUNCTION_TEXT in human.content


@pytest.mark.parametrize("owner", ["parallel", "reexecute"])
def test_execution_owners_pass_scoped_registry_to_real_factory(
    authored_runtime, monkeypatch, owner
):
    """Exercise each dependency owner and its real factory/agent/model transport."""
    from core.application.orchestration import graph, parallel_executor_node, step_replay_runtime

    runtime = authored_runtime
    created = []

    def wrap(agent, manifest):
        # Keep owner/factory/execution real; isolate cursor and message bookkeeping.
        created.append(agent)

        def execute(state):
            result = agent.execute(
                AgentInput(
                    task_context={},
                    step_context={},
                    agent_config={},
                    skill=runtime.skill_id,
                    state_ref=state,
                )
            )
            return {
                "agent_messages": [
                    {
                        "status": result.status,
                        "summary": result.summary,
                        "artifacts": result.artifacts,
                    }
                ]
            }

        return execute

    monkeypatch.setattr(graph.nodes, "wrap_agent_as_node", wrap)
    if owner == "parallel":
        monkeypatch.setattr(parallel_executor_node, "_AGENT_NODE_CACHE", {})
        node = parallel_executor_node._resolve_agent_node(runtime.agent_id)
        assert node is not None
        node(runtime.state)
    else:
        monkeypatch.setattr(step_replay_runtime, "_replay_definition_state", lambda *_a, **_k: {})
        step_replay_runtime.invoke_step_reexecute_runtime(
            bl=SimpleNamespace(get_agent_messages=lambda *_a, **_k: []),
            job={"id": "42", "selected_workflow": "report"},
            message={
                "id": "m-1",
                "from_agent": runtime.agent_id,
                "skill": runtime.skill_id,
                "step_id": "draft",
            },
            trace_payload={},
            project_config={
                **runtime.state["project_config"],
                "account_id": DEFAULT_ACCOUNT_ID,
                "project_id": str(runtime.state["project_id"]),
            },
            policy_snapshot_hash=lambda _value: "",
            to_json_obj=lambda value: value,
            serialize_agent_message_row=lambda row: row,
        )
    assert len(created) == 1
    assert created[0].skill_registry is runtime.registry.skills
    assert SYSTEM_TEXT in runtime.model.calls[0][0].content
    human = next(message for message in runtime.model.calls[0] if message.type == "human")
    assert BASE_TEXT in human.content


def test_nested_runtime_scope_requires_an_explicit_mapping(authored_runtime):
    from types import MappingProxyType

    from core.application.management.errors import ServiceScopeNotFoundError

    runtime = authored_runtime
    observations = {}
    for name, scope in {
        "mapping": MappingProxyType({"account_id": DEFAULT_ACCOUNT_ID}),
        "scalar": "invalid-scope",
        "empty": {},
    }.items():
        runtime.state["scope_context"] = scope
        if name == "mapping":
            runtime.state.pop("account_id", None)
        else:
            runtime.state["account_id"] = DEFAULT_ACCOUNT_ID
        runtime.model.calls.clear()
        try:
            runtime.run()
        except ServiceScopeNotFoundError as exc:
            observations[name] = (str(exc), len(runtime.model.calls))
        else:
            human = next(m for m in runtime.model.calls[0] if m.type == "human")
            observations[name] = (BASE_TEXT in human.content, len(runtime.model.calls))
    assert observations == {
        "mapping": (True, 1),
        "scalar": ("Selected skill is unavailable in the current scope.", 0),
        "empty": ("Selected skill is unavailable in the current scope.", 0),
    }
