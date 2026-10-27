"""Real PostgreSQL regressions for project trash lifecycle planning."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
import pytest

from core.application.auth.config import (
    DEFAULT_ACCOUNT_ID,
    DEFAULT_TENANT_ID,
    DEFAULT_WORKSPACE_ID,
)
from core.application.management.command_services_trash import TrashLifecycleService
from core.application.management.errors import ServiceConflictError, ServiceValidationError
from core.application.management.scope import ScopeContext
from core.application.registry.db_store import RegistryDBStore
from core.infrastructure.db.platform_stores import PlatformIdentityStore, PlatformProjectStore

pytestmark = pytest.mark.integration


def _require_audit_database(connection) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT current_database()")
        database_name = str(cur.fetchone()[0])
    if database_name != "flow_steward_audit":
        pytest.skip("destructive lifecycle regression requires the isolated pg-audit database")


def _admin_scope() -> ScopeContext:
    return ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="trash-lifecycle-postgres",
        principal_type="admin",
    )


def _seed_project_dependencies(connection) -> dict[str, str]:
    project_id = str(uuid4())
    workflow_key = f"trash-lifecycle-workflow-{uuid4().hex}"
    dataset_key = f"trash-lifecycle-dataset-{uuid4().hex}"
    connection_id = f"trash-lifecycle-connection-{uuid4().hex}"
    job_id = str(uuid4())
    artifact_id = f"rs5-artifact-{uuid4().hex}"
    extension_id = f"rs5.extension.{uuid4().hex}"
    extension_install_id = f"rs5-install-{uuid4().hex}"
    artifact_object_key = f"artifacts/{artifact_id}/blob"
    with connection.cursor() as cur:
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
                DEFAULT_ACCOUNT_ID,
                f"lifecycle-project-{project_id}",
            ),
        )
        cur.execute(
            """
            INSERT INTO registry_workflows
                (workflow_key, display_name, account_id, scope_kind)
            VALUES (%s, %s, %s, 'account')
            """,
            (workflow_key, "Lifecycle workflow", DEFAULT_ACCOUNT_ID),
        )
        cur.execute(
            """
            INSERT INTO project_workflows (project_id, workflow_id, is_default, enabled)
            VALUES (%s, %s, true, true)
            """,
            (project_id, workflow_key),
        )
        cur.execute(
            """
            INSERT INTO registry_datasets
                (dataset_key, workspace_id, display_name, availability_mode)
            VALUES (%s, %s, %s, 'specific_projects_only')
            """,
            (dataset_key, DEFAULT_WORKSPACE_ID, "Lifecycle dataset"),
        )
        cur.execute(
            "INSERT INTO project_datasets (dataset_id, project_id) VALUES (%s, %s)",
            (dataset_key, project_id),
        )
        cur.execute(
            """
            INSERT INTO project_connections
                (connection_id, project_id, account_id, display_name)
            VALUES (%s, %s, %s, %s)
            """,
            (connection_id, project_id, DEFAULT_ACCOUNT_ID, "Lifecycle connection"),
        )
        cur.execute(
            """
            INSERT INTO extension_installs
                (extension_id, account_id, install_id, installed_by)
            VALUES (%s, %s, %s, 'trash-lifecycle-postgres')
            """,
            (extension_id, DEFAULT_ACCOUNT_ID, extension_install_id),
        )
        cur.execute(
            """
            INSERT INTO extension_project_state
                (account_id, project_id, extension_id, extension_install_id,
                 connection_id, namespace, state_key, value_jsonb, created_by)
            VALUES (%s, %s, %s, %s, %s, 'rs5', 'populated-project',
                    '{"present": true}'::jsonb, 'trash-lifecycle-postgres')
            """,
            (
                DEFAULT_ACCOUNT_ID,
                project_id,
                extension_id,
                extension_install_id,
                connection_id,
            ),
        )
        cur.execute(
            """
            INSERT INTO jobs
                (id, account_id, tenant_id, workspace_id, project_id, task_id,
                 task_summary, status, finished_at, meta)
            VALUES (%s, %s, %s, %s, %s, %s, 'Populated lifecycle job',
                    'completed', now(), %s::jsonb)
            """,
            (
                job_id,
                DEFAULT_ACCOUNT_ID,
                DEFAULT_TENANT_ID,
                DEFAULT_WORKSPACE_ID,
                project_id,
                f"rs5-populated-{job_id}",
                json.dumps(
                    {
                        "files": [],
                        "workflow_outputs": {
                            "artifacts": {
                                "report": {
                                    "artifact_id": artifact_id,
                                    "artifact_handle": f"artifact:{artifact_id}",
                                }
                            }
                        },
                    }
                ),
            ),
        )
        cur.execute(
            """
            INSERT INTO platform_agent_messages
                (job_id, correlation_id, from_agent, to_agent, step_id, skill,
                 status, summary, artifacts, envelope)
            VALUES (%s, %s, 'builder', 'steward', 'rs5', 'release-stabilization',
                    'completed', 'Populated lifecycle message', %s::jsonb, %s::jsonb)
            """,
            (
                job_id,
                f"rs5-correlation-{job_id}",
                json.dumps(
                    {
                        "artifact_id": artifact_id,
                        "object_key": artifact_object_key,
                    }
                ),
                json.dumps({"schema_version": "agent_message_v1"}),
            ),
        )
    connection.commit()
    return {
        "project_id": project_id,
        "workflow_id": workflow_key,
        "dataset_id": dataset_key,
        "connection_id": connection_id,
        "job_id": job_id,
        "artifact_id": artifact_id,
        "artifact_object_key": artifact_object_key,
        "extension_id": extension_id,
        "extension_install_id": extension_install_id,
    }


def _cleanup_project_dependencies(connection, fixture: dict[str, str]) -> None:
    connection.rollback()
    with connection.cursor() as cur:
        cur.execute("DELETE FROM platform_agent_messages WHERE job_id = %s", (fixture["job_id"],))
        cur.execute("DELETE FROM jobs WHERE id = %s", (fixture["job_id"],))
        cur.execute(
            "DELETE FROM extension_project_state WHERE extension_install_id = %s",
            (fixture["extension_install_id"],),
        )
        cur.execute(
            "DELETE FROM project_connections WHERE project_id = %s", (fixture["project_id"],)
        )
        cur.execute("DELETE FROM project_datasets WHERE project_id = %s", (fixture["project_id"],))
        cur.execute("DELETE FROM project_workflows WHERE project_id = %s", (fixture["project_id"],))
        cur.execute(
            "DELETE FROM registry_datasets WHERE dataset_key = %s", (fixture["dataset_id"],)
        )
        cur.execute(
            "DELETE FROM registry_workflows WHERE workflow_key = %s", (fixture["workflow_id"],)
        )
        cur.execute(
            "DELETE FROM platform_action_log WHERE task_id = %s",
            (f"lifecycle:project:{fixture['project_id']}",),
        )
        cur.execute("DELETE FROM projects WHERE id = %s", (fixture["project_id"],))
        cur.execute(
            "DELETE FROM extension_installs WHERE install_id = %s",
            (fixture["extension_install_id"],),
        )
    connection.commit()


class _MemoryArtifactStorage:
    def __init__(self, fixture: dict[str, str]) -> None:
        self.objects = {
            fixture["artifact_object_key"]: b"rs5-populated-project-artifact\n",
            f"artifacts/_metadata/{fixture['artifact_id']}.json": json.dumps(
                {
                    "artifact_id": fixture["artifact_id"],
                    "object_key": fixture["artifact_object_key"],
                    "owner_scope": {
                        "project_id": fixture["project_id"],
                        "job_id": fixture["job_id"],
                    },
                }
            ).encode(),
        }

    def get(self, key: str):
        return self.objects.get(key)

    def delete(self, key: str) -> bool:
        return self.objects.pop(key, None) is not None

    def put(self, key: str, body: bytes, **_kwargs):
        payload = bytes(body)
        self.objects[str(key)] = payload
        return {"key": str(key), "size_bytes": len(payload)}

    def stat(self, key: str):
        return None if key not in self.objects else {"size": len(self.objects[key])}

    def list_key_page(
        self,
        prefix: str,
        *,
        limit: int,
        continuation_token: str | None = None,
    ) -> tuple[list[str], str | None]:
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        start = int(continuation_token or 0)
        page = keys[start : start + limit]
        next_offset = start + len(page)
        return page, str(next_offset) if next_offset < len(keys) else None


def _artifact_plane(fixture: dict[str, str]) -> tuple[SimpleNamespace, _MemoryArtifactStorage]:
    storage = _MemoryArtifactStorage(fixture)
    return SimpleNamespace(storage=storage), storage


def _assert_artifact_purge_committed(
    storage: _MemoryArtifactStorage, fixture: dict[str, str]
) -> None:
    assert fixture["artifact_object_key"] not in storage.objects
    assert f"artifacts/_metadata/{fixture['artifact_id']}.json" not in storage.objects
    cleanup_journals = [
        body for key, body in storage.objects.items() if key.startswith("_replay_job_cleanup/")
    ]
    assert len(cleanup_journals) == 1
    assert json.loads(cleanup_journals[0])["state"] == "committed"


def _project_lifecycle_counts(connection, project_id: str) -> dict[str, int]:
    queries = {
        "projects": "SELECT count(*) FROM projects WHERE id = %s",
        "project_workflows": (
            "SELECT count(*) FROM project_workflows WHERE project_id = %s AND deleted_at IS NULL"
        ),
        "project_datasets": "SELECT count(*) FROM project_datasets WHERE project_id = %s",
        "project_connections": (
            "SELECT count(*) FROM project_connections WHERE project_id = %s AND enabled"
        ),
        "jobs": "SELECT count(*) FROM jobs WHERE project_id = %s",
        "platform_agent_messages": (
            "SELECT count(*) FROM platform_agent_messages m "
            "JOIN jobs j ON j.id = m.job_id WHERE j.project_id = %s"
        ),
        "extension_project_state": (
            "SELECT count(*) FROM extension_project_state WHERE project_id = %s"
        ),
    }
    counts: dict[str, int] = {}
    with connection.cursor() as cur:
        for table_name, query in queries.items():
            cur.execute(query, (project_id,))
            counts[table_name] = int(cur.fetchone()[0])
    return counts


def test_dataset_catalog_resolves_a_trashed_project_only_in_its_account(
    pg_conn,
    default_account,
) -> None:
    del default_account
    _require_audit_database(pg_conn)
    project_id = uuid4()
    dataset_key = f"trash-lifecycle-{uuid4().hex}"
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO projects
                (id, tenant_id, workspace_id, account_id, name, deleted_at)
            VALUES (%s, %s, %s, %s, %s, now())
            """,
            (
                project_id,
                DEFAULT_TENANT_ID,
                DEFAULT_WORKSPACE_ID,
                DEFAULT_ACCOUNT_ID,
                f"trashed-project-{project_id}",
            ),
        )
        cur.execute(
            """
            INSERT INTO registry_datasets
                (dataset_key, workspace_id, display_name, availability_mode)
            VALUES (%s, %s, %s, 'specific_projects_only')
            """,
            (dataset_key, DEFAULT_WORKSPACE_ID, "Lifecycle dataset"),
        )
        cur.execute(
            "INSERT INTO project_datasets (dataset_id, project_id) VALUES (%s, %s)",
            (dataset_key, project_id),
        )
    pg_conn.commit()

    store = RegistryDBStore(os.environ["FS_DB_DSN"])
    try:
        catalog = store.list_project_dataset_catalog(
            str(project_id),
            include_trashed=True,
            account_id=DEFAULT_ACCOUNT_ID,
        )

        assert catalog["project_id"] == str(project_id)
        assert catalog["workspace_id"] == DEFAULT_WORKSPACE_ID
        assert catalog["explicit_dataset_ids"] == [dataset_key]
        assert [row["dataset_id"] for row in catalog["available_datasets"]] == [dataset_key]

        _raises_input_472_1 = str(project_id)
        with pytest.raises(KeyError, match="Unknown project"):
            store.list_project_dataset_catalog(
                _raises_input_472_1,
                include_trashed=True,
                account_id="00000000-0000-0000-0000-000000000099",
            )
    finally:
        if store._conn is not None:
            store._conn.close()
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM project_datasets WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM registry_datasets WHERE dataset_key = %s", (dataset_key,))
            cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        pg_conn.commit()


