"""Captured C1 prompt/schema/validation remains C1 during a live registry mutation."""

from __future__ import annotations

import copy
import json

import pytest
from langchain_core.messages import AIMessage

from core.application.orchestration.graph.nodes import load_agent_implementation, wrap_agent_as_node
from core.application.orchestration.output_contract_receipt import OutputProducerContext
from core.application.registry.workflow_contract_service import collect_runtime_outputs
from tests._step_run_state import step_run_state
from tests.unit.test_agent_instruction_transport import (
    BASE_TEXT,
    SYSTEM_TEXT,
)
from tests.unit.test_agent_instruction_transport import (
    authored_runtime as authored_runtime,
)
from tests.unit.test_workflow_output_provenance import interface


def test_direct_prompt_agent_without_workflow_capture_keeps_selected_skill_contract(
    authored_runtime,
):
    runtime = authored_runtime
    runtime.store.save_skill(
        {
            **runtime.skill_payload,
            "expected_outputs": [{"name": "summary", "type": "text", "required": True}],
        }
    )
    runtime.registry.reload_all()
    runtime.model.responses = [
        AIMessage(
            content=json.dumps(
                {
                    "schema_version": "phase_result_v2",
                    "status": "pass",
                    "summary": "Completed",
                    "artifacts": {"summary": "direct result"},
                }
            )
        )
    ]
    output = runtime.run()
    assert output.status == "pass", output
    assert output.artifacts["summary"] == "direct result"
    rendered = "\n".join(message.content for message in runtime.model.calls[0])
    assert SYSTEM_TEXT in rendered and BASE_TEXT in rendered
    assert '"summary"' in rendered
    assert "output_contract" not in output.metadata


def test_actual_prompt_schema_and_both_validators_use_captured_contract(
    authored_runtime, monkeypatch
):
    runtime = authored_runtime
    c1 = {
        **runtime.skill_payload,
        "expected_outputs": [
            {
                "name": "summary",
                "type": "text",
                "required": True,
                "description": "C1 authored output guidance",
            }
        ],
    }
    runtime.store.save_skill(c1)
    runtime.registry.reload_all()
    manifest = runtime.registry.agents.get_agent_or_raise(runtime.agent_id)
    agent = load_agent_implementation(manifest, runtime.registry.tools, runtime.registry.skills)
    schemas = []
    original_schema = agent._resolve_final_response_schema

    def schema(*args, **kwargs):
        value = original_schema(*args, **kwargs)
        schemas.append(copy.deepcopy(value))
        return value

    monkeypatch.setattr(agent, "_resolve_final_response_schema", schema)
    original_invoke = runtime.model.invoke

    def mutate(messages, **kwargs):
        runtime.store.save_skill(
            {
                **c1,
                "expected_outputs": [
                    {
                        "name": "changed",
                        "type": "number",
                        "required": True,
                        "description": "C2 replacement guidance",
                    }
                ],
            }
        )
        runtime.registry.reload_all()
        runtime.model.responses = [
            AIMessage(
                content=json.dumps(
                    {
                        "schema_version": "phase_result_v2",
                        "status": "pass",
                        "summary": "Finished",
                        "artifacts": {"summary": "C1 value"},
                    }
                )
            )
        ]
        return original_invoke(messages, **kwargs)

    monkeypatch.setattr(runtime.model, "invoke", mutate)
    step = {
        "step_id": "write",
        "step_kind": "agent",
        "agent_id": runtime.agent_id,
        "skill_id": runtime.skill_id,
    }
    state = step_run_state(step, **runtime.state)
    wrapped = wrap_agent_as_node(agent, manifest)
    first = wrapped(state)["agent_messages"][-1]
    assert first["status"] == "pass", first
    assert [r["name"] for r in first["output_contract"]["declarations"]] == ["summary"]
    assert "summary" in json.dumps(schemas[0])
    assert '"changed"' not in json.dumps(schemas[0])
    rendered = "\n".join(m.content for m in runtime.model.calls[0])
    assert SYSTEM_TEXT in rendered and BASE_TEXT in rendered
    assert "## Input Handling" in rendered
    assert '"summary"' in rendered
    assert '"changed"' not in rendered
    assert "C1 authored output guidance" in rendered
    assert "C2 replacement guidance" not in rendered
    declaration = first["output_contract"]["declarations"][0]
    assert declaration["description"] == "C1 authored output guidance"
    assert (
        schemas[0]["properties"]["artifacts"]["properties"]["summary"]["description"]
        == "C1 authored output guidance"
    )
    context = OutputProducerContext.for_state(state)
    assert collect_runtime_outputs(
        {**state, "agent_messages": [first]}, interface=interface(), producer_context=context
    )[0] == {"summary": "C1 value"}
    # Fresh invocation sees C2 and rejects the former C1 response in the real PromptAgent.
    second = wrapped(state)["agent_messages"][-1]
    assert second["status"] == "fail", second
    assert second["output_contract"] is None
    assert '"changed"' in json.dumps(schemas[-1])


