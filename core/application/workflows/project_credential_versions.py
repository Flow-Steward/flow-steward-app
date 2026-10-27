"""Coordinate established project credential versions on the caller's transaction."""

from __future__ import annotations

from typing import Any

from psycopg.errors import LockNotAvailable

from core.application.management.errors import ServiceConflictError


class WorkflowCredentialVersionConflict(ServiceConflictError):
    """The complete owning mutation must unwind before transport maps this conflict."""

    def __init__(self) -> None:
        super().__init__(
            "Workflow credential changed concurrently; retry the operation",
            payload={"code": "workflow_credential_version_conflict"},
        )


def guard_project_credential_versions(cursor: Any, *, project_id: str, secret_key: str) -> None:
    """Lock every retained version, then refresh once without retrying a failure.

    Retained rows serialize supported established-key writers. Generated public
    initial identities are the creation boundary; absence supplies no lock.
    """
    for _ in range(2):
        try:
            cursor.execute(
                "SELECT id FROM project_secrets WHERE project_id = %s AND secret_key = %s "
                "ORDER BY id FOR UPDATE NOWAIT",
                (project_id, secret_key),
            )
            cursor.fetchall()
        except LockNotAvailable as exc:
            raise WorkflowCredentialVersionConflict() from exc
