"""Application coordination for authorized public workflow event streams."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from core.application.jobs.public_workflow_event_stream import (
    PublicWorkflowStreamEvent,
    stream_public_workflow,
)
from core.application.jobs.public_workflow_stream_preflight import (
    PublicWorkflowStreamPreflight,
    PublicWorkflowStreamRepository,
    PublicWorkflowStreamTarget,
)
from core.application.management.scope import ScopeContext


class PublicWorkflowStreamService:
    """Keep repository reads behind one application-owned stream use case."""

    def __init__(
        self,
        repository: PublicWorkflowStreamRepository,
        *,
        reconcile_snapshot: Callable[[dict[str, Any]], Any],
        require_project_access_fn: Callable[[ScopeContext, dict[str, Any]], None],
        require_workflow_access_fn: Callable[[ScopeContext, str], None],
    ) -> None:
        self._repository = repository
        self._preflight = PublicWorkflowStreamPreflight(
            repository,
            reconcile_snapshot=reconcile_snapshot,
            require_project_access_fn=require_project_access_fn,
            require_workflow_access_fn=require_workflow_access_fn,
        )

    def prepare(
        self, *, scope: ScopeContext, job_id: str, cursor: str
    ) -> PublicWorkflowStreamTarget:
        return self._preflight.execute(scope=scope, job_id=job_id, cursor=cursor)

    def events(
        self,
        *,
        scope: ScopeContext,
        job_id: str,
        cursor: str,
        project_id: str,
        poll_interval_seconds: float,
        is_disconnected: Callable[[], Awaitable[bool]],
        logger: Any,
        sleep: Callable[[float], Awaitable[None]],
    ) -> AsyncIterator[PublicWorkflowStreamEvent]:
        return stream_public_workflow(
            self._repository,
            scope,
            job_id,
            cursor,
            project_id=project_id,
            poll_interval_seconds=poll_interval_seconds,
            is_disconnected=is_disconnected,
            logger=logger,
            sleep=sleep,
        )


__all__ = ["PublicWorkflowStreamService"]
