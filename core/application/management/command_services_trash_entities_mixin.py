# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .command_services_common import *  # NOSONAR

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


class TrashLifecycleServiceEntitiesMixin:
    def _restore_entity_direct(self, bl: Any, entity_type: str, entity_id: str) -> None:
        cfg = self._entity_config(entity_type)
        table = cfg["table"]
        id_col = cfg["id_col"]
        has_deleted_by = self._has_column(bl, table, "deleted_by")
        set_clause = (
            "deleted_at = NULL, deleted_by = NULL" if has_deleted_by else "deleted_at = NULL"
        )
        with bl.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {table} SET {set_clause} WHERE {id_col} = %s AND deleted_at IS NOT NULL",
                (entity_id,),
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
            self._workflow_with_sample_files(bl.conn, entity_id)
            if entity_type == "registry_workflow"
            else None
        )
        with bl.conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {table} WHERE {id_col} = %s AND deleted_at IS NOT NULL",
                (entity_id,),
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
    def _workflow_with_sample_files(conn: Any, workflow_id: str) -> dict[str, Any] | None:
        """The workflow row a hard delete is about to remove, for its test-run files."""
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT account_id, settings FROM registry_workflows WHERE workflow_id = %s",
                    (workflow_id,),
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
        ownership = {}
        if changelog_type == "guardrail":
            # Guardrail history is account-qualified; unowned lifecycle markers
            # are invisible even after the entity itself was restored.
            ownership = {
                "account_id": str(scope.account_id or "").strip(),
                "actor_id": str(scope.actor_id or scope.actor_name).strip(),
                "actor_type": str(scope.principal_type or "unknown").strip(),
            }
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
    ) -> None:
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

    def _load_trashed_project_row(self, bl: Any, project_id: str | int) -> dict[str, Any]:
        if callable(getattr(bl, "get_project", None)):
            project_row = bl.get_project(str(project_id), include_trashed=True)
            if project_row and project_row.get("deleted_at") is not None:
                return dict(project_row)
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, account_id, name, deleted_at
                FROM projects
                WHERE id = %s AND deleted_at IS NOT NULL
                LIMIT 1
                """,
                (str(project_id),),
            )
            row = cur.fetchone()
        if not row:
            raise ServiceNotFoundError("project not found or not trashed")
        if isinstance(row, dict):
            return dict(row)
        return {
            "id": row[0],
            "account_id": row[1],
            "name": row[2],
            "deleted_at": row[3],
        }

    def _purge_project_before_delete(self, bl: Any, project_row: Mapping[str, Any]) -> None:
        project_id = str(project_row.get("id") or "").strip()
        account_id = str(project_row.get("account_id") or "").strip()
        project_name = str(project_row.get("name") or "").strip()
        if not project_id:
            raise ServiceValidationError("project hard delete requires a valid project id")
        self._purge_project_jobs_before_delete(bl, project_id)
        self._purge_project_mcp_projections_before_delete(
            bl,
            project_id=project_id,
            account_id=account_id,
        )
        self._purge_project_owned_postgres_rows(bl, project_id)

        def cleanup_external_project_scope() -> None:
            cleanup_error: Exception | None = None
            try:
                self._purge_project_memory_and_indexes(
                    bl,
                    account_id=account_id,
                    project_id=project_id,
                )
            except Exception as exc:
                logger.exception(
                    "Project relational hard delete committed but memory cleanup failed "
                    "(account_id=%s, project_id=%s, project_name=%s)",
                    account_id,
                    project_id,
                    project_name,
                )
                cleanup_error = exc
            try:
                if self._delete_project_queues is not None:
                    self._delete_project_queues(project_id)
            except Exception as exc:
                logger.exception(
                    "Project relational hard delete committed but queue cleanup failed "
                    "(project_id=%s)",
                    project_id,
                )
                cleanup_error = cleanup_error or exc
            if cleanup_error is not None:
                raise ServiceUnavailableError(
                    "Project was deleted, but external project cleanup failed"
                ) from cleanup_error

        from core.infrastructure.db.db_pool import register_transaction_after_commit

        if not register_transaction_after_commit(bl.conn, cleanup_external_project_scope):
            cleanup_external_project_scope()

    def _purge_project_jobs_before_delete(self, bl: Any, project_id: str) -> None:
        lease_lister = getattr(bl, "list_project_execution_leases", None)
        active_leases = list(
            lease_lister(active_only=True, project_id=project_id) or []
            if callable(lease_lister)
            else []
        )
        if active_leases:
            active_job_ids = sorted(
                {
                    str(lease.get("active_job_id") or "").strip()
                    for lease in active_leases
                    if str(lease.get("active_job_id") or "").strip()
                }
            )
            raise ServiceConflictError(
                "Project executors did not quiesce before hard delete",
                payload={
                    "code": "project_executors_not_quiesced",
                    "project_id": project_id,
                    "active_job_ids": active_job_ids,
                },
            )
        if not callable(getattr(bl, "delete_job", None)):
            return
        with bl.conn.cursor() as cur:
            cur.execute("SELECT id FROM jobs WHERE project_id = %s", (project_id,))
            job_ids = [str(row[0]) for row in cur.fetchall() or [] if row and row[0] is not None]
        jobs = [
            (job_id, bl.get_job(job_id))
            for job_id in job_ids
            if callable(getattr(bl, "get_job", None))
        ]
        blocking_jobs = [
            {
                "job_id": job_id,
                "status": str((job or {}).get("status") or "unknown").strip() or "unknown",
            }
            for job_id, job in jobs
            if job is not None and _job_delete_blocker(job) is not None
        ]
        if blocking_jobs:
            raise ServiceConflictError(
                "Project hard delete requires every job to be settled",
                payload={
                    "code": "project_has_unsettled_jobs",
                    "project_id": project_id,
                    "blocking_jobs": blocking_jobs,
                },
            )
        for job_id in job_ids:
            purged_job = self._purge_job_artifacts_before_delete(bl, job_id)
            if bl.delete_job(job_id, allow_active=False):
                self._finalize_job_artifact_purge(purged_job)

    def _purge_project_owned_postgres_rows(self, bl: Any, project_id: str) -> None:
        table_names = self._project_owned_table_names(bl)
        for table_name in table_names:
            if table_name in {"projects", "jobs"}:
                continue
            with bl.conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {table_name} WHERE project_id = %s",
                    (project_id,),
                )

    @staticmethod
    def _purge_project_mcp_projections_before_delete(
        bl: Any,
        *,
        project_id: str,
        account_id: str,
    ) -> None:
        """Delete project-owned MCP projections that have no project FK.

        The project lifecycle transaction owns this purge. Both the relational
        scope and the immutable projection lineage must match, so a malformed
        row cannot cause another project's tool to be deleted.
        """
        from core.application.auth.mcp_connector_oauth import (
            prepare_mcp_connector_oauth_delete_locked,
            schedule_prepared_mcp_connector_revocation,
        )

        with bl.conn.cursor() as cur:
            cur.execute(
                """SELECT connector_id, account_id, project_id, tenant_id, base_url,
                          oauth_metadata_jsonb, oauth_secret_refs_jsonb,
                          oauth_credential_revision, oauth_connected_identity
                     FROM mcp_connectors
                    WHERE project_id = %s AND account_id = %s
                    FOR UPDATE""",
                (project_id, account_id),
            )
            columns = [str(item[0]) for item in (cur.description or [])]
            connector_rows = [
                dict(raw)
                if isinstance(raw, Mapping)
                else {columns[index]: value for index, value in enumerate(raw)}
                for raw in (cur.fetchall() or [])
            ]
            for connector_row in connector_rows:
                prepared, _retained_metadata = prepare_mcp_connector_oauth_delete_locked(
                    bl.conn,
                    connector_row=connector_row,
                )
                schedule_prepared_mcp_connector_revocation(bl.conn, prepared)
            cur.execute(
                """DELETE FROM registry_tools
                     WHERE project_id = %s
                       AND account_id = %s
                       AND COALESCE(implementation->>'type', implementation->>'kind') = 'mcp'
                       AND implementation->>'owner_project_id' = %s
                       AND implementation->>'owner_account_id' = %s""",
                (project_id, account_id, project_id, account_id),
            )

    def _project_owned_table_names(self, bl: Any) -> list[str]:
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT cls.relname
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                JOIN pg_namespace ns ON ns.oid = cls.relnamespace
                JOIN pg_attribute attr
                  ON attr.attrelid = con.conrelid
                 AND attr.attnum = ANY(con.conkey)
                WHERE con.contype = 'f'
                  AND con.confrelid = 'public.projects'::regclass
                  AND ns.nspname = 'public'
                  AND attr.attname = 'project_id'
                ORDER BY cls.relname
                """
            )
            return [str(row[0]) for row in cur.fetchall() or [] if row and row[0]]

    def _purge_project_memory_and_indexes(
        self,
        bl: Any,
        *,
        account_id: str,
        project_id: str,
    ) -> None:
        from core.application.memory.runtime import build_memory_services

        services = build_memory_services(logger_instance=bl)
        if account_id:
            services.vector_backend.delete_project_scope(
                account_id=account_id,
                project_id=project_id,
                repository=services.repository,
            )
            services.repository.delete_project_scope(
                account_id=account_id,
                project_id=project_id,
            )

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

    def _artifact_storage(self) -> Any | None:
        plane = self._artifact_plane
        storage = getattr(plane, "storage", None) or getattr(plane, "_storage", None)
        if callable(getattr(storage, "delete", None)):
            return storage
        plane = _resolve_artifact_plane(plane)
        if plane is None:
            return None
        storage = getattr(plane, "storage", None) or getattr(plane, "_storage", None)
        if callable(getattr(storage, "delete", None)):
            return storage
        if callable(getattr(plane, "delete", None)):
            return plane
        return None

    def _purge_job_artifacts_before_delete(self, bl: Any, job_id: Any) -> dict[str, Any] | None:
        if not callable(getattr(bl, "get_job", None)):
            return None
        try:
            job_row = bl.get_job(str(job_id))
            if not isinstance(job_row, dict):
                return None
            storage = self._artifact_storage()
            if storage is None:
                has_durable_owner = bool(job_row.get("id") and job_row.get("project_id"))
                if job_row.get("meta") or has_durable_owner:
                    raise RuntimeError("artifact storage is unavailable")
                return None
            stats = purge_job_artifacts(job_row, storage=storage)
            if int(stats.get("failed") or 0) > 0:
                raise ServiceConflictError(
                    "Artifact cleanup did not complete",
                    payload={
                        "error": "Artifact cleanup did not complete",
                        "code": "artifact_cleanup_incomplete",
                        "job_id": str(job_id),
                    },
                )
            return job_row
        except ServiceConflictError:
            raise
        except Exception as exc:
            raise ServiceConflictError(
                "Artifact cleanup must complete before the job can be hard-deleted",
                payload={
                    "error": "Artifact cleanup failed",
                    "code": "artifact_cleanup_failed",
                    "job_id": str(job_id),
                },
            ) from exc

    def _finalize_job_artifact_purge(self, job_row: Mapping[str, Any] | None) -> None:
        if not isinstance(job_row, Mapping):
            return
        storage = self._artifact_storage()
        if storage is not None:

            def finalize_after_commit() -> None:
                if not finalize_job_artifact_purge(job_row, storage=storage):
                    raise ServiceUnavailableError(
                        "Database transition committed, but artifact cleanup journal remains"
                    )

            from core.infrastructure.db.db_pool import register_transaction_after_commit

            bl = self._logger()
            if not register_transaction_after_commit(bl.conn, finalize_after_commit):
                finalize_after_commit()


__all__ = [name for name in globals() if not name.startswith("__")]
