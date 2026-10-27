"""Actual wrapper publication, receipt recovery, egress and collection precedence."""

from __future__ import annotations

import copy
from itertools import combinations
from types import SimpleNamespace

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.orchestration.agents.base import AgentOutput
from core.application.orchestration.graph.nodes import wrap_agent_as_node
from core.application.orchestration.output_contract_receipt import OutputProducerContext
from core.application.registry.registry_manager import RegistryManager
from core.application.registry.skill_registry import SkillRegistry
from core.application.registry.workflow_contract_service import (
    WorkflowInterfaceService,
    collect_runtime_outputs,
)
from tests._step_run_state import step_run_state
from tests.unit.test_step_output_contract import ContractStore
from tests.unit.test_workflow_output_provenance import interface


@pytest.fixture
def producer(monkeypatch):
    store = ContractStore()
    store.skill.update(
        account_id=DEFAULT_ACCOUNT_ID,
        input_schema={"type": "object", "properties": {}, "required": []},
    )
    skills = SkillRegistry(store)
    registry = SimpleNamespace(db_store=store, skills=skills)
    monkeypatch.setattr(RegistryManager, "instance", lambda: registry)
    manifest = SimpleNamespace(
        agent_id="writer",
        display_name="Writer",
        skills=["selected"],
        config={},
        timeout_minutes=0,
        outputs=[],
    )
    artifacts = {"summary": "declared result"}
    status = {"value": "pass"}
    metadata = {}
    inputs = []

    class Agent:
        skill_registry = skills

        def execute(self, agent_input):
            inputs.append(agent_input)
            return AgentOutput(
                status=status["value"],
                artifacts=copy.deepcopy(artifacts),
                summary="Produced",
                metadata=copy.deepcopy(metadata),
            )

    state = step_run_state(
        {"step_id": "write", "step_kind": "agent", "agent_id": "writer", "skill_id": "selected"},
        {"step_id": "other", "step_kind": "agent", "agent_id": "writer", "skill_id": "selected"},
        account_id=DEFAULT_ACCOUNT_ID,
    )
    wrapped = wrap_agent_as_node(Agent(), manifest)
    return SimpleNamespace(
        store=store,
        manifest=manifest,
        state=state,
        wrapped=wrapped,
        artifacts=artifacts,
        status=status,
        metadata=metadata,
        inputs=inputs,
    )


def published(producer):
    state = {**producer.state, **producer.wrapped(producer.state)}
    message = state["agent_messages"][-1]
    assert message["status"] == "pass", message
    return state, message, OutputProducerContext.for_state(state)


def test_legacy_wrapper_without_pinned_definition_keeps_existing_contract_path(
    producer, monkeypatch
):
    from core.application.orchestration import output_contract_receipt

    def unexpected_capture(*_args, **_kwargs):
        raise AssertionError("Legacy invocation must not capture workflow authority")

    monkeypatch.setattr(
        output_contract_receipt, "capture_agent_output_contract", unexpected_capture
    )
    result = producer.wrapped({"account_id": DEFAULT_ACCOUNT_ID, "agent_messages": []})
    message = result["agent_messages"][-1]
    assert message["status"] == "pass", message
    assert message["artifacts"]["summary"] == "declared result"
    assert message["output_contract"] is None
    assert producer.inputs[-1].output_contract is None
    assert producer.inputs[-1].selected_skill is None


def test_real_wrapper_publishes_unmapped_selected_skill_result(producer):
    state, message, context = published(producer)
    assert "outputs" not in state
    assert message["output_contract"]["declarations"][0]["name"] == "summary"
    result = WorkflowInterfaceService().validate_outputs(
        interface(), state, producer_context=context
    )
    assert result.ok
    assert result.outputs == {"summary": "declared result"}


@pytest.mark.parametrize("source", ["own", "legacy"])
def test_real_wrapper_publishes_true_agent_own_contract(producer, source):
    producer.store.skill["_outputs"] = []
    if source == "own":
        producer.store.own = [{"name": "summary", "type": "text", "required": False}]
    else:
        producer.manifest.outputs = ["summary"]
    state, _, context = published(producer)
    assert collect_runtime_outputs(state, interface=interface(), producer_context=context)[0] == {
        "summary": "declared result"
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("definition_digest", "changed"),
        ("snapshot_id", "another-snapshot"),
        ("job_id", "another-job"),
        ("step_id", "other"),
        ("step_execution_id", "00000000-0000-4000-8000-000000000009"),
        ("attempt", 2),
        ("from_agent", "different-agent"),
        ("skill", "other-skill"),
        ("contract_digest", "changed"),
        ("schema_version", "unknown"),
    ],
)
def test_receipt_identity_mismatch_never_grants_direct_authority(producer, key, value):
    state, message, context = published(producer)
    message["output_contract"][key] = value
    assert collect_runtime_outputs(state, interface=interface(), producer_context=context) == (
        {},
        {},
    )


