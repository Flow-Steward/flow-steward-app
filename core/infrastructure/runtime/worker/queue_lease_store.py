"""Project execution lease helpers for worker queues."""

from __future__ import annotations

import logging
import os
import socket
import uuid
from collections.abc import Collection
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger(__name__)

DEFAULT_LEASE_TTL_SECONDS = max(15, int(_runtime_env.get("FS_PROJECT_LEASE_TTL_SECONDS", "45")))


def normalize_entity_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("entity id is required")
    return normalized


def project_ids_from_project_rows(rows: Any) -> list[str]:
    project_ids: set[str] = set()
    if not isinstance(rows, list):
        return []

    for row in rows:
        raw_project_id = None
        if isinstance(row, dict):
            raw_project_id = row.get("id") or row.get("project_id")
        else:
            raw_project_id = getattr(row, "id", None) or getattr(row, "project_id", None)
        try:
            project_ids.add(normalize_entity_id(raw_project_id))
        except ValueError:
            continue
    return sorted(project_ids)


def lease_worker_id(worker_id: str | None = None) -> str:
    normalized = str(worker_id or "").strip()
    if normalized:
        return normalized
    return f"{socket.gethostname()}-{os.getpid()}"


class ProjectLeaseStore:
    def __init__(
        self,
        worker_id: str,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        terminal_statuses: Collection[str] = (),
    ) -> None:
        self.worker_id = str(worker_id or "").strip() or lease_worker_id("")
        self.lease_ttl_seconds = max(15, int(lease_ttl_seconds or DEFAULT_LEASE_TTL_SECONDS))
        self.terminal_statuses = frozenset(
            str(status or "").strip().lower() for status in terminal_statuses if str(status).strip()
        )
        self._logger = None

    def _get_logger(self):
        if self._logger is not None:
            return self._logger
        from core.infrastructure.config.runtime_config import CONFIG
        from core.infrastructure.db.platform_stores import PlatformWorkerStore

        dsn = (
            _runtime_env.get("FS_OBSERVABILITY_DSN")
            or _runtime_env.get("FS_DB_DSN")
            or CONFIG.get("fs_observability_dsn", "")
            or CONFIG.get("fs_db_dsn", "")
        )
        if not dsn:
            return None
        self._logger = PlatformWorkerStore(dsn)
        return self._logger

    def acquire(self, project_id: Any) -> dict | None:
        logger_obj = self._get_logger()
        if not logger_obj or not hasattr(logger_obj, "acquire_project_execution_lease"):
            return None
        return logger_obj.acquire_project_execution_lease(
            project_id=normalize_entity_id(project_id),
            worker_instance_id=self.worker_id,
            lease_token=uuid.uuid4().hex,
            ttl_seconds=self.lease_ttl_seconds,
            drain_status="acquired",
            active_job_id=None,
        )

    def project_exists(self, project_id: Any) -> bool:
        logger_obj = self._get_logger()
        if not logger_obj or not hasattr(logger_obj, "get_project"):
            return True
        try:
            return bool(logger_obj.get_project(normalize_entity_id(project_id)))
        except Exception:
            return True

    def list_project_ids(self) -> list[str]:
        logger_obj = self._get_logger()
        if not logger_obj:
            return []
        if hasattr(logger_obj, "get_projects"):
            try:
                return project_ids_from_project_rows(logger_obj.get_projects(include_trashed=False))
            except TypeError:
                return project_ids_from_project_rows(logger_obj.get_projects())
        if hasattr(logger_obj, "list_projects"):
            try:
                return project_ids_from_project_rows(
                    logger_obj.list_projects(limit=500, include_trashed=False)
                )
            except TypeError:
                return project_ids_from_project_rows(logger_obj.list_projects())
        return []

    def list_pending_project_ids(self) -> list[str]:
        logger_obj = self._get_logger()
        if not logger_obj:
            return []
        project_ids = self.list_project_ids()
        if not project_ids or not hasattr(logger_obj, "get_pending_jobs"):
            return project_ids

        pending_project_ids: list[str] = []
        for project_id in project_ids:
            try:
                if logger_obj.get_pending_jobs(project_id):
                    pending_project_ids.append(project_id)
            except Exception as exc:
                logger.debug("pending project discovery failed for project=%s: %s", project_id, exc)
                pending_project_ids.append(project_id)
        return pending_project_ids

    def heartbeat(
        self, lease: dict, *, drain_status: str = "draining", active_job_id: Any | None = None
    ) -> dict | None:
        logger_obj = self._get_logger()
        if not logger_obj or not hasattr(logger_obj, "heartbeat_project_execution_lease"):
            return None
        return logger_obj.heartbeat_project_execution_lease(
            project_id=normalize_entity_id(lease.get("project_id")),
            worker_instance_id=self.worker_id,
            lease_token=str(lease.get("lease_token") or ""),
            ttl_seconds=self.lease_ttl_seconds,
            drain_status=drain_status,
            active_job_id=active_job_id,
        )

    def active_job_is_terminal(self, job_id: Any) -> bool:
        """Return whether a leased job can no longer justify lease renewal."""
        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            return False
        logger_obj = self._get_logger()
        getter = getattr(logger_obj, "get_job", None) if logger_obj is not None else None
        if not callable(getter):
            return False
        try:
            row = getter(normalized_job_id)
        except Exception as exc:
            logger.debug("terminal job probe failed for job=%s: %s", normalized_job_id, exc)
            return False
        if not row:
            return True
        status = (
            str(row.get("status") if isinstance(row, dict) else getattr(row, "status", ""))
            .strip()
            .lower()
        )
        return status in self.terminal_statuses

    def release(self, lease: dict) -> None:
        logger_obj = self._get_logger()
        if not logger_obj or not hasattr(logger_obj, "release_project_execution_lease"):
            return
        try:
            logger_obj.release_project_execution_lease(
                project_id=normalize_entity_id(lease.get("project_id")),
                worker_instance_id=self.worker_id,
                lease_token=str(lease.get("lease_token") or ""),
            )
        except Exception as exc:
            logger.debug(
                "release_project_execution_lease failed for project=%s: %s",
                lease.get("project_id"),
                exc,
            )


__all__ = [
    "DEFAULT_LEASE_TTL_SECONDS",
    "ProjectLeaseStore",
    "lease_worker_id",
    "normalize_entity_id",
    "project_ids_from_project_rows",
]
