"""An agent's own inputs and outputs, saved beside a shared skill, in the canonical schema.

The I/O tab could not edit them: the older /inputs endpoint writes the primary skill's
fields when the agent has one, which changes every agent sharing that skill.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.agent_own_io import get_agent_own_io, save_agent_own_io
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.step_output_contract import resolve_step_output_contract


@pytest.fixture
def shared_skill(audit_db):
    store, conn, _workflow_id, _project_id = audit_db
    suffix = uuid4().hex
    skill_id = f"shared_{suffix}"
    agents = (f"writer_{suffix}", f"peer_{suffix}")
    store.save_skill(
        {
            "skill_id": skill_id,
            "display_name": "Shared skill",
            "enabled": True,
            "skill_type": "base",
            "instructions": ["Summarize."],
            "expected_inputs": [{"name": "request", "type": "text", "required": True}],
            "expected_outputs": [{"name": "summary", "type": "text", "required": True}],
            "account_id": DEFAULT_ACCOUNT_ID,
        }
    )
    for agent_id in agents:
        store.save_agent(
            {
                "agent_id": agent_id,
                "display_name": agent_id,
                "category": "ai",
                "enabled": True,
                "skills": [skill_id],
                "tools": [],
                "prompts": {"system": "Write."},
                "config": {"max_iterations": 3, "timeout_minutes": 0},
                "account_id": DEFAULT_ACCOUNT_ID,
            }
        )
    try:
        yield store, skill_id, agents
    finally:
        for agent_id in agents:
            conn.execute("DELETE FROM registry_agents WHERE agent_id = %s", (agent_id,))
        conn.execute("DELETE FROM registry_skills WHERE skill_id = %s", (skill_id,))


def _names(rows) -> list[str]:
    return [row["name"] for row in rows]


def _materialized(store, agent_id: str, kind: str) -> dict[str, str]:
    rows = (
        store.get_agent_inputs(agent_id) if kind == "inputs" else store.get_agent_outputs(agent_id)
    )
    return {row["name"]: row["source"] for row in rows}


def test_own_fields_save_beside_a_shared_skill_without_touching_it(shared_skill) -> None:
    store, skill_id, (agent_id, peer_id) = shared_skill
    skill_before = (store.get_skill_inputs(skill_id), store.get_skill_outputs(skill_id))
    peer_before = (
        _materialized(store, peer_id, "inputs"),
        _materialized(store, peer_id, "outputs"),
    )

    inputs, input_issues = save_agent_own_io(
        store,
        agent_id,
        "inputs",
        [{"name": "tone", "type": "text", "description": "Voice to use", "required": True}],
    )
    outputs, output_issues = save_agent_own_io(
        store,
        agent_id,
        "outputs",
        [{"name": "confidence", "type": "number", "description": "0 to 1", "required": False}],
    )

    assert (input_issues, output_issues) == ([], [])
    assert _names(inputs) == ["tone"]
    assert inputs[0]["required"] is True
    assert _names(outputs) == ["confidence"]
    assert outputs[0]["type"] == "number"
    skill_after = (store.get_skill_inputs(skill_id), store.get_skill_outputs(skill_id))
    assert skill_after == skill_before
    peer_after = (
        _materialized(store, peer_id, "inputs"),
        _materialized(store, peer_id, "outputs"),
    )
    assert peer_after == peer_before
    assert _materialized(store, agent_id, "inputs") == {
        "request": f"skill:{skill_id}",
        "tone": "own",
    }
    assert _materialized(store, agent_id, "outputs") == {
        "summary": f"skill:{skill_id}",
        "confidence": "own",
    }


def test_own_fields_survive_a_reload_and_every_contract_rebuild(shared_skill) -> None:
    store, skill_id, (agent_id, _peer_id) = shared_skill
    save_agent_own_io(store, agent_id, "outputs", [{"name": "confidence", "type": "number"}])

    store.materialize_agent_contract(agent_id)
    store.materialize_agents_for_skill(skill_id)
    store.materialize_all_agent_contracts()
    fresh = RegistryDBStore(store.dsn)

    assert _names(get_agent_own_io(fresh, agent_id, "outputs")) == ["confidence"]
    assert _materialized(fresh, agent_id, "outputs") == {
        "summary": f"skill:{skill_id}",
        "confidence": "own",
    }
    # The skill pair has a derived published block, and ADR 0040 makes an applicable
    # block the entire output surface: the own output is kept but not exposed there.
    contract = resolve_step_output_contract(
        fresh, agent_id=agent_id, skill_id=skill_id, account_id=str(DEFAULT_ACCOUNT_ID)
    )
    assert {row.get("name") for row in contract.rows} == {"summary"}


def test_own_outputs_join_the_run_contract_when_no_block_applies(audit_db) -> None:
    store, conn, _workflow_id, _project_id = audit_db
    agent_id = f"solo_{uuid4().hex}"
    store.save_agent(
        {
            "agent_id": agent_id,
            "display_name": agent_id,
            "category": "ai",
            "enabled": True,
            "skills": [],
            "tools": [],
            "prompts": {"system": "Write."},
            "config": {"max_iterations": 3, "timeout_minutes": 0},
            "account_id": DEFAULT_ACCOUNT_ID,
        }
    )
    try:
        save_agent_own_io(store, agent_id, "outputs", [{"name": "confidence", "type": "number"}])
        store.materialize_all_agent_contracts()

        contract = resolve_step_output_contract(
            RegistryDBStore(store.dsn),
            agent_id=agent_id,
            skill_id="",
            account_id=str(DEFAULT_ACCOUNT_ID),
        )

        assert [row.get("name") for row in contract.rows] == ["confidence"]
    finally:
        conn.execute("DELETE FROM registry_agents WHERE agent_id = %s", (agent_id,))


@pytest.mark.parametrize(
    ("kind", "items", "code"),
    [
        ("inputs", [{"name": "Request"}], "agent.own_field_inherited"),
        ("outputs", [{"name": "SUMMARY"}], "agent.own_field_inherited"),
        ("outputs", [{"name": "note"}, {"name": "Note"}], "agent.own_field_case_duplicate"),
        ("outputs", [{"name": "note"}, {"name": "note"}], "contract.duplicate_name"),
    ],
)
def test_a_name_the_contract_rebuild_would_drop_is_refused(shared_skill, kind, items, code) -> None:
    store, skill_id, (agent_id, _peer_id) = shared_skill
    save_agent_own_io(store, agent_id, kind, [{"name": "kept", "type": "text"}])

    saved, issues = save_agent_own_io(store, agent_id, kind, items)

    assert saved == []
    assert code in {issue["code"] for issue in issues}
    assert _names(get_agent_own_io(store, agent_id, kind)) == ["kept"]
    skill_fields = store.get_skill_inputs if kind == "inputs" else store.get_skill_outputs
    assert len(skill_fields(skill_id)) == 1


def test_clearing_own_fields_leaves_the_inherited_ones(shared_skill) -> None:
    store, skill_id, (agent_id, _peer_id) = shared_skill
    save_agent_own_io(store, agent_id, "inputs", [{"name": "tone"}])

    saved, issues = save_agent_own_io(store, agent_id, "inputs", [])

    assert (saved, issues) == ([], [])
    assert _materialized(store, agent_id, "inputs") == {"request": f"skill:{skill_id}"}
