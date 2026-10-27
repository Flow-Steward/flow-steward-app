# mypy: ignore-errors
from __future__ import annotations

from typing import Any

from core.infrastructure.db import (
    action_log_store,
    agent_message_store,
    event_store,
    job_activity_store,
    job_admin_store,
    job_attachment_store,
    project_execution_store,
)
from core.infrastructure.db.platform_logger_common import (
    SQL_AND,
    SQL_JOB_ID_EQ,
    SQL_JOB_PROJECT_ID_EQ,
    SQL_PROJECT_ACCOUNT_ID_EQ,
    SQL_PROJECT_ID_EQ,
    SQL_STATUS_EQ,
    _as_dict,
    _coerce_bytes,
    _coerce_int,
    _decrypt_attachment_value,
    _default_account_id,
    _encrypt_attachment_value,
    _normalize_string_list,
    logger,
)


class PlatformLoggerActivityMixin:
    def record_public_progress_transition(self, **transition: Any):
        """Persist one strict public snapshot and marker transaction."""
        from core.application.jobs.public_progress_repository import PublicProgressRepository
        from core.application.jobs.public_progress_service import PublicProgressService

        repository = PublicProgressRepository(self.conn)
        return PublicProgressService(repository).record_transition(**transition)

    def reconcile_public_run_trace(self, **evidence: Any):
        from core.application.jobs.public_progress_repository import PublicProgressRepository
        from core.application.jobs.public_progress_service import PublicProgressService

        return PublicProgressService(PublicProgressRepository(self.conn)).record_committed_trace(
            **evidence
        )

    def reconcile_public_terminal_stop(self, **evidence: Any):
        """Enrich verified Stop metadata without changing operation lifecycle."""
        from core.application.jobs.public_progress_repository import PublicProgressRepository
        from core.application.jobs.public_progress_service import PublicProgressService

        return PublicProgressService(PublicProgressRepository(self.conn)).record_terminal_stop(
            **evidence
        )

    def get_public_progress_snapshot(self, job_id: str):
        """Load the current durable public snapshot without rebuilding history."""
        from core.application.jobs.public_progress_repository import PublicProgressRepository
        from core.application.jobs.public_progress_service import PublicProgressService

        repository = PublicProgressRepository(self.conn)
        return PublicProgressService(repository).current_snapshot(job_id=job_id)

    def get_public_state_marker(
        self,
        cursor_id: Any,
        job_id: str,
        account_id: str,
        *,
        project_id: str | None = None,
    ) -> dict | None:
        """Strictly resolve a public-state cursor for one authorized job."""
        return event_store.get_public_state_marker(
            cursor_id,
            job_id,
            account_id,
            conn=self.conn,
            logger=logger,
            sql_project_account_id_eq=SQL_PROJECT_ACCOUNT_ID_EQ,
            sql_and=SQL_AND,
            project_id=project_id,
        )

    def get_latest_public_state_marker(
        self,
        job_id: str,
        account_id: str,
        *,
        project_id: str | None = None,
    ) -> dict | None:
        """Strictly load the latest public-state marker for one job."""
        return event_store.get_latest_public_state_marker(
            job_id,
            account_id,
            conn=self.conn,
            logger=logger,
            sql_project_account_id_eq=SQL_PROJECT_ACCOUNT_ID_EQ,
            sql_and=SQL_AND,
            project_id=project_id,
        )

    def list_public_state_markers_since(
        self,
        cursor_id: Any,
        job_id: str,
        account_id: str,
        *,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list:
        """Strictly list public-state markers for one job and cursor."""
        return event_store.list_public_state_markers_since(
            cursor_id,
            job_id,
            account_id,
            conn=self.conn,
            logger=logger,
            sql_project_account_id_eq=SQL_PROJECT_ACCOUNT_ID_EQ,
            sql_and=SQL_AND,
            project_id=project_id,
            limit=limit,
        )

    def delete_job(self, job_id: str, allow_active: bool = False) -> bool:
        """Delete a job and all dependent observability rows."""
        return job_admin_store.delete_job(
            self.conn,
            table_exists=self._table_exists,
            get_job_func=self.get_job,
            job_id=job_id,
            allow_active=allow_active,
        )

    def get_job(self, job_id: str) -> dict | None:
        """Return a single job by id."""
        return job_admin_store.get_job(
            self.conn,
            job_id=job_id,
        )

    def save_files(self, job_id: str, files: list) -> list:
        """Persist uploaded files to job_attachments and return canonical file_ref[] rows.

        files: list of {filename, mime_type, size_bytes, content (bytes)}
        Returns: list of
          {artifact_id, filename, content_type, size_bytes, sha256, object_key, source_ref, retention_class}
        """
        return job_attachment_store.save_files(
            conn=self.conn,
            job_id=job_id,
            files=files,
            column_exists=self._column_exists,
            encrypt_attachment_value=_encrypt_attachment_value,
        )

    def get_attachments(self, job_id: str) -> list:
        """Load all attachments for a job, including binary content.
        Returns: list of {id, filename, mime_type, size_bytes, content (bytes)}
        """
        return job_attachment_store.get_attachments(
            conn=self.conn,
            job_id=job_id,
            column_exists=self._column_exists,
            decrypt_attachment_value=_decrypt_attachment_value,
            coerce_bytes=_coerce_bytes,
        )

    def get_awaiting_approval_jobs(self, *, account_id: str | None = None) -> list:
        """Return jobs currently awaiting user approval, optionally filtered by account."""
        return job_admin_store.get_awaiting_approval_jobs(
            self.conn,
            account_id=account_id,
            sql_project_account_id_eq=SQL_PROJECT_ACCOUNT_ID_EQ,
            sql_and=SQL_AND,
        )

    def get_pending_jobs(self, project_id: str) -> list:
        """
        Return all pending (queued, not yet started) jobs for a project,
        ordered by queued_at ascending (FIFO queue order).
        Used by the UI to show queue position.
        """
        return job_admin_store.get_pending_jobs(
            self.conn,
            project_id=project_id,
        )

    def claim_external_agent_job(
        self,
        *,
        project_id: str,
        worker_instance_id: str,
        workflow_id: str | None = None,
        allowed_workflow_ids: list[str] | None = None,
        claim_token: str | None = None,
        agent_name: str | None = None,
    ) -> dict | None:
        return project_execution_store.claim_external_agent_job(
            conn=self.conn,
            normalize_string_list=_normalize_string_list,
            as_dict=_as_dict,
            default_account_id=_default_account_id,
            sql_and=SQL_AND,
            sql_job_project_id_eq=SQL_JOB_PROJECT_ID_EQ,
            project_id=project_id,
            worker_instance_id=worker_instance_id,
            workflow_id=workflow_id,
            allowed_workflow_ids=allowed_workflow_ids,
            claim_token=claim_token,
            agent_name=agent_name,
        )

    @staticmethod
    def _normalize_project_execution_lease_row(row: dict | None) -> dict:
        return project_execution_store.normalize_project_execution_lease_row(
            row,
            coerce_int=_coerce_int,
        )

    def list_project_execution_leases(
        self,
        *,
        active_only: bool = True,
        project_id: str | None = None,
        worker_instance_id: str | None = None,
        lease_token: str | None = None,
    ) -> list[dict]:
        return project_execution_store.list_project_execution_leases(
            conn=self.conn,
            table_exists=self._table_exists,
            coerce_int=_coerce_int,
            sql_and=SQL_AND,
            _sql_project_id_eq=SQL_PROJECT_ID_EQ,
            active_only=active_only,
            project_id=project_id,
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
        )

    def get_project_execution_lease(
        self,
        project_id: str,
        *,
        active_only: bool = True,
        worker_instance_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict | None:
        leases = self.list_project_execution_leases(
            active_only=active_only,
            project_id=project_id,
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
        )
        return leases[0] if leases else None

    def acquire_project_execution_lease(
        self,
        *,
        project_id: str,
        worker_instance_id: str,
        lease_token: str | None = None,
        ttl_seconds: int = 45,
        drain_status: str = "acquired",
        active_job_id: str | None = None,
    ) -> dict | None:
        return project_execution_store.acquire_project_execution_lease(
            conn=self.conn,
            table_exists=self._table_exists,
            coerce_int=_coerce_int,
            project_id=project_id,
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
            ttl_seconds=ttl_seconds,
            drain_status=drain_status,
            active_job_id=active_job_id,
        )

    def heartbeat_project_execution_lease(
        self,
        *,
        project_id: str,
        worker_instance_id: str,
        lease_token: str,
        ttl_seconds: int = 45,
        drain_status: str = "draining",
        active_job_id: str | None = None,
    ) -> dict | None:
        return project_execution_store.heartbeat_project_execution_lease(
            conn=self.conn,
            table_exists=self._table_exists,
            coerce_int=_coerce_int,
            project_id=project_id,
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
            ttl_seconds=ttl_seconds,
            drain_status=drain_status,
            active_job_id=active_job_id,
        )

    def release_project_execution_lease(
        self,
        *,
        project_id: str,
        worker_instance_id: str,
        lease_token: str,
    ) -> bool:
        return project_execution_store.release_project_execution_lease(
            conn=self.conn,
            table_exists=self._table_exists,
            project_id=project_id,
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
        )

    def get_active_jobs(self) -> list:
        """Return all active jobs with runtime observability fields."""
        return job_activity_store.get_active_jobs(conn=self.conn)

    def get_logs(
        self,
        job_id: str,
        since_id: Any = 0,
        limit: int = 200,
        before_id: Any | None = None,
        newest: bool = False,
        include_deleted: bool = False,
    ) -> list:
        """Return log rows for a job with incremental and pagination filters."""
        return action_log_store.get_logs(
            conn=self.conn,
            logger=logger,
            sql_job_id_eq=SQL_JOB_ID_EQ,
            sql_and=SQL_AND,
            job_id=job_id,
            since_id=since_id,
            limit=limit,
            before_id=before_id,
            newest=newest,
            include_deleted=include_deleted,
        )

    def log_agent_message(self, job_id: str, envelope: dict) -> bool:
        """Persist canonical AgentMessageV1 envelope."""
        from core.application.orchestration.models import validate_agent_message_payload

        return agent_message_store.log_agent_message(
            conn=self.conn,
            logger=logger,
            validate_agent_message_payload=validate_agent_message_payload,
            job_id=job_id,
            envelope=envelope,
        )

    def get_agent_messages(
        self,
        job_id: str,
        since_id: Any = 0,
        limit: int = 500,
        before_id: Any | None = None,
        newest: bool = False,
        step_id: str | None = None,
        from_agent: str | None = None,
        skill: str | None = None,
        status: str | None = None,
    ) -> list:
        """Return AgentMessageV1 rows for a job with optional filters."""
        return agent_message_store.get_agent_messages(
            conn=self.conn,
            logger=logger,
            sql_job_id_eq=SQL_JOB_ID_EQ,
            sql_status_eq=SQL_STATUS_EQ,
            sql_and=SQL_AND,
            job_id=job_id,
            since_id=since_id,
            limit=limit,
            before_id=before_id,
            newest=newest,
            step_id=step_id,
            from_agent=from_agent,
            skill=skill,
            status=status,
        )

    def get_agent_message_by_id(self, message_id: Any) -> dict | None:
        """Return a single AgentMessageV1 row by id."""
        return agent_message_store.get_agent_message_by_id(
            conn=self.conn,
            logger=logger,
            message_id=message_id,
        )

    def get_latest_agent_message(
        self,
        job_id: str,
        from_agent: str | None = None,
        step_id: str | None = None,
    ) -> dict | None:
        """Return latest AgentMessageV1 envelope row for a job."""
        return agent_message_store.get_latest_agent_message(
            conn=self.conn,
            logger=logger,
            sql_job_id_eq=SQL_JOB_ID_EQ,
            sql_and=SQL_AND,
            job_id=job_id,
            from_agent=from_agent,
            step_id=step_id,
        )

    def get_latest_log_entry(
        self,
        job_id: str,
        action_types: list[str] | None = None,
        require_error: bool = False,
    ) -> dict | None:
        """Return the latest action log row for a job matching optional filters."""
        return action_log_store.get_latest_log_entry(
            conn=self.conn,
            logger=logger,
            sql_job_id_eq=SQL_JOB_ID_EQ,
            sql_and=SQL_AND,
            job_id=job_id,
            action_types=action_types,
            require_error=require_error,
        )

    def _insert_event_row(
        self,
        job_id: str,
        phase: str,
        agent: str,
        event_type: str,
        title: str,
        detail: dict | None = None,
        severity: str = "info",
    ) -> None:
        return event_store.insert_event_row(
            conn=self.conn,
            job_id=job_id,
            phase=phase,
            agent=agent,
            event_type=event_type,
            title=title,
            detail=detail,
            severity=severity,
        )

    def log_event(
        self,
        job_id: str,
        phase: str,
        agent: str,
        event_type: str,
        title: str,
        detail: dict | None = None,
        severity: str = "info",
    ) -> bool:
        """Insert a semantic event row into platform_events.

        Returns True on success, False on failure. Errors are logged at DEBUG level
        and swallowed so agent nodes are never interrupted by observability failures.
        """
        return event_store.log_event(
            insert_event_row_func=self._insert_event_row,
            logger=logger,
            job_id=job_id,
            phase=phase,
            agent=agent,
            event_type=event_type,
            title=title,
            detail=detail,
            severity=severity,
        )

    def queue_event_for_retry(self, payload: dict, failure_reason: str = "") -> bool:
        """Persist an event payload in DB for later replay."""
        return event_store.queue_event_for_retry(
            conn=self.conn,
            logger=logger,
            payload=payload,
            failure_reason=failure_reason,
        )

    def replay_queued_events(self, max_events: int = 200) -> int:
        """Best-effort replay of queued events from DB-backed retry table."""
        return event_store.replay_queued_events(
            conn=self.conn,
            insert_event_row_func=self._insert_event_row,
            logger=logger,
            max_events=max_events,
        )

    def get_events(self, job_id: str, since_id: Any = 0, limit: int = 500) -> list:
        """Return event rows for a job after an opaque event cursor."""
        return event_store.get_events(
            conn=self.conn,
            logger=logger,
            job_id=job_id,
            since_id=since_id,
            limit=limit,
        )

    def get_latest_event(
        self,
        job_id: str,
        event_types: list[str] | None = None,
    ) -> dict | None:
        """Return the latest event row for a job filtered by optional event types."""
        return event_store.get_latest_event(
            conn=self.conn,
            logger=logger,
            sql_job_id_eq=SQL_JOB_ID_EQ,
            sql_and=SQL_AND,
            job_id=job_id,
            event_types=event_types,
        )

    def list_events_since(
        self,
        cursor_id: Any,
        account_id: str,
        *,
        project_id: str | None = None,
        event_type: str | None = None,
        allowed_project_ids: list[str] | None = None,
        limit: int = 50,
    ) -> list:
        """Return events across all jobs for an account, with cursor-based pagination.

        Joins through jobs → projects to enforce account scoping since
        ``platform_events`` has no ``account_id`` column directly.

        Parameters
        ----------
        allowed_project_ids:
            When non-empty, restrict results to this set of project IDs.
            Used to enforce service-account / restricted-user membership without
            requiring the caller to pass an explicit ``project_id`` filter.
        """
        return event_store.list_events_since(
            cursor_id,
            account_id,
            conn=self.conn,
            logger=logger,
            sql_project_account_id_eq=SQL_PROJECT_ACCOUNT_ID_EQ,
            sql_and=SQL_AND,
            project_id=project_id,
            event_type=event_type,
            allowed_project_ids=allowed_project_ids,
            limit=limit,
        )
