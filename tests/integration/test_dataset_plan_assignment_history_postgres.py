"""PostgreSQL regressions for Dataset assignment lifecycle audit metadata."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.errors import DivisionByZero

from core.application.auth.config import DEFAULT_TENANT_ID, DEFAULT_WORKSPACE_ID
from core.application.management.command_services_trash import TrashLifecycleService
from core.application.management.errors import ServiceForbiddenError, ServiceValidationError
from core.application.management.scope import ScopeContext
from core.application.registry.db_store import RegistryDBStore
from core.infrastructure.db.platform_stores import PlatformProjectStore
from tests._db_env import resolve_test_db_dsn


@pytest.fixture(autouse=True)
def require_audit_database(pg_conn):
    """Fail before this test module can write outside the audit database."""
    assert pg_conn.info.host == "pg-audit"
    assert pg_conn.info.dbname == "flow_steward_audit"


@pytest.fixture
def owned_dataset_assignment(pg_conn, default_account):
    project_id = str(uuid4())
    dataset_id = f"fixture-dataset-plan-history-{uuid4().hex}"
    actor = "dataset-plan-history-admin"
    project_store = None
    dataset_store = None
    scope = ScopeContext(
        account_id=str(default_account),
        actor_id=actor,
        principal_type="admin",
        allowed_project_ids=frozenset({project_id}),
    )
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projects
                    (id, tenant_id, workspace_id, account_id, name)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    project_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_WORKSPACE_ID,
                    default_account,
                    f"Dataset plan history {project_id}",
                ),
            )
            cur.execute(
                """
                INSERT INTO registry_datasets
                    (dataset_key, workspace_id, display_name, availability_mode)
                VALUES (%s, %s, %s, 'specific_projects_only')
                """,
                (dataset_id, DEFAULT_WORKSPACE_ID, "Dataset plan history fixture"),
            )
            cur.execute(
                "INSERT INTO project_datasets (dataset_id, project_id) VALUES (%s, %s)",
                (dataset_id, project_id),
            )
        pg_conn.commit()

        dsn = resolve_test_db_dsn()
        project_store = PlatformProjectStore(dsn)
        dataset_store = RegistryDBStore(dsn)
        yield SimpleNamespace(
            actor=actor,
            conn=pg_conn,
            dataset_id=dataset_id,
            project_id=project_id,
            project_store=project_store,
            scope=scope,
            service=TrashLifecycleService(
                get_logger=lambda: project_store,
                get_project_dataset_catalog=lambda: dataset_store,
            ),
        )
    finally:
        pg_conn.rollback()
        if dataset_store is not None and dataset_store._pool is not None:
            dataset_store._pool.closeall()
        elif dataset_store is not None and dataset_store._conn is not None:
            dataset_store._conn.close()
        if project_store is not None and project_store._direct_conn is not None:
            project_store._direct_conn.close()
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM platform_action_log
                WHERE task_id = %s
                  AND action_type = 'lifecycle_hard_delete'
                  AND agent = %s
                """,
                (f"lifecycle:project:{project_id}", actor),
            )
            cur.execute(
                "DELETE FROM registry_changelog WHERE entity_type = 'dataset' AND entity_id = %s",
                (dataset_id,),
            )
            cur.execute(
                "DELETE FROM project_datasets WHERE dataset_id = %s AND project_id = %s",
                (dataset_id, project_id),
            )
            cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
            cur.execute("DELETE FROM registry_datasets WHERE dataset_key = %s", (dataset_id,))
        pg_conn.commit()


def _assignment_history(fixture):
    with fixture.conn.cursor() as cur:
        cur.execute(
            """
            SELECT action, snapshot, author
            FROM registry_changelog
            WHERE entity_type = 'dataset'
              AND entity_id = %s
            ORDER BY created_at DESC, id DESC
            """,
            (fixture.dataset_id,),
        )
        rows = cur.fetchall()
    fixture.conn.commit()
    return rows


