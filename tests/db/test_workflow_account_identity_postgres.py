"""Workflow identity is account-qualified (ADR 0020, step C4b).

Two accounts own a workflow with the same key, with the same step, Group and For
each ids, without clashing; reads, updates, toggles, deletes and the run pin stay in
their account; every table that names a workflow refuses another account's workflow
at the database; and the migration upgrades a database with the previous global
identity, runs again, and stops on a row that names another account's workflow.
Every test runs on the disposable audit database inside a transaction that is
rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.jobs.workflow_definition_snapshot_repository import (
    WorkflowDefinitionSnapshotRepository,
)
from core.application.management.approval_assignees import ApprovalAssigneeRepository
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.workflow_account import (
    WorkflowAccountMissingError,
    installation_account_scope,
    workflow_account_scope,
)
from core.infrastructure.db import psycopg_runtime

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261126180000_workflow_account_identity.sql"
TENANT_ID = "00000000-0000-0000-0000-000000000001"
KEY = "acme.invoice_review"


@dataclass(frozen=True)
class Account:
    id: str
    project: str
    user: str


@dataclass(frozen=True)
class Registry:
    conn: object
    store: RegistryDBStore
    a: Account
    b: Account


def _account(conn) -> Account:
    account_id, workspace_id, project_id, user_id = (str(uuid.uuid4()) for _ in range(4))
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
    conn.execute(
        """INSERT INTO users (id, username, password_hash, account_id)
           VALUES (%s, %s, 'x', %s)""",
        (user_id, f"approver-{suffix}@example.invalid", account_id),
    )
    return Account(account_id, project_id, user_id)


@pytest.fixture
def registry(disposable_db) -> Iterator[Registry]:
    conn = psycopg_runtime.connect(disposable_db.dsn)
    conn.autocommit = False
    store = RegistryDBStore(disposable_db.dsn)
    # Every store read and write joins this one transaction, rolled back below.
    store._set_tx_conn(conn)
    try:
        yield Registry(conn, store, _account(conn), _account(conn))
    finally:
        store._set_tx_conn(None)
        conn.rollback()
        conn.close()


def _workflow(display_name: str, step_name: str) -> dict:
    return {
        "workflow_id": KEY,
        "display_name": display_name,
        "enabled": True,
        "groups": [{"group_id": "review", "display_name": "Review", "body": {}}],
        "for_each": [
            {
                "for_each_id": "lines",
                "group_id": "review",
                "over": "$.input.lines",
                "item_name": "line",
                "body": {},
            }
        ],
        "steps": [
            {
                "step_id": "check",
                "step_kind": "set_variable",
                "group_id": "review",
                "display_name": step_name,
            },
            {
                "step_id": "each_line",
                "step_kind": "set_variable",
                "for_each_id": "lines",
                "display_name": f"{step_name} per line",
            },
        ],
    }


def _rows(conn, sql: str, params: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [tuple(row) for row in cur.fetchall()]


def _both(registry: Registry) -> None:
    store = registry.store
    store.save_workflow(_workflow("A review", "A check"), account_id=registry.a.id)
    store.save_workflow(_workflow("B review", "B check"), account_id=registry.b.id)


def test_two_accounts_own_the_same_workflow_key_without_clashing(registry: Registry) -> None:
    store, a, b = registry.store, registry.a.id, registry.b.id
    _both(registry)

    workflow_a = store.get_workflow(KEY, account_id=a)
    workflow_b = store.get_workflow(KEY, account_id=b)
    assert workflow_a["display_name"] == "A review"
    assert workflow_b["display_name"] == "B review"
    assert {step["display_name"] for step in workflow_a["steps"]} == {
        "A check",
        "A check per line",
    }
    assert {step["display_name"] for step in workflow_b["steps"]} == {
        "B check",
        "B check per line",
    }
    assert [group["group_id"] for group in workflow_b["groups"]] == ["review"]
    assert [item["for_each_id"] for item in workflow_b["for_each"]] == ["lines"]
    assert [row["workflow_id"] for row in store.list_workflows(account_id=a)] == [KEY]
    assert [row["workflow_id"] for row in store.list_workflows_summary(account_id=b)] == [KEY]
    # A call without an account works on the bound one; with none bound it is
    # refused, never answered from the default account (step C5).
    with workflow_account_scope(b):
        assert store.get_workflow(KEY)["display_name"] == "B review"
    with pytest.raises(WorkflowAccountMissingError):
        store.get_workflow(KEY)

    # A second create in the same account is refused; the other account is untouched.
    from core.application.registry.db_store_workflows_mixin import WorkflowCreateConflictError

    with pytest.raises(WorkflowCreateConflictError):
        store.save_workflow(_workflow("A again", "x"), account_id=a, create_only=True)


def test_update_toggle_and_delete_stay_in_their_account(registry: Registry) -> None:
    store, a, b = registry.store, registry.a.id, registry.b.id
    _both(registry)

    changed = _workflow("A renamed", "A changed")
    changed["steps"] = changed["steps"][:1]
    store.save_workflow(changed, account_id=a)
    assert store.toggle_workflow(KEY, account_id=a) is False

    workflow_b = store.get_workflow(KEY, account_id=b)
    assert workflow_b["display_name"] == "B review"
    assert workflow_b["enabled"] is True
    assert len(workflow_b["steps"]) == 2
    assert len(store.get_workflow(KEY, account_id=a)["steps"]) == 1

    store.delete_workflow(KEY, account_id=a)
    assert store.get_workflow(KEY, account_id=a) is None
    assert store.get_workflow(KEY, include_trashed=True, account_id=a) is not None
    assert store.get_workflow(KEY, account_id=b)["display_name"] == "B review"


def test_the_run_pin_reads_the_jobs_account(registry: Registry) -> None:
    _both(registry)
    repository = WorkflowDefinitionSnapshotRepository(registry.conn)

    with repository._cursor() as cur:
        pinned_a = repository._workflow_row(cur, KEY, account_id=registry.a.id)
        pinned_b = repository._workflow_row(cur, KEY, account_id=registry.b.id)

    assert str(pinned_a["account_id"]) == registry.a.id
    assert str(pinned_b["account_id"]) == registry.b.id
    assert {step["display_name"] for step in pinned_b["steps"]} == {
        "B check",
        "B check per line",
    }


def test_definition_rows_cannot_name_another_accounts_workflow(registry: Registry) -> None:
    conn, a, b = registry.conn, registry.a.id, registry.b.id
    registry.store.save_workflow(_workflow("A review", "A check"), account_id=a)
    (group_a,) = _rows(
        conn,
        "SELECT id FROM registry_workflow_groups WHERE account_id = %s AND workflow_id = %s",
        (a, KEY),
    )

    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            """INSERT INTO registry_workflow_steps (account_id, owner_workflow_id, step_id)
               VALUES (%s, %s, 'stray')""",
            (b, KEY),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            """INSERT INTO registry_workflow_groups (account_id, workflow_id, group_id, body_jsonb)
               VALUES (%s, %s, 'stray', '{}'::jsonb)""",
            (b, KEY),
        )
    # B owns the key too: its step still cannot sit in A's Group.
    registry.store.save_workflow(_workflow("B review", "B check"), account_id=b)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            """INSERT INTO registry_workflow_steps
                   (account_id, owner_workflow_id, step_id, group_id)
               VALUES (%s, %s, 'stray', %s)""",
            (b, KEY, group_a[0]),
        )


def test_bindings_schedules_roles_and_approvers_stay_in_their_account(
    registry: Registry,
) -> None:
    conn, a, b = registry.conn, registry.a, registry.b
    registry.store.save_workflow(_workflow("B only", "B check"), account_id=b.id)

    # A project of account A cannot bind, schedule or scope a role to B's workflow.
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            "INSERT INTO project_workflows (project_id, workflow_id) VALUES (%s, %s)",
            (a.project, KEY),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            """INSERT INTO scheduled_jobs (account_id, project_id, workflow_id, cron_expr)
               VALUES (%s, %s, %s, '0 * * * *')""",
            (a.id, a.project, KEY),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            """INSERT INTO user_roles (user_id, role_id, project_id, workflow_id)
               SELECT %s, id, %s, %s FROM roles WHERE key = 'viewer'""",
            (a.user, a.project, KEY),
        )

    # The same writes in B's own project take B's account and are accepted.
    conn.execute(
        "INSERT INTO project_workflows (project_id, workflow_id) VALUES (%s, %s)",
        (b.project, KEY),
    )
    conn.execute(
        """INSERT INTO scheduled_jobs (account_id, project_id, workflow_id, cron_expr)
           VALUES (%s, %s, %s, '0 * * * *')""",
        (b.id, b.project, KEY),
    )
    conn.execute(
        """INSERT INTO user_roles (user_id, role_id, project_id, workflow_id)
           SELECT %s, id, %s, %s FROM roles WHERE key = 'viewer'""",
        (b.user, b.project, KEY),
    )
    assert _rows(
        conn, "SELECT account_id::text FROM project_workflows WHERE project_id = %s", (b.project,)
    ) == [(b.id,)]
    assert _rows(
        conn,
        "SELECT workflow_account_id::text FROM user_roles WHERE user_id = %s AND workflow_id = %s",
        (b.user, KEY),
    ) == [(b.id,)]

    # Assigned approvers of the same step id are kept per account.
    registry.store.save_workflow(_workflow("A review", "A check"), account_id=a.id)
    ApprovalAssigneeRepository(conn, account_id=a.id).replace_approvers(
        KEY, "check", [a.user], created_by="admin"
    )
    ApprovalAssigneeRepository(conn, account_id=b.id).replace_approvers(
        KEY, "check", [b.user], created_by="admin"
    )
    assert ApprovalAssigneeRepository(conn, account_id=a.id).list_approver_ids(KEY, "check") == (
        a.user,
    )
    assert ApprovalAssigneeRepository(conn, account_id=b.id).list_approver_ids(KEY, "check") == (
        b.user,
    )


_PREVIOUS_SHAPE = """
-- The later same-account step check (C4c) reads the column C4b added.
DROP TRIGGER IF EXISTS trg_registry_workflow_steps_same_account ON registry_workflow_steps;
DROP FUNCTION IF EXISTS registry_workflow_steps_same_account();
DROP TRIGGER trg_project_workflows_account ON project_workflows;
DROP TRIGGER trg_user_roles_workflow_account ON user_roles;
DROP FUNCTION project_workflows_copy_account();
DROP FUNCTION user_roles_copy_workflow_account();
ALTER TABLE registry_workflows DROP CONSTRAINT registry_workflows_account_id_workflow_id_key CASCADE;
ALTER TABLE registry_workflows DROP CONSTRAINT registry_workflows_account_id_workflow_key_key;
ALTER TABLE registry_workflow_groups
    DROP CONSTRAINT registry_workflow_groups_account_id_workflow_id_id_key CASCADE;
