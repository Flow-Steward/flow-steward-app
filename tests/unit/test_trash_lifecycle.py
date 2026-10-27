"""Tests for unified trash / restore / hard-delete lifecycle."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from psycopg import errors as psycopg_errors

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_services import (
    _TRASH_ENTITY_CONFIG,
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
    TrashLifecycleService,
)
from core.application.management.scope import ScopeContext


def _mock_logger():
    bl = MagicMock()
    bl.conn = MagicMock()
    bl._column_exists.return_value = True
    bl._table_exists.return_value = False
    bl.get_workspace.return_value = {
        "id": "ws-default",
        "workspace_key": "ws-default",
        "display_name": "Workspace Default",
        "status": "active",
        "deleted_at": None,
        "account_id": DEFAULT_ACCOUNT_ID,
        "tenant_id": "default",
    }
    bl.get_project.return_value = {
        "id": "1",
        "account_id": DEFAULT_ACCOUNT_ID,
        "workspace_id": "ws-default",
        "name": "Default",
        "deleted_at": None,
    }
    return bl


class _PlanCursor:
    """Cursor stub that routes the two delete-plan queries by their target table."""

    def __init__(self, conn):
        self._conn = conn
        self._rows: list[dict] = []
        self.description = None
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=()):
        self._conn.executed.append((sql, params))
        workspace_id = str(params[0]) if params else ""
        if "FROM workspace_data_connections" in sql:
            self._rows = [
                dict(row)
                for row in self._conn.connections
                if str(row.pop("workspace_id", workspace_id)) == workspace_id
            ]
        elif "FROM registry_datasets" in sql:
            self._rows = [
                dict(row)
                for row in self._conn.datasets
                if str(row.pop("workspace_id", workspace_id)) == workspace_id
            ]
        elif "RETURNING" in sql.upper():
            # UPDATE/DELETE ... RETURNING <id>: report one affected row.
            self._rows = [{"id": workspace_id or "row"}]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _PlanConn:
    """Connection stub exposing only `.cursor()` — the surface production relies on."""

    def __init__(self, *, datasets=(), connections=()):
        self.datasets = [dict(row) for row in datasets]
        self.connections = [dict(row) for row in connections]
        self.executed: list[tuple] = []

    def cursor(self, *_args, **_kwargs):
        return _PlanCursor(self)


class _PlanStore:
    """Store shaped like production's PlatformWorkerStore.

    It deliberately does NOT define `list_datasets` or
    `list_workspace_data_connections`. A MagicMock auto-creates those attributes,
    which is why the delete plan could return an empty connection list in
    production while every mock-based test passed.
    """

    def __init__(self, conn):
        self.conn = conn

    def _column_exists(self, *_args, **_kwargs):
        return True

    def _table_exists(self, *_args, **_kwargs):
        return True


class _ProjectPlanStoreWithoutDependencyPorts:
    """Concrete regression double for the audited PlatformWorkerStore seam."""

    def __init__(self):
        self.conn = _PlanConn()

    def get_project(self, project_id, *, include_trashed=False):
        _ = include_trashed
        return {
            "id": str(project_id),
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": "ws-default",
            "name": "Audited project",
            "deleted_at": None,
        }


class _ProjectPlanStoreWithDependencies(_ProjectPlanStoreWithoutDependencyPorts):
    def __init__(self):
        super().__init__()
        self.workflow_count = 9

    def get_project(self, project_id, *, include_trashed=False):
        project = super().get_project(project_id, include_trashed=include_trashed)
        project["deleted_at"] = "2026-08-11T00:00:00Z"
        return project

    def get_project_workflows(self, project_id, enabled_only=True, *, include_deleted=True):
        _ = (enabled_only, include_deleted)
        return [
            {
                "project_id": str(project_id),
                "workflow_id": f"workflow-{index}",
                "workflow_display_name": f"Workflow {index}",
                "is_default": index == 0,
                "enabled": True,
                "deleted_at": None,
            }
            for index in range(self.workflow_count)
        ]

    def list_project_connections(self, project_id, *, enabled_only=True):
        _ = (project_id, enabled_only)
        return []


class _ProjectDatasetCatalog:
    def get_dataset(self, dataset_id, *, include_trashed=False):
        _ = include_trashed
        return {"dataset_id": str(dataset_id), "workspace_id": "ws-default", "deleted_at": None}

    def list_project_dataset_catalog(
        self,
        project_id,
        *,
        include_trashed=False,
        account_id=None,
    ):
        assert include_trashed is True
        assert account_id == DEFAULT_ACCOUNT_ID
        return {
            "project_id": str(project_id),
            "available_datasets": [],
            "explicit_dataset_ids": [],
        }


class _NoMutationTrashLifecycleService(TrashLifecycleService):
    def _execute_lifecycle_selection(self, *_args, **_kwargs):
        return {"root_applied": False}

    def _record_hard_delete_audit(self, *_args, **_kwargs):
        return None


def _scope(principal_type: str = "admin") -> ScopeContext:
    return ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="test-user",
        principal_type=principal_type,
    )


def _foreign_account_scope(principal_type: str = "admin") -> ScopeContext:
    return ScopeContext(
        account_id="00000000-0000-0000-0000-000000000099",
        actor_id="foreign-user",
        principal_type=principal_type,
    )


class TestTrashLifecycleService:
    def test_direct_workflow_hard_delete_purges_secrets_in_delete_transaction(self):
        from contextlib import contextmanager

        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("wf_sync",)
        cur.rowcount = 1
        secret_service = SimpleNamespace(purge_workflow_secrets=MagicMock())
        transaction = MagicMock(name="workflow-hard-delete-transaction")

        @contextmanager
        def transaction_connection(_conn):
            yield transaction

        service = TrashLifecycleService(
            get_logger=lambda: bl,
            workflow_secret_default_service=secret_service,
        )
        service._require_direct_entity_in_scope = MagicMock()

        with (
            patch(
                "core.application.workflows.workflow_deletion_guard."
                "assert_workflow_has_no_nonterminal_jobs"
            ),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                transaction_connection,
            ),
        ):
            result = service.hard_delete("registry_workflow", "wf_sync", scope=_scope("admin"))

        assert result.status == "hard_deleted"
        secret_service.purge_workflow_secrets.assert_called_once_with(
            workflow_id="wf_sync",
            account_id=DEFAULT_ACCOUNT_ID,
            transaction=transaction,
        )
        delete_call = next(
            call
            for call in cur.execute.call_args_list
            if "DELETE FROM registry_workflows" in call.args[0]
        )
        assert delete_call.args[1] == ("wf_sync",)

    def test_direct_workflow_hard_delete_rolls_back_when_conditional_delete_loses_race(self):
        from contextlib import contextmanager

        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("wf_sync",)
        cur.rowcount = 0
        audit = MagicMock()
        secret_service = SimpleNamespace(purge_workflow_secrets=MagicMock())
        transaction = MagicMock(name="workflow-race-transaction")
        rolled_back = False

        @contextmanager
        def transaction_connection(_conn):
            nonlocal rolled_back
            try:
                yield transaction
            except Exception:
                rolled_back = True
                raise

        service = TrashLifecycleService(
            get_logger=lambda: bl,
            get_audit_store=lambda: SimpleNamespace(log_action=audit),
            workflow_secret_default_service=secret_service,
        )
        service._require_direct_entity_in_scope = MagicMock()

        _raises_input_289_1 = _scope("admin")
        with (
            patch(
                "core.application.workflows.workflow_deletion_guard."
                "assert_workflow_has_no_nonterminal_jobs"
            ),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                transaction_connection,
            ),
            pytest.raises(ServiceValidationError, match="must be trashed"),
        ):
            service.hard_delete("registry_workflow", "wf_sync", scope=_raises_input_289_1)

        secret_service.purge_workflow_secrets.assert_called_once()
        assert rolled_back is True
        audit.assert_not_called()

    def test_planned_workflow_hard_delete_purges_exact_scope_but_link_delete_does_not(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        secret_service = SimpleNamespace(purge_workflow_secrets=MagicMock())
        transaction = MagicMock(name="plan-transaction")
        service = TrashLifecycleService(
            get_logger=lambda: bl,
            workflow_secret_default_service=secret_service,
        )

        with (
            patch(
                "core.application.workflows.workflow_deletion_guard."
                "assert_workflow_has_no_nonterminal_jobs"
            ),
            patch(
                "core.infrastructure.db.db_pool.active_transaction_connection",
                return_value=transaction,
            ),
        ):
            service._hard_delete_entity_direct(
                bl,
                "registry_workflow",
                "wf_sync",
                account_id=DEFAULT_ACCOUNT_ID,
            )
            service._hard_delete_project_workflow_link(
                bl,
                project_id="project-a",
                workflow_id="wf_link_only",
            )

        secret_service.purge_workflow_secrets.assert_called_once_with(
            workflow_id="wf_sync",
            account_id=DEFAULT_ACCOUNT_ID,
            transaction=transaction,
        )

    def test_registry_workflow_has_a_real_hard_delete_plan_root(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {
            "workflow_id": "wf_sync",
            "display_name": "Sync workflow",
            "deleted_at": "2026-09-06T00:00:00Z",
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._require_lifecycle_root_in_scope = MagicMock()

        plan = service.plan(
            "registry_workflow",
            "wf_sync",
            action_type="hard_delete",
            scope=_scope("admin"),
        )

        assert plan.tree["node_id"] == "registry_workflow:wf_sync"
        assert plan.tree["entity_type"] == "registry_workflow"
        assert plan.tree["selected"] is True
        assert plan.tree["status"] == "trashed"

    def test_hard_delete_audit_uses_the_explicit_audit_store_port(self):
        lifecycle_store = SimpleNamespace()
        audit_store = SimpleNamespace(log_action=MagicMock())
        service = TrashLifecycleService(
            get_logger=lambda: lifecycle_store,
            get_audit_store=lambda: audit_store,
        )

        service._record_hard_delete_audit(
            lifecycle_store,
            entity_type="registry_guardrail",
            entity_id="guardrail-1",
            scope=_scope("admin"),
            status="hard_deleted",
        )

        audit_store.log_action.assert_called_once()
        assert audit_store.log_action.call_args.kwargs["action_type"] == "lifecycle_hard_delete"

    def test_execute_rejects_a_dependency_snapshot_that_changed_after_plan(self):
        store = _ProjectPlanStoreWithDependencies()
        dataset_catalog = _ProjectDatasetCatalog()
        service = _NoMutationTrashLifecycleService(
            get_logger=lambda: store,
            get_project_dataset_catalog=lambda: dataset_catalog,
        )
        plan = service.plan(
            "project",
            "project-with-nine-bindings",
            action_type="hard_delete",
            scope=_scope("admin"),
        )
        store.workflow_count = 10

        _raises_input_402_1 = _scope("admin")
        with pytest.raises(ServiceValidationError, match="Lifecycle plan is stale"):
            service.execute_plan(
                "project",
                "project-with-nine-bindings",
                action_type="hard_delete",
                selection={
                    "selected_node_ids": ["project:project-with-nine-bindings"],
                    "plan_version": plan.plan_version,
                },
                scope=_raises_input_402_1,
            )

    def test_project_plan_reports_all_nine_audited_workflow_bindings(self):
        store = _ProjectPlanStoreWithDependencies()
        dataset_catalog = _ProjectDatasetCatalog()

        payload = TrashLifecycleService(
            get_logger=lambda: store,
            get_project_dataset_catalog=lambda: dataset_catalog,
        ).plan(
            "project",
            "project-with-nine-bindings",
            action_type="hard_delete",
            scope=_scope("admin"),
        )

        workflow_children = [
            child
            for child in payload.tree["children"]
            if child["entity_type"] == "project_workflow"
        ]
        assert len(workflow_children) == 9
        assert payload.impact_summary["workflows"] == 9

    def test_project_plan_fails_closed_when_dependency_projection_is_unavailable(self):
        store = _ProjectPlanStoreWithoutDependencyPorts()

        _raises_callable_439_1 = TrashLifecycleService(get_logger=lambda: store).plan
        _raises_input_439_2 = _scope("admin")
        with pytest.raises(
            ServiceUnavailableError,
            match=(
                "project lifecycle dependency projection is unavailable: "
                "get_project_workflows, list_project_dataset_catalog, "
                "get_dataset, list_project_connections"
            ),
        ):
            _raises_callable_439_1(
                "project",
                "project-with-nine-bindings",
                action_type="hard_delete",
                scope=_raises_input_439_2,
            )

    @pytest.mark.parametrize(
        ("entity_type", "entity_id", "ownership_columns", "scope_row"),
        [
            ("registry_data_contract", "contract-1", set(), ("contract-1",)),
            (
                "registry_guardrail",
                "00000000-0000-0000-0000-000000000029",
                {"project_id"},
                ("00000000-0000-0000-0000-000000000029", None),
            ),
        ],
    )
    def test_default_account_admin_can_scope_deployment_global_trash_entities(
        self,
        entity_type,
        entity_id,
        ownership_columns,
        scope_row,
    ):
        bl = _mock_logger()
        bl._column_exists.side_effect = lambda _table, column: column in ownership_columns
        cur = bl.conn.cursor.return_value.__enter__.return_value
        columns = [_TRASH_ENTITY_CONFIG[entity_type]["id_col"], *ownership_columns]
        cur.description = [(column,) for column in columns]
        cur.fetchone.return_value = scope_row

        TrashLifecycleService(get_logger=lambda: bl)._require_direct_entity_in_scope(
            bl,
            cfg=_TRASH_ENTITY_CONFIG[entity_type],
            entity_type=entity_type,
            entity_id=entity_id,
            scope=_scope("admin"),
        )

    @pytest.mark.parametrize(
        ("entity_type", "entity_id", "ownership_columns", "scope_row"),
        [
            ("registry_data_contract", "contract-1", set(), ("contract-1",)),
            (
                "registry_guardrail",
                "00000000-0000-0000-0000-000000000029",
                {"project_id"},
                ("00000000-0000-0000-0000-000000000029", None),
            ),
        ],
    )
    def test_foreign_account_admin_cannot_scope_deployment_global_trash_entities(
        self,
        entity_type,
        entity_id,
        ownership_columns,
        scope_row,
    ):
        bl = _mock_logger()
        bl._column_exists.side_effect = lambda _table, column: column in ownership_columns
        cur = bl.conn.cursor.return_value.__enter__.return_value
        columns = [_TRASH_ENTITY_CONFIG[entity_type]["id_col"], *ownership_columns]
        cur.description = [(column,) for column in columns]
        cur.fetchone.return_value = scope_row

        _raises_callable_514_1 = TrashLifecycleService(
            get_logger=lambda: bl
        )._require_direct_entity_in_scope
        _raises_input_514_2 = _foreign_account_scope("admin")
        with pytest.raises(ServiceForbiddenError, match="Access denied"):
            _raises_callable_514_1(
                bl,
                cfg=_TRASH_ENTITY_CONFIG[entity_type],
                entity_type=entity_type,
                entity_id=entity_id,
                scope=_raises_input_514_2,
            )

    @pytest.mark.parametrize(
        ("entity_type", "entity_id", "ownership_columns", "scope_row"),
        [
            ("registry_data_contract", "contract-1", set(), ("contract-1",)),
            (
                "registry_guardrail",
                "00000000-0000-0000-0000-000000000029",
                {"project_id"},
                ("00000000-0000-0000-0000-000000000029", None),
            ),
        ],
    )
    def test_deployment_global_registry_entity_supports_full_trash_lifecycle(
        self,
        entity_type,
        entity_id,
        ownership_columns,
        scope_row,
    ):
        for action, expected_status in (
            ("trash", "trashed"),
            ("restore", "restored"),
            ("hard_delete", "hard_deleted"),
        ):
            bl = _mock_logger()
            bl._column_exists.side_effect = lambda _table, column: column in ownership_columns
            cur = bl.conn.cursor.return_value.__enter__.return_value
            columns = [_TRASH_ENTITY_CONFIG[entity_type]["id_col"], *ownership_columns]
            cur.description = [(column,) for column in columns]
            cur.fetchone.side_effect = [scope_row, (entity_id,)]

            result = getattr(TrashLifecycleService(get_logger=lambda bl=bl: bl), action)(
                entity_type,
                entity_id,
                scope=_scope("admin"),
            )

            assert result.status == expected_status

    def test_workspace_hard_delete_routes_child_project_through_owned_connection_purge(self):
        bl = _mock_logger()
        workspace_cursor = bl.conn.cursor.return_value.__enter__.return_value
        workspace_cursor.rowcount = 1
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._list_workspace_projects_including_trashed = MagicMock(
            return_value=[
                {
                    "id": "project-1",
                    "name": "Project One",
                    "deleted_at": "2026-08-03T00:00:00Z",
                }
            ]
        )
        bl.list_workspace_data_connections.return_value = []
        service._hard_delete_project_direct = MagicMock()

        service._hard_delete_workspace(bl, "workspace-1")

        service._hard_delete_project_direct.assert_called_once_with(bl, "project-1")
        assert "DELETE FROM workspaces" in workspace_cursor.execute.call_args.args[0]

    def test_workspace_hard_delete_refuses_live_data_connection(self):
        # Store shaped like production's: no lister methods, reads go via conn.
        bl = _PlanStore(
            _PlanConn(
                connections=[
                    {
                        "connection_id": "connection-live",
                        "display_name": "Live",
                        "workspace_id": "workspace-1",
                        "deleted_at": None,
                        "linked_dataset_count": 0,
                    }
                ]
            )
        )
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._list_workspace_projects_including_trashed = MagicMock(return_value=[])

        with pytest.raises(ServiceValidationError, match="live Data Connection"):
            service._hard_delete_workspace(bl, "workspace-1")

    def test_workspace_hard_delete_purges_trashed_data_connection_before_parent(self):
        bl = _PlanStore(
            _PlanConn(
                connections=[
                    {
                        "connection_id": "connection-trashed",
                        "display_name": "Trashed",
                        "workspace_id": "workspace-1",
                        "deleted_at": "2026-08-05T00:00:00Z",
                        "linked_dataset_count": 0,
                    }
                ]
            )
        )
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._list_workspace_projects_including_trashed = MagicMock(return_value=[])

        service._hard_delete_workspace(bl, "workspace-1")

        statements = [sql for sql, _params in bl.conn.executed]
        # The trashed connection must actually have been enumerated, not skipped.
        assert any("FROM workspace_data_connections" in sql for sql in statements)
        connection_delete = next(
            index
            for index, statement in enumerate(statements)
            if "DELETE FROM workspace_data_connections" in statement
        )
        workspace_delete = next(
            index
            for index, statement in enumerate(statements)
            if "DELETE FROM workspaces" in statement
        )
        assert connection_delete < workspace_delete

    def test_project_owned_purge_explicitly_removes_enabled_connection_rows(self):
        bl = _mock_logger()
        cursor = bl.conn.cursor.return_value.__enter__.return_value
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._project_owned_table_names = MagicMock(
            return_value=[
                "jobs",
                "project_connections",
                "project_connection_sessions",
                "projects",
            ]
        )

        service._purge_project_owned_postgres_rows(bl, "project-1")

        executed = [call.args for call in cursor.execute.call_args_list]
        assert ("DELETE FROM project_connections WHERE project_id = %s", ("project-1",)) in executed
        assert (
            "DELETE FROM project_connection_sessions WHERE project_id = %s",
            ("project-1",),
        ) in executed

    def test_trash_entity_sets_deleted_at(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)

        service = TrashLifecycleService(get_logger=lambda: bl)
        result = service.trash("project", 1, deleted_by="alice", scope=_scope("admin"))

        assert result.status == "trashed"
        assert result.entity_type == "project"
        assert result.entity_id == 1
        cur.execute.assert_called_once()
        sql = cur.execute.call_args[0][0]
        assert "deleted_at" in sql
        assert "deleted_by" in sql

    def test_trash_requires_operator_role(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_681_1 = _scope("")
        with pytest.raises(ServiceForbiddenError):
            service.trash("project", 1, scope=_raises_input_681_1)

    def test_trash_registry_workflow_requires_admin_role(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_688_1 = _scope("operator")
        with pytest.raises(ServiceForbiddenError):
            service.trash("registry_workflow", "wf_sync", scope=_raises_input_688_1)

    def test_trash_registry_workflow_blocks_nonterminal_job(self):
        from contextlib import nullcontext

        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)
        conflict = ServiceConflictError(
            "Workflow has non-terminal dependent jobs",
            payload={"code": "workflow_has_nonterminal_jobs"},
        )

        _raises_input_699_1 = _scope("admin")
        with (
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=nullcontext(bl.conn),
            ),
            patch(
                "core.application.workflows.workflow_trigger_lifecycle."
                "assert_workflow_has_no_nonterminal_jobs",
                side_effect=conflict,
            ) as guard,
            patch(
                "core.application.workflows.workflow_trigger_lifecycle.lock_workflow_owner",
                return_value={"account_id": DEFAULT_ACCOUNT_ID},
            ),
            pytest.raises(ServiceConflictError) as exc_info,
        ):
            service.trash("registry_workflow", "wf_sync", scope=_raises_input_699_1)

        assert exc_info.value.payload["code"] == "workflow_has_nonterminal_jobs"
        guard.assert_called_once_with(
            bl,
            workflow_id="wf_sync",
            account_id=DEFAULT_ACCOUNT_ID,
            cursor=bl.conn.cursor.return_value.__enter__.return_value,
        )

    def test_project_workflow_bulk_delete_blocks_nonterminal_job(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)
        conflict = ServiceConflictError("Workflow has non-terminal dependent jobs")

        with (
            patch(
                "core.application.workflows.workflow_deletion_guard."
                "assert_workflow_has_no_nonterminal_jobs",
                side_effect=conflict,
            ) as guard,
            pytest.raises(ServiceConflictError),
        ):
            service._hard_delete_project_workflow_link(
                bl,
                project_id="project-1",
                workflow_id="wf_sync",
            )

        guard.assert_called_once_with(
            bl,
            workflow_id="wf_sync",
            project_id="project-1",
        )

    def test_project_workflow_hard_delete_is_idempotent_for_already_deleted_link(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 0
        cur.fetchone.return_value = {"deleted_at": "2026-08-14T00:00:00Z"}
        service = TrashLifecycleService(get_logger=lambda: bl)

        service._hard_delete_project_workflow_link(
            bl,
            project_id="project-1",
            workflow_id="wf_sync",
        )

        assert "SELECT deleted_at" in cur.execute.call_args_list[-1].args[0]

    def test_trash_rejects_project_outside_scope_before_update(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "foreign-project",
            "account_id": "acc_other",
            "workspace_id": "ws-foreign",
            "name": "Foreign",
            "deleted_at": None,
        }
        cur = bl.conn.cursor.return_value.__enter__.return_value

        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_769_1 = _scope("admin")
        with pytest.raises(ServiceForbiddenError):
            service.trash(
                "project", "foreign-project", deleted_by="alice", scope=_raises_input_769_1
            )

        cur.execute.assert_not_called()

    def test_restore_requires_admin_role(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_783_1 = _scope("user")
        with pytest.raises(ServiceForbiddenError):
            service.restore("project", 1, scope=_raises_input_783_1)

    def test_restore_sets_deleted_at_to_null_for_generic_entities(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)

        service = TrashLifecycleService(get_logger=lambda: bl)
        result = service.restore("registry_skill", 1, scope=_scope("admin"))

        assert result.status == "restored"
        sql = cur.execute.call_args[0][0]
        assert "deleted_at = NULL" in sql

    def test_workspace_trash_keeps_active_status_and_sets_deleted_at(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            {
                "id": "ws-1",
                "workspace_key": "ws-1",
                "display_name": "Workspace One",
                "status": "active",
                "deleted_at": None,
                "account_id": DEFAULT_ACCOUNT_ID,
                "tenant_id": "default",
            },
            (1,),
        ]

        service = TrashLifecycleService(get_logger=lambda: bl)
        result = service.trash("workspace", "ws-1", deleted_by="alice", scope=_scope("admin"))

        assert result.status == "trashed"
        sql = cur.execute.call_args_list[1].args[0]
        assert "status = 'active'" in sql

    def test_plan_returns_workspace_tree_with_project_children(self):
        bl = _mock_logger()
        bl.get_workspace.return_value = {
            "id": "ws-1",
            "display_name": "Workspace One",
            "workspace_key": "workspace-one",
            "status": "active",
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._list_workspace_projects_including_trashed = MagicMock(
            return_value=[
                {
                    "id": "project-1",
                    "name": "Project One",
                    "workspace_id": "ws-1",
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "deleted_at": "2026-05-11T12:00:00Z",
                }
            ]
        )
        service._build_project_plan_root = MagicMock(
            return_value={
                "node_id": "project:project-1",
                "entity_type": "project",
                "entity_id": "project-1",
                "label": "Project One",
                "selectable": True,
                "selected": True,
                "status": "trashed",
                "children": [],
                "metadata": {},
            }
        )

        payload = service.plan("workspace", "ws-1", action_type="restore", scope=_scope("admin"))

        assert payload.root_entity["entity_type"] == "workspace"
        assert payload.tree["node_id"] == "workspace:ws-1"
        assert payload.tree["children"][0]["node_id"] == "project:project-1"

    def test_workspace_plan_filters_projects_outside_allowed_project_scope(self):
        bl = _mock_logger()
        bl.get_workspace.return_value = {
            "id": "ws-1",
            "display_name": "Workspace One",
            "workspace_key": "workspace-one",
            "status": "active",
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._list_workspace_projects_including_trashed = MagicMock(
            return_value=[
                {
                    "id": "project-1",
                    "name": "Project One",
                    "workspace_id": "ws-1",
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "deleted_at": "2026-05-11T12:00:00Z",
                },
                {
                    "id": "project-2",
                    "name": "Project Two",
                    "workspace_id": "ws-1",
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "deleted_at": "2026-05-11T12:00:00Z",
                },
            ]
        )
        service._build_project_plan_root = MagicMock(
            side_effect=lambda _bl, project_id, action_type, include_root_metadata=False: {
                "node_id": f"project:{project_id}",
                "entity_type": "project",
                "entity_id": project_id,
                "label": project_id,
                "selectable": True,
                "selected": True,
                "status": "trashed",
                "children": [],
                "metadata": {},
            }
        )

        payload = service.plan(
            "workspace",
            "ws-1",
            action_type="restore",
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="test-user",
                principal_type="admin",
                allowed_workspace_ids=frozenset({"ws-1"}),
                allowed_project_ids=frozenset({"project-1"}),
            ),
        )

        assert [child["entity_id"] for child in payload.tree["children"]] == ["project-1"]
        assert payload.tree["selectable"] is False
        assert payload.warnings

    def test_workspace_plan_keeps_same_account_non_default_projects_visible(self):
        bl = _mock_logger()
        bl.get_workspace.return_value = {
            "id": "ws-1",
            "display_name": "Workspace One",
            "workspace_key": "workspace-one",
            "status": "archived",
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._list_workspace_projects_including_trashed = MagicMock(
            return_value=[
                {
                    "id": "project-1",
                    "name": "Project One",
                    "workspace_id": "ws-1",
                    "account_id": "acc_other",
                    "deleted_at": "2026-05-11T12:00:00Z",
                }
            ]
        )
        service._build_project_plan_root = MagicMock(
            return_value={
                "node_id": "project:project-1",
                "entity_type": "project",
                "entity_id": "project-1",
                "label": "Project One",
                "selectable": True,
                "selected": True,
                "status": "trashed",
                "children": [],
                "metadata": {},
            }
        )

        payload = service.plan(
            "workspace",
            "ws-1",
            action_type="restore",
            scope=ScopeContext(
                account_id="acc_other",
                actor_id="test-user",
                principal_type="admin",
                allowed_workspace_ids=frozenset({"ws-1"}),
            ),
        )

        assert [child["entity_id"] for child in payload.tree["children"]] == ["project-1"]
        assert payload.tree["selectable"] is True

    def test_workspace_delete_plan_lists_active_children_and_reassign_targets(self):
        # The store is deliberately NOT a MagicMock: production passes a
        # PlatformWorkerStore, which has no dataset/connection lister methods.
        bl = _PlanStore(
            _PlanConn(
                datasets=[
                    {
                        "dataset_id": "dataset-1",
                        "display_name": "Workspace Dataset",
                        "workspace_id": "ws-1",
                        "deleted_at": None,
                    },
                    {
                        "dataset_id": "dataset-other",
                        "display_name": "Other Dataset",
                        "workspace_id": "ws-2",
                        "deleted_at": None,
                    },
                ],
                connections=[
                    {
                        "connection_id": "connection-1",
                        "display_name": "Workspace Connection",
                        "workspace_id": "ws-1",
                        "deleted_at": None,
                        "linked_dataset_count": 1,
                    }
                ],
            )
        )
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._get_workspace_row_including_trashed_and_scoped = MagicMock(
            return_value={
                "id": "ws-1",
                "display_name": "Workspace One",
                "workspace_key": "workspace-one",
                "status": "active",
                "deleted_at": None,
            }
        )
        service._list_workspace_projects_visible_in_scope = MagicMock(
            return_value=(
                [
                    {
                        "id": "project-1",
                        "name": "Project One",
                        "workspace_id": "ws-1",
                        "account_id": DEFAULT_ACCOUNT_ID,
                        "deleted_at": None,
                    }
                ],
                0,
            )
        )
        service._list_workspace_reassignment_targets = MagicMock(
            return_value=[{"workspace_id": "ws-2", "label": "Workspace Two"}]
        )

        payload = service.workspace_delete_plan("ws-1", scope=_scope("admin"))

        assert payload["workspace"]["workspace_id"] == "ws-1"
        assert payload["projects"][0]["project_id"] == "project-1"
        assert payload["projects"][0]["can_reassign"] is True
        assert payload["target_workspaces"] == [{"workspace_id": "ws-2", "label": "Workspace Two"}]
        assert payload["datasets"] == [
            {
                "dataset_id": "dataset-1",
                "label": "Workspace Dataset",
                "status": "active",
            }
        ]
        assert payload["data_connections"] == [
            {
                "connection_id": "connection-1",
                "label": "Workspace Connection",
                "status": "active",
                "linked_dataset_count": 1,
                "allowed_actions": ["block", "reassign", "delete"],
            }
        ]
        # The plan must be read through the store's own connection, so that the
        # re-read inside the delete transaction observes in-transaction state.
        executed_sql = " ".join(sql for sql, _params in bl.conn.executed)
        assert "FROM workspace_data_connections" in executed_sql
        assert "FROM registry_datasets" in executed_sql
        assert payload["validation_errors"] == []

    def test_registry_entity_trash_and_restore_are_written_to_the_changelog(self):
        """B14 regression: registry lifecycle transitions must be audited.

        Registry entities recorded create/update/delete in registry_changelog but
        nothing when they were trashed or restored, so an entity's history went
        silent exactly when it left circulation.
        """
        for action, method in (("trash", "trash"), ("restore", "restore")):
            bl = _PlanStore(_PlanConn())
            service = TrashLifecycleService(get_logger=lambda bl=bl: bl)
            service._require_direct_entity_in_scope = MagicMock(return_value=None)

            getattr(service, method)("registry_skill", "skill-1", scope=_scope("admin"))

            changelog_writes = [
                (sql, params)
                for sql, params in bl.conn.executed
                if "INSERT INTO registry_changelog" in sql
            ]
            assert changelog_writes, f"{method} wrote no changelog row"
            params = changelog_writes[0][1]
            # (entity_type, entity_id, action, snapshot, author)
            assert len(params) == 5
            assert "actor_id" not in changelog_writes[0][0]
            assert params[0] == "skill"
            assert params[1] == "skill-1"
            assert params[2] == action

    @pytest.mark.parametrize("field", ["account_id", "actor_id", "actor_type"])
    @pytest.mark.parametrize("value", [None, " "])
    def test_lifecycle_changelog_rejects_partial_ownership_before_sql(self, field, value):
        conn = _PlanConn()
        ownership = {"account_id": "account-1", "actor_id": "actor-1", "actor_type": "admin"}
        ownership[field] = value
        cursor = _PlanCursor(conn)
        with pytest.raises(ValueError, match="must be supplied together"):
            TrashLifecycleService._log_registry_changelog_entry(
                cursor,
                entity_type="guardrail",
                entity_id="guardrail-1",
                action="restore",
                snapshot={"lifecycle_action": "restore"},
                **ownership,
            )
        assert conn.executed == []

    @pytest.mark.parametrize(
        ("entity_type", "action", "root_applied", "recorded"),
        [
            ("registry_workflow", "restore", True, True),
            ("registry_workflow", "restore", False, False),
            ("registry_workflow", "hard_delete", True, False),
            ("registry_workflow", "hard_delete", False, False),
            ("project", "restore", True, False),
            ("workspace", "restore", True, False),
        ],
    )
    def test_plan_lifecycle_marker_only_for_applied_workflow_restore(
        self, entity_type, action, root_applied, recorded
    ):
        bl = _PlanStore(_PlanConn())
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._validate_and_execute_lifecycle_plan = MagicMock(
            return_value={"root_applied": root_applied}
        )
        service._record_lifecycle_changelog = MagicMock()
        service._record_hard_delete_audit = MagicMock()
        scope = _scope("admin")

        service.execute_plan(entity_type, "entity-1", action_type=action, scope=scope)

        if recorded:
            service._record_lifecycle_changelog.assert_called_once_with(
                bl,
                entity_type="registry_workflow",
                entity_id="entity-1",
                action="restore",
                actor=scope.actor_name,
                scope=scope,
            )
        else:
            service._record_lifecycle_changelog.assert_not_called()

    def test_lifecycle_changelog_rows_are_not_restorable_content_revisions(self):
        """A lifecycle row carries a marker, not an entity payload.

        restore_entity_revision applies any entry's snapshot through
        apply_entity_snapshot, so replaying a lifecycle row would overwrite a live
        entity with a stub — or resurrect a purged one.
        """
        from core.application.registry.db_store import RegistryDBStore
        from core.application.registry.db_store_common import LIFECYCLE_CHANGELOG_ACTIONS

        store = RegistryDBStore("postgresql://unused/unused")
        store.get_changelog_entry = MagicMock(
            return_value={"id": "7", "action": "trash", "snapshot": {"lifecycle_action": "trash"}}
        )

        assert {"trash", "restore", "hard_delete"} <= set(LIFECYCLE_CHANGELOG_ACTIONS)
        with pytest.raises(ValueError, match="lifecycle transition"):
            store.restore_entity_revision("skill", "skill-1", entry_id="7")

    def test_workspace_delete_plan_enumerates_connections_without_store_lister_methods(self):
        """RS5-010 regression.

        The delete plan is the only interlock guarding workspace deletion: every
        live Data Connection must be enumerated so the caller is forced to choose
        block/reassign/delete. The plan previously looked the listers up with
        getattr() on the store and returned [] when they were absent — which is
        exactly the production shape (PlatformWorkerStore defines neither). The
        interlock then compared empty-to-empty, passed, and deletion orphaned
        live connection rows.
        """
        bl = _PlanStore(
            _PlanConn(
                connections=[
                    {
                        "connection_id": "orphan-candidate",
                        "display_name": "Live Connection",
                        "workspace_id": "ws-1",
                        "deleted_at": None,
                        "linked_dataset_count": 0,
                    }
                ]
            )
        )
        assert not hasattr(bl, "list_workspace_data_connections")
        assert not hasattr(bl, "list_datasets")

        service = TrashLifecycleService(get_logger=lambda: bl)
        service._get_workspace_row_including_trashed_and_scoped = MagicMock(
            return_value={
                "id": "ws-1",
                "display_name": "Workspace One",
                "workspace_key": "workspace-one",
                "status": "active",
                "deleted_at": None,
            }
        )
        service._list_workspace_projects_visible_in_scope = MagicMock(return_value=([], 0))
        service._list_workspace_reassignment_targets = MagicMock(return_value=[])

        payload = service.workspace_delete_plan("ws-1", scope=_scope("admin"))

        assert payload["summary"]["data_connection_count"] == 1
        assert [row["connection_id"] for row in payload["data_connections"]] == ["orphan-candidate"]
        assert payload["data_connections"][0]["allowed_actions"] == [
            "block",
            "reassign",
            "delete",
        ]

    def test_workspace_delete_execute_requires_action_for_every_live_data_connection(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)
        service.workspace_delete_plan = MagicMock(
            return_value={
                "validation_errors": [],
                "projects": [],
                "data_connections": [
                    {
                        "connection_id": "connection-1",
                        "status": "active",
                        "linked_dataset_count": 0,
                    }
                ],
                "target_workspaces": [],
            }
        )

        _raises_input_1171_1 = _scope("admin")
        with pytest.raises(
            ServiceValidationError,
            match="explicit action for every live Data Connection",
        ):
            service.trash_workspace_with_plan("ws-1", scope=_raises_input_1171_1)

    def test_workspace_delete_execute_block_action_prevents_any_mutation(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)
        service.workspace_delete_plan = MagicMock(
            return_value={
                "validation_errors": [],
                "projects": [],
                "data_connections": [
                    {
                        "connection_id": "connection-1",
                        "status": "active",
                        "linked_dataset_count": 0,
                    }
                ],
                "target_workspaces": [],
            }
        )
        service.trash = MagicMock()

        _raises_input_1196_1 = _scope("admin")
        with pytest.raises(
            ServiceValidationError, match="blocked by Data Connection 'connection-1'"
        ):
            service.trash_workspace_with_plan(
                "ws-1",
                connection_actions=[{"connection_id": "connection-1", "action": "block"}],
                scope=_raises_input_1196_1,
            )

        service.trash.assert_not_called()

    def test_workspace_delete_execute_reassigns_data_connection_before_workspace_trash(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)
        service.workspace_delete_plan = MagicMock(
            return_value={
                "validation_errors": [],
                "projects": [],
                "data_connections": [
                    {
                        "connection_id": "connection-1",
                        "status": "active",
                        "linked_dataset_count": 0,
                    }
                ],
                "target_workspaces": [{"workspace_id": "ws-2", "label": "Workspace Two"}],
            }
        )
        service._move_workspace_data_connection_direct = MagicMock()
        service.trash = MagicMock(
            return_value=MagicMock(status="trashed", entity_type="workspace", entity_id="ws-1")
        )

        payload = service.trash_workspace_with_plan(
            "ws-1",
            connection_actions=[
                {
                    "connection_id": "connection-1",
                    "action": "reassign",
                    "target_workspace_id": "ws-2",
                }
            ],
            scope=_scope("admin"),
        )

        service._move_workspace_data_connection_direct.assert_called_once_with(
            bl,
            connection_id="connection-1",
            workspace_id="ws-2",
        )
        assert payload["reassigned_connection_ids"] == ["connection-1"]

    def test_workspace_delete_execute_refuses_delete_with_linked_dataset(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)
        service.workspace_delete_plan = MagicMock(
            return_value={
                "validation_errors": [],
                "projects": [],
                "data_connections": [
                    {
                        "connection_id": "connection-1",
                        "status": "active",
                        "linked_dataset_count": 1,
                    }
                ],
                "target_workspaces": [],
            }
        )

        _raises_input_1266_1 = _scope("admin")
        with pytest.raises(ServiceValidationError, match="has 1 linked dataset"):
            service.trash_workspace_with_plan(
                "ws-1",
                connection_actions=[{"connection_id": "connection-1", "action": "delete"}],
                scope=_raises_input_1266_1,
            )

    def test_workspace_delete_execute_reassigns_selected_projects_before_trashing_workspace(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)
        service.workspace_delete_plan = MagicMock(
            return_value={
                "validation_errors": [],
                "projects": [
                    {"project_id": "project-1", "status": "active"},
                    {"project_id": "project-2", "status": "active"},
                ],
                "target_workspaces": [{"workspace_id": "ws-2", "label": "Workspace Two"}],
            }
        )
        service._move_project_to_workspace_direct = MagicMock()
        service.trash = MagicMock(
            return_value=MagicMock(
                status="trashed",
                entity_type="workspace",
                entity_id="ws-1",
            )
        )

        payload = service.trash_workspace_with_plan(
            "ws-1",
            deleted_by="alice",
            project_actions=[
                {
                    "project_id": "project-1",
                    "action": "reassign",
                    "target_workspace_id": "ws-2",
                }
            ],
            scope=_scope("admin"),
        )

        service._move_project_to_workspace_direct.assert_called_once_with(
            bl,
            project_id="project-1",
            workspace_id="ws-2",
        )
        service.trash.assert_called_once()
        assert payload["reassigned_project_ids"] == ["project-1"]
        assert payload["trashed_project_ids"] == ["project-2"]

    def test_workspace_delete_execute_requires_target_workspace_for_reassign(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)
        service.workspace_delete_plan = MagicMock(
            return_value={
                "validation_errors": [],
                "projects": [{"project_id": "project-1", "status": "active"}],
                "target_workspaces": [{"workspace_id": "ws-2", "label": "Workspace Two"}],
            }
        )

        _raises_input_1328_1 = _scope("admin")
        with pytest.raises(ServiceValidationError, match="must choose a target workspace"):
            service.trash_workspace_with_plan(
                "ws-1",
                project_actions=[{"project_id": "project-1", "action": "reassign"}],
                scope=_raises_input_1328_1,
            )

    def test_plan_requires_workspace_scope(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None

        _raises_callable_1340_1 = TrashLifecycleService(get_logger=lambda: bl).plan
        _raises_input_1340_2 = _scope("admin")
        with pytest.raises(ServiceNotFoundError):
            _raises_callable_1340_1(
                "workspace", "ws-1", action_type="restore", scope=_raises_input_1340_2
            )

    def test_project_plan_requires_allowed_workspace_scope(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-foreign",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }

        _raises_callable_1358_1 = TrashLifecycleService(get_logger=lambda: bl).plan
        _raises_input_1358_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="test-user",
            principal_type="admin",
            allowed_workspace_ids=frozenset({"ws-allowed"}),
        )
        with pytest.raises(ServiceForbiddenError):
            _raises_callable_1358_1(
                "project", "project-1", action_type="restore", scope=_raises_input_1358_2
            )

    def test_project_plan_uses_project_link_nodes_instead_of_registry_assets(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        bl.list_project_workflows.return_value = [
            {
                "workflow_id": "wf-1",
                "workflow_display_name": "Workflow One",
                "enabled": True,
                "deleted_at": None,
            }
        ]
        bl.list_project_connections.return_value = [
            {"connection_id": "conn-1", "display_name": "Connection One", "project_id": "project-1"}
        ]
        bl.list_project_dataset_catalog.return_value = {
            "explicit_dataset_ids": ["dataset-1"],
            "available_datasets": [{"dataset_id": "dataset-1", "display_name": "Dataset One"}],
        }

        payload = TrashLifecycleService(get_logger=lambda: bl).plan(
            "project",
            "project-1",
            action_type="restore",
            scope=_scope("admin"),
        )

        child_types = [child["entity_type"] for child in payload.tree["children"]]
        assert "project_workflow" in child_types
        assert "project_dataset" in child_types
        assert "project_connection" in child_types

    def test_project_plan_leaves_out_a_hooks_storage_workflow(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        bl.list_project_workflows.return_value = [
            {
                "workflow_id": "wf-1",
                "workflow_display_name": "Workflow One",
                "enabled": True,
                "deleted_at": None,
                "hook_storage": False,
            },
            {
                "workflow_id": "wf-hook-storage",
                "workflow_display_name": "Before finish",
                "enabled": True,
                "deleted_at": None,
                "hook_storage": True,
            },
        ]
        bl.list_project_connections.return_value = []
        bl.list_project_dataset_catalog.return_value = {
            "explicit_dataset_ids": [],
            "available_datasets": [],
        }

        payload = TrashLifecycleService(get_logger=lambda: bl).plan(
            "project",
            "project-1",
            action_type="restore",
            scope=_scope("admin"),
        )

        workflow_ids = [
            child["entity_id"]
            for child in payload.tree["children"]
            if child["entity_type"] == "project_workflow"
        ]
        assert workflow_ids == ["wf-1"]

    def test_project_hard_delete_rejects_root_with_incomplete_descendants_before_mutation(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._build_lifecycle_plan = MagicMock(
            return_value={
                "tree": {
                    "node_id": "project:project-1",
                    "entity_type": "project",
                    "entity_id": "project-1",
                    "selectable": True,
                    "selected": True,
                    "children": [
                        {
                            "node_id": "project_workflow:project-1:wf-1",
                            "entity_type": "project_workflow",
                            "entity_id": "wf-1",
                            "selectable": True,
                            "selected": True,
                            "children": [],
                            "metadata": {"project_id": "project-1", "is_default": False},
                        },
                        {
                            "node_id": "project_workflow:project-1:wf-2",
                            "entity_type": "project_workflow",
                            "entity_id": "wf-2",
                            "selectable": True,
                            "selected": True,
                            "children": [],
                            "metadata": {"project_id": "project-1", "is_default": True},
                        },
                        {
                            "node_id": "project_dataset:project-1:dataset-1",
                            "entity_type": "project_dataset",
                            "entity_id": "dataset-1",
                            "selectable": True,
                            "selected": True,
                            "children": [],
                            "metadata": {"project_id": "project-1"},
                        },
                    ],
                    "metadata": {"root_kind": "project"},
                },
                "validation_errors": [],
            }
        )
        service._hard_delete_project_workflow_link = MagicMock()
        service._hard_delete_project_dataset_link = MagicMock()
        service._hard_delete_project_direct = MagicMock()

        _raises_input_1464_1 = _scope("admin")
        with pytest.raises(ServiceValidationError, match="complete descendant selection"):
            service.execute_plan(
                "project",
                "project-1",
                action_type="hard_delete",
                selection={
                    "selected_node_ids": ["project:project-1", "project_workflow:project-1:wf-1"]
                },
                scope=_raises_input_1464_1,
            )

        service._hard_delete_project_workflow_link.assert_not_called()
        service._hard_delete_project_dataset_link.assert_not_called()
        service._hard_delete_project_direct.assert_not_called()

    def test_execute_plan_accepts_newly_selected_previously_unchecked_child_node(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._build_lifecycle_plan = MagicMock(
            return_value={
                "tree": {
                    "node_id": "project:project-1",
                    "entity_type": "project",
                    "entity_id": "project-1",
                    "selectable": True,
                    "selected": True,
                    "children": [
                        {
                            "node_id": "project_workflow:project-1:wf-1",
                            "entity_type": "project_workflow",
                            "entity_id": "wf-1",
                            "selectable": True,
                            "selected": False,
                            "status": "trashed",
                            "children": [],
                            "metadata": {"project_id": "project-1"},
                        }
                    ],
                    "metadata": {"root_kind": "project"},
                },
                "validation_errors": [],
            }
        )
        service._restore_project_workflow_link = MagicMock()

        payload = service.execute_plan(
            "project",
            "project-1",
            action_type="restore",
            selection={"selected_node_ids": ["project_workflow:project-1:wf-1"]},
            scope=_scope("admin"),
        )

        assert payload.status == "restored"
        service._restore_project_workflow_link.assert_called_once_with(
            bl,
            project_id="project-1",
            workflow_id="wf-1",
        )

    def test_project_plan_includes_deleted_dataset_link_nodes_for_restore(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        bl.list_project_workflows.return_value = []
        bl.list_project_connections.return_value = []
        bl.list_project_dataset_catalog.return_value = {
            "explicit_dataset_ids": [],
            "available_datasets": [],
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._list_deleted_project_dataset_snapshots = MagicMock(
            return_value=[
                {
                    "dataset_id": "dataset-1",
                    "display_name": "Dataset One",
                    "visible_in_catalog": True,
                }
            ]
        )

        payload = service.plan(
            "project",
            "project-1",
            action_type="restore",
            scope=_scope("admin"),
        )

        dataset_node = next(
            child for child in payload.tree["children"] if child["entity_type"] == "project_dataset"
        )
        assert dataset_node["entity_id"] == "dataset-1"
        assert dataset_node["selected"] is False
        assert dataset_node["status"] == "trashed"

    def test_project_plan_uses_get_project_workflows_fallback_when_list_method_is_unavailable(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        bl.list_project_workflows = None
        bl.get_project_workflows.return_value = [
            {
                "workflow_id": "wf-1",
                "workflow_display_name": "Workflow One",
                "enabled": True,
                "deleted_at": None,
                "is_default": True,
            }
        ]
        bl.list_project_connections.return_value = []
        bl.list_project_dataset_catalog.return_value = {
            "explicit_dataset_ids": [],
            "available_datasets": [],
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._list_deleted_project_dataset_snapshots = MagicMock(return_value=[])

        payload = service.plan(
            "project",
            "project-1",
            action_type="restore",
            scope=_scope("admin"),
        )

        workflow_nodes = [
            child
            for child in payload.tree["children"]
            if child["entity_type"] == "project_workflow"
        ]
        assert [node["entity_id"] for node in workflow_nodes] == ["wf-1"]
        assert workflow_nodes[0]["selected"] is True
        assert workflow_nodes[0]["label"] == "Workflow One"

    def test_project_restore_plan_warns_when_parent_workspace_is_trashed(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        bl.get_workspace.return_value = {
            "id": "ws-1",
            "display_name": "Workspace One",
            "workspace_key": "workspace-one",
            "status": "active",
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        bl.list_project_workflows.return_value = []
        bl.list_project_connections.return_value = []
        bl.list_project_dataset_catalog.return_value = {
            "explicit_dataset_ids": [],
            "available_datasets": [],
        }

        payload = TrashLifecycleService(get_logger=lambda: bl).plan(
            "project",
            "project-1",
            action_type="restore",
            scope=_scope("admin"),
        )

        assert payload.warnings
        assert "Workspace One" in payload.warnings[0]

    def test_restore_project_auto_restores_trashed_parent_workspace(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._restore_workspace_row_if_trashed = MagicMock()
        service._restore_entity_direct = MagicMock()

        payload = service.restore("project", "project-1", scope=_scope("admin"))

        assert payload.status == "restored"
        service._restore_workspace_row_if_trashed.assert_called_once_with(bl, "ws-1")
        service._restore_entity_direct.assert_called_once_with(bl, "project", "project-1")

    def test_execute_plan_restores_parent_workspace_for_root_project_restore(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._build_lifecycle_plan = MagicMock(
            return_value={
                "tree": {
                    "node_id": "project:project-1",
                    "entity_type": "project",
                    "entity_id": "project-1",
                    "selectable": True,
                    "selected": True,
                    "children": [],
                    "metadata": {"root_kind": "project"},
                },
                "validation_errors": [],
            }
        )
        service._restore_project_root = MagicMock()

        payload = service.execute_plan(
            "project",
            "project-1",
            action_type="restore",
            selection={"selected_node_ids": ["project:project-1"]},
            scope=_scope("admin"),
        )

        assert payload.status == "restored"
        service._restore_project_root.assert_called_once_with(bl, "project-1")

    def test_hard_delete_project_connection_link_preserves_row_for_restore(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._get_connection_row = MagicMock(
            return_value={"project_id": "project-1", "connection_id": "conn-1", "last_error": ""}
        )
        service._update_connection_row = MagicMock()
        service._delete_connection_row = MagicMock()

        service._hard_delete_project_connection_link(
            bl, project_id="project-1", connection_id="conn-1"
        )

        service._update_connection_row.assert_called_once()
        service._delete_connection_row.assert_not_called()

    def test_plan_reports_direct_connection_dependency_conflicts(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        bl.list_project_workflows.return_value = [
            {
                "workflow_id": "wf-1",
                "workflow_display_name": "Workflow One",
                "enabled": True,
                "deleted_at": None,
            }
        ]
        bl.list_project_connections.return_value = [
            {
                "connection_id": "conn-1",
                "display_name": "Connection One",
                "project_id": "project-1",
                "enabled": False,
                "last_error": "__lifecycle_deleted__",
            }
        ]
        bl.list_project_dataset_catalog.return_value = {
            "explicit_dataset_ids": [],
            "available_datasets": [],
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._list_workflow_runtime_dependencies = MagicMock(
            return_value=[
                {
                    "dependency_kind": "project_connection",
                    "dependency_ref": "conn-1",
                    "required_for_execution": True,
                }
            ]
        )

        payload = service.plan(
            "project",
            "project-1",
            action_type="restore",
            scope=_scope("admin"),
        )

        assert payload.direct_dependency_conflicts
        assert "Connection One" in payload.direct_dependency_conflicts[0]["message"]

    def test_restore_project_dataset_link_rejects_unknown_dataset_cleanly(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        bl.get_dataset.return_value = None

        service = TrashLifecycleService(get_logger=lambda: bl)

        with pytest.raises(ServiceValidationError, match="Unknown dataset"):
            service._restore_project_dataset_link(
                bl, project_id="project-1", dataset_id="dataset-missing"
            )

    def test_restore_project_dataset_link_rejects_cross_workspace_dataset(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        bl.get_dataset.return_value = {
            "dataset_id": "dataset-1",
            "workspace_id": "ws-2",
            "deleted_at": None,
        }

        service = TrashLifecycleService(get_logger=lambda: bl)

        with pytest.raises(ServiceValidationError, match="different workspace"):
            service._restore_project_dataset_link(
                bl, project_id="project-1", dataset_id="dataset-1"
            )

    def test_plan_reports_project_workflow_integrity_conflict_for_partial_hard_delete(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        bl.list_project_workflows.return_value = [
            {
                "workflow_id": "wf-1",
                "workflow_display_name": "Workflow One",
                "enabled": True,
                "deleted_at": None,
                "is_default": True,
            }
        ]
        bl.list_project_connections.return_value = []
        bl.list_project_dataset_catalog.return_value = {
            "explicit_dataset_ids": [],
            "available_datasets": [],
        }

        payload = TrashLifecycleService(get_logger=lambda: bl).plan(
            "project",
            "project-1",
            action_type="hard_delete",
            scope=_scope("admin"),
        )

        assert payload.direct_dependency_conflicts == []

        service = TrashLifecycleService(get_logger=lambda: bl)
        _raises_input_1847_1 = _scope("admin")
        with pytest.raises(ServiceValidationError, match="retain at least one linked workflow"):
            service.execute_plan(
                "project",
                "project-1",
                action_type="hard_delete",
                selection={"selected_node_ids": ["project_workflow:project-1:wf-1"]},
                scope=_raises_input_1847_1,
            )

    def test_scope_filtered_workspace_restore_does_not_implicitly_restore_root(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._restore_workspace_row = MagicMock()
        service._restore_entity_direct = MagicMock()
        service._restore_project_workflow_link = MagicMock()

        payload = service._execute_container_lifecycle_selection(
            bl,
            action_type="restore",
            selected_node_ids={"project:project-1", "project_workflow:project-1:wf-1"},
            tree_node={
                "node_id": "workspace:ws-1",
                "entity_type": "workspace",
                "entity_id": "ws-1",
                "selectable": False,
                "selected": False,
                "children": [
                    {
                        "node_id": "project:project-1",
                        "entity_type": "project",
                        "entity_id": "project-1",
                        "selectable": True,
                        "selected": True,
                        "children": [
                            {
                                "node_id": "project_workflow:project-1:wf-1",
                                "entity_type": "project_workflow",
                                "entity_id": "wf-1",
                                "selectable": True,
                                "selected": True,
                                "children": [],
                                "metadata": {"project_id": "project-1"},
                            }
                        ],
                        "metadata": {"root_kind": "project"},
                    }
                ],
                "metadata": {"root_kind": "workspace", "scope_filtered": True},
            },
        )

        assert payload["root_applied"] is False
        service._restore_workspace_row.assert_not_called()
        service._restore_entity_direct.assert_called_once_with(bl, "project", "project-1")

    def test_plan_reports_direct_dataset_dependency_conflicts(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        bl.list_project_workflows.return_value = [
            {
                "workflow_id": "wf-1",
                "workflow_display_name": "Workflow One",
                "enabled": True,
                "deleted_at": None,
                "is_default": True,
            }
        ]
        bl.list_project_connections.return_value = []
        bl.list_project_dataset_catalog.return_value = {
            "explicit_dataset_ids": [],
            "available_datasets": [],
        }
        # The definition shape the registry returns: steps owned by the workflow.
        bl.get_workflow.return_value = {
            "workflow_id": "wf-1",
            "steps": [
                {
                    "step_id": "load",
                    "step_kind": "dataset_query",
                    "override": {"dataset_query": {"query_template_id": "query-1"}},
                }
            ],
        }
        bl.get_dataset_query_template.return_value = {
            "query_template_id": "query-1",
            "dataset_id": "dataset-1",
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._list_deleted_project_dataset_snapshots = MagicMock(
            return_value=[
                {
                    "dataset_id": "dataset-1",
                    "display_name": "Dataset One",
                    "visible_in_catalog": True,
                }
            ]
        )

        payload = service.plan(
            "project",
            "project-1",
            action_type="restore",
            scope=_scope("admin"),
        )

        assert payload.direct_dependency_conflicts
        assert "Dataset One" in payload.direct_dependency_conflicts[0]["message"]

    def test_execute_plan_returns_updated_when_partial_project_hard_delete_skips_root_delete(self):
        bl = _mock_logger()
        bl.get_project.return_value = {
            "id": "project-1",
            "name": "Project One",
            "workspace_id": "ws-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._build_lifecycle_plan = MagicMock(
            return_value={
                "tree": {
                    "node_id": "project:project-1",
                    "entity_type": "project",
                    "entity_id": "project-1",
                    "selected": True,
                    "children": [
                        {
                            "node_id": "project_workflow:project-1:wf-1",
                            "entity_type": "project_workflow",
                            "entity_id": "wf-1",
                            "selectable": True,
                            "selected": True,
                            "children": [],
                            "metadata": {"project_id": "project-1", "is_default": False},
                        },
                        {
                            "node_id": "project_workflow:project-1:wf-2",
                            "entity_type": "project_workflow",
                            "entity_id": "wf-2",
                            "selectable": True,
                            "selected": True,
                            "children": [],
                            "metadata": {"project_id": "project-1", "is_default": True},
                        },
                    ],
                    "metadata": {"root_kind": "project"},
                },
                "validation_errors": [],
            }
        )
        service._hard_delete_project_workflow_link = MagicMock()
        service._hard_delete_project_direct = MagicMock()

        payload = service.execute_plan(
            "project",
            "project-1",
            action_type="hard_delete",
            selection={"selected_node_ids": ["project_workflow:project-1:wf-1"]},
            scope=_scope("admin"),
        )

        assert payload.status == "updated"
        service._hard_delete_project_direct.assert_not_called()
        audit = bl.log_action.call_args.kwargs
        assert audit["action_type"] == "lifecycle_hard_delete"
        assert audit["agent"] == "test-user"
        assert audit["status"] == "updated"
        assert audit["payload"]["entity_type"] == "project"
        assert audit["payload"]["entity_id"] == "project-1"

    def test_hard_delete_requires_admin_role(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_2029_1 = _scope("user")
        with pytest.raises(ServiceForbiddenError):
            service.hard_delete("project", 1, scope=_raises_input_2029_1)

    def test_hard_delete_requires_trashed_first(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None  # not trashed

        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_2039_1 = _scope("admin")
        with pytest.raises(ServiceValidationError, match="must be trashed"):
            service.hard_delete("project", 1, scope=_raises_input_2039_1)

    def test_hard_delete_registry_workflow_blocks_nonterminal_job(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("wf_sync",)
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._require_direct_entity_in_scope = MagicMock()
        conflict = ServiceConflictError("Workflow has non-terminal dependent jobs")

        _raises_input_2050_1 = _scope("admin")
        with (
            patch(
                "core.application.workflows.workflow_deletion_guard."
                "assert_workflow_has_no_nonterminal_jobs",
                side_effect=conflict,
            ) as guard,
            pytest.raises(ServiceConflictError),
        ):
            service.hard_delete("registry_workflow", "wf_sync", scope=_raises_input_2050_1)

        guard.assert_called_once_with(
            bl,
            workflow_id="wf_sync",
            account_id=DEFAULT_ACCOUNT_ID,
        )

    def test_hard_delete_removes_trashed_entity(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)  # is trashed
        bl.get_project.return_value = {
            "id": "1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "name": "Default",
            "deleted_at": "2026-05-11T12:00:00Z",
        }

        service = TrashLifecycleService(get_logger=lambda: bl)
        service._purge_project_before_delete = MagicMock()
        result = service.hard_delete("project", 1, scope=_scope("admin"))

        assert result.status == "hard_deleted"
        service._purge_project_before_delete.assert_called_once_with(
            bl,
            bl.get_project.return_value,
        )
        audit = bl.log_action.call_args.kwargs
        assert audit["project_id"] is None
        assert audit["action_type"] == "lifecycle_hard_delete"
        assert audit["agent"] == "test-user"
        assert audit["status"] == "hard_deleted"
        assert audit["payload"] == {"entity_type": "project", "entity_id": "1"}
        assert audit["meta"]["account_id"] == DEFAULT_ACCOUNT_ID
        assert audit["meta"]["actor_id"] == "test-user"
        assert cur.execute.call_count == 2

    def test_hard_delete_project_runs_scope_cleanup_before_delete(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)
        project_row = {
            "id": "project-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "name": "Default",
            "deleted_at": "2026-05-11T12:00:00Z",
        }
        bl.get_project.return_value = project_row

        service = TrashLifecycleService(get_logger=lambda: bl)
        service._purge_project_before_delete = MagicMock()

        service.hard_delete("project", "project-1", scope=_scope("admin"))

        service._purge_project_before_delete.assert_called_once_with(bl, project_row)

    def test_hard_delete_project_rejects_while_executor_quiescence_times_out(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)
        bl.get_project.return_value = {
            "id": "project-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "name": "Default",
            "deleted_at": "2026-08-15T12:00:00Z",
        }
        begin_quiescence = MagicMock(return_value=False)
        end_quiescence = MagicMock()
        service = TrashLifecycleService(
            get_logger=lambda: bl,
            begin_project_executor_quiescence=begin_quiescence,
            end_project_executor_quiescence=end_quiescence,
        )
        service._purge_project_before_delete = MagicMock()

        _raises_input_2134_1 = _scope("admin")
        with pytest.raises(ServiceConflictError) as exc_info:
            service.hard_delete("project", "project-1", scope=_raises_input_2134_1)

        assert exc_info.value.payload["code"] == "project_executors_not_quiesced"
        begin_quiescence.assert_called_once_with("project-1")
        end_quiescence.assert_called_once_with("project-1")
        service._purge_project_before_delete.assert_not_called()

    def test_hard_delete_project_rejects_terminal_job_with_live_worker_lease(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)
        bl.get_project.return_value = {
            "id": "project-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "name": "Default",
            "deleted_at": "2026-08-15T12:00:00Z",
        }
        bl.list_project_execution_leases.return_value = [
            {
                "project_id": "project-1",
                "worker_instance_id": "worker-1",
                "active_job_id": "job-1",
                "drain_status": "draining",
            }
        ]
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._purge_project_owned_postgres_rows = MagicMock()

        _raises_input_2163_1 = _scope("admin")
        with pytest.raises(ServiceConflictError) as exc_info:
            service.hard_delete("project", "project-1", scope=_raises_input_2163_1)

        assert exc_info.value.payload["code"] == "project_executors_not_quiesced"
        assert exc_info.value.payload["active_job_ids"] == ["job-1"]
        service._purge_project_owned_postgres_rows.assert_not_called()

    def test_project_purge_clears_memory_vector_and_repository_scopes(self):
        bl = _mock_logger()
        services = SimpleNamespace(
            vector_backend=MagicMock(),
            repository=MagicMock(),
        )
        service = TrashLifecycleService(get_logger=lambda: bl)

        with patch(
            "core.application.memory.runtime.build_memory_services",
            return_value=services,
        ):
            service._purge_project_memory_and_indexes(
                bl,
                account_id=DEFAULT_ACCOUNT_ID,
                project_id="project-1",
            )

        services.vector_backend.delete_project_scope.assert_called_once_with(
            account_id=DEFAULT_ACCOUNT_ID,
            project_id="project-1",
            repository=services.repository,
        )
        services.repository.delete_project_scope.assert_called_once_with(
            account_id=DEFAULT_ACCOUNT_ID,
            project_id="project-1",
        )

    def test_hard_delete_job_uses_job_admin_delete_helper(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)  # is trashed
        bl.delete_job.return_value = True
        bl.get_job.return_value = {
            "id": 7,
            "status": "completed",
            "finished_at": "2026-04-11T08:00:00Z",
        }

        service = TrashLifecycleService(get_logger=lambda: bl)
        result = service.hard_delete("job", 7, scope=_scope("admin"))

        assert result.status == "hard_deleted"
        bl.delete_job.assert_called_once_with("7", allow_active=False)
        assert cur.execute.call_count == 2

    def test_hard_delete_job_purges_artifact_storage_from_meta(self):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

        storage = InMemoryObjectStorage()
        artifact_plane = ArtifactDatasetPlane(storage=storage)
        _artifact, artifact_meta = artifact_plane.create_artifact(
            artifact_id="art_777",
            owner_scope={"project_id": "project-7", "job_id": "7"},
            run_id="7",
            step_id="upload",
            filename="blob",
            content_type="application/octet-stream",
            body=b"body",
        )
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)  # is trashed
        bl.delete_job.return_value = True
        bl.get_job.return_value = {
            "id": 7,
            "project_id": "project-7",
            "status": "completed",
            "finished_at": "2026-04-11T08:00:00Z",
            "meta": {
                "files": [
                    {
                        "artifact_id": "art_777",
                        "object_key": "artifacts/art_777/blob",
                    }
                ]
            },
        }
        service = TrashLifecycleService(get_logger=lambda: bl, artifact_plane=artifact_plane)
        result = service.hard_delete("job", 7, scope=_scope("admin"))

        assert result.status == "hard_deleted"
        assert storage.stat(str(artifact_meta["object_key"])) is None
        assert storage.stat("artifacts/_metadata/art_777.json") is None
        bl.delete_job.assert_called_once_with("7", allow_active=False)

    def test_hard_delete_job_retries_after_storage_purge_then_database_failure(self):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

        storage = InMemoryObjectStorage()
        artifact_plane = ArtifactDatasetPlane(storage=storage)
        artifact, _artifact_meta = artifact_plane.create_artifact(
            owner_scope={"project_id": "project-7", "job_id": "7"},
            run_id="7",
            step_id="upload",
            filename="input.txt",
            content_type="text/plain",
            body=b"input",
        )
        job_row = {
            "id": "7",
            "project_id": "project-7",
            "status": "completed",
            "finished_at": "2026-04-11T08:00:00Z",
            "meta": {"files": [{"artifact_id": artifact.artifact_id}]},
        }
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)
        bl.get_job.return_value = job_row
        bl.delete_job.side_effect = [RuntimeError("database unavailable"), True]
        service = TrashLifecycleService(get_logger=lambda: bl, artifact_plane=artifact_plane)

        _raises_input_2287_1 = _scope("admin")
        with pytest.raises(RuntimeError, match="database unavailable"):
            service.hard_delete("job", 7, scope=_raises_input_2287_1)

        marker_page, marker_token = storage.list_key_page(
            "_replay_job_cleanup/", limit=10, continuation_token=None
        )
        assert len(marker_page) == 1
        assert marker_token is None

        result = service.hard_delete("job", 7, scope=_scope("admin"))

        assert result.status == "hard_deleted"
        assert bl.delete_job.call_count == 2
        marker_keys, _ = storage.list_key_page(
            "_replay_job_cleanup/", limit=10, continuation_token=None
        )
        assert len(marker_keys) == 1
        assert json.loads(storage.get(marker_keys[0]))["state"] == "committed"

    def test_hard_delete_job_defers_cleanup_journal_removal_until_commit(self):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

        storage = InMemoryObjectStorage()
        artifact_plane = ArtifactDatasetPlane(storage=storage)
        artifact, _meta = artifact_plane.create_artifact(
            owner_scope={"project_id": "project-7", "job_id": "7"},
            run_id="7",
            step_id="upload",
            filename="input.txt",
            content_type="text/plain",
            body=b"input",
        )
        job_row = {
            "id": "7",
            "project_id": "project-7",
            "status": "completed",
            "finished_at": "2026-04-11T08:00:00Z",
            "meta": {"files": [{"artifact_id": artifact.artifact_id}]},
        }
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)
        bl.get_job.return_value = job_row
        bl.delete_job.return_value = True
        callbacks = []
        service = TrashLifecycleService(get_logger=lambda: bl, artifact_plane=artifact_plane)

        with patch(
            "core.infrastructure.db.db_pool.register_transaction_after_commit",
            side_effect=lambda _conn, callback: callbacks.append(callback) or True,
        ):
            result = service.hard_delete("job", 7, scope=_scope("admin"))

        assert result.status == "hard_deleted"
        assert len(callbacks) == 1
        assert storage.list_key_page("_replay_job_cleanup/", limit=10)[0]
        callbacks[0]()
        marker_keys, _ = storage.list_key_page("_replay_job_cleanup/", limit=10)
        assert json.loads(storage.get(marker_keys[0]))["state"] == "committed"

    def test_project_job_cleanup_journal_removal_is_deferred_until_outer_commit(self):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

        storage = InMemoryObjectStorage()
        artifact_plane = ArtifactDatasetPlane(storage=storage)
        artifact, _meta = artifact_plane.create_artifact(
            owner_scope={"project_id": "project-7", "job_id": "7"},
            run_id="7",
            step_id="upload",
            filename="input.txt",
            content_type="text/plain",
            body=b"input",
        )
        job_row = {
            "id": "7",
            "project_id": "project-7",
            "status": "completed",
            "finished_at": "2026-04-11T08:00:00Z",
            "meta": {"files": [{"artifact_id": artifact.artifact_id}]},
        }
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [("7",)]
        bl.get_job.return_value = job_row
        bl.delete_job.return_value = True
        callbacks = []
        service = TrashLifecycleService(get_logger=lambda: bl, artifact_plane=artifact_plane)

        with patch(
            "core.infrastructure.db.db_pool.register_transaction_after_commit",
            side_effect=lambda _conn, callback: callbacks.append(callback) or True,
        ):
            service._purge_project_jobs_before_delete(bl, "project-7")

        assert len(callbacks) == 1
        assert storage.list_key_page("_replay_job_cleanup/", limit=10)[0]
        callbacks[0]()
        marker_keys, _ = storage.list_key_page("_replay_job_cleanup/", limit=10)
        assert json.loads(storage.get(marker_keys[0]))["state"] == "committed"

    def test_hard_delete_job_purges_restart_discovered_replay_owned_handles(self, monkeypatch):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.application.extensions.artifact_plane_values import replay_owner_manifest_key
        from core.application.jobs.job_invocation_artifacts import (
            capture_replay_snapshot,
            mint_replay_file_handles,
        )
        from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage
        from core.infrastructure.security import secret_codec

        monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "trash-test-key"})
        storage = InMemoryObjectStorage()
        creator = ArtifactDatasetPlane(storage=storage)
        artifact, artifact_meta = creator.create_artifact(
            owner_scope={"project_id": "project-7"},
            run_id="prelaunch",
            step_id="upload",
            filename="input.txt",
            content_type="text/plain",
            body=b"input",
        )
        dataset, dataset_meta = creator.create_dataset(
            owner_scope={"project_id": "project-7"},
            run_id="prelaunch",
            step_id="upload",
            rows=[{"id": 1}],
        )
        files = mint_replay_file_handles(
            artifact_plane=creator,
            project_id="project-7",
            job_id="job-7",
            files=[
                {"artifact_id": artifact.artifact_id},
                {"dataset_id": dataset.dataset_id},
            ],
        )
        pointer = capture_replay_snapshot(
            artifact_plane=creator,
            project_id="project-7",
            job_id="job-7",
            snapshot={
                "workflow_id": "wf-1",
                "inputs": {},
                "files": files,
                "test_mode": False,
                "input_contract": "direct_inputs",
                "task_envelope": {"inputs": {}, "files": files},
                "_workflow_secret_ref_fingerprints": [],
            },
        )
        pointer_meta = creator.get_artifact(pointer["artifact_id"])
        representation_key = f"datasets/{dataset.dataset_id}/representations/rows.ndjson"
        storage.put(representation_key, b'{"id":1}\n', content_type="application/x-ndjson")
        storage.put(
            f"{representation_key}.json",
            b'{"content_type":"application/x-ndjson"}',
            content_type="application/json",
        )

        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("job-7",)
        bl.delete_job.return_value = True
        bl.get_job.return_value = {
            "id": "job-7",
            "project_id": "project-7",
            "status": "completed",
            "finished_at": "2026-04-11T08:00:00Z",
            "meta": {"replay_snapshot": pointer},
        }
        restarted = ArtifactDatasetPlane(storage=storage)

        result = TrashLifecycleService(
            get_logger=lambda: bl,
            artifact_plane=restarted,
        ).hard_delete("job", "job-7", scope=_scope("admin"))

        assert result.status == "hard_deleted"
        for key in (
            artifact_meta["object_key"],
            f"artifacts/_metadata/{artifact.artifact_id}.json",
            f"_replay_owners/artifact/{artifact.artifact_id}.json",
            dataset_meta["object_key"],
            f"datasets/{dataset.dataset_id}/meta.json",
            f"_replay_owners/dataset/{dataset.dataset_id}.json",
            representation_key,
            f"{representation_key}.json",
            pointer_meta["object_key"],
            f"artifacts/_metadata/{pointer['artifact_id']}.json",
            replay_owner_manifest_key(
                project_id="project-7",
                job_id="job-7",
                kind="artifact",
                object_id=artifact.artifact_id,
            ),
            replay_owner_manifest_key(
                project_id="project-7",
                job_id="job-7",
                kind="dataset",
                object_id=dataset.dataset_id,
            ),
            replay_owner_manifest_key(
                project_id="project-7",
                job_id="job-7",
                kind="artifact",
                object_id=pointer["artifact_id"],
            ),
        ):
            assert storage.stat(str(key)) is None

    def test_hard_delete_job_blocks_untyped_foreign_object_key(self):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

        storage = InMemoryObjectStorage()
        plane = ArtifactDatasetPlane(storage=storage)
        victim, victim_meta = plane.create_artifact(
            owner_scope={"project_id": "p-other", "job_id": "j-other"},
            run_id="j-other",
            step_id="upload",
            filename="blob.bin",
            content_type="application/octet-stream",
            body=b"victim",
        )
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("j-delete",)
        bl.get_job.return_value = {
            "id": "j-delete",
            "project_id": "p-delete",
            "status": "completed",
            "finished_at": "2026-04-11T08:00:00Z",
            "meta": {"files": [{"object_key": victim_meta["object_key"]}]},
        }

        _raises_callable_2532_1 = TrashLifecycleService(
            get_logger=lambda: bl, artifact_plane=plane
        ).hard_delete
        _raises_input_2532_2 = _scope("admin")
        with pytest.raises(ServiceConflictError, match="Artifact cleanup did not complete"):
            _raises_callable_2532_1("job", "j-delete", scope=_raises_input_2532_2)

        bl.delete_job.assert_not_called()
        assert storage.stat(str(victim_meta["object_key"])) is not None
        assert storage.stat(f"artifacts/_metadata/{victim.artifact_id}.json") is not None

    def test_project_hard_delete_rejects_active_jobs_before_any_purge(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [("job-active",), ("job-complete",)]
        bl.get_job.side_effect = [
            {"id": "job-active", "status": "in_progress", "finished_at": None},
            {
                "id": "job-complete",
                "status": "completed",
                "finished_at": "2026-08-15T08:00:00Z",
            },
        ]
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._purge_job_artifacts_before_delete = MagicMock()

        with pytest.raises(ServiceConflictError) as exc_info:
            service._purge_project_jobs_before_delete(bl, "project-1")

        assert exc_info.value.payload["code"] == "project_has_unsettled_jobs"
        assert exc_info.value.payload["blocking_jobs"] == [
            {"job_id": "job-active", "status": "in_progress"}
        ]
        service._purge_job_artifacts_before_delete.assert_not_called()
        bl.delete_job.assert_not_called()

    def test_project_hard_delete_schedules_product_owned_queue_cleanup(self):
        bl = _mock_logger()
        delete_project_queues = MagicMock()
        service = TrashLifecycleService(
            get_logger=lambda: bl,
            delete_project_queues=delete_project_queues,
        )
        service._purge_project_jobs_before_delete = MagicMock()
        service._purge_project_owned_postgres_rows = MagicMock()
        service._purge_project_memory_and_indexes = MagicMock()

        with patch(
            "core.infrastructure.db.db_pool.register_transaction_after_commit",
            side_effect=lambda _conn, callback: callback() or True,
        ):
            service._purge_project_before_delete(
                bl,
                {
                    "id": "project-1",
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "name": "Project One",
                },
            )

        delete_project_queues.assert_called_once_with("project-1")

    def test_project_hard_delete_surfaces_queue_cleanup_failure_after_commit(self):
        bl = _mock_logger()
        service = TrashLifecycleService(
            get_logger=lambda: bl,
            delete_project_queues=MagicMock(side_effect=RuntimeError("broker unavailable")),
        )
        service._purge_project_jobs_before_delete = MagicMock()
        service._purge_project_owned_postgres_rows = MagicMock()
        service._purge_project_memory_and_indexes = MagicMock()

        with (
            patch(
                "core.infrastructure.db.db_pool.register_transaction_after_commit",
                side_effect=lambda _conn, callback: callback() or True,
            ),
            pytest.raises(ServiceUnavailableError, match="external project cleanup failed"),
        ):
            service._purge_project_before_delete(
                bl,
                {
                    "id": "project-1",
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "name": "Project One",
                },
            )

    def test_hard_delete_job_raises_conflict_while_shutdown_still_finishing(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)  # is trashed
        bl.get_job.return_value = {"id": 7, "status": "cancelled", "finished_at": None}

        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_2627_1 = _scope("admin")
        with pytest.raises(ServiceConflictError, match="shutdown completes"):
            service.hard_delete("job", 7, scope=_raises_input_2627_1)

        bl.delete_job.assert_not_called()

    def test_hard_delete_skill_rejects_all_dependent_agents(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("skill-1",)
        cur.fetchall.return_value = [("agent-1",), ("agent-2",)]
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._require_direct_entity_in_scope = MagicMock()

        _raises_input_2640_1 = _scope("admin")
        with pytest.raises(ServiceConflictError) as exc_info:
            service.hard_delete("registry_skill", "skill-1", scope=_raises_input_2640_1)

        assert exc_info.value.payload["dependent_ids"] == ["agent:agent-1", "agent:agent-2"]
        assert "hard deleting the skill" in exc_info.value.payload["action"]

        executed_sql = [str(call.args[0]) for call in cur.execute.call_args_list]
        assert not any(sql.startswith("DELETE FROM registry_skills") for sql in executed_sql)

    def test_trash_skill_rejects_enabled_and_disabled_dependents(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [
            ("agent", "agent-enabled"),
            ("agent", "agent-disabled"),
            ("workflow_step", "wf-live#run-skill"),
        ]
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._require_direct_entity_in_scope = MagicMock()

        _raises_input_2660_1 = _scope("admin")
        with pytest.raises(ServiceConflictError) as exc_info:
            service.trash("registry_skill", "skill-1", scope=_raises_input_2660_1)

        assert exc_info.value.payload == {
            "error": "registry_dependency_conflict",
            "entity_type": "registry_skill",
            "entity_id": "skill-1",
            "dependent_ids": [
                "agent:agent-enabled",
                "agent:agent-disabled",
                "workflow_step:wf-live#run-skill",
            ],
            "action": "Remove or replace every listed dependency before trashing the skill.",
        }
        executed_sql = [str(call.args[0]).strip() for call in cur.execute.call_args_list]
        assert not any(
            sql.startswith("UPDATE registry_skills SET deleted_at") for sql in executed_sql
        )

    def test_fk_blocked_hard_delete_returns_dependency_conflict(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("tool-1",)

        def execute(statement, _params):
            if str(statement).startswith("DELETE FROM registry_tools"):
                raise psycopg_errors.ForeignKeyViolation("still referenced")

        cur.execute.side_effect = execute
        service = TrashLifecycleService(get_logger=lambda: bl)
        service._require_direct_entity_in_scope = MagicMock()

        _raises_input_2692_1 = _scope("admin")
        with pytest.raises(
            ServiceConflictError,
            match="registry_tool 'tool-1' still has dependent records",
        ):
            service.hard_delete("registry_tool", "tool-1", scope=_raises_input_2692_1)

    def test_unknown_entity_type_raises(self):
        bl = _mock_logger()
        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_2702_1 = _scope("admin")
        with pytest.raises(ServiceValidationError, match="Unknown entity type"):
            service.trash("unknown_thing", 1, scope=_raises_input_2702_1)

    def test_all_entity_types_have_valid_config(self):
        for _entity_type, cfg in _TRASH_ENTITY_CONFIG.items():
            assert "table" in cfg
            assert "id_col" in cfg

    def test_trash_not_found_raises(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None  # not found

        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_2717_1 = _scope("admin")
        with pytest.raises(ServiceNotFoundError, match="not found or already trashed"):
            service.trash("project", 999, scope=_raises_input_2717_1)

    def test_trash_entity_without_deleted_by_column_uses_deleted_at_only(self):
        bl = _mock_logger()
        bl._column_exists.side_effect = lambda _table, column: column != "deleted_by"
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)

        service = TrashLifecycleService(get_logger=lambda: bl)
        result = service.trash(
            "project_settings_revision", 1, deleted_by="alice", scope=_scope("admin")
        )

        assert result.status == "trashed"
        sql = cur.execute.call_args[0][0]
        assert "deleted_at = NOW()" in sql
        assert "deleted_by" not in sql

    def test_trash_invalid_uuid_lookup_returns_not_found(self):
        bl = _mock_logger()
        bl.get_project.side_effect = Exception('invalid input syntax for type uuid: "999999999"')

        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_2742_1 = _scope("admin")
        with pytest.raises(ServiceNotFoundError, match="not found"):
            service.trash("project", "999999999", scope=_raises_input_2742_1)

    def test_restore_invalid_uuid_lookup_returns_not_found(self):
        bl = _mock_logger()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.execute.side_effect = Exception('invalid input syntax for type uuid: "999999999"')

        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_2752_1 = _scope("admin")
        with pytest.raises(ServiceNotFoundError, match="not found or not trashed"):
            service.restore("registry_skill", "999999999", scope=_raises_input_2752_1)

    def test_direct_entity_scope_rejects_null_account_id(self):
        bl = _mock_logger()
        bl._column_exists.side_effect = lambda _table, column: column == "account_id"
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.description = [("skill_id",), ("account_id",)]
        cur.fetchone.return_value = ("skill-1", None)

        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_2764_1 = _scope("admin")
        with pytest.raises(ServiceForbiddenError):
            service._require_direct_entity_in_scope(
                bl,
                cfg=_TRASH_ENTITY_CONFIG["registry_skill"],
                entity_type="registry_skill",
                entity_id="skill-1",
                scope=_raises_input_2764_1,
            )

    def test_hard_delete_invalid_uuid_lookup_returns_validation_error(self):
        bl = _mock_logger()
        bl.get_project.side_effect = Exception('invalid input syntax for type uuid: "999999999"')

        service = TrashLifecycleService(get_logger=lambda: bl)

        _raises_input_2779_1 = _scope("admin")
        with pytest.raises(ServiceValidationError, match="must be trashed before hard delete"):
            service.hard_delete("project", "999999999", scope=_raises_input_2779_1)


class TestDatasetAssignmentActorPropagation:
    """Orchestration-only contracts; real PostgreSQL tests own atomicity."""

    @pytest.fixture
    def dataset_actor_ports(self):
        bl = _mock_logger()
        bl._table_exists.return_value = True
        bl.get_project.return_value = {
            "id": "project-actor",
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": "ws-actor",
            "deleted_at": None,
        }
        bl.get_dataset.return_value = {
            "dataset_id": "dataset-actor",
            "workspace_id": "ws-actor",
            "deleted_at": None,
            "project_ids": ["project-actor"],
        }
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        cur.fetchone.return_value = {
            "id": "ws-actor",
            "account_id": DEFAULT_ACCOUNT_ID,
            "tenant_id": "default",
            "deleted_at": None,
        }
        return TrashLifecycleService(get_logger=lambda: bl), bl, cur

    @staticmethod
    def _assignment_tree(root_kind):
        dataset = {
            "node_id": "project_dataset:project-actor:dataset-actor",
            "entity_type": "project_dataset",
            "entity_id": "dataset-actor",
            "selectable": True,
            "selected": True,
            "actor": "forged-dataset-actor",
            "metadata": {"project_id": "project-actor", "actor": "forged-metadata-actor"},
            "children": [],
        }
        project = {
            "node_id": "project:project-actor",
            "entity_type": "project",
            "entity_id": "project-actor",
            "selectable": False,
            "selected": False,
            "actor": "forged-project-actor",
            "metadata": {"root_kind": "project"},
            "children": [dataset],
        }
        if root_kind == "project":
            return project
        return {
            "node_id": "workspace:ws-actor",
            "entity_type": "workspace",
            "entity_id": "ws-actor",
            "selectable": False,
            "selected": False,
            "actor": "forged-workspace-actor",
            "metadata": {"root_kind": "workspace"},
            "children": [project],
        }

    @pytest.mark.parametrize("root_kind", ["project", "workspace"])
    @pytest.mark.parametrize(
        ("action", "expected_history_action"),
        [("restore", "project_assignment_restore"), ("hard_delete", "project_assignment_delete")],
    )
    def test_validated_plan_uses_scope_actor_through_dataset_recursion(
        self, dataset_actor_ports, root_kind, action, expected_history_action
    ):
        service, bl, cur = dataset_actor_ports
        tree = self._assignment_tree(root_kind)
        # Only the catalog/plan projection is supplied. Scope validation, selection,
        # recursive execution, Dataset helpers and the SQL history writer stay real.
        service._build_lifecycle_plan = MagicMock(
            return_value={"tree": tree, "plan_version": "owned-plan", "validation_errors": []}
        )
        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            tenant_id="default",
            actor_id="validated-dataset-admin",
            principal_type="admin",
            allowed_workspace_ids=frozenset({"ws-actor"}),
            allowed_project_ids=frozenset({"project-actor"}),
        )

        result = service._validate_and_execute_lifecycle_plan(
            bl,
            entity_type=root_kind,
            entity_id="project-actor" if root_kind == "project" else "ws-actor",
            action_type=action,
            selection={
                "plan_version": "owned-plan",
                "selected_node_ids": ["project_dataset:project-actor:dataset-actor"],
                "actor": "forged-selection-actor",
            },
            scope=scope,
        )

        assert result == {"root_applied": False}
        history_params = [
            call.args[1]
            for call in cur.execute.call_args_list
            if "INSERT INTO registry_changelog" in call.args[0]
        ]
        assert len(history_params) == 1
        assert len(history_params[0]) == 5
        assert history_params[0][:3] == ("dataset", "dataset-actor", expected_history_action)
        assert history_params[0][4] == "validated-dataset-admin"

    @pytest.mark.parametrize("action", ["restore", "hard_delete"])
    def test_direct_dataset_assignment_helpers_keep_system_author_default(
        self, dataset_actor_ports, action
    ):
        service, bl, cur = dataset_actor_ports
        if action == "restore":
            service._restore_project_dataset_link(
                bl, project_id="project-actor", dataset_id="dataset-actor"
            )
        else:
            service._hard_delete_project_dataset_link(
                bl, project_id="project-actor", dataset_id="dataset-actor"
            )

        history_params = [
            call.args[1]
            for call in cur.execute.call_args_list
            if "INSERT INTO registry_changelog" in call.args[0]
        ]
        assert len(history_params) == 1
        assert history_params[0][4] == "system"


class TestAgentPublishedBlockOrphans:
    """RS5-002 regression: an agent's published block must not outlive the agent."""

    def test_skill_dependency_query_ignores_blocks_whose_agent_is_gone(self):
        """The orphan made the skill undeletable through every product surface.

        The 'agent' branch of the dependency query drops deleted agents through
        its JOIN. The 'published_block' branch had no matching guard, so a block
        left behind by a hard-deleted agent counted as a live dependent forever.
        """
        source = inspect.getsource(TrashLifecycleService._require_skill_without_dependents)
        published_block_branch = source.split("'published_block'")[1]
        assert "FROM registry_agents owner" in published_block_branch, (
            "published_block dependants are not constrained to existing agents"
        )
        assert "split_part(blocks.source_ref, '::', 1)" in published_block_branch

    def test_agent_hard_delete_purges_the_blocks_it_owns(self):
        bl = _PlanStore(_PlanConn())
        service = TrashLifecycleService(get_logger=lambda: bl)

        service._purge_agent_published_blocks(bl, "agent-1")

        statements = [sql for sql, _params in bl.conn.executed]
        assert any("DELETE FROM registry_published_block_contracts" in sql for sql in statements), (
            "agent hard delete leaves its derived published blocks behind"
        )
        params = next(p for sql, p in bl.conn.executed if "published_block_contracts" in sql)
        assert params == ("agent-1",)

    def test_purge_is_a_no_op_without_an_agent_id(self):
        bl = _PlanStore(_PlanConn())
        TrashLifecycleService(get_logger=lambda: bl)._purge_agent_published_blocks(bl, "  ")
        assert bl.conn.executed == []
