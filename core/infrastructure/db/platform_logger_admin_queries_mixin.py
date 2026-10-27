# mypy: ignore-errors
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from core.infrastructure.db import (
    action_log_store,
    command_audit_store,
    job_cost_store,
    job_queries_store,
    project_settings_store,
)
from core.infrastructure.db.job_scope_contract import JobQueryScope
from core.infrastructure.db.platform_logger_common import (
    SQL_AND,
    SQL_DELETED_AT_IS_NULL,
    SQL_ID_EQ,
    SQL_PROJECT_ID_EQ,
    _as_dict,
    _as_list,
    _default_account_id,
    _json_clone,
    _normalize_string_list,
    logger,
)


class PlatformLoggerCommandAuditMixin:
    """Durable command audit rows, shared by every store whose consumers record commands.

    Kept separate from the admin queries so a narrow store (the identity store
    behind the extension lifecycle) can write the audit without inheriting the
    job and project admin surface.
    """

    def log_command_execution(
        self,
        *,
        command_id: str,
        command_type: str,
        account_id: str,
        actor_type: str,
        actor_id: str,
        target: dict | None = None,
        payload: dict | None = None,
        status: str = "accepted",
        result: dict | None = None,
        error_text: str | None = None,
        event_id: str | None = None,
        dispatch_id: str | None = None,
    ) -> dict | None:
        return command_audit_store.log_command_execution(
            conn=self.conn,
            table_exists=self._table_exists,
            column_exists=self._column_exists,
            default_account_id=_default_account_id,
            as_dict=_as_dict,
            command_id=command_id,
            command_type=command_type,
            account_id=account_id,
            actor_type=actor_type,
            actor_id=actor_id,
            target=target,
            payload=payload,
            status=status,
            result=result,
            error_text=error_text,
            event_id=event_id,
            dispatch_id=dispatch_id,
        )

    def update_command_execution(
        self,
        *,
        command_id: str,
        status: str,
        result: dict | None = None,
        error_text: str | None = None,
    ) -> dict | None:
        return command_audit_store.update_command_execution(
            conn=self.conn,
            as_dict=_as_dict,
            command_id=command_id,
            status=status,
            result=result,
            error_text=error_text,
        )