ALTER TABLE registry_workflow_groups
    DROP CONSTRAINT registry_workflow_groups_account_id_workflow_id_group_id_key;
ALTER TABLE registry_workflow_for_each
    DROP CONSTRAINT registry_workflow_for_each_account_id_workflow_id_id_key CASCADE;
ALTER TABLE registry_workflow_for_each
    DROP CONSTRAINT registry_workflow_for_each_account_workflow_for_each_key;
ALTER TABLE registry_workflow_steps DROP CONSTRAINT registry_workflow_steps_account_owner_workflow_step_key;
ALTER TABLE workflow_runtime_dependencies DROP CONSTRAINT workflow_runtime_dependencies_account_step_ref_key;
ALTER TABLE catalog_secret_bindings DROP CONSTRAINT catalog_secret_bindings_workflow_field_key;
ALTER TABLE catalog_secret_approvals DROP CONSTRAINT catalog_secret_approvals_workflow_key;
ALTER TABLE approval_step_approvers DROP CONSTRAINT approval_step_approvers_pkey;
ALTER TABLE user_roles DROP CONSTRAINT user_roles_workflow_account_check;
ALTER TABLE project_workflows DROP CONSTRAINT project_workflows_project_account_fkey;
ALTER TABLE registry_workflow_groups DROP COLUMN account_id;
ALTER TABLE registry_workflow_for_each DROP COLUMN account_id;
ALTER TABLE registry_workflow_steps DROP COLUMN account_id;
ALTER TABLE project_workflows DROP COLUMN account_id;
ALTER TABLE approval_step_approvers DROP COLUMN account_id;
ALTER TABLE job_reaction_events DROP COLUMN account_id;
ALTER TABLE user_roles DROP COLUMN workflow_account_id;
ALTER TABLE registry_workflows ALTER COLUMN account_id DROP NOT NULL;
ALTER TABLE scheduled_jobs ALTER COLUMN account_id DROP NOT NULL;
ALTER TABLE workflow_runtime_dependencies ALTER COLUMN account_id DROP NOT NULL;
ALTER TABLE workflow_runtime_dependencies ALTER COLUMN account_id DROP DEFAULT;
ALTER TABLE registry_workflows ADD CONSTRAINT registry_workflows_workflow_key_key UNIQUE (workflow_key);
ALTER TABLE registry_workflows ADD CONSTRAINT registry_workflows_workflow_id_key UNIQUE (workflow_id);
ALTER TABLE registry_workflow_groups
    ADD CONSTRAINT registry_workflow_groups_workflow_id_group_id_key UNIQUE (workflow_id, group_id);