def test_actual_step_reexecution_captures_fresh_current_contract(authored_runtime, monkeypatch):
    from types import SimpleNamespace

    from core.application.orchestration import step_replay_runtime
    from core.application.workflows.workflow_definition_snapshot import definition_digest
    from tests._step_run_state import pinned_definition

    runtime = authored_runtime
    step = {
        "step_id": "write",
        "step_kind": "agent",
        "agent_id": runtime.agent_id,
        "skill_id": runtime.skill_id,
    }
    definition = pinned_definition(step)
    row = {
        "id": "replay-snapshot",
        "revision": 1,
        "digest": definition_digest(definition),
        "definition_jsonb": definition,
    }

    class Repository:
        def __init__(self, _conn):
            pass

        def load_for_job(self, job_id):
            assert job_id == "42"
            return row

    monkeypatch.setattr(
        "core.application.jobs.workflow_definition_snapshot_repository.WorkflowDefinitionSnapshotRepository",
        Repository,
    )
    digests = []
    execution_ids = []
    for name, value, typ in [("summary", "C1", "text"), ("changed", 2, "number")]:
        runtime.store.save_skill(
            {
                **runtime.skill_payload,
                "expected_outputs": [{"name": name, "type": typ, "required": True}],
            }
        )
        runtime.registry.reload_all()
        runtime.model.responses = [
            AIMessage(
                content=json.dumps(
                    {
                        "schema_version": "phase_result_v2",
                        "status": "pass",
                        "summary": "Reexecuted",
                        "artifacts": {name: value},
                    }
                )
            )
        ]
        outcome = step_replay_runtime.invoke_step_reexecute_runtime(
            bl=SimpleNamespace(conn="archive", get_agent_messages=lambda *_a, **_k: []),
            job={"id": "42", "selected_workflow": "test_workflow"},
            message={
                "id": f"message-{name}",
                "from_agent": runtime.agent_id,
                "skill": runtime.skill_id,
                "step_id": "write",
            },
            trace_payload={},
            project_config={
                **runtime.state["project_config"],
                "account_id": runtime.state["account_id"],
            },
            policy_snapshot_hash=lambda _: "",
            to_json_obj=lambda v: v,
            serialize_agent_message_row=lambda v: v,
        )
        message = outcome["replay_message"]
        assert message["status"] == "pass", message
        receipt = message["output_contract"]
        assert [r["name"] for r in receipt["declarations"]] == [name]
        assert receipt["definition_digest"] == row["digest"]
        assert receipt["step_execution_id"] == message["step_execution_id"]
        digests.append(receipt["contract_digest"])
        execution_ids.append(receipt["step_execution_id"])
    assert digests[0] != digests[1]
    assert execution_ids[0] != execution_ids[1]


