# ruff: noqa: F403, F405
# mypy: ignore-errors
"""Hard-delete purge steps of the trash lifecycle.

Before a trashed project row is deleted, everything it owns goes first: its
jobs and their stored artifacts, MCP projections, project-owned Postgres rows,
memory and search indexes. ``TrashLifecycleServicePurgeMixin`` holds those
steps; ``TrashLifecycleServiceEntitiesMixin`` calls them from its hard-delete
paths.
"""

from __future__ import annotations

from .command_services_common import *  # NOSONAR


class TrashLifecycleServicePurgeMixin:
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