ALTER TABLE registry_workflow_groups
    ADD CONSTRAINT registry_workflow_groups_workflow_id_id_key UNIQUE (workflow_id, id);
ALTER TABLE registry_workflow_steps
    ADD CONSTRAINT registry_workflow_steps_owner_workflow_id_step_id_key UNIQUE (owner_workflow_id, step_id);
ALTER TABLE registry_workflow_groups ADD CONSTRAINT registry_workflow_groups_workflow_id_fkey
    FOREIGN KEY (workflow_id) REFERENCES registry_workflows(workflow_id) ON DELETE CASCADE;
ALTER TABLE registry_workflow_steps ADD CONSTRAINT registry_workflow_steps_owner_workflow_id_fkey
    FOREIGN KEY (owner_workflow_id) REFERENCES registry_workflows(workflow_id) ON DELETE CASCADE;
ALTER TABLE registry_workflow_steps ADD CONSTRAINT registry_workflow_steps_group_fkey
    FOREIGN KEY (owner_workflow_id, group_id) REFERENCES registry_workflow_groups(workflow_id, id)
    ON DELETE CASCADE;
ALTER TABLE project_workflows ADD CONSTRAINT project_workflows_workflow_id_fkey
    FOREIGN KEY (workflow_id) REFERENCES registry_workflows(workflow_id) ON DELETE CASCADE;
