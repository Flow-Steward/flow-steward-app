"""Importing definitions against the real audit database: atomic, create-only, race-safe.

The fakes in test_workflow_portability_artifact_import.py prove the import's decisions;
these prove the SQL the decisions rely on: every definition insert refuses an id that is
already taken, and a failure anywhere in the import rolls back what it had written.
"""

from __future__ import annotations

import os
import uuid
from functools import partial

import pytest
import yaml

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.db_store_common import RegistryCreateConflictError
from core.application.workflows.workflow_portability import (
    SCHEMA_VERSION,
    WorkflowPortabilityError,
    WorkflowPortabilityService,
)
from core.infrastructure.runtime.entrypoint_db_adapters import (
    create_integration_store,
    workflow_import_transaction,
)
from tests._workflow_definitions import linear_workflow

_TABLES = (
    ("registry_guardrails", "id"),
    ("registry_agents", "agent_id"),
    ("registry_skills", "skill_id"),
    ("registry_data_contracts", "contract_id"),
    ("registry_workflows", "workflow_id"),
)


@pytest.fixture
def ids(audit_db):
    """Fresh ids for one test, and the rows they name removed afterwards."""
    _registry, conn, _workflow_id, _project_id = audit_db
    suffix = uuid.uuid4().hex[:12]
    names = {
        "contract": f"brief_{suffix}.v1",
        "skill": f"draft_{suffix}",
        "agent": f"writer_{suffix}",
        "guardrail": str(uuid.uuid4()),
        "workflow": f"brief_flow_{suffix}",
    }
    yield names
    by_table = {
        "registry_guardrails": names["guardrail"],
        "registry_agents": names["agent"],
        "registry_skills": names["skill"],
        "registry_data_contracts": names["contract"],
        "registry_workflows": names["workflow"],
    }
    for table, column in _TABLES:
        conn.execute(f"DELETE FROM {table} WHERE {column} = %s", (by_table[table],))  # nosec B608


def _bundle(ids: dict) -> str:
    workflow = linear_workflow(
        {
            "step_id": "write",
            "step_kind": "agent",
            "participant_key": "author",
            "agent_id": ids["agent"],
        },
        workflow_id=ids["workflow"],
        settings={"participants": [{"participant_key": "author", "agent_id": ids["agent"]}]},
    )
    definitions = {
        "data_contracts": [
            {"contract_id": ids["contract"], "display_name": "Brief", "schema": {"type": "object"}}
        ],
        "skills": [
            {
                "skill_id": ids["skill"],
                "display_name": "Draft",
                "expected_inputs": [
                    {"name": "brief", "type": "json", "contract_id": ids["contract"]}
                ],
            }
        ],
        "agents": [
            {
                "agent_id": ids["agent"],
                "display_name": "Writer",
                "enabled": False,
                "skills": [{"skill_id": ids["skill"], "skill_prompt": "Draft."}],
            }
        ],
        "guardrails": [
            {
                "id": ids["guardrail"],
                "display_name": "No PII",
                "mode": "deterministic",
                "rule_type": "forbidden_patterns",
                "config": {"patterns": ["ssn"]},
                "agents": [ids["agent"]],
                "scope": "global",
            }
        ],
    }
    return yaml.safe_dump(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "workflow_bundle",
            "metadata": {"source_workflow_id": ids["workflow"]},
            "workflows": [workflow],
            "dependencies": {"agents": [{"agent_id": ids["agent"]}]},
            "definitions": definitions,
            "import_hints": {"default_enabled": False},
        },
        sort_keys=False,
    )


def _service(registry: RegistryDBStore) -> WorkflowPortabilityService:
    """The service as the admin import builds it: one transaction for every write."""
    integration = create_integration_store(os.environ["FS_DB_DSN"])
    return WorkflowPortabilityService(
        registry,
        project_connection_store=integration,
        extension_install_store=integration,
        account_id=DEFAULT_ACCOUNT_ID,
        import_transaction_factory=partial(workflow_import_transaction, registry, integration),
    )


def _apply(registry: RegistryDBStore, ids: dict, project_id) -> dict:
    return _service(registry).apply_import(
        _bundle(ids),
        resolutions=[],
        author="tester",
        owner_account_id=DEFAULT_ACCOUNT_ID,
        owner_project_id=str(project_id),
    )


def _count(conn, table: str, column: str, value: str) -> int:
    return conn.execute(
        f"SELECT count(*) FROM {table} WHERE {column} = %s",  # nosec B608
        (value,),
    ).fetchone()[0]