@pytest.mark.parametrize(
    "variant",
    [
        "absent",
        "malformed",
        "spoof-artifact",
        "failed",
        "no-context",
        "unknown-step",
        "borrowed-step",
        "static-agent",
        "changed-declaration",
    ],
)
def test_missing_or_spoofed_authority_is_not_a_result(producer, variant):
    state, message, context = published(producer)
    if variant == "absent":
        message.pop("output_contract")
    elif variant == "malformed":
        message["output_contract"] = "not a receipt"
    elif variant == "spoof-artifact":
        message["artifacts"]["output_contract"] = message.pop("output_contract")
    elif variant == "failed":
        message["status"] = "fail"
    elif variant == "no-context":
        context = None
    elif variant in {"unknown-step", "borrowed-step"}:
        message["step_id"] = "missing" if variant == "unknown-step" else "other"
    elif variant == "static-agent":
        message["from_agent"] = message["output_contract"]["from_agent"] = "different"
    else:
        message["output_contract"]["declarations"][0]["name"] = "diagnostic"
    result = WorkflowInterfaceService().validate_outputs(
        interface(), state, producer_context=context
    )
    assert not result.ok
    assert result.outputs == {}


def test_failed_validation_publishes_no_receipt(producer):
    producer.artifacts["summary"] = 7
    message = producer.wrapped(producer.state)["agent_messages"][-1]
    assert message["status"] == "fail"
    assert message["output_contract"] is None


def test_contract_lookup_failure_precedes_agent_invocation(producer):
    producer.store.get_agent_own_outputs = lambda _: (_ for _ in ()).throw(
        RuntimeError("lookup failed")
    )
    message = producer.wrapped(producer.state)["agent_messages"][-1]
    assert message["status"] == "fail"
    assert message["output_contract"] is None
    assert producer.inputs == []


def test_runtime_participant_executor_is_allowed(producer):
    producer.state = step_run_state(
        {
            "step_id": "write",
            "step_kind": "agent",
            "participant_key": "author",
            "skill_id": "selected",
        },
        account_id=DEFAULT_ACCOUNT_ID,
        workflow_participants={"author": "writer"},
    )
    state, _, context = published(producer)
    assert collect_runtime_outputs(state, interface=interface(), producer_context=context)[0] == {
        "summary": "declared result"
    }


SOURCES = ["state", "nested", "root", "patch", "direct", "terminal"]


def put(state, message, source, value):
    if source == "state":
        state.setdefault("outputs", {})["summary"] = value
    elif source == "nested":
        state.setdefault("workflow_data", {}).setdefault("outputs", {})["summary"] = value
    elif source == "root":
        state.setdefault("workflow_data", {})["summary"] = value
    elif source == "patch":
        message["artifacts"]["_state_patch"] = {"outputs": {"summary": value}}
    elif source == "direct":
        message["artifacts"]["summary"] = value
    else:
        message["artifacts"]["workflow_terminal"] = {"outputs": {"summary": value}}


@pytest.mark.parametrize("earlier,later", list(combinations(SOURCES, 2)))
@pytest.mark.parametrize("value", [False, 0, None, "", [], {}])
def test_every_precedence_pair_preserves_present_falsy_value(producer, earlier, later, value):
    state, message, context = published(producer)
    message["artifacts"] = {}
    put(state, message, later, "loser")
    put(state, message, earlier, value)
    assert collect_runtime_outputs(state, interface=interface(), producer_context=context)[0] == {
        "summary": value
    }


@pytest.mark.parametrize("fallback", ["patch", "terminal"])
def test_invalid_receipt_preserves_same_message_structured_fallback(producer, fallback):
    state, message, context = published(producer)
    message["output_contract"] = ["invalid"]
    put(state, message, fallback, "explicit")
    assert collect_runtime_outputs(state, interface=interface(), producer_context=context)[0] == {
        "summary": "explicit"
    }


