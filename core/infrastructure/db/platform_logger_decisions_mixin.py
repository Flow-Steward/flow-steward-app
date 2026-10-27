# mypy: ignore-errors
from __future__ import annotations

from core.infrastructure.db import (
    decision_override_store,
    job_error_store,
    step_replay_store,
)
from core.infrastructure.db.platform_logger_common import (
    SQL_AND,
    SQL_JOB_ID_EQ,
    _normalize_error_category,
    _normalize_error_source,
    _truncate_text,
)


class PlatformLoggerDecisionsMixin:
    @staticmethod
    def _normalize_override_domain(domain: str) -> str:
        return decision_override_store.normalize_override_domain(domain)

    def create_job_decision_override(
        self,
        job_id: str,
        *,
        domain: str,
        override_value,
        reason: str,
        original_value=None,
        created_by: str = "operator",
        active: bool = True,
        message_id: str | None = None,
        workflow_id: str | None = None,
        step_id: str | None = None,
        step_execution_id: str | None = None,
    ) -> dict | None:
        """Create one decision override row and deactivate prior active scope rows."""
        return decision_override_store.create_job_decision_override(
            job_id,
            conn=self.conn,
            table_exists=self._table_exists,
            normalize_domain=self._normalize_override_domain,
            domain=domain,
            override_value=override_value,
            reason=reason,
            original_value=original_value,
            created_by=created_by,
            active=active,
            message_id=message_id,
            workflow_id=workflow_id,
            step_id=step_id,
            step_execution_id=step_execution_id,
        )

    def list_job_decision_overrides(
        self,
        job_id: str,
        *,
        active_only: bool = False,
        domain: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        return decision_override_store.list_job_decision_overrides(
            job_id,
            conn=self.conn,
            table_exists=self._table_exists,
            normalize_domain=self._normalize_override_domain,
            sql_job_id_eq=SQL_JOB_ID_EQ,
            sql_and=SQL_AND,
            active_only=active_only,
            domain=domain,
            limit=limit,
        )

    def get_active_job_decision_overrides(
        self,
        job_id: str,
        *,
        domain: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        return self.list_job_decision_overrides(
            job_id,
            active_only=True,
            domain=domain,
            limit=limit,
        )

    def log_job_error(
        self,
        job_id: str,
        *,
        source: str = "unknown",
        category: str = "unknown",
        provider_name: str | None = None,
        http_status: int | None = None,
        provider_error_code: str | None = None,
        provider_error_type: str | None = None,
        request_id: str | None = None,
        retryable: bool | None = None,
        error_message: str = "",
        response_excerpt: str | None = None,
        traceback_text: str | None = None,
        context_jsonb: dict | None = None,
        message_id: str | None = None,
        agent_id: str | None = None,
        group_id: str | None = None,
        step_id: str | None = None,
        step_execution_id: str | None = None,
    ) -> dict | None:
        return job_error_store.insert_job_error(
            conn=self.conn,
            table_exists=self._table_exists,
            normalize_error_source=_normalize_error_source,
            normalize_error_category=_normalize_error_category,
            truncate_text=_truncate_text,
            job_id=job_id,
            source=source,
            category=category,
            provider_name=provider_name,
            http_status=http_status,
            provider_error_code=provider_error_code,
            provider_error_type=provider_error_type,
            request_id=request_id,
            retryable=retryable,
            error_message=error_message,
            response_excerpt=response_excerpt,
            traceback_text=traceback_text,
            context_jsonb=context_jsonb,
            message_id=message_id,
            agent_id=agent_id,
            group_id=group_id,
            step_id=step_id,
            step_execution_id=step_execution_id,
        )

    def list_job_errors(self, job_id: str, limit: int = 200) -> list[dict]:
        return job_error_store.list_job_errors(
            conn=self.conn,
            table_exists=self._table_exists,
            job_id=job_id,
            limit=limit,
        )

    def create_job_step_replay(
        self,
        job_id: str,
        *,
        message_id: str | None = None,
        correlation_id: str | None = None,
        requested_by: str = "operator",
        replay_mode: str = "prompt_reinvoke",
        status: str = "running",
        prompt_version: str | None = None,
        policy_snapshot: dict | None = None,
        captured_input: dict | None = None,
        original_outcome: dict | None = None,
    ) -> dict | None:
        return step_replay_store.create_job_step_replay(
            job_id,
            conn=self.conn,
            table_exists=self._table_exists,
            truncate_text=_truncate_text,
            message_id=message_id,
            correlation_id=correlation_id,
            requested_by=requested_by,
            replay_mode=replay_mode,
            status=status,
            prompt_version=prompt_version,
            policy_snapshot=policy_snapshot,
            captured_input=captured_input,
            original_outcome=original_outcome,
        )

    def update_job_step_replay(
        self,
        replay_id: str,
        *,
        status: str,
        replay_outcome: dict | None = None,
        original_outcome: dict | None = None,
        diff_excerpt: str | None = None,
        error_message: str | None = None,
    ) -> dict | None:
        return step_replay_store.update_job_step_replay(
            replay_id,
            conn=self.conn,
            table_exists=self._table_exists,
            truncate_text=_truncate_text,
            status=status,
            replay_outcome=replay_outcome,
            original_outcome=original_outcome,
            diff_excerpt=diff_excerpt,
            error_message=error_message,
        )

    def list_job_step_replays(
        self,
        job_id: str,
        *,
        message_id: str | None = None,
        correlation_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        return step_replay_store.list_job_step_replays(
            job_id,
            conn=self.conn,
            table_exists=self._table_exists,
            sql_job_id_eq=SQL_JOB_ID_EQ,
            sql_and=SQL_AND,
            message_id=message_id,
            correlation_id=correlation_id,
            limit=limit,
        )

    @staticmethod
    def infer_exception_hints(
        error: BaseException | str | None,
        *,
        context: dict | None = None,
    ) -> tuple[str, str]:
        return job_error_store.infer_exception_hints(
            error,
            context=context,
        )

    @staticmethod
    def normalize_exception_error(
        error: BaseException | str | None,
        *,
        source_hint: str = "unknown",
        category_hint: str = "unknown",
        context: dict | None = None,
    ) -> dict:
        return job_error_store.normalize_exception_error(
            error,
            source_hint=source_hint,
            category_hint=category_hint,
            context=context,
            normalize_error_source=_normalize_error_source,
            normalize_error_category=_normalize_error_category,
            truncate_text=_truncate_text,
        )