ALTER TABLE user_roles ADD CONSTRAINT user_roles_workflow_id_fkey
    FOREIGN KEY (workflow_id) REFERENCES registry_workflows(workflow_id) ON DELETE CASCADE;
ALTER TABLE approval_step_approvers ADD CONSTRAINT approval_step_approvers_pkey
    PRIMARY KEY (workflow_id, step_id, user_id);
"""


def _previous_shape_with_rows(registry: Registry) -> tuple[str, str]:
    conn, a = registry.conn, registry.a
    legacy = f"acme.legacy_{uuid.uuid4().hex[:8]}"
    owned = f"acme.owned_{uuid.uuid4().hex[:8]}"
    conn.execute(_PREVIOUS_SHAPE)
    conn.execute(
        """INSERT INTO registry_workflows (workflow_key, display_name, account_id)
           VALUES (%s, 'Legacy', NULL), (%s, 'Owned', %s)""",
        (legacy, owned, a.id),
    )
    (group_id,) = _rows(
        conn,
        """INSERT INTO registry_workflow_groups (workflow_id, group_id, body_jsonb)
           VALUES (%s, 'review', '{}'::jsonb) RETURNING id""",
        (legacy,),
    )[0]
    conn.execute(
        """INSERT INTO registry_workflow_steps (owner_workflow_id, step_id, group_id)
           VALUES (%s, 'check', %s)""",
        (legacy, group_id),
    )
    conn.execute(
        """INSERT INTO approval_step_approvers (workflow_id, step_id, user_id)
           VALUES (%s, 'check', %s)""",
        (legacy, a.user),
    )
    conn.execute(
        "INSERT INTO project_workflows (project_id, workflow_id) VALUES (%s, %s)",
        (a.project, owned),
    )
    conn.execute(
        """INSERT INTO user_roles (user_id, role_id, project_id, workflow_id)
           SELECT %s, id, %s, %s FROM roles WHERE key = 'viewer'""",
        (a.user, a.project, owned),
    )
    return legacy, owned


def test_the_migration_upgrades_the_global_identity_and_runs_again(registry: Registry) -> None:
    conn, a = registry.conn, registry.a
    legacy, owned = _previous_shape_with_rows(registry)

    sql = MIGRATION.read_text(encoding="utf-8")
    conn.execute(sql)
    conn.execute(sql)

    assert _rows(
        conn, "SELECT account_id::text FROM registry_workflows WHERE workflow_key = %s", (legacy,)
    ) == [(DEFAULT_ACCOUNT_ID,)]
    for table, column in (
        ("registry_workflow_groups", "workflow_id"),
        ("registry_workflow_steps", "owner_workflow_id"),
        ("approval_step_approvers", "workflow_id"),
    ):
        assert _rows(
            conn, f"SELECT account_id::text FROM {table} WHERE {column} = %s", (legacy,)
        ) == [(DEFAULT_ACCOUNT_ID,)], table
    assert _rows(
        conn, "SELECT account_id::text FROM project_workflows WHERE workflow_id = %s", (owned,)
    ) == [(a.id,)]
    assert _rows(
        conn, "SELECT workflow_account_id::text FROM user_roles WHERE workflow_id = %s", (owned,)
    ) == [(a.id,)]

    # The upgraded database accepts a second account using the legacy key.
    registry.store.save_workflow(
        {"workflow_id": legacy, "display_name": "Other account"}, account_id=registry.b.id
    )
    assert registry.store.get_workflow(legacy, account_id=registry.b.id)["display_name"] == (
        "Other account"
    )
    with installation_account_scope():
        assert registry.store.get_workflow(legacy)["display_name"] == "Legacy"


def test_the_migration_stops_on_a_binding_to_another_accounts_workflow(
    registry: Registry,
) -> None:
    conn, b = registry.conn, registry.b
    _legacy, owned = _previous_shape_with_rows(registry)
    # Under the global identity, account B's project could bind A's workflow.
    conn.execute(
        "INSERT INTO project_workflows (project_id, workflow_id) VALUES (%s, %s)",
        (b.project, owned),
    )

    with pytest.raises(psycopg.errors.RaiseException, match="project_workflows"):
        conn.execute(MIGRATION.read_text(encoding="utf-8"))
