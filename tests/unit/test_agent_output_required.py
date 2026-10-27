"""Accepted Agent output flags survive real API, storage and materialization."""

import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

from core.application.registry.agent_manifest import AgentManifest
from core.application.registry.db_store import RegistryDBStore
from core.entrypoints.admin.agents_helpers import _primary_skill_id
from core.entrypoints.admin.agents_io_routes import register_agent_io_routes
from tests._xdist_isolation import audit_database_name


@pytest.fixture
def agent_output_fixture():
    assert os.environ.get("FS_AUDIT_ISOLATED") == "1"
    dsn = os.environ["FS_DB_DSN"]
    info = conninfo_to_dict(dsn)
    assert (info.get("host"), info.get("dbname"), info.get("user")) == (
        "pg-audit",
        audit_database_name(),
        "audit",
    )
    agent_id = f"unit_output_required_{uuid4().hex}"
    skill_id = f"unit_output_skill_{uuid4().hex}"
    db = RegistryDBStore(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO registry_agents (agent_id, display_name, enabled, agent_type_id) "
            "SELECT %s, %s, false, id FROM agent_types WHERE key = 'researcher' RETURNING agent_id",
            (agent_id, "Audit output fixture"),
        ).fetchone()
        assert row == (agent_id,)
        conn.execute(
            "INSERT INTO registry_skills (skill_id, display_name, enabled) VALUES (%s, %s, false)",
            (skill_id, "Audit skill fixture"),
        )
        db.set_agent_inputs(
            agent_id,
            [{"name": "context", "type": "string", "description": "Keep", "required": False}],
        )
        app = FastAPI()
        router = APIRouter(prefix="/api/agents")
        register_agent_io_routes(
            router,
            get_db=lambda: db,
            get_registry=lambda: SimpleNamespace(reload_all=lambda: None),
            primary_skill_id=_primary_skill_id,
        )
        app.include_router(router)
        try:
            with TestClient(app) as client:
                yield SimpleNamespace(
                    db=db, conn=conn, agent_id=agent_id, skill_id=skill_id, client=client, dsn=dsn
                )
        finally:
            conn.execute("DELETE FROM registry_agents WHERE agent_id = %s", (agent_id,))
            conn.execute("DELETE FROM registry_skills WHERE skill_id = %s", (skill_id,))


def _flags(rows):
    return {row["name"]: row.get("required") for row in rows}


def _input_values(db, agent_id):
    return [
        {key: row[key] for key in ("name", "type", "description", "required", "source")}
        for row in db.get_agent_own_inputs(agent_id)
    ]


def _outputs():
    return [
        {"name": "required_result", "type": "string", "required": True},
        {"name": "optional_result", "type": "string", "required": False},
        {"name": "default_result", "type": "string"},
    ]


@pytest.mark.parametrize("skill_bound", [False, True])
def test_agent_output_flags_round_trip_after_repeated_materialization(
    agent_output_fixture, skill_bound
):
    fixture = agent_output_fixture
    if skill_bound:
        fixture.conn.execute(
            "INSERT INTO registry_agent_skills (agent_id, skill_id) VALUES (%s, %s)",
            (fixture.agent_id, fixture.skill_id),
        )
    inputs_before = _input_values(fixture.db, fixture.agent_id)
    path = f"/api/agents/{fixture.agent_id}"
    response = fixture.client.put(f"{path}/outputs", json=_outputs())
    assert response.status_code == 200, response.text
    expected = {"required_result": True, "optional_result": False, "default_result": True}
    assert _flags(response.json()) == expected
    for _ in range(2):
        fixture.db.materialize_agent_contract(fixture.agent_id)
        response = fixture.client.get(f"{path}/outputs")
        assert response.status_code == 200, response.text
        assert _flags(response.json()) == expected
        effective = fixture.client.get(f"{path}/effective-io")
        assert effective.status_code == 200, effective.text
        assert _flags(effective.json()["outputs"]) == expected
    reopened = RegistryDBStore(fixture.dsn)
    assert _flags(reopened.get_agent_outputs(fixture.agent_id)) == expected
    row = reopened.get_agent(fixture.agent_id)
    assert row["enabled"] is False
    assert _flags(AgentManifest.from_db_row(row).structured_outputs) == expected
    assert _input_values(reopened, fixture.agent_id) == inputs_before


@pytest.mark.parametrize("case", ["duplicate", "invalid_required", "empty_name"])
def test_invalid_outputs_leave_complete_agent_io_rows_unchanged(agent_output_fixture, case):
    fixture = agent_output_fixture
    fixture.db.set_agent_outputs(fixture.agent_id, [{"name": "before", "type": "string"}])
    before_inputs = fixture.db.get_agent_inputs(fixture.agent_id)
    before_outputs = fixture.db.get_agent_outputs(fixture.agent_id)
    payload = _outputs()
    if case == "duplicate":
        payload.append(dict(payload[0]))
    elif case == "invalid_required":
        payload[0]["required"] = "not-a-boolean"
    else:
        payload[0]["name"] = ""
    response = fixture.client.put(f"/api/agents/{fixture.agent_id}/outputs", json=payload)
    assert response.status_code in (400, 422), response.text
    assert fixture.db.get_agent_inputs(fixture.agent_id) == before_inputs
    assert fixture.db.get_agent_outputs(fixture.agent_id) == before_outputs


def test_inherited_input_requiredness_remains_optional(agent_output_fixture):
    fixture = agent_output_fixture
    fixture.conn.execute(
        "INSERT INTO registry_agent_skills (agent_id, skill_id) VALUES (%s, %s)",
        (fixture.agent_id, fixture.skill_id),
    )
    fixture.db.set_skill_inputs(
        fixture.skill_id, [{"name": "skill_context", "type": "string", "required": True}]
    )
    fixture.db.materialize_agent_contract(fixture.agent_id)
    assert _flags(fixture.db.get_agent_inputs(fixture.agent_id)) == {
        "context": False,
        "skill_context": False,
    }