def test_active_project_can_be_trashed_planned_and_restored_with_dependencies(
    pg_conn,
    default_account,
) -> None:
    del default_account
    _require_audit_database(pg_conn)
    fixture = _seed_project_dependencies(pg_conn)
    dsn = os.environ["FS_DB_DSN"]
    project_store = PlatformProjectStore(dsn)
    dataset_store = RegistryDBStore(dsn)
    service = TrashLifecycleService(
        get_logger=lambda: project_store,
        get_project_dataset_catalog=lambda: dataset_store,
    )
    try:
        trashed = service.trash(
            "project",
            fixture["project_id"],
            deleted_by="trash-lifecycle-postgres",
            scope=_admin_scope(),
        )
        assert trashed.status == "trashed"

        plan = service.plan(
            "project",
            fixture["project_id"],
            action_type="restore",
            scope=_admin_scope(),
        )
        assert plan.validation_errors == []
        assert {child["entity_type"] for child in plan.tree["children"]} == {
            "project_workflow",
            "project_dataset",
            "project_connection",
        }

        restored = service.execute_plan(
            "project",
            fixture["project_id"],
            action_type="restore",
            selection={"plan_version": plan.plan_version},
            scope=_admin_scope(),
        )
        assert restored.status == "restored"

        with pg_conn.cursor() as cur:
            cur.execute("SELECT deleted_at FROM projects WHERE id = %s", (fixture["project_id"],))
            assert cur.fetchone()[0] is None
            cur.execute(
                "SELECT workflow_id FROM project_workflows WHERE project_id = %s",
                (fixture["project_id"],),
            )
            assert [row[0] for row in cur.fetchall()] == [fixture["workflow_id"]]
            cur.execute(
                "SELECT dataset_id FROM project_datasets WHERE project_id = %s",
                (fixture["project_id"],),
            )
            assert [row[0] for row in cur.fetchall()] == [fixture["dataset_id"]]
            cur.execute(
                "SELECT connection_id FROM project_connections WHERE project_id = %s",
                (fixture["project_id"],),
            )
            assert [row[0] for row in cur.fetchall()] == [fixture["connection_id"]]
    finally:
        project_store.conn.close()
        if dataset_store._conn is not None:
            dataset_store._conn.close()
        _cleanup_project_dependencies(pg_conn, fixture)