def _assignment_exists(fixture) -> bool:
    with fixture.conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM project_datasets
                WHERE dataset_id = %s AND project_id = %s
            )
            """,
            (fixture.dataset_id, fixture.project_id),
        )
        exists = bool(cur.fetchone()[0])
    fixture.conn.commit()
    return exists


def _project_is_trashed(fixture) -> bool:
    with fixture.conn.cursor() as cur:
        cur.execute(
            "SELECT deleted_at IS NOT NULL FROM projects WHERE id = %s", (fixture.project_id,)
        )
        trashed = bool(cur.fetchone()[0])
    fixture.conn.commit()
    return trashed


def _selected_dataset_node(plan) -> str:
    return next(
        child["node_id"]
        for child in plan.tree["children"]
        if child["entity_type"] == "project_dataset"
    )


def _trash_project_and_delete_assignment(fixture) -> None:
    fixture.service.trash("project", fixture.project_id, scope=fixture.scope)
    plan = fixture.service.plan(
        "project", fixture.project_id, action_type="hard_delete", scope=fixture.scope
    )
    fixture.service.execute_plan(
        "project",
        fixture.project_id,
        action_type="hard_delete",
        selection={
            "plan_version": plan.plan_version,
            "selected_node_ids": [_selected_dataset_node(plan)],
        },
        scope=fixture.scope,
    )


def test_dataset_assignment_plan_history_records_authenticated_actor(owned_dataset_assignment):
    fixture = owned_dataset_assignment
    _trash_project_and_delete_assignment(fixture)
    assert not _assignment_exists(fixture)
    delete_history = _assignment_history(fixture)
    assert [row[2] for row in delete_history] == [fixture.actor]
    assert delete_history == [
        (
            "project_assignment_delete",
            {
                "dataset_id": fixture.dataset_id,
                "project_id": fixture.project_id,
                "project_ids": [fixture.project_id],
                "display_name": fixture.dataset_id,
                "visible_in_catalog": True,
            },
            fixture.actor,
        )
    ]

    restore_plan = fixture.service.plan(
        "project", fixture.project_id, action_type="restore", scope=fixture.scope
    )
    result = fixture.service.execute_plan(
        "project",
        fixture.project_id,
        action_type="restore",
        selection={
            "plan_version": restore_plan.plan_version,
            "selected_node_ids": [_selected_dataset_node(restore_plan)],
        },
        scope=fixture.scope,
    )

    assert result.status == "restored"
    assert _assignment_exists(fixture)
    history = _assignment_history(fixture)
    assert [row[0] for row in history] == [
        "project_assignment_restore",
        "project_assignment_delete",
    ]
    for action, snapshot, author in history:
        assert snapshot == {
            "dataset_id": fixture.dataset_id,
            "project_id": fixture.project_id,
            "project_ids": [fixture.project_id],
            "display_name": fixture.dataset_id,
            "visible_in_catalog": True,
        }
        assert author == fixture.actor, action


def test_dataset_assignment_restore_rolls_back_link_and_history(
    owned_dataset_assignment, monkeypatch
):
    fixture = owned_dataset_assignment
    _trash_project_and_delete_assignment(fixture)
    history_before = _assignment_history(fixture)
    restore_plan = fixture.service.plan(
        "project", fixture.project_id, action_type="restore", scope=fixture.scope
    )
    real_writer = fixture.service._log_registry_changelog_entry
    observed = []

    def write_then_fail(cur, **kwargs):
        real_writer(cur, **kwargs)
        cur.execute(
            """
            SELECT
                EXISTS (
                    SELECT 1 FROM project_datasets
                    WHERE dataset_id = %s AND project_id = %s
                ) AS assignment_restored,
                EXISTS (
                    SELECT 1 FROM registry_changelog
                    WHERE entity_type = 'dataset'
                      AND entity_id = %s
                      AND action = 'project_assignment_restore'
                ) AS history_recorded
            """,
            (fixture.dataset_id, fixture.project_id, fixture.dataset_id),
        )
        row = cur.fetchone()
        evidence = tuple(row.values()) if isinstance(row, dict) else tuple(row)
        assert evidence == (True, True)
        observed.append(evidence)
        cur.execute("SELECT 1 / 0")

    monkeypatch.setattr(fixture.service, "_log_registry_changelog_entry", write_then_fail)
    selection = {
        "plan_version": restore_plan.plan_version,
        "selected_node_ids": [_selected_dataset_node(restore_plan)],
    }
    with pytest.raises(DivisionByZero) as raised:
        fixture.service.execute_plan(
            "project",
            fixture.project_id,
            action_type="restore",
            selection=selection,
            scope=fixture.scope,
        )

    assert raised.value.sqlstate == "22012"
    assert observed == [(True, True)]
    assert not _assignment_exists(fixture)
    assert _project_is_trashed(fixture)
    assert _assignment_history(fixture) == history_before


@pytest.mark.parametrize("boundary", ["foreign-account", "stale-plan"])
def test_dataset_assignment_restore_denial_keeps_link_and_history_unchanged(
    owned_dataset_assignment, boundary
):
    fixture = owned_dataset_assignment
    _trash_project_and_delete_assignment(fixture)
    history_before = _assignment_history(fixture)
    restore_plan = fixture.service.plan(
        "project", fixture.project_id, action_type="restore", scope=fixture.scope
    )
    scope = fixture.scope
    if boundary == "foreign-account":
        scope = ScopeContext(
            account_id="00000000-0000-0000-0000-000000000099",
            actor_id="foreign-dataset-plan-admin",
            principal_type="admin",
            allowed_project_ids=frozenset({fixture.project_id}),
        )

    selection = {
        "plan_version": (
            "stale-dataset-assignment-plan"
            if boundary == "stale-plan"
            else restore_plan.plan_version
        ),
        "selected_node_ids": [_selected_dataset_node(restore_plan)],
    }
    with pytest.raises(
        ServiceForbiddenError if boundary == "foreign-account" else ServiceValidationError
    ):
        fixture.service.execute_plan(
            "project",
            fixture.project_id,
            action_type="restore",
            selection=selection,
            scope=scope,
        )

    assert not _assignment_exists(fixture)
    assert _project_is_trashed(fixture)
    assert _assignment_history(fixture) == history_before