class PlatformLoggerAdminQueriesMixin(PlatformLoggerCommandAuditMixin):
    def log_action(
        self,
        project_id,
        task_id,
        task_summary,
        agent,
        action_type,
        status=None,
        iteration_no=None,
        cycle_no=None,
        duration=None,
        payload=None,
        result=None,
        error=None,
        meta=None,
        lifecycle_id=None,
        job_id=None,
        tokens_prompt=None,
        tokens_completion=None,
        tokens_total=None,
        cost_usd=None,
        model_used=None,
    ):
        return action_log_store.log_action(
            conn=self.conn,
            project_id=project_id,
            job_id=job_id,
            task_id=task_id,
            task_summary=task_summary,
            lifecycle_id=lifecycle_id,
            agent=agent,
            action_type=action_type,
            status=status,
            iteration_no=iteration_no,
            cycle_no=cycle_no,
            duration=duration,
            payload=payload,
            result=result,
            error=error,
            meta=meta,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            tokens_total=tokens_total,
            cost_usd=cost_usd,
            model_used=model_used,
        )

    def get_job_cost_summary(self, job_id: str) -> dict:
        return job_cost_store.get_job_cost_summary(
            conn=self.conn,
            job_id=job_id,
            logger=logger,
        )

    def get_jobs_cost_summary(self, job_ids: list[str]) -> dict[str, dict]:
        return job_cost_store.get_jobs_cost_summary(
            conn=self.conn,
            job_ids=job_ids,
            logger=logger,
        )

    def get_job_cost_breakdown(self, job_id: str) -> list:
        """Return per-agent, per-model cost breakdown for a job."""
        return action_log_store.get_job_cost_breakdown(
            conn=self.conn,
            logger=logger,
            job_id=job_id,
        )

    def get_project_settings_snapshot(self, project_id: str) -> dict:
        return project_settings_store.get_project_settings_snapshot(
            project_id,
            conn=self.conn,
            table_exists=self._table_exists,
            get_project=self.get_project,
            get_project_workflows=self.get_project_workflows,
            get_project_policy=self.get_project_policy,
            get_project_provider_bindings=self.get_project_provider_bindings,
            list_project_data_connections=getattr(self, "list_project_data_connections", None),
            as_dict=_as_dict,
            json_clone=_json_clone,
        )

    def create_project_settings_revision(
        self,
        project_id: str,
        *,
        author: str = "system",
        action: str = "save",
        source: str = "ui",
        snapshot: dict | None = None,
    ) -> str | None:
        return project_settings_store.create_project_settings_revision(
            project_id,
            conn=self.conn,
            table_exists=self._table_exists,
            as_dict=_as_dict,
            get_project_settings_snapshot=self.get_project_settings_snapshot,
            author=author,
            action=action,
            source=source,
            snapshot=snapshot,
        )

    def list_project_settings_revisions(
        self,
        project_id: str,
        limit: int = 50,
        *,
        include_trashed: bool = False,
    ) -> list[dict]:
        return project_settings_store.list_project_settings_revisions(
            project_id,
            conn=self.conn,
            table_exists=self._table_exists,
            column_exists=self._column_exists,
            sql_project_id_eq=SQL_PROJECT_ID_EQ,
            sql_deleted_at_is_null=SQL_DELETED_AT_IS_NULL,
            sql_and=SQL_AND,
            limit=limit,
            include_trashed=include_trashed,
        )

    def get_project_settings_revision(
        self,
        project_id: str,
        revision_id: str,
        *,
        include_trashed: bool = False,
    ) -> dict | None:
        return project_settings_store.get_project_settings_revision(
            project_id,
            revision_id,
            conn=self.conn,
            table_exists=self._table_exists,
            column_exists=self._column_exists,
            as_dict=_as_dict,
            sql_project_id_eq=SQL_PROJECT_ID_EQ,
            sql_id_eq=SQL_ID_EQ,
            sql_deleted_at_is_null=SQL_DELETED_AT_IS_NULL,
            sql_and=SQL_AND,
            include_trashed=include_trashed,
        )

    def restore_project_settings_revision(
        self,
        project_id: str,
        revision_id: str,
        *,
        author: str = "system",
    ) -> dict:
        return project_settings_store.restore_project_settings_revision(
            project_id,
            revision_id,
            as_dict=_as_dict,
            as_list=_as_list,
            normalize_string_list=_normalize_string_list,
            get_project_settings_revision=self.get_project_settings_revision,
            get_project=self.get_project,
            update_project=self.update_project,
            set_project_data_connections=getattr(self, "set_project_data_connections", None),
            get_project_provider_bindings=self.get_project_provider_bindings,
            save_project_provider_binding=self.save_project_provider_binding,
            create_project_settings_revision=self.create_project_settings_revision,
            author=author,
        )

    def get_jobs(
        self,
        project_id: str,
        limit: int = 50,
        offset: int = 0,
        *,
        include_trashed: bool = False,
    ) -> list:
        """Return jobs for a project, newest first, with pagination."""
        return job_queries_store.get_jobs(
            self.conn,
            column_exists=self._column_exists,
            project_id=project_id,
            limit=limit,
            offset=offset,
            include_trashed=include_trashed,
        )

    def count_jobs(self, project_id: str, *, include_trashed: bool = False) -> int:
        """Return total number of jobs for a project."""
        return job_queries_store.count_jobs(
            self.conn,
            column_exists=self._column_exists,
            project_id=project_id,
            include_trashed=include_trashed,
        )

    def list_jobs_for_attention(
        self,
        *,
        project_id: str | None = None,
        project_ids: list[str] | tuple[str, ...] | None = None,
        limit: int = 200,
        offset: int = 0,
        status: str | Sequence[str] | None = None,
        workflow_id: str | None = None,
        task_source: str | None = None,
        search: str | None = None,
        account_id: str | None = None,
        workspace_id: str | None = None,
        finished_after: datetime | str | None = None,
        finished_before: datetime | str | None = None,
        include_trashed: bool = False,
        scope: JobQueryScope | None = None,
        hide_acknowledged: bool = False,
    ) -> list[dict]:
        """Return recent jobs with project metadata for operator job views."""
        return job_queries_store.list_jobs_for_attention(
            self.conn,
            column_exists=self._column_exists,
            scope_conditions=self._job_scope_conditions(scope) if scope is not None else None,
            project_id=project_id,
            project_ids=project_ids,
            limit=limit,
            offset=offset,
            status=status,
            workflow_id=workflow_id,
            task_source=task_source,
            search=search,
            account_id=account_id,
            workspace_id=workspace_id,
            finished_after=finished_after,
            finished_before=finished_before,
            include_trashed=include_trashed,
            hide_acknowledged=hide_acknowledged,
        )

    def count_jobs_for_attention(
        self,
        *,
        project_id: str | None = None,
        project_ids: list[str] | tuple[str, ...] | None = None,
        status: str | Sequence[str] | None = None,
        workflow_id: str | None = None,
        task_source: str | None = None,
        search: str | None = None,
        account_id: str | None = None,
        workspace_id: str | None = None,
        include_trashed: bool = False,
        scope: JobQueryScope | None = None,
        hide_acknowledged: bool = False,
    ) -> int:
        """Return total jobs count for operator job views."""
        return job_queries_store.count_jobs_for_attention(
            self.conn,
            column_exists=self._column_exists,
            scope_conditions=self._job_scope_conditions(scope) if scope is not None else None,
            project_id=project_id,
            project_ids=project_ids,
            status=status,
            workflow_id=workflow_id,
            task_source=task_source,
            search=search,
            account_id=account_id,
            workspace_id=workspace_id,
            include_trashed=include_trashed,
            hide_acknowledged=hide_acknowledged,
        )

    def get_workflow_stats(
        self,
        *,
        workflow_id: str,
        account_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Return aggregated job + cost statistics for a workflow."""
        return job_queries_store.get_workflow_stats(
            self.conn,
            column_exists=self._column_exists,
            workflow_id=workflow_id,
            account_id=account_id,
            workspace_id=workspace_id,
        )

    def acknowledge_job(self, job_id: str, username: str) -> bool:
        """Mark a failed job as acknowledged (triaged)."""
        return job_queries_store.acknowledge_job(
            self.conn,
            job_id=job_id,
            username=username,
        )

    def unacknowledge_job(self, job_id: str) -> bool:
        """Remove acknowledgment from a job."""
        return job_queries_store.unacknowledge_job(
            self.conn,
            job_id=job_id,
        )