def test_a_clean_import_writes_every_definition(audit_db, ids) -> None:
    registry, conn, _workflow_id, project_id = audit_db

    result = _apply(registry, ids, project_id)

    assert {item["kind"] for item in result["definitions"]["created"]} == {
        "data_contract",
        "skill",
        "agent",
        "guardrail",
    }
    assert _count(conn, "registry_agents", "agent_id", ids["agent"]) == 1
    assert _count(conn, "registry_guardrails", "id", ids["guardrail"]) == 1
    assert _count(conn, "registry_workflows", "workflow_id", ids["workflow"]) == 1


def test_a_late_failure_rolls_back_every_definition(audit_db, ids, monkeypatch) -> None:
    """The workflow save fails after all definitions were written: none of them stay."""
    registry, conn, _workflow_id, project_id = audit_db

    def fail(*args, **kwargs):
        raise RuntimeError("workflow save failed")

    monkeypatch.setattr(registry, "save_workflow", fail)
    with pytest.raises(RuntimeError, match="workflow save failed"):
        _apply(registry, ids, project_id)

    for table, column in _TABLES:
        name = {
            "registry_guardrails": ids["guardrail"],
            "registry_agents": ids["agent"],
            "registry_skills": ids["skill"],
            "registry_data_contracts": ids["contract"],
            "registry_workflows": ids["workflow"],
        }[table]
        assert _count(conn, table, column, name) == 0, table


def test_an_agent_saved_meanwhile_wins_and_the_import_rolls_back(audit_db, ids) -> None:
    """An ordinary save takes the agent id after preflight: the import conflicts, overwrites nothing."""
    registry, conn, _workflow_id, project_id = audit_db
    other_session = RegistryDBStore(os.environ["FS_DB_DSN"])
    save_skill = registry.save_skill

    def save_skill_then_race(data, *args, **kwargs):
        save_skill(data, *args, **kwargs)
        # Committed on its own connection while the import's transaction is open.
        other_session.save_agent(
            {
                "agent_id": ids["agent"],
                "display_name": "Saved meanwhile",
                "account_id": DEFAULT_ACCOUNT_ID,
                "scope_kind": "account",
            },
            author="someone",
        )

    registry.save_skill = save_skill_then_race
    with pytest.raises(WorkflowPortabilityError) as caught:
        _apply(registry, ids, project_id)

    assert caught.value.code == "workflow_import_definition_conflict"
    assert other_session.get_agent(ids["agent"])["display_name"] == "Saved meanwhile"
    assert _count(conn, "registry_skills", "skill_id", ids["skill"]) == 0
    assert _count(conn, "registry_data_contracts", "contract_id", ids["contract"]) == 0
    assert _count(conn, "registry_workflows", "workflow_id", ids["workflow"]) == 0


def test_create_only_saves_refuse_a_taken_id_and_keep_the_row(audit_db, ids) -> None:
    registry, _conn, _workflow_id, _project_id = audit_db

    registry.save_data_contract(
        {"contract_id": ids["contract"], "display_name": "First", "schema": {"type": "object"}},
        create_only=True,
    )
    with pytest.raises(RegistryCreateConflictError):
        registry.save_data_contract(
            {"contract_id": ids["contract"], "display_name": "Second", "schema": {}},
            create_only=True,
        )
    assert registry.get_data_contract(ids["contract"])["display_name"] == "First"

    registry.save_skill({"skill_id": ids["skill"], "display_name": "First"}, create_only=True)
    with pytest.raises(RegistryCreateConflictError):
        registry.save_skill({"skill_id": ids["skill"], "display_name": "Second"}, create_only=True)
    assert registry.get_skill(ids["skill"])["display_name"] == "First"

    agent = {
        "agent_id": ids["agent"],
        "display_name": "First",
        "account_id": DEFAULT_ACCOUNT_ID,
        "scope_kind": "account",
    }
    registry.save_agent(agent, create_only=True)
    with pytest.raises(RegistryCreateConflictError):
        registry.save_agent({**agent, "display_name": "Second"}, create_only=True)
    assert registry.get_agent(ids["agent"])["display_name"] == "First"

    rule = {
        "id": ids["guardrail"],
        "display_name": "First",
        "mode": "deterministic",
        "rule_type": "forbidden_patterns",
        "config": {"patterns": ["ssn"]},
        "agents": [ids["agent"]],
    }
    actor = {"account_id": DEFAULT_ACCOUNT_ID, "actor_id": "tester", "actor_type": "user"}
    registry.save_guardrail(rule, create_only=True, **actor)
    with pytest.raises(RegistryCreateConflictError):
        registry.save_guardrail({**rule, "display_name": "Second"}, create_only=True, **actor)
    assert (
        registry.get_guardrail(ids["guardrail"], account_id=DEFAULT_ACCOUNT_ID)["display_name"]
        == "First"
    )
