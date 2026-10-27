# mypy: ignore-errors
from __future__ import annotations

from core.infrastructure.db import (
    scheduled_job_lock_store,
)
from core.infrastructure.db.platform_logger_common import (
    _coerce_int,
)


class PlatformLoggerScheduledLocksMixin:
    def acquire_scheduled_job_execution_lock(
        self,
        *,
        scheduled_job_id: str,
        account_id: str,
        project_id: str,
        workflow_id: str,
        lock_key: str,
        slot_ordinal: int = 1,
        job_id: str | None = None,
        run_type: str = "scheduled",
        status: str = "pre_publish",
        scheduled_for=None,
        scheduler_instance_id: str = "",
        worker_instance_id: str = "",
        expires_at=None,
    ):
        return scheduled_job_lock_store.acquire_scheduled_job_execution_lock(
            conn=self.conn,
            table_exists=self._table_exists,
            coerce_int=_coerce_int,
            scheduled_job_id=scheduled_job_id,
            account_id=account_id,
            project_id=project_id,
            workflow_id=workflow_id,
            lock_key=lock_key,
            slot_ordinal=slot_ordinal,
            job_id=job_id,
            run_type=run_type,
            status=status,
            scheduled_for=scheduled_for,
            scheduler_instance_id=scheduler_instance_id,
            worker_instance_id=worker_instance_id,
            expires_at=expires_at,
        )

    def activate_scheduled_job_execution_lock(
        self,
        *,
        lock_id: str,
        worker_instance_id: str = "",
        status: str = "active",
        expires_at=None,
    ):
        return scheduled_job_lock_store.activate_scheduled_job_execution_lock(
            conn=self.conn,
            table_exists=self._table_exists,
            coerce_int=_coerce_int,
            lock_id=lock_id,
            worker_instance_id=worker_instance_id,
            status=status,
            expires_at=expires_at,
        )

    def release_scheduled_job_execution_lock_by_job_id(
        self,
        *,
        job_id: str,
        release_reason: str = "",
        status: str = "released",
        worker_instance_id: str = "",
    ):
        return scheduled_job_lock_store.release_scheduled_job_execution_lock_by_job_id(
            conn=self.conn,
            table_exists=self._table_exists,
            coerce_int=_coerce_int,
            job_id=job_id,
            release_reason=release_reason,
            status=status,
            worker_instance_id=worker_instance_id,
        )

    def release_scheduled_job_execution_lock_by_id(
        self,
        *,
        lock_id: str,
        release_reason: str = "",
        status: str = "released",
    ):
        return scheduled_job_lock_store.release_scheduled_job_execution_lock_by_id(
            conn=self.conn,
            table_exists=self._table_exists,
            coerce_int=_coerce_int,
            lock_id=lock_id,
            release_reason=release_reason,
            status=status,
        )

    def list_active_scheduled_job_execution_locks(
        self,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
        scheduled_job_id: str | None = None,
        lock_key: str | None = None,
        job_id: str | None = None,
        limit: int = 100,
    ):
        return scheduled_job_lock_store.list_active_scheduled_job_execution_locks(
            conn=self.conn,
            table_exists=self._table_exists,
            coerce_int=_coerce_int,
            account_id=account_id,
            project_id=project_id,
            scheduled_job_id=scheduled_job_id,
            lock_key=lock_key,
            job_id=job_id,
            limit=limit,
        )

    def reconcile_scheduled_job_execution_locks(
        self,
        *,
        now=None,
        limit: int = 200,
    ):
        return scheduled_job_lock_store.reconcile_scheduled_job_execution_locks(
            conn=self.conn,
            table_exists=self._table_exists,
            coerce_int=_coerce_int,
            now=now,
            limit=limit,
        )