def test_newer_terminal_beats_older_patch_and_invalid_direct_falls_through(producer):
    state, newer, context = published(producer)
    older = copy.deepcopy(newer)
    older["correlation_id"] = "older"
    older["timestamps"]["published_at"] = "2020-01-01T00:00:00+00:00"
    put(state, older, "patch", "older patch")
    newer["output_contract"] = None
    put(state, newer, "terminal", "newer terminal")
    state["agent_messages"] = [older, newer]
    assert collect_runtime_outputs(state, interface=interface(), producer_context=context)[0] == {
        "summary": "newer terminal"
    }
    newer["artifacts"].pop("workflow_terminal")
    assert collect_runtime_outputs(state, interface=interface(), producer_context=context)[0] == {
        "summary": "older patch"
    }


def test_terminal_validation_never_reloads_current_contract(producer, monkeypatch):
    state, _, context = published(producer)
    monkeypatch.setattr(
        RegistryManager,
        "instance",
        lambda: (_ for _ in ()).throw(AssertionError("terminal registry read")),
    )
    assert WorkflowInterfaceService().validate_outputs(
        interface(), state, producer_context=context
    ).outputs == {"summary": "declared result"}


def test_bus_redaction_checkpoint_history_and_public_egress_are_separate(producer, monkeypatch):
    import json

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    from core.application.management.snapshot_serializers import (
        serialize_agent_message_row,
        serialize_job_step_replay_row,
    )
    from core.application.orchestration.agent_messages import get_state_messages
    from core.application.orchestration.message_bus import MessageBus
    from core.application.orchestration.state import OrchestratorState
    from tests.unit.test_message_bus import _FakeLogger

    logger = _FakeLogger()
    bus = MessageBus("test", logger_client=logger)
    monkeypatch.setattr(MessageBus, "for_state", lambda _: bus)
    producer.state["job_id"] = "41"
    state, message, context = published(producer)
    receipt = copy.deepcopy(message["output_contract"])
    redacted = copy.deepcopy(message)
    redacted["output_contract"] = {"redacted": True}
    redacted["artifacts"]["summary"] = "[REDACTED]"
    monkeypatch.setattr(
        "core.infrastructure.security.integration.scan_outbound",
        lambda *_a, **_k: SimpleNamespace(action="redact", redacted_text=json.dumps(redacted)),
    )
    logger.publish_calls.clear()
    assert bus.publish(state["job_id"], message)
    persisted = logger.publish_calls[0][1]
    assert persisted["output_contract"] == receipt
    assert persisted["artifacts"]["summary"] == "[REDACTED]"
    logger.rows = [{"id": "message-1", "envelope": json.dumps(persisted)}]
    monkeypatch.setattr(MessageBus, "for_state", lambda _: bus)
    recovered = bus.list_messages(state["job_id"])[0]
    assert recovered["output_contract"] == receipt
    merged = get_state_messages({**state, "agent_messages": [message]})
    assert len(merged) == 1
    assert merged[0]["output_contract"] == receipt
    graph = StateGraph(OrchestratorState)
    graph.add_node("restore", lambda _: {"agent_messages": [recovered]})
    graph.add_edge(START, "restore")
    graph.add_edge("restore", END)
    compiled = graph.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "receipt-roundtrip"}}
    compiled.invoke(state, config)
    checkpoint = compiled.get_state(config).values
    assert checkpoint["agent_messages"][-1]["output_contract"] == receipt
    assert collect_runtime_outputs(checkpoint, interface=interface(), producer_context=context)[
        0
    ] == {"summary": "[REDACTED]"}
    public = serialize_agent_message_row(
        {"envelope": persisted, "artifacts": persisted["artifacts"]}
    )
    assert "output_contract" not in public["envelope"]
    assert public["envelope"] == {k: v for k, v in persisted.items() if k != "output_contract"}
    outcomes = {
        "original_outcome": {"replay_message": persisted},
        "replay_outcome": {"replay_message": persisted},
    }
    replay = serialize_job_step_replay_row(outcomes)
    for name in outcomes:
        assert "output_contract" not in replay[name]["replay_message"]
    assert persisted["output_contract"] == receipt


