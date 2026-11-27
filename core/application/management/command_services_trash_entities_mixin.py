# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .command_services_common import *  # NOSONAR
from .command_services_trash_purge_mixin import TrashLifecycleServicePurgeMixin

# Trash entity types that are backed by the registry, mapped to the short
# entity_type the registry changelog uses.
_LIFECYCLE_CHANGELOG_TYPES = {
    "registry_agent": "agent",
    "registry_tool": "tool",
    "registry_skill": "skill",
    "registry_workflow": "workflow",
    "registry_guardrail": "guardrail",
    "registry_data_contract": "data_contract",
    "dataset": "dataset",
}


class TrashLifecycleServiceEntitiesMixin(TrashLifecycleServicePurgeMixin):
    def _restore_entity_direct(
        self, bl: Any, entity_type: str, entity_id: str, *, account_id: str = ""
    ) -> None:
        cfg = self._entity_config(entity_type)
        table = cfg["table"]
        id_col = cfg["id_col"]
        has_deleted_by = self._has_column(bl, table, "deleted_by")
        set_clause = (
            "deleted_at = NULL, deleted_by = NULL" if has_deleted_by else "deleted_at = NULL"
        )
        with bl.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {table} SET {set_clause} WHERE {id_col} = %s{_trash_key_sql(cfg)} "
                "AND deleted_at IS NOT NULL",
                _trash_key_params(cfg, entity_id, account_id),
            )
            if not cur.rowcount:
                raise ServiceNotFoundError(f"{entity_type} not found or not trashed")

    def _hard_delete_entity_direct(
        self,
        bl: Any,
        entity_type: str,
        entity_id: str,
        *,
        account_id: str = "",
    ) -> None:
        if entity_type == "project":
            self._hard_delete_project_direct(bl, entity_id)
            return
        cfg = self._entity_config(entity_type)
        table = cfg["table"]
        id_col = cfg["id_col"]
        if entity_type == "registry_workflow":
            from core.application.workflows.workflow_deletion_guard import (
                assert_workflow_has_no_nonterminal_jobs,
            )

            assert_workflow_has_no_nonterminal_jobs(
                bl,
                workflow_id=entity_id,
                account_id=account_id,
            )
            from core.infrastructure.db.db_pool import active_transaction_connection

            transaction = active_transaction_connection(bl.conn)
            if transaction is None:
                raise ServiceUnavailableError(
                    "Workflow hard delete requires an active database transaction"
                )
            self._purge_workflow_secrets(
                workflow_id=entity_id,
                account_id=account_id,
                transaction=transaction,
            )
        sample_workflow = (
            self._workflow_with_sample_files(bl.conn, entity_id, account_id)
            if entity_type == "registry_workflow"
            else None
        )
        with bl.conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {table} WHERE {id_col} = %s{_trash_key_sql(cfg)} "
                "AND deleted_at IS NOT NULL",
                _trash_key_params(cfg, entity_id, account_id),
            )
            if not cur.rowcount:
                raise ServiceValidationError(f"{entity_type} must be trashed before hard delete")
        self._delete_workflow_sample_files(sample_workflow)

    def _purge_workflow_secrets(
        self,
        *,
        workflow_id: str,
        account_id: str,
        transaction: Any,
    ) -> None:
        service = self._workflow_secret_default_service
        purge = getattr(service, "purge_workflow_secrets", None)
        if not callable(purge):
            raise ServiceUnavailableError("Workflow secret lifecycle service is unavailable")
        purge(
            workflow_id=str(workflow_id or "").strip(),
            account_id=str(account_id or "").strip(),
            transaction=transaction,
        )

    @staticmethod
    def _workflow_with_sample_files(
        conn: Any, workflow_id: str, account_id: str
    ) -> dict[str, Any] | None:
        """The workflow row a hard delete is about to remove, for its test-run files."""
        if not str(account_id or "").strip():
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT account_id, settings FROM registry_workflows "
                    "WHERE workflow_id = %s AND account_id = %s",
                    (workflow_id, str(account_id).strip()),
                )
                row = cur.fetchone()
            if isinstance(row, dict):
                account_id, settings = row.get("account_id"), row.get("settings")
            elif isinstance(row, (tuple, list)) and len(row) >= 2:
                account_id, settings = row[0], row[1]
            else:
                return None
            if isinstance(settings, str):
                settings = json.loads(settings or "{}")
        except Exception:
            # Reading the files to delete must never block deleting the workflow.
            return None
        return {
            "workflow_id": str(workflow_id),
            "account_id": str(account_id or ""),
            "settings": settings if isinstance(settings, dict) else {},
        }

    def _delete_workflow_sample_files(self, workflow: dict[str, Any] | None) -> None:
        """Delete a removed workflow's test-run files; a leftover only costs storage."""
        if not workflow:
            return
        from core.application.workflows.workflow_canvas_annotation_service import (
            WorkflowCanvasAnnotationService,
        )
        from core.application.workflows.workflow_sample_file_service import (
            WorkflowSampleFileService,
        )

        try:
            plane = _resolve_artifact_plane(self._artifact_plane)
            WorkflowSampleFileService(None, plane).purge_workflow_samples(workflow)
            WorkflowCanvasAnnotationService(None, plane).purge_workflow_images(workflow)
        except Exception:
            logger.warning(
                "workflow test-run files were not deleted workflow_id=%s",
                workflow.get("workflow_id"),
            )

    def _restore_project_workflow_link(self, bl: Any, *, project_id: str, workflow_id: str) -> None:
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE project_workflows
                   SET enabled = true,
                       deleted_at = NULL,
                       updated_at = now()
                 WHERE project_id = %s
                   AND workflow_id = %s
                """,
                (project_id, workflow_id),
            )
            if not cur.rowcount:
                raise ServiceNotFoundError("project workflow link not found")

    def _hard_delete_project_workflow_link(
        self, bl: Any, *, project_id: str, workflow_id: str
    ) -> None:
        from core.application.workflows.workflow_deletion_guard import (
            assert_workflow_has_no_nonterminal_jobs,
        )

        assert_workflow_has_no_nonterminal_jobs(
            bl,
            workflow_id=workflow_id,
            project_id=project_id,
        )
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE project_workflows
                   SET enabled = false,
                       is_default = false,
                       deleted_at = now(),
                       updated_at = now()
                 WHERE project_id = %s
                   AND workflow_id = %s
                   AND deleted_at IS NULL
                """,
                (project_id, workflow_id),
            )
            if not cur.rowcount:
                cur.execute(
                    """
                    SELECT deleted_at
                      FROM project_workflows
                     WHERE project_id = %s
                       AND workflow_id = %s
                    """,
                    (project_id, workflow_id),
                )
                row = cur.fetchone()
                deleted_at = None
                if isinstance(row, dict):
                    deleted_at = row.get("deleted_at")
                elif row:
                    deleted_at = row[0]
                if deleted_at is not None:
                    return
                raise ServiceValidationError(
                    "Lifecycle selection is stale: project workflow link no longer exists. "
                    "Refresh the plan and try again."
                )

    def _restore_project_dataset_link(
        self,
        bl: Any,
        *,
        project_id: str,
        dataset_id: str,
        author: str = "system",
    ) -> None:
        self._validate_project_dataset_restore(bl, project_id=project_id, dataset_id=dataset_id)
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO project_datasets (dataset_id, project_id)
                VALUES (%s, %s)
                ON CONFLICT (dataset_id, project_id) DO NOTHING
                """,
                (dataset_id, project_id),
            )
            table_exists = getattr(bl, "_table_exists", None)
            if callable(table_exists) and bool(table_exists("registry_changelog")):
                self._log_registry_changelog_entry(
                    cur,
                    entity_type="dataset",
                    entity_id=dataset_id,
                    action="project_assignment_restore",
                    snapshot=self._dataset_project_assignment_snapshot(bl, dataset_id, project_id),
                    author=author,
                )

    def _hard_delete_project_dataset_link(
        self,
        bl: Any,
        *,
        project_id: str,
        dataset_id: str,
        author: str = "system",
    ) -> None:
        snapshot = self._dataset_project_assignment_snapshot(bl, dataset_id, project_id)
        with bl.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM project_datasets WHERE dataset_id = %s AND project_id = %s",
                (dataset_id, project_id),
            )
            if not cur.rowcount:
                already_deleted = any(
                    str(row.get("dataset_id") or "").strip() == str(dataset_id)
                    for row in self._list_deleted_project_dataset_snapshots(bl, project_id)
                )
                if already_deleted:
                    return
                raise ServiceValidationError(
                    "Lifecycle selection is stale: project dataset link no longer exists. "
                    "Refresh the plan and try again."
                )
            table_exists = getattr(bl, "_table_exists", None)
            if callable(table_exists) and bool(table_exists("registry_changelog")):
                self._log_registry_changelog_entry(
                    cur,
                    entity_type="dataset",
                    entity_id=dataset_id,
                    action="project_assignment_delete",
                    snapshot=snapshot,
                    author=author,
                )

    def _validate_project_dataset_restore(
        self, bl: Any, *, project_id: str, dataset_id: str
    ) -> None:
        project = self._get_project_row_including_trashed(bl, project_id)
        project_workspace_id = str(project.get("workspace_id") or "").strip()
        dataset = self._get_dataset_row_including_trashed(bl, dataset_id)
        if not dataset:
            raise ServiceValidationError(f"Unknown dataset '{dataset_id}'")
        if dataset.get("deleted_at") is not None:
            raise ServiceValidationError(
                f"Dataset '{dataset_id}' is deleted and cannot be restored into a project link."
            )
        dataset_workspace_id = str(dataset.get("workspace_id") or "").strip()
        if project_workspace_id and dataset_workspace_id != project_workspace_id:
            raise ServiceValidationError(
                f"Dataset '{dataset_id}' belongs to a different workspace and cannot be restored into this project."
            )

    def _get_dataset_row_including_trashed(self, bl: Any, dataset_id: str) -> dict[str, Any] | None:
        dataset_repository = (
            self._project_dataset_catalog() if self._project_dataset_catalog else bl
        )
        getter = getattr(dataset_repository, "get_dataset", None)
        if not callable(getter):
            raise ServiceUnavailableError(
                "project lifecycle dependency projection is unavailable: get_dataset"
            )
        row = getter(str(dataset_id), include_trashed=True)
        return dict(row) if isinstance(row, dict) else None

    def _get_connection_row(self, bl: Any, connection_id: str) -> dict[str, Any] | None:
        if not hasattr(bl, "cursor"):
            return None
        try:
            return project_connection_store.get_project_connection_row(bl, connection_id)
        except Exception as exc:
            logger.warning("Failed to read project_connection '%s': %s", connection_id, exc)
            raise

    def _update_connection_row(self, bl: Any, connection_id: str, updates: dict[str, Any]) -> None:
        if not hasattr(bl, "cursor"):
            return
        try:
            project_connection_store.update_project_connection_row(bl, connection_id, updates)
        except Exception as exc:
            logger.warning("Failed to update project_connection: %s", exc)
            raise

    def _restore_project_connection_link(
        self, bl: Any, *, project_id: str, connection_id: str
    ) -> None:
        connection = self._get_connection_row(bl, connection_id)
        if not isinstance(connection, dict) or str(connection.get("project_id") or "") != str(
            project_id
        ):
            raise ServiceValidationError(
                "Lifecycle selection is stale: project connection no longer exists. "
                "Refresh the plan and try again."
            )
        if str(connection.get("last_error") or "") == _PROJECT_CONNECTION_LIFECYCLE_DELETED:
            return
        self._update_connection_row(
            bl,
            connection_id,
            {
                "enabled": True,
                "health_status": None,
                "runtime_status": None,
                "last_error": None
                if str(connection.get("last_error") or "").strip()
                == _PROJECT_CONNECTION_LIFECYCLE_DELETED
                else connection.get("last_error"),
            },
        )

    def _hard_delete_project_connection_link(
        self, bl: Any, *, project_id: str, connection_id: str
    ) -> None:
        connection = self._get_connection_row(bl, connection_id)
        if not isinstance(connection, dict) or str(connection.get("project_id") or "") != str(
            project_id
        ):
            raise ServiceNotFoundError("project connection not found")
        self._update_connection_row(
            bl,
            connection_id,
            {
                "enabled": False,
                "health_status": None,
                "runtime_status": None,
                "last_seen_ok_at": None,
                "last_error": _PROJECT_CONNECTION_LIFECYCLE_DELETED,
            },
        )

    def _list_deleted_project_dataset_snapshots(
        self, bl: Any, project_id: str
    ) -> list[dict[str, Any]]:
        table_exists = getattr(bl, "_table_exists", None)
        if not callable(table_exists) or not bool(table_exists("registry_changelog")):
            return []
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, action, snapshot
                  FROM registry_changelog
                 WHERE entity_type = 'dataset'
                   AND action IN ('project_assignment', 'project_assignment_delete', 'project_assignment_restore')
                 ORDER BY created_at DESC
                """
            )
            rows = cur.fetchall() or []
            description = cur.description
        seen_dataset_ids: set[str] = set()
        deleted_rows: list[dict[str, Any]] = []
        for row in rows:
            payload = self._row_dict_from_cursor_result(row, description)
            snapshot = self._to_json_object(payload.get("snapshot"))
            dataset_id = str(payload.get("entity_id") or snapshot.get("dataset_id") or "").strip()
            if not dataset_id or dataset_id in seen_dataset_ids:
                continue
            snapshot_project_id = str(snapshot.get("project_id") or "").strip()
            snapshot_project_ids = {
                str(item).strip()
                for item in (snapshot.get("project_ids") or [])
                if str(item).strip()
            }
            if (
                snapshot_project_id != str(project_id)
                and str(project_id) not in snapshot_project_ids
            ):
                continue
            seen_dataset_ids.add(dataset_id)
            if str(payload.get("action") or "").strip() != "project_assignment_delete":
                continue
            deleted_rows.append(
                {
                    "dataset_id": dataset_id,
                    "display_name": str(
                        snapshot.get("display_name") or snapshot.get("dataset_id") or dataset_id
                    ).strip(),
                    "visible_in_catalog": bool(snapshot.get("visible_in_catalog", True)),
                }
            )
        return deleted_rows

    def _dataset_project_assignment_snapshot(
        self,
        bl: Any,
        dataset_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {}
        getter = getattr(bl, "get_dataset", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                loaded = getter(str(dataset_id), include_trashed=True)
                if isinstance(loaded, dict):
                    row = dict(loaded)
        snapshot_project_ids = [
            str(item).strip() for item in (row.get("project_ids") or []) if str(item).strip()
        ]
        if str(project_id).strip() and str(project_id).strip() not in snapshot_project_ids:
            snapshot_project_ids.append(str(project_id).strip())
        return {
            "dataset_id": str(row.get("dataset_id") or dataset_id).strip(),
            "project_id": str(project_id).strip(),
            "project_ids": sorted(dict.fromkeys(snapshot_project_ids)),
            "display_name": str(
                row.get("display_name") or row.get("dataset_id") or dataset_id
            ).strip(),
            "visible_in_catalog": bool(row.get("visible_in_catalog", True)),
        }

    @staticmethod
    def _is_lifecycle_deleted_project_connection(row: Mapping[str, Any]) -> bool:
        return str(row.get("last_error") or "").strip() == _PROJECT_CONNECTION_LIFECYCLE_DELETED

    @staticmethod
    def _row_dict_from_cursor_result(row: Any, description: Any) -> dict[str, Any]:
        if isinstance(row, Mapping):
            return dict(row)
        columns = [
            col[0] if hasattr(col, "__getitem__") else getattr(col, "name", "")
            for col in (description or [])
        ]
        if not isinstance(row, (tuple, list)):
            return {}
        return {
            str(columns[idx]): row[idx]
            for idx in range(min(len(columns), len(row)))
            if str(columns[idx]).strip()
        }

    @staticmethod
    def _to_json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            with contextlib.suppress(Exception):
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return dict(parsed)
        return {}

    def _record_lifecycle_changelog(
        self,
        bl: Any,
        *,
        entity_type: str,
        entity_id: str,
        action: str,
        actor: str,
        scope: ScopeContext,
    ) -> None:
        """Record a trash/restore transition in the registry changelog.

        Registry entities kept a content history (create/update/delete) but no
        record of being trashed or restored, so an entity's history simply went
        silent at the moment it left circulation. The snapshot written here is a
        lifecycle marker, not an entity payload: restore_entity_revision refuses
        to apply rows whose action is in LIFECYCLE_CHANGELOG_ACTIONS, so these
        rows can never be replayed over a live entity.
        """
        changelog_type = _LIFECYCLE_CHANGELOG_TYPES.get(str(entity_type or "").strip())
        if not changelog_type:
            return
        table_exists = getattr(bl, "_table_exists", None)
        if not callable(table_exists) or not bool(table_exists("registry_changelog")):
            return
        ownership: dict[str, str] = {}
        if changelog_type == "guardrail":
            # Guardrail history is account-qualified; unowned lifecycle markers
            # are invisible even after the entity itself was restored.
            ownership = {
                "account_id": str(scope.account_id or "").strip(),
                "actor_id": str(scope.actor_id or scope.actor_name).strip(),
                "actor_type": str(scope.principal_type or "unknown").strip(),
            }
        elif changelog_type in ACCOUNT_KEYED_CHANGELOG_ENTITY_TYPES:
            # A workflow's or a contract's history is kept per account (ADR 0020,
            # C5): the marker belongs to the account whose row was trashed.
            ownership = {"owner_account_id": str(scope.account_id or "").strip()}
        with bl.conn.cursor() as cur:
            snapshot = {
                "lifecycle_action": action,
                "entity_type": changelog_type,
                "entity_id": str(entity_id),
                "actor": actor,
            }
            self._log_registry_changelog_entry(
                cur,
                entity_type=changelog_type,
                entity_id=str(entity_id),
                action=action,
                snapshot=snapshot,
                author=actor,
                **ownership,
            )

    @staticmethod
    def _log_registry_changelog_entry(
        cur: Any,
        *,
        entity_type: str,
        entity_id: str,
        action: str,
        snapshot: Mapping[str, Any],
        author: str = "system",
        account_id: str | None = None,
        actor_id: str | None = None,
        actor_type: str | None = None,
        owner_account_id: str | None = None,
    ) -> None:
        if owner_account_id is not None and account_id is None:
            if (
                entity_type in ACCOUNT_KEYED_CHANGELOG_ENTITY_TYPES
                and not str(owner_account_id or "").strip()
            ):
                raise ValueError(f"A {entity_type} history entry needs its account")
            cur.execute(
                """
                INSERT INTO registry_changelog
                    (entity_type, entity_id, action, snapshot, author, account_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    entity_type,
                    entity_id,
                    action,
                    json.dumps(dict(snapshot), default=str),
                    author,
                    str(owner_account_id).strip() or None,
                ),
            )
            return
        if account_id is not None or actor_id is not None or actor_type is not None:
            if not all(str(value or "").strip() for value in (account_id, actor_id, actor_type)):
                raise ValueError("account_id, actor_id, and actor_type must be supplied together")
            cur.execute(
                """
                INSERT INTO registry_changelog
                    (entity_type, entity_id, action, snapshot, author,
                     account_id, actor_id, actor_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    entity_type,
                    entity_id,
                    action,
                    json.dumps(dict(snapshot), default=str),
                    author,
                    account_id,
                    actor_id,
                    actor_type,
                ),
            )
            return
        cur.execute(
            """
            INSERT INTO registry_changelog (entity_type, entity_id, action, snapshot, author)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (entity_type, entity_id, action, json.dumps(dict(snapshot), default=str), author),
        )

    def _hard_delete_project_direct(self, bl: Any, project_id: str) -> None:
        fenced = False
        if self._begin_project_executor_quiescence is not None:
            fenced = True
            if not self._begin_project_executor_quiescence(project_id):
                if self._end_project_executor_quiescence is not None:
                    self._end_project_executor_quiescence(project_id)
                raise ServiceConflictError(
                    "Project executors did not quiesce before hard delete",
                    payload={
                        "code": "project_executors_not_quiesced",
                        "project_id": project_id,
                    },
                )
        try:
            project_row = self._load_trashed_project_row(bl, project_id)
            self._purge_project_before_delete(bl, project_row)
            with bl.conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM projects WHERE id = %s AND deleted_at IS NOT NULL",
                    (project_id,),
                )
                if not cur.rowcount:
                    raise ServiceValidationError("project must be trashed before hard delete")
        finally:
            if fenced and self._end_project_executor_quiescence is not None:
                self._end_project_executor_quiescence(project_id)

    def _hard_delete_workspace(self, bl: Any, workspace_id: str) -> None:
        # Read the connections through the shared delete-plan reader. Looking the
        # lister up with getattr() on the store silently returned [] in
        # production (PlatformWorkerStore defines no such method), which disabled
        # both guards below and let the workspace be purged out from under its
        # still-live Data Connections.
        connections = self._list_workspace_delete_plan_data_connections(bl, str(workspace_id))
        live_connections = [row for row in connections if row.get("status") == "active"]
        if live_connections:
            raise ServiceValidationError(
                "workspace hard delete requires every live Data Connection to be reassigned or deleted"
            )
        linked_trashed_connections = [
            row
            for row in connections
            if row.get("status") == "trashed" and int(row.get("linked_dataset_count") or 0) > 0
        ]
        if linked_trashed_connections:
            raise ServiceValidationError(
                "workspace hard delete cannot purge Data Connections that still have linked datasets"
            )
        projects = self._list_workspace_projects_including_trashed(bl, workspace_id)
        remaining_projects = [
            project for project in projects if str(project.get("id") or "").strip()
        ]
        if remaining_projects:
            active_projects = [
                project for project in remaining_projects if project.get("deleted_at") is None
            ]
            if active_projects:
                raise ServiceValidationError(
                    "workspace hard delete requires every child project to be trashed or selected"
                )
            for project in remaining_projects:
                self._hard_delete_project_direct(bl, str(project.get("id") or ""))
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM workspace_data_connections
                 WHERE workspace_id = %s
                   AND deleted_at IS NOT NULL
                """,
                (str(workspace_id),),
            )
            cur.execute(
                "DELETE FROM workspaces WHERE id = %s AND deleted_at IS NOT NULL",
                (str(workspace_id),),
            )
            if not cur.rowcount:
                raise ServiceNotFoundError("workspace not found or not trashed")

    @staticmethod
    def _entity_config(entity_type: str) -> dict[str, Any]:
        cfg = _TRASH_ENTITY_CONFIG.get(entity_type)
        if not cfg:
            raise ServiceValidationError(
                f"Unknown entity type: {entity_type}. "
                f"Supported: {', '.join(sorted(_TRASH_ENTITY_CONFIG))}"
            )
        return cfg

    @staticmethod
    def _has_column(bl: Any, table_name: str, column_name: str) -> bool:
        checker = getattr(bl, "_column_exists", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(str(table_name or "").strip(), str(column_name or "").strip()))
        except Exception:
            return False


__all__ = [name for name in globals() if not name.startswith("__")]
