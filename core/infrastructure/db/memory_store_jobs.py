# mypy: ignore-errors
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

from core.application.memory.models import MemoryRecord, ScopeId
from core.infrastructure.db import action_log_store
from core.infrastructure.db.memory_store_common import _db_account_id, _db_scope_id, _row_to_record

logger = logging.getLogger(__name__)


class MemoryJobStoreMixin:
    def load_job_source(self, *, job_id: ScopeId) -> dict[str, Any] | None:
        return self._fetchone(
            """
            SELECT
                j.*,
                p.name AS project_name,
                p.account_id AS account_id
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            WHERE j.id = %s
            LIMIT 1
            """,
            [_db_scope_id(job_id)],
        )

    def load_job_agent_messages(self, *, job_id: ScopeId, limit: int = 200) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            SELECT
                m.id,
                m.job_id,
                m.correlation_id,
                m.from_agent,
                m.to_agent,
                m.step_id,
                m.step_execution_id,
                m.attempt,
                m.group_id,
                m.skill,
                m.status,
                m.summary,
                m.artifacts,
                m.errors,
                j.project_id,
                p.account_id
            FROM platform_agent_messages m
            JOIN jobs j ON j.id = m.job_id
            JOIN projects p ON p.id = j.project_id
            WHERE m.job_id = %s
            ORDER BY m."timestamp" DESC, m.id DESC
            LIMIT %s
            """,
            [_db_scope_id(job_id), max(1, min(int(limit or 200), 500))],
        )

    def load_project_profile_record(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
    ) -> MemoryRecord | None:
        row = self._fetchone(
            """
            SELECT *
            FROM memory_records
            WHERE account_id = %s
              AND (project_id = %s OR project_id IS NULL)
              AND memory_type = 'docs_note'
              AND (
                    source_type = 'project_profile_sync'
                    OR EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(COALESCE(tags_jsonb, '[]'::jsonb)) AS tag(value)
                        WHERE tag.value = 'project_profile'
                    )
                  )
              AND lifecycle_status IN ('active', 'candidate')
            ORDER BY
                CASE WHEN source_trust IN ('canonical_doc', 'human_approved') THEN 0 ELSE 1 END,
                CASE WHEN lifecycle_status = 'active' THEN 0 ELSE 1 END,
                updated_at DESC,
                id DESC
            LIMIT 1
            """,
            [_db_account_id(account_id), _db_scope_id(project_id)],
        )
        return _row_to_record(row) if row else None

    def load_exact_project_profile_record(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
    ) -> MemoryRecord | None:
        row = self._fetchone(
            """
            SELECT *
            FROM memory_records
            WHERE account_id = %s
              AND project_id = %s
              AND memory_type = 'docs_note'
              AND (
                    source_type = 'project_profile_sync'
                    OR EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(COALESCE(tags_jsonb, '[]'::jsonb)) AS tag(value)
                        WHERE tag.value = 'project_profile'
                    )
                  )
              AND lifecycle_status IN ('active', 'candidate')
            ORDER BY
                CASE WHEN source_trust IN ('canonical_doc', 'human_approved') THEN 0 ELSE 1 END,
                CASE WHEN lifecycle_status = 'active' THEN 0 ELSE 1 END,
                updated_at DESC,
                id DESC
            LIMIT 1
            """,
            [_db_account_id(account_id), _db_scope_id(project_id)],
        )
        return _row_to_record(row) if row else None

    def record_audit_event(
        self,
        *,
        account_id: str,
        actor_id: str,
        action_type: str,
        project_id: ScopeId | None = None,
        job_id: ScopeId | None = None,
        task_id: str = "",
        task_summary: str = "",
        status: str | None = None,
        payload: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            action_log_store.log_action(
                conn=cast(Any, self._current_conn()),
                project_id=project_id,
                job_id=job_id,
                task_id=task_id,
                task_summary=task_summary,
                lifecycle_id="memory",
                agent=str(actor_id or "memory").strip() or "memory",
                action_type=action_type,
                status=status,
                payload=dict(payload or {}),
                result=dict(result or {}),
                error=error,
                meta={"account_id": _db_account_id(account_id), **dict(meta or {})},
            )
            self._maybe_commit()
        except Exception as exc:
            logger.debug("record_audit_event failed (non-fatal): %s", exc)
