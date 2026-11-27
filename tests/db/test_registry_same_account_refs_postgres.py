"""Every reference a workflow's steps make stays in the workflow's account (ADR 0020, C4c).

On a real database: a step of account B cannot name account A's agent, skill or
published block (the store refuses it, and so does the database), while platform
objects stay usable by every account; an Invoke child that exists only in account A
is an unknown workflow for account B; a hook binding cannot name a project of another
account, and its revisions stay in their binding's account and project; a schedule of
account B resolves its workflow in account B; and the migration runs again and stops
on a step that already names another account's agent. Every test runs on the
disposable audit database inside a transaction that is rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

import core.infrastructure.runtime.control.scans_scheduled_jobs as scheduled_mod
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.contract_validation_invoke import validate_invoke_workflow_step
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.workflow_account import (
    WorkflowAccountMissingError,
    workflow_account_scope,
)
from core.application.registry.workflow_lookup import get_workflow_row
from core.infrastructure.db import psycopg_runtime

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261126190000_registry_same_account_refs.sql"
TENANT_ID = "00000000-0000-0000-0000-000000000001"


@dataclass(frozen=True)
class Account:
    id: str
    project: str


@dataclass(frozen=True)
class Registry:
    conn: Any
    store: RegistryDBStore
    a: Account
    b: Account
    suffix: str


def _account(conn) -> Account:
    account_id, workspace_id, project_id = (str(uuid.uuid4()) for _ in range(3))
    suffix = uuid.uuid4().hex[:8]
    conn.execute(
        """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
           VALUES (%s, %s, %s, %s, true)""",
        (account_id, TENANT_ID, f"acme-{suffix}", f"Acme {suffix}"),
    )
    conn.execute(
        """INSERT INTO workspaces (id, tenant_id, account_id, workspace_key, display_name)
           VALUES (%s, %s, %s, %s, 'Workspace')""",
        (workspace_id, TENANT_ID, account_id, f"ws-{suffix}"),
    )
    conn.execute(
        """INSERT INTO projects (id, tenant_id, workspace_id, account_id, name, description)
           VALUES (%s, %s, %s, %s, %s, '')""",
        (project_id, TENANT_ID, workspace_id, account_id, f"Invoices {suffix}"),
    )
    return Account(account_id, project_id)


@pytest.fixture
def registry(disposable_db) -> Iterator[Registry]:
    conn = psycopg_runtime.connect(disposable_db.dsn)
    conn.autocommit = False
    store = RegistryDBStore(disposable_db.dsn)
    # Every store read and write joins this one transaction, rolled back below.
    store._set_tx_conn(conn)
    try:
        yield Registry(conn, store, _account(conn), _account(conn), uuid.uuid4().hex[:8])
    finally:
        store._set_tx_conn(None)
        conn.rollback()
        conn.close()


def _ids(registry: Registry) -> dict[str, str]:
    s = registry.suffix
    return {
        "a_agent": f"acme.a_agent_{s}",
        "platform_agent": f"acme.platform_agent_{s}",
        "a_skill": f"acme.a_skill_{s}",
        "a_block": f"acme.a_block_{s}",
        "platform_block": f"acme.platform_block_{s}",
    }


def _shared_objects(registry: Registry) -> dict[str, str]:
    conn, ids = registry.conn, _ids(registry)
    conn.execute(
        """INSERT INTO registry_agents (agent_id, display_name, account_id)
           VALUES (%s, 'A agent', %s), (%s, 'Platform agent', NULL)""",
        (ids["a_agent"], registry.a.id, ids["platform_agent"]),
    )
    conn.execute(
        "INSERT INTO registry_skills (skill_id, display_name, account_id) VALUES (%s, 'A', %s)",
        (ids["a_skill"], registry.a.id),
    )
    conn.execute(
        """INSERT INTO registry_published_block_contracts
               (block_key, display_name, source_kind, source_ref, account_id)
           VALUES (%s, 'A block', 'agent', %s, %s), (%s, 'Platform block', 'agent', %s, NULL)""",
        (
            ids["a_block"],
            ids["a_agent"],
            registry.a.id,
            ids["platform_block"],
            ids["platform_agent"],
        ),
    )
    return ids


def _workflow(key: str, *steps: dict) -> dict:
    # A definition save carries all three collections together.
    return {
        "workflow_id": key,
        "display_name": key,
        "enabled": True,
        "steps": list(steps),
        "groups": [],
        "for_each": [],
    }


def _rows(conn, sql: str, params: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [tuple(row) for row in cur.fetchall()]


def test_a_step_uses_platform_objects_and_its_own_accounts(registry: Registry) -> None:
    ids = _shared_objects(registry)
    key = f"acme.platform_user_{registry.suffix}"

    registry.store.save_workflow(
        _workflow(
            key,
            {"step_id": "ask", "agent_id": ids["platform_agent"]},
            {"step_id": "block", "step_kind": "set_variable", "block_key": ids["platform_block"]},
        ),
        account_id=registry.b.id,
    )
    registry.store.save_workflow(
        _workflow(key, {"step_id": "ask", "agent_id": ids["a_agent"]}),
        account_id=registry.a.id,
    )

    assert _rows(
        registry.conn,
        "SELECT account_id::text, agent_id FROM registry_workflow_steps "
        "WHERE owner_workflow_id = %s AND step_id = 'ask' ORDER BY agent_id",
        (key,),
    ) == sorted(
        [(registry.a.id, ids["a_agent"]), (registry.b.id, ids["platform_agent"])],
        key=lambda row: row[1],
    )


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"agent_id": "a_agent"}, "Agent"),
        ({"step_kind": "set_variable", "block_key": "a_block"}, "Published block"),
        (
            {"step_kind": "set_variable", "override": {"published_block_key": "a_block"}},
            "Published block",
        ),
    ],
)
def test_the_store_refuses_another_accounts_agent_or_block(
    registry: Registry, step: dict, message: str
) -> None:
    ids = _shared_objects(registry)
    resolved = {
        name: (ids[value] if isinstance(value, str) and value in ids else value)
        for name, value in step.items()
    }
    if "override" in resolved:
        resolved["override"] = {"published_block_key": ids["a_block"]}

    with pytest.raises(ValueError, match=f"{message} '.*' not found"):
        registry.store.save_workflow(
            _workflow(f"acme.thief_{registry.suffix}", {"step_id": "s", **resolved}),
            account_id=registry.b.id,
        )


def test_the_database_refuses_another_accounts_agent_skill_or_block(registry: Registry) -> None:
    ids = _shared_objects(registry)
    key = f"acme.raw_{registry.suffix}"
    registry.conn.execute(
        "INSERT INTO registry_workflows (workflow_key, display_name, account_id) VALUES (%s, %s, %s)",
        (key, key, registry.b.id),
    )
    for column, value, kind in (
        ("agent_id", ids["a_agent"], "agent"),
        ("skill_id", ids["a_skill"], "skill"),
        ("block_key", ids["a_block"], "published block"),
    ):
        registry.conn.execute("SAVEPOINT attempt")
        with pytest.raises(psycopg.errors.ForeignKeyViolation, match=f"names an? {kind}"):
            registry.conn.execute(
                f"INSERT INTO registry_workflow_steps (account_id, owner_workflow_id, step_id, "
                f"{column}) VALUES (%s, %s, %s, %s)",
                (registry.b.id, key, f"step_{column}", value),
            )
        registry.conn.execute("ROLLBACK TO SAVEPOINT attempt")

    # The platform agent is fine; moving the step to point at A's agent is not.
    registry.conn.execute(
        "INSERT INTO registry_workflow_steps (account_id, owner_workflow_id, step_id, agent_id) "
        "VALUES (%s, %s, 'ok', %s)",
        (registry.b.id, key, ids["platform_agent"]),
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        registry.conn.execute(
            "UPDATE registry_workflow_steps SET agent_id = %s "
            "WHERE account_id = %s AND owner_workflow_id = %s AND step_id = 'ok'",
            (ids["a_agent"], registry.b.id, key),
        )


def test_an_invoke_child_of_another_account_is_unknown(registry: Registry) -> None:
    store, s = registry.store, registry.suffix
    child, parent = f"acme.child_{s}", f"acme.parent_{s}"
    store.save_workflow(
        {
            **_workflow(child, {"step_id": "done", "step_kind": "stop_success"}),
            "settings": {"callable": {"enabled": True, "trigger_channels": ["parent_workflow"]}},
        },
        account_id=registry.a.id,
    )
    step = {
        "step_id": "call",
        "step_kind": "invoke_workflow",
        "override": {"invoke_workflow": {"workflow_id": child}},
    }
    store.save_workflow(_workflow(parent, step), account_id=registry.b.id)

    with workflow_account_scope(registry.b.id):
        assert get_workflow_row(store, child) is None
        result = validate_invoke_workflow_step(
            store, workflow_id=parent, step=step, path_prefix="workflow.steps.call"
        )
    assert "invoke.child_workflow_missing" in [issue["code"] for issue in result["errors"]]

    # The same id in B's own account is B's child.
    store.save_workflow(
        {
            **_workflow(child, {"step_id": "done", "step_kind": "stop_success"}),
            "settings": {"callable": {"enabled": True, "trigger_channels": ["parent_workflow"]}},
        },
        account_id=registry.b.id,
    )
    with workflow_account_scope(registry.b.id):
        result = validate_invoke_workflow_step(
            store, workflow_id=parent, step=step, path_prefix="workflow.steps.call"
        )
    assert "invoke.child_workflow_missing" not in [issue["code"] for issue in result["errors"]]


def _binding(conn, *, account_id: str, project_id: str, workflow: str) -> str:
    ((binding_id,),) = _rows(
        conn,
        """INSERT INTO registry_workflow_hook_bindings
               (account_id, project_id, local_workflow_id, hook_id)
           VALUES (%s, %s, %s, 'before_send') RETURNING id::text""",
        (account_id, project_id, workflow),
    )
    return binding_id


def test_a_hook_binding_and_its_revisions_stay_in_one_account(registry: Registry) -> None:
    conn, a, b = registry.conn, registry.a, registry.b
    workflow = f"acme.author_{registry.suffix}"

    conn.execute("SAVEPOINT attempt")
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="project_account_fkey"):
        _binding(conn, account_id=b.id, project_id=a.project, workflow=workflow)
    conn.execute("ROLLBACK TO SAVEPOINT attempt")

    binding = _binding(conn, account_id=b.id, project_id=b.project, workflow=workflow)
    revision_sql = """INSERT INTO registry_workflow_hook_binding_revisions
            (binding_id, revision, account_id, project_id, target_workflow_id,
             hook_contract_version, hook_semantic_digest)
        VALUES (%s, %s, %s, %s, %s, 1, 'digest')"""
    conn.execute(revision_sql, (binding, 1, b.id, b.project, f"acme.target_{registry.suffix}"))
    conn.execute("SAVEPOINT attempt")
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="revisions_scope_fkey"):
        conn.execute(revision_sql, (binding, 2, a.id, a.project, "acme.target"))
    conn.execute("ROLLBACK TO SAVEPOINT attempt")


def test_a_schedule_of_another_account_resolves_its_own_workflow(registry: Registry) -> None:
    store, key = registry.store, f"acme.nightly_{registry.suffix}"
    store.save_workflow({**_workflow(key), "display_name": "A nightly"}, account_id=registry.a.id)
    store.save_workflow({**_workflow(key), "display_name": "B nightly"}, account_id=registry.b.id)
    seen: dict[str, Any] = {}

    def materialize(workflow_id: str, inputs: dict) -> tuple[dict, list[str]]:
        # The launch resolves the workflow with no account named, as the real one does.
        seen["resolved"] = get_workflow_row(store, workflow_id)["display_name"]
        return dict(inputs), []

    def enqueue(_bl, **kwargs: Any) -> dict:
        seen["job_account"] = kwargs["account_id"]
        return {"status": "enqueued", "job_id": "job-1"}

    now = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
    result = scheduled_mod._run_scheduled_job_once(
        SimpleNamespace(get_project_run_config=lambda _project_id: {"project_name": "Nightly"}),
        {
            "id": str(uuid.uuid4()),
            "project_id": registry.b.project,
            "account_id": registry.b.id,
            "workflow_id": key,
            "inputs": {},
            "cron_expr": "0 2 * * *",
            "next_run_at": now,
            "admission_allowed": True,
            "admission_workflow_revision": 1,
            "current_workflow_revision": 1,
        },
        SimpleNamespace(),
        now_utc=now,
        _record_scheduled_job_outcome_fn=lambda *_args, **_kwargs: None,
        _enqueue_scheduled_job_fn=enqueue,
        _materialize_scheduled_job_launch_fn=materialize,
    )

    assert result["status"] == "enqueued", result
    assert seen == {"resolved": "B nightly", "job_account": registry.b.id}
    # Outside the schedule run nothing is bound: a lookup is refused (step C5), and
    # the default account has no workflow with this key.
    with pytest.raises(WorkflowAccountMissingError):
        get_workflow_row(store, key)
    assert get_workflow_row(store, key, account_id=DEFAULT_ACCOUNT_ID) is None


def test_the_migration_runs_again_and_stops_on_a_crossing_step(registry: Registry) -> None:
    conn = registry.conn
    sql = MIGRATION.read_text(encoding="utf-8")
    conn.execute(sql)
    conn.execute(sql)

    ids = _shared_objects(registry)
    key = f"acme.legacy_{registry.suffix}"
    conn.execute(
        "INSERT INTO registry_workflows (workflow_key, display_name, account_id) VALUES (%s, %s, %s)",
        (key, key, registry.b.id),
    )
    # A row written before the rule existed.
    conn.execute(
        "ALTER TABLE registry_workflow_steps DISABLE TRIGGER trg_registry_workflow_steps_same_account"
    )
    conn.execute(
        "INSERT INTO registry_workflow_steps (account_id, owner_workflow_id, step_id, agent_id) "
        "VALUES (%s, %s, 'old', %s)",
        (registry.b.id, key, ids["a_agent"]),
    )
    conn.execute(
        "ALTER TABLE registry_workflow_steps ENABLE TRIGGER trg_registry_workflow_steps_same_account"
    )

    with pytest.raises(psycopg.errors.RaiseException, match="registry_workflow_steps"):
        conn.execute(sql)