@pytest.mark.parametrize("variant", ["empty", "hidden", "tool", "secondary", "mapping"])
def test_real_wrapper_diagnostics_do_not_expand_captured_authority(producer, variant):
    from tests.unit.test_step_output_contract import block

    if variant in {"empty", "hidden"}:
        producer.store.blocks = [
            block(
                account=None,
                outputs=[]
                if variant == "empty"
                else [{"field_key": "summary", "exposed_by_default": False}],
            )
        ]
    else:
        producer.store.skill["_outputs"] = []
        producer.store.own = [
            {"name": "summary", "type": "text", "source": "tool" if variant == "tool" else "skill"}
        ]
        if variant == "mapping":
            producer.manifest.structured_outputs = [{"name": "summary", "type": "text"}]
    state, message, context = published(producer)
    assert message["artifacts"]["summary"] == "declared result"
    assert message["output_contract"]["declarations"] == []
    assert collect_runtime_outputs(state, interface=interface(), producer_context=context)[0] == {}


def test_agent_metadata_cannot_replace_host_receipt(producer):
    # A producer-supplied object is an ordinary diagnostic, never envelope authority.
    producer.artifacts["output_contract"] = {
        "schema_version": "agent_output_contract_v1",
        "declarations": [{"name": "diagnostic"}],
    }
    producer.metadata["output_contract"] = producer.artifacts["output_contract"]
    producer.artifacts["diagnostic"] = "unrelated"
    state, message, context = published(producer)
    assert message["output_contract"] != producer.artifacts["output_contract"]
    assert [r["name"] for r in message["output_contract"]["declarations"]] == ["summary"]
    assert collect_runtime_outputs(state, interface=interface(), producer_context=context)[0] == {
        "summary": "declared result"
    }


def test_failed_actual_producer_cannot_publish_authority(producer):
    producer.status["value"] = "fail"
    message = producer.wrapped(producer.state)["agent_messages"][-1]
    assert message["status"] == "fail"
    assert message["output_contract"] is None


def test_websocket_message_frame_uses_public_projection(producer, monkeypatch):
    from core.application.management.snapshot_serializers import serialize_agent_message_row
    from core.entrypoints.ws import jobs as ws_jobs
    from tests.unit.test_ws_jobs_revalidation import _client, _Store

    _, message, _ = published(producer)
    create_router = ws_jobs.create_jobs_websocket_router

    def router(**kwargs):
        return create_router(
            **{**kwargs, "serialize_agent_message_row": serialize_agent_message_row}
        )

    monkeypatch.setattr(ws_jobs, "create_jobs_websocket_router", router)
    store = _Store(polls_until_done=3)
    store.get_agent_messages = lambda _job, since_id="": (
        [] if since_id else [{"id": "1", "envelope": message, "artifacts": message["artifacts"]}]
    )
    client = _client(
        monkeypatch,
        sessions=[{"user_id": "u1", "account_id": "acc_default"}],
        access=[True],
        store=store,
    )
    frames = []
    with client.websocket_connect("/ws/jobs/job-1") as websocket:
        while True:
            frame = websocket.receive_json()
            frames.append(frame)
            if frame["type"] == "done":
                break
    outgoing = [frame["data"] for frame in frames if frame["type"] == "message"]
    assert len(outgoing) == 1
    assert "output_contract" not in outgoing[0]["envelope"]
    assert outgoing[0]["envelope"]["artifacts"] == message["artifacts"]
    assert message["output_contract"]


@pytest.mark.parametrize("precise", [False, True])
@pytest.mark.parametrize(
    ("kind", "value"), [("json", "generic scalar"), ("list", [1, {"flag": False}])]
)
def test_wrapper_preserves_generic_json_scalar_and_rejects_precise_object(
    producer, precise, kind, value
):
    producer.store.skill["_outputs"] = [{"name": "summary", "type": kind, "required": True}]
    if precise:
        producer.store.skill["output_schema"] = {
            "type": "object",
            "properties": {
                "summary": {"type": "object"}
                if kind == "json"
                else {"type": "array", "items": {"type": "string"}}
            },
            "required": ["summary"],
        }
    producer.artifacts["summary"] = value
    message = producer.wrapped(producer.state)["agent_messages"][-1]
    assert producer.inputs, "The actual fake Agent ran; this is wrapper output validation"
    assert message["status"] == ("fail" if precise else "pass"), message
    if precise:
        assert message["output_contract"] is None
        assert "Invalid type or value for output 'summary'" in message["errors"]
    else:
        assert message["artifacts"]["summary"] == value
        assert message["output_contract"] is not None
    contract = producer.inputs[-1].output_contract
    assert contract.rows[0]["type"] == (
        ("object" if precise else "json") if kind == "json" else "array"
    )