def test_project_hard_delete_rejects_active_job_without_mutation(
    pg_conn,
    default_account,
) -> None:
    del default_account
    _require_audit_database(pg_conn)
    fixture = _seed_project_dependencies(pg_conn)
    dsn = os.environ["FS_DB_DSN"]
    project_store = PlatformProjectStore(dsn)
    service = TrashLifecycleService(get_logger=lambda: project_store)
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'in_progress', finished_at = NULL WHERE id = %s",
                (fixture["job_id"],),
            )
        pg_conn.commit()
        service.trash(
            "project",
            fixture["project_id"],
            deleted_by="trash-lifecycle-postgres",
            scope=_admin_scope(),
        )

        _raises_input_607_1 = _admin_scope()
        with pytest.raises(ServiceConflictError) as exc_info:
            service.hard_delete("project", fixture["project_id"], scope=_raises_input_607_1)

        assert exc_info.value.payload["code"] == "project_has_unsettled_jobs"
        with pg_conn.cursor() as cur:
            cur.execute("SELECT deleted_at FROM projects WHERE id = %s", (fixture["project_id"],))
            assert cur.fetchone()[0] is not None
            cur.execute("SELECT status FROM jobs WHERE id = %s", (fixture["job_id"],))
            assert cur.fetchone()[0] == "in_progress"
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'completed', finished_at = now() WHERE id = %s",
                (fixture["job_id"],),
            )
        pg_conn.commit()
        project_store.conn.close()
        _cleanup_project_dependencies(pg_conn, fixture)


