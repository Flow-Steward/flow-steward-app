from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import MemoryRepository, ScopeId


class MemoryAuditService:
    def __init__(self, *, repository: MemoryRepository) -> None:
        self._repository = repository

    def record(
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
        self._repository.record_audit_event(
            account_id=account_id,
            actor_id=actor_id,
            action_type=action_type,
            project_id=project_id,
            job_id=job_id,
            task_id=task_id,
            task_summary=task_summary,
            status=status,
            payload=payload,
            result=result,
            error=error,
            meta=meta,
        )
