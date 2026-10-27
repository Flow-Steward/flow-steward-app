"""Authorization and durable-state preflight for public job event streams."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from core.application.jobs.workflow_identity import resolve_job_workflow_id
from core.application.management.errors import ServiceNotFoundError
from core.application.management.guards import require_project_access, require_workflow_access
from core.application.management.scope import ScopeContext


@dataclass(frozen=True)
class PublicWorkflowStreamTarget:
    job: dict[str, Any]
    project_id: str


class PublicWorkflowStreamCursorExpired(RuntimeError):
    """The supplied durable stream marker is no longer available."""

    code = "cursor_expired"


class PublicWorkflowStreamRepository(Protocol):
    def get_job(self, job_id: str) -> Any: ...

    def get_project(self, project_id: str) -> Any: ...

    def get_public_state_marker(
        self,
        marker_id: str,
        job_id: str,
        account_id: str,
        *,
        project_id: str,
    ) -> Any: ...


class PublicWorkflowStreamPreflight:
    """Resolve and authorize a job stream before its response is opened."""

    def __init__(
        self,
        repository: PublicWorkflowStreamRepository,
        *,
        reconcile_snapshot: Callable[[dict[str, Any]], Any],
        require_project_access_fn: Callable[
            [ScopeContext, dict[str, Any]], None
        ] = require_project_access,
        require_workflow_access_fn: Callable[[ScopeContext, str], None] = require_workflow_access,
    ) -> None:
        self._repository = repository
        self._reconcile_snapshot = reconcile_snapshot
        self._require_project_access = require_project_access_fn
        self._require_workflow_access = require_workflow_access_fn

    def execute(
        self,
        *,
        scope: ScopeContext,
        job_id: str,
        cursor: str,
    ) -> PublicWorkflowStreamTarget:
        job = self._repository.get_job(job_id)
        if not isinstance(job, dict):
            raise ServiceNotFoundError("Not found")
        project_id = str(job.get("project_id") or "").strip()
        project = self._repository.get_project(project_id) if project_id else None
        if not isinstance(project, dict):
            raise ServiceNotFoundError("Not found")

        self._require_project_access(scope, project)
        self._require_workflow_access(
            scope,
            resolve_job_workflow_id(job),
        )
        self._reconcile_snapshot(job)

        if cursor:
            marker = self._repository.get_public_state_marker(
                cursor,
                job_id,
                scope.account_id,
                project_id=project_id,
            )
            if not marker:
                raise PublicWorkflowStreamCursorExpired("Cursor expired or invalid")
        return PublicWorkflowStreamTarget(job=dict(job), project_id=project_id)


__all__ = [
    "PublicWorkflowStreamCursorExpired",
    "PublicWorkflowStreamPreflight",
    "PublicWorkflowStreamRepository",
    "PublicWorkflowStreamTarget",
]