def test_changed_dependency_graph_rejects_the_stale_hard_delete_plan(
    pg_conn,
    default_account,
) -> None:
    del default_account
    _require_audit_database(pg_conn)
    fixture = _seed_project_dependencies(pg_conn)
    dsn = os.environ["FS_DB_DSN"]
    project_store = PlatformProjectStore(dsn)
    dataset_store = RegistryDBStore(dsn)
    service = TrashLifecycleService(
        get_logger=lambda: project_store,
        get_project_dataset_catalog=lambda: dataset_store,
    )
    added_connection_id = f"trash-lifecycle-added-{uuid4().hex}"
    try:
        service.trash(
            "project",
            fixture["project_id"],
            deleted_by="trash-lifecycle-postgres",
            scope=_admin_scope(),
        )
        plan = service.plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            scope=_admin_scope(),
        )
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO project_connections
                    (connection_id, project_id, account_id, display_name)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    added_connection_id,
                    fixture["project_id"],
                    DEFAULT_ACCOUNT_ID,
                    "Added after planning",
                ),
            )
        pg_conn.commit()

        _raises_input_675_1 = _admin_scope()
        with pytest.raises(ServiceValidationError, match="Lifecycle plan is stale"):
            service.execute_plan(
                "project",
                fixture["project_id"],
                action_type="hard_delete",
                selection={"plan_version": plan.plan_version},
                scope=_raises_input_675_1,
            )

        with pg_conn.cursor() as cur:
            cur.execute("SELECT deleted_at FROM projects WHERE id = %s", (fixture["project_id"],))
            assert cur.fetchone()[0] is not None
            cur.execute(
                "SELECT count(*) FROM project_connections WHERE project_id = %s",
                (fixture["project_id"],),
            )
            assert cur.fetchone()[0] == 2
    finally:
        project_store.conn.close()
        if dataset_store._conn is not None:
            dataset_store._conn.close()
        _cleanup_project_dependencies(pg_conn, fixture)


def test_project_hard_delete_rolls_back_after_child_failure(
    pg_conn,
    default_account,
) -> None:
    del default_account
    _require_audit_database(pg_conn)
    fixture = _seed_project_dependencies(pg_conn)
    dsn = os.environ["FS_DB_DSN"]
    project_store = PlatformProjectStore(dsn)
    dataset_store = RegistryDBStore(dsn)
    artifact_plane, artifact_storage = _artifact_plane(fixture)
    service = TrashLifecycleService(
        get_logger=lambda: project_store,
        artifact_plane=artifact_plane,
        get_project_dataset_catalog=lambda: dataset_store,
    )
    try:
        service.trash(
            "project",
            fixture["project_id"],
            deleted_by="trash-lifecycle-postgres",
            scope=_admin_scope(),
        )
        plan = service.plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            scope=_admin_scope(),
        )
        before = _project_lifecycle_counts(pg_conn, fixture["project_id"])
        real_action = service._execute_tree_node_action

        def fail_after_first_child(*args, **kwargs) -> None:
            real_action(*args, **kwargs)
            raise RuntimeError("injected lifecycle failure")

        service._execute_tree_node_action = fail_after_first_child
        _raises_input_736_1 = _admin_scope()
        with pytest.raises(RuntimeError, match="injected lifecycle failure"):
            service.execute_plan(
                "project",
                fixture["project_id"],
                action_type="hard_delete",
                selection={"plan_version": plan.plan_version},
                scope=_raises_input_736_1,
            )

        assert _project_lifecycle_counts(pg_conn, fixture["project_id"]) == before
        assert artifact_storage.objects[fixture["artifact_object_key"]] == (
            b"rs5-populated-project-artifact\n"
        )
    finally:
        project_store.conn.close()
        if dataset_store._conn is not None:
            dataset_store._conn.close()
        _cleanup_project_dependencies(pg_conn, fixture)