@pytest.mark.parametrize("precise", [False, True])
@pytest.mark.parametrize(
    ("kind", "value"), [("json", "generic scalar"), ("list", [1, {"flag": False}])]
)
def test_workflow_capture_preserves_generic_json_scalar_and_rejects_precise_object(
    authored_runtime, monkeypatch, request, precise, kind, value
):
    runtime = authored_runtime
    field = f"shape_{runtime.skill_id}"
    output_row = {
        "name": field,
        "type": kind,
        "required": True,
        "description": "Authored output details",
    }
    if precise:
        contract_id = f"manual.{runtime.skill_id}"
        runtime.store.save_data_contract(
            {
                "contract_id": contract_id,
                "kind": kind,
                "schema": {"type": "object"}
                if kind == "json"
                else {"type": "array", "items": {"type": "string"}},
                "projection_mode": "manual",
                "enabled": True,
            }
        )
        request.addfinalizer(
            lambda: runtime.conn.execute(
                "DELETE FROM registry_data_contracts WHERE contract_id = %s", (contract_id,)
            )
        )
        output_row["contract_id"] = contract_id
    runtime.store.save_skill({**runtime.skill_payload, "expected_outputs": [output_row]})
    runtime.registry.reload_all()
    response = AIMessage(
        content=json.dumps(
            {
                "schema_version": "phase_result_v2",
                "status": "pass",
                "summary": "Completed",
                "artifacts": {field: value},
            }
        )
    )
    runtime.model.responses = [response]
    legacy = runtime.run()
    assert legacy.status == "pass", legacy
    manifest = runtime.registry.agents.get_agent_or_raise(runtime.agent_id)
    agent = load_agent_implementation(manifest, runtime.registry.tools, runtime.registry.skills)
    captured = []
    schema_builder = agent._resolve_final_response_schema

    def response_schema(*args, **kwargs):
        schema = schema_builder(*args, **kwargs)
        captured.append(schema)
        return schema

    monkeypatch.setattr(agent, "_resolve_final_response_schema", response_schema)
    state = step_run_state(
        {
            "step_id": "write",
            "step_kind": "agent",
            "agent_id": runtime.agent_id,
            "skill_id": runtime.skill_id,
        },
        **runtime.state,
    )
    runtime.model.responses = [response]
    message = wrap_agent_as_node(agent, manifest)(state)["agent_messages"][-1]
    response_field = captured[0]["properties"]["artifacts"]["properties"][field]
    expected_response = (
        ({"type": "object"} if precise else {"type": "object", "additionalProperties": True})
        if kind == "json"
        else {"type": "array", "items": {"type": "string"}}
    )
    assert response_field == {**expected_response, "description": "Authored output details"}
    assert message["status"] == ("fail" if precise else "pass"), message
    if precise:
        assert (
            f"Invalid type or value for output '{field}'"
            in message["artifacts"]["_contract_errors"]
        )
        assert message["output_contract"] is None
    else:
        assert message["artifacts"][field] == value
        assert message["output_contract"] is not None


