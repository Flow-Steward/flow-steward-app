"""PostgreSQL adapter for durable workflow test-run queries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.infrastructure.db import job_admin_store, job_thread_store


@dataclass(frozen=True)
class PostgresWorkflowTestRunJobRepository:
    get_db: Callable[[], Any]

    def find_by_thread_id(self, thread_id: str) -> dict[str, Any] | None:
        db = self.get_db()
        row = job_thread_store.get_job_context_by_thread_id(db.conn, thread_id)
        if not isinstance(row, dict):
            return None
        job_id = str(row.get("id") or "").strip()
        if not job_id:
            return dict(row)
        job = job_admin_store.get_job(db.conn, job_id=job_id)
        return dict(job) if isinstance(job, dict) else dict(row)