def test_incomplete_root_hard_delete_selection_rejects_without_mutation(
    pg_conn,
    default_account,
) -> None:
    del default_account
    _require_audit_database(pg_conn)
    fixture = _seed_project_dependencies(pg_conn)
    dsn = os.environ["FS_DB_DSN"]
    project_store = PlatformProjectStore(dsn)
    dataset_store = RegistryDBStore(dsn)
    service = TrashLifecycleService(
        get_logger=lambda: project_store,
        get_project_dataset_catalog=lambda: dataset_store,
    )
    try:
        service.trash(
            "project",
            fixture["project_id"],
            deleted_by="trash-lifecycle-postgres",
            scope=_admin_scope(),
        )
        plan = service.plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            scope=_admin_scope(),
        )
        before = _project_lifecycle_counts(pg_conn, fixture["project_id"])
        root_id = str(plan.tree["node_id"])
        child_id = str(plan.tree["children"][0]["node_id"])

        _raises_input_787_1 = _admin_scope()
        with pytest.raises(ServiceValidationError, match="complete descendant selection"):
            service.execute_plan(
                "project",
                fixture["project_id"],
                action_type="hard_delete",
                selection={
                    "plan_version": plan.plan_version,
                    "selected_node_ids": [root_id, child_id],
                },
                scope=_raises_input_787_1,
            )

        assert _project_lifecycle_counts(pg_conn, fixture["project_id"]) == before
    finally:
        project_store.conn.close()
        if dataset_store._conn is not None:
            dataset_store._conn.close()
        _cleanup_project_dependencies(pg_conn, fixture)


def test_partial_dataset_hard_delete_is_idempotent_on_fresh_replan(
    pg_conn,
    default_account,
) -> None:
    del default_account
    _require_audit_database(pg_conn)
    fixture = _seed_project_dependencies(pg_conn)
    dsn = os.environ["FS_DB_DSN"]
    project_store = PlatformProjectStore(dsn)
    dataset_store = RegistryDBStore(dsn)
    audit_store = PlatformIdentityStore(dsn)
    service = TrashLifecycleService(
        get_logger=lambda: project_store,
        get_project_dataset_catalog=lambda: dataset_store,
        get_audit_store=lambda: audit_store,
    )
    try:
        service.trash(
            "project",
            fixture["project_id"],
            deleted_by="trash-lifecycle-postgres",
            scope=_admin_scope(),
        )
        plan = service.plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            scope=_admin_scope(),
        )
        dataset_node_id = next(
            str(child["node_id"])
            for child in plan.tree["children"]
            if child["entity_type"] == "project_dataset"
        )
        first = service.execute_plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            selection={
                "plan_version": plan.plan_version,
                "selected_node_ids": [dataset_node_id],
            },
            scope=_admin_scope(),
        )
        assert first.status == "updated"

        retry_plan = service.plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            scope=_admin_scope(),
        )
        second = service.execute_plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            selection={
                "plan_version": retry_plan.plan_version,
                "selected_node_ids": [dataset_node_id],
            },
            scope=_admin_scope(),
        )
        assert second.status == "updated"
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM project_datasets WHERE project_id = %s",
                (fixture["project_id"],),
            )
            assert cur.fetchone()[0] == 0
    finally:
        project_store.conn.close()
        audit_store.conn.close()
        if dataset_store._conn is not None:
            dataset_store._conn.close()
        _cleanup_project_dependencies(pg_conn, fixture)


def test_project_hard_delete_clean_retry_is_not_partially_applied(
    pg_conn,
    default_account,
) -> None:
    del default_account
    _require_audit_database(pg_conn)
    fixture = _seed_project_dependencies(pg_conn)
    dsn = os.environ["FS_DB_DSN"]
    project_store = PlatformProjectStore(dsn)
    dataset_store = RegistryDBStore(dsn)
    audit_store = PlatformIdentityStore(dsn)
    artifact_plane, artifact_storage = _artifact_plane(fixture)
    service = TrashLifecycleService(
        get_logger=lambda: project_store,
        artifact_plane=artifact_plane,
        get_project_dataset_catalog=lambda: dataset_store,
        get_audit_store=lambda: audit_store,
    )
    try:
        service.trash(
            "project",
            fixture["project_id"],
            deleted_by="trash-lifecycle-postgres",
            scope=_admin_scope(),
        )
        plan = service.plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            scope=_admin_scope(),
        )
        real_action = service._execute_tree_node_action

        def fail_after_first_child(*args, **kwargs) -> None:
            real_action(*args, **kwargs)
            raise RuntimeError("injected lifecycle failure")

        service._execute_tree_node_action = fail_after_first_child
        _raises_input_922_1 = _admin_scope()
        with pytest.raises(RuntimeError, match="injected lifecycle failure"):
            service.execute_plan(
                "project",
                fixture["project_id"],
                action_type="hard_delete",
                selection={"plan_version": plan.plan_version},
                scope=_raises_input_922_1,
            )

        service._execute_tree_node_action = real_action
        retry_plan = service.plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            scope=_admin_scope(),
        )
        result = service.execute_plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            selection={"plan_version": retry_plan.plan_version},
            scope=_admin_scope(),
        )
        assert result.status == "hard_deleted"
        _assert_artifact_purge_committed(artifact_storage, fixture)
    finally:
        project_store.conn.close()
        audit_store.conn.close()
        if dataset_store._conn is not None:
            dataset_store._conn.close()
        _cleanup_project_dependencies(pg_conn, fixture)