def test_linked_authored_schema_is_captured_before_mutation_and_refreshed_next_attempt(
    authored_runtime, monkeypatch, request
):
    runtime = authored_runtime
    field = f"linked_{runtime.skill_id}"
    contract_id = f"manual.{runtime.skill_id}"
    schema1 = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    schema2 = {"type": "object", "properties": {"n": {"type": "string"}}, "required": ["n"]}
    payload = {
        "contract_id": contract_id,
        "kind": "json",
        "schema": schema1,
        "projection_mode": "manual",
        "enabled": True,
    }
    runtime.store.save_data_contract(payload)
    request.addfinalizer(
        lambda: runtime.conn.execute(
            "DELETE FROM registry_data_contracts WHERE contract_id = %s", (contract_id,)
        )
    )
    runtime.store.save_skill(
        {
            **runtime.skill_payload,
            "expected_outputs": [
                {
                    "name": field,
                    "type": "json",
                    "required": True,
                    "contract_id": contract_id,
                    "description": "Linked authored details",
                }
            ],
        }
    )
    runtime.registry.reload_all()
    manifest = runtime.registry.agents.get_agent_or_raise(runtime.agent_id)
    agent = load_agent_implementation(manifest, runtime.registry.tools, runtime.registry.skills)
    original = runtime.model.invoke

    def mutate(messages, **kwargs):
        runtime.store.save_data_contract({**payload, "schema": schema2})
        runtime.model.responses = [
            AIMessage(
                content=json.dumps(
                    {
                        "schema_version": "phase_result_v2",
                        "status": "pass",
                        "summary": "Produced",
                        "artifacts": {field: {"n": 0}},
                    }
                )
            )
        ]
        return original(messages, **kwargs)

    monkeypatch.setattr(runtime.model, "invoke", mutate)
    state = step_run_state(
        {
            "step_id": "write",
            "step_kind": "agent",
            "agent_id": runtime.agent_id,
            "skill_id": runtime.skill_id,
        },
        **runtime.state,
    )
    wrapped = wrap_agent_as_node(agent, manifest)
    first = wrapped(state)["agent_messages"][-1]
    assert first["status"] == "pass", first
    row = first["output_contract"]["declarations"][0]
    assert row["contract_schema"] == schema1
    assert row["response_schema"] == {**schema1, "description": "Linked authored details"}
    second = wrapped(state)["agent_messages"][-1]
    assert second["status"] == "fail", second
    assert second["output_contract"] is None
    assert f"Invalid type or value for output '{field}'" in second["errors"]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": ["string", "number"]},
        {"type": ["number", "string"]},
        {"anyOf": [{"type": "string", "minLength": 2}, {"type": "number", "minimum": 0}]},
    ],
    ids=["union", "reversed", "composition"],
)
@pytest.mark.parametrize("value", ["authored", 0, False], ids=["string", "number", "invalid"])
def test_actual_prompt_and_wrapper_preserve_schema_only_alternatives(
    authored_runtime, monkeypatch, schema, value
):
    runtime = authored_runtime
    store = runtime.registry.db_store
    # The supported SkillManifest read boundary permits schema-only declarations.
    # Supply that registry representation without mocking authorization/capture/checks.
    row = store.get_skill(runtime.skill_id)
    row.update(
        _outputs=[],
        output_schema={"type": "object", "properties": {"result": schema}, "required": ["result"]},
    )
    original_get = store.get_skill
    monkeypatch.setattr(
        store,
        "get_skill",
        lambda key: copy.deepcopy(row) if key == runtime.skill_id else original_get(key),
    )
    monkeypatch.setattr(store, "list_published_block_contracts", lambda **_kwargs: [])
    manifest = runtime.registry.agents.get_agent_or_raise(runtime.agent_id)
    agent = load_agent_implementation(manifest, runtime.registry.tools, runtime.registry.skills)
    schemas = []
    original_schema = agent._resolve_final_response_schema

    def response_schema(*args, **kwargs):
        result = original_schema(*args, **kwargs)
        schemas.append(copy.deepcopy(result))
        return result

    monkeypatch.setattr(agent, "_resolve_final_response_schema", response_schema)
    runtime.model.responses = [
        AIMessage(
            content=json.dumps(
                {
                    "schema_version": "phase_result_v2",
                    "status": "pass",
                    "summary": "Produced",
                    "artifacts": {"result": value},
                }
            )
        )
    ]
    state = step_run_state(
        {
            "step_id": "write",
            "step_kind": "agent",
            "agent_id": runtime.agent_id,
            "skill_id": runtime.skill_id,
        },
        **runtime.state,
    )
    message = wrap_agent_as_node(agent, manifest)(state)["agent_messages"][-1]
    assert runtime.model.calls, message
    assert schemas[0]["properties"]["artifacts"]["properties"]["result"] == schema
    assert message["status"] == ("fail" if value is False else "pass"), message
    if value is False:
        assert "Invalid type or value for output 'result'" in message["errors"]
        assert message["output_contract"] is None
    else:
        declaration = message["output_contract"]["declarations"][0]
        assert declaration["contract_schema"] == declaration["response_schema"] == schema
        assert message["artifacts"]["result"] == value