def test_project_hard_delete_plans_and_atomically_purges_mcp_projections(
    disposable_db,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FS_DB_DSN", disposable_db.dsn)
    pg_conn = psycopg.connect(disposable_db.dsn)
    fixture = _seed_project_dependencies(pg_conn)
    connector_id = f"mcp-project-delete-{uuid4().hex}"
    tool_id = f"mcp.project.delete.{uuid4().hex}"
    dsn = os.environ["FS_DB_DSN"]
    project_store = PlatformProjectStore(dsn)
    dataset_store = RegistryDBStore(dsn)
    audit_store = PlatformIdentityStore(dsn)
    artifact_plane, artifact_storage = _artifact_plane(fixture)
    service = TrashLifecycleService(
        get_logger=lambda: project_store,
        artifact_plane=artifact_plane,
        get_project_dataset_catalog=lambda: dataset_store,
        get_audit_store=lambda: audit_store,
    )
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_connectors
                       (connector_id, account_id, project_id, tenant_id,
                        display_name, base_url, auth_mode, auth_status,
                        oauth_metadata_jsonb, oauth_secret_refs_jsonb,
                        oauth_connected_identity)
                    VALUES (%s, %s, %s, %s, 'Project-owned MCP',
                            'https://mcp.example.test/mcp', 'oauth2', 'connected',
                            %s::jsonb, %s::jsonb, 'alice@example.test')""",
                (
                    connector_id,
                    DEFAULT_ACCOUNT_ID,
                    fixture["project_id"],
                    DEFAULT_TENANT_ID,
                    json.dumps(
                        {
                            "discovery": {
                                "connector_endpoint": "https://mcp.example.test/mcp",
                                "authorization_server": {
                                    "revocation_endpoint": "https://auth.example.test/revoke"
                                },
                            },
                            "registration": {"token_endpoint_auth_method": "none"},
                            "token": {"connector_endpoint": "https://mcp.example.test/mcp"},
                        }
                    ),
                    json.dumps(
                        {
                            "refs": {
                                "_oauth_access_token": f"mcp_connector:{connector_id}:_oauth_access_token"
                            }
                        }
                    ),
                ),
            )
            cur.execute(
                """INSERT INTO secret_refs
                       (account_id, project_id, tenant_id, ref_key, encrypted_value)
                    VALUES (%s, %s, %s, %s, 'encrypted')""",
                (
                    DEFAULT_ACCOUNT_ID,
                    fixture["project_id"],
                    DEFAULT_TENANT_ID,
                    f"mcp_connector:{connector_id}:_oauth_access_token",
                ),
            )
            cur.execute(
                """INSERT INTO registry_tools
                       (tool_id, display_name, account_id, project_id, scope_kind,
                        implementation, config)
                    VALUES (%s, 'Project-owned MCP tool', %s, %s, 'project',
                            %s::jsonb, '{}'::jsonb)""",
                (
                    tool_id,
                    DEFAULT_ACCOUNT_ID,
                    fixture["project_id"],
                    json.dumps(
                        {
                            "type": "mcp",
                            "connector_id": connector_id,
                            "owner_account_id": DEFAULT_ACCOUNT_ID,
                            "owner_project_id": fixture["project_id"],
                            "owner_tenant_id": DEFAULT_TENANT_ID,
                        }
                    ),
                ),
            )
        pg_conn.commit()

        service.trash(
            "project",
            fixture["project_id"],
            deleted_by="trash-lifecycle-postgres",
            scope=_admin_scope(),
        )
        plan = service.plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            scope=_admin_scope(),
        )
        assert plan.impact_summary["mcp_connectors"] == 1
        assert plan.impact_summary["mcp_tools"] == 1

        remote_revoke = MagicMock(return_value={})
        _raises_input_1060_1 = _admin_scope()
        with (
            patch(
                "core.application.auth.mcp_connector_oauth._resolve_connector_oauth_secrets",
                return_value={"_oauth_access_token": "provider-token"},
            ),
            patch(
                "core.application.auth.mcp_connector_oauth._post_form",
                remote_revoke,
            ),
            patch.object(
                service,
                "_purge_project_owned_postgres_rows",
                side_effect=RuntimeError("rollback after OAuth cleanup"),
            ),
            pytest.raises(RuntimeError, match="rollback after OAuth cleanup"),
        ):
            service.execute_plan(
                "project",
                fixture["project_id"],
                action_type="hard_delete",
                selection={"plan_version": plan.plan_version},
                scope=_raises_input_1060_1,
            )
        remote_revoke.assert_not_called()
        # Storage purge is irreversible; its journal stays complete until SQL commits.
        cleanup_journals = [
            json.loads(body)
            for key, body in artifact_storage.objects.items()
            if key.startswith("_replay_job_cleanup/")
        ]
        assert len(cleanup_journals) == 1
        assert cleanup_journals[0]["state"] == "complete"
        assert (
            pg_conn.execute(
                "SELECT count(*) FROM jobs WHERE id = %s",
                (fixture["job_id"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            pg_conn.execute(
                "SELECT count(*) FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            ).fetchone()[0]
            == 1
        )

        with (
            patch(
                "core.application.auth.mcp_connector_oauth._resolve_connector_oauth_secrets",
                return_value={"_oauth_access_token": "provider-token"},
            ),
            patch(
                "core.application.auth.mcp_connector_oauth._post_form",
                remote_revoke,
            ),
        ):
            result = service.execute_plan(
                "project",
                fixture["project_id"],
                action_type="hard_delete",
                selection={"plan_version": plan.plan_version},
                scope=_admin_scope(),
            )
        assert result.status == "hard_deleted"
        _assert_artifact_purge_committed(artifact_storage, fixture)
        remote_revoke.assert_called_once()
        with pg_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM registry_tools WHERE tool_id = %s", (tool_id,))
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT count(*) FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            )
            assert cur.fetchone()[0] == 0
    finally:
        project_store.conn.close()
        audit_store.conn.close()
        if dataset_store._conn is not None:
            dataset_store._conn.close()
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM registry_tools WHERE tool_id = %s", (tool_id,))
        pg_conn.commit()
        _cleanup_project_dependencies(pg_conn, fixture)
        pg_conn.close()


def test_workspace_hard_delete_revokes_child_mcp_only_after_commit(
    disposable_db,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FS_DB_DSN", disposable_db.dsn)
    workspace_id = str(uuid4())
    project_id = str(uuid4())
    connector_id = f"mcp-workspace-delete-{uuid4().hex}"
    dsn = disposable_db.dsn
    setup = psycopg.connect(dsn)
    project_store = PlatformProjectStore(dsn)
    dataset_store = RegistryDBStore(dsn)
    audit_store = PlatformIdentityStore(dsn)
    scope = _admin_scope().model_copy(
        update={
            "tenant_id": DEFAULT_TENANT_ID,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "allowed_workspace_ids": frozenset({workspace_id}),
            "allowed_project_ids": frozenset({project_id}),
        }
    )
    service = TrashLifecycleService(
        get_logger=lambda: project_store,
        get_project_dataset_catalog=lambda: dataset_store,
        get_audit_store=lambda: audit_store,
    )
    try:
        setup.execute(
            """INSERT INTO workspaces
                   (id, tenant_id, account_id, workspace_key, display_name, deleted_at)
                VALUES (%s, %s, %s, %s, 'MCP workspace delete', now())""",
            (
                workspace_id,
                DEFAULT_TENANT_ID,
                DEFAULT_ACCOUNT_ID,
                f"mcp-workspace-delete-{uuid4().hex}",
            ),
        )
        setup.execute(
            """INSERT INTO projects
                   (id, tenant_id, account_id, workspace_id, name, deleted_at)
                VALUES (%s, %s, %s, %s, 'MCP workspace child', now())""",
            (project_id, DEFAULT_TENANT_ID, DEFAULT_ACCOUNT_ID, workspace_id),
        )
        setup.execute(
            """INSERT INTO mcp_connectors
                   (connector_id, account_id, project_id, tenant_id, display_name,
                    base_url, auth_mode, auth_status, oauth_metadata_jsonb,
                    oauth_secret_refs_jsonb, oauth_connected_identity)
                VALUES (%s, %s, %s, %s, 'Workspace child MCP',
                        'https://mcp.example.test/mcp', 'oauth2', 'connected',
                        %s::jsonb, %s::jsonb, 'alice@example.test')""",
            (
                connector_id,
                DEFAULT_ACCOUNT_ID,
                project_id,
                DEFAULT_TENANT_ID,
                json.dumps(
                    {
                        "discovery": {
                            "connector_endpoint": "https://mcp.example.test/mcp",
                            "authorization_server": {
                                "revocation_endpoint": "https://auth.example.test/revoke"
                            },
                        },
                        "registration": {"token_endpoint_auth_method": "none"},
                        "token": {"connector_endpoint": "https://mcp.example.test/mcp"},
                    }
                ),
                json.dumps(
                    {
                        "refs": {
                            "_oauth_access_token": (
                                f"mcp_connector:{connector_id}:_oauth_access_token"
                            )
                        }
                    }
                ),
            ),
        )
        setup.execute(
            """INSERT INTO secret_refs
                   (account_id, project_id, tenant_id, ref_key, encrypted_value)
                VALUES (%s, %s, %s, %s, 'encrypted')""",
            (
                DEFAULT_ACCOUNT_ID,
                project_id,
                DEFAULT_TENANT_ID,
                f"mcp_connector:{connector_id}:_oauth_access_token",
            ),
        )
        setup.commit()

        plan = service.plan(
            "workspace",
            workspace_id,
            action_type="hard_delete",
            scope=scope,
        )
        remote_revoke = MagicMock(return_value={})
        with (
            patch(
                "core.application.auth.mcp_connector_oauth._resolve_connector_oauth_secrets",
                return_value={"_oauth_access_token": "provider-token"},
            ),
            patch("core.application.auth.mcp_connector_oauth._post_form", remote_revoke),
            patch.object(
                service,
                "_purge_project_owned_postgres_rows",
                side_effect=RuntimeError("workspace rollback after OAuth cleanup"),
            ),
            pytest.raises(RuntimeError, match="workspace rollback"),
        ):
            service.execute_plan(
                "workspace",
                workspace_id,
                action_type="hard_delete",
                selection={"plan_version": plan.plan_version},
                scope=scope,
            )
        remote_revoke.assert_not_called()

        with (
            patch(
                "core.application.auth.mcp_connector_oauth._resolve_connector_oauth_secrets",
                return_value={"_oauth_access_token": "provider-token"},
            ),
            patch("core.application.auth.mcp_connector_oauth._post_form", remote_revoke),
        ):
            result = service.execute_plan(
                "workspace",
                workspace_id,
                action_type="hard_delete",
                selection={"plan_version": plan.plan_version},
                scope=scope,
            )
        assert result.status == "hard_deleted"
        remote_revoke.assert_called_once()
        assert (
            setup.execute(
                "SELECT count(*) FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        project_store.conn.close()
        audit_store.conn.close()
        if dataset_store._conn is not None:
            dataset_store._conn.close()
        setup.rollback()
        setup.execute("DELETE FROM secret_refs WHERE project_id = %s", (project_id,))
        setup.execute("DELETE FROM mcp_connectors WHERE project_id = %s", (project_id,))
        setup.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        setup.execute("DELETE FROM workspaces WHERE id = %s", (workspace_id,))
        setup.commit()
        setup.close()


def test_trashed_project_plan_permanently_deletes_every_planned_dependency(
    pg_conn,
    default_account,
) -> None:
    del default_account
    _require_audit_database(pg_conn)
    fixture = _seed_project_dependencies(pg_conn)
    dsn = os.environ["FS_DB_DSN"]
    project_store = PlatformProjectStore(dsn)
    dataset_store = RegistryDBStore(dsn)
    audit_store = PlatformIdentityStore(dsn)
    artifact_plane, artifact_storage = _artifact_plane(fixture)
    service = TrashLifecycleService(
        get_logger=lambda: project_store,
        artifact_plane=artifact_plane,
        get_project_dataset_catalog=lambda: dataset_store,
        get_audit_store=lambda: audit_store,
    )
    try:
        service.trash(
            "project",
            fixture["project_id"],
            deleted_by="trash-lifecycle-postgres",
            scope=_admin_scope(),
        )
        plan = service.plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            scope=_admin_scope(),
        )
        deleted = service.execute_plan(
            "project",
            fixture["project_id"],
            action_type="hard_delete",
            selection={"plan_version": plan.plan_version},
            scope=_admin_scope(),
        )
        assert deleted.status == "hard_deleted"

        with pg_conn.cursor() as cur:
            for table in (
                "projects",
                "project_workflows",
                "project_datasets",
                "project_connections",
                "jobs",
                "extension_project_state",
            ):
                cur.execute(
                    f"SELECT count(*) FROM {table} WHERE project_id = %s"
                    if table != "projects"
                    else "SELECT count(*) FROM projects WHERE id = %s",
                    (fixture["project_id"],),
                )
                assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT count(*) FROM platform_agent_messages WHERE job_id = %s",
                (fixture["job_id"],),
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                """
                SELECT status
                  FROM platform_action_log
                 WHERE task_id = %s
                   AND action_type = 'lifecycle_hard_delete'
                """,
                (f"lifecycle:project:{fixture['project_id']}",),
            )
            assert cur.fetchone()[0] == "hard_deleted"
        _assert_artifact_purge_committed(artifact_storage, fixture)
    finally:
        project_store.conn.close()
        audit_store.conn.close()
        if dataset_store._conn is not None:
            dataset_store._conn.close()
        _cleanup_project_dependencies(pg_conn, fixture)
