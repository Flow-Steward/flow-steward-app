"""
Global SSE event stream — realtime operational events with cursor/resume and scoping.

Endpoint: GET /api/events/stream?cursor=0&project_id=&event_type=

Source: ``platform_events`` table (append-only per-job semantic events). Account
scoping is enforced via a jobs→projects join; project membership restrictions
(``allowed_project_ids``) are applied for restricted principals (service accounts,
scoped PATs) even when no explicit ``project_id`` filter is given.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncGenerator
from datetime import datetime
from inspect import isawaitable
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from core.application.auth.principal import principal_has_required_scopes
from core.application.jobs.public_workflow_snapshot_query import reconcile_public_job_snapshot
from core.application.jobs.public_workflow_stream_preflight import (
    PublicWorkflowStreamCursorExpired,
)
from core.application.jobs.public_workflow_stream_service import PublicWorkflowStreamService
from core.application.jobs.workflow_run_models import WorkflowRunStateV1
from core.application.management import ServiceError
from core.application.management.guards import (
    require_event_access,
    require_project_access,
    require_workflow_access,
)
from core.application.management.scope import ScopeContext, scope_from_principal
from core.entrypoints.api import dependencies as deps
from core.infrastructure.config.runtime_config import CONFIG

_MESSAGE_ACCESS_DENIED = "Access denied"

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events-stream"])

_POLL_INTERVAL_SECONDS = 2.0
_MAX_CURSOR_LENGTH = 255


_MAX_JOB_STREAMS_PER_PRINCIPAL = int(CONFIG["public_sse_connections_per_principal"])
_MAX_JOB_STREAMS_PER_ACCOUNT = int(CONFIG["public_sse_connections_per_account"])


class _JobStreamConnectionGuard:
    """Bound concurrent job-scoped streams by principal and account.

    This object deliberately has no async lock: acquisition happens before the
    response is returned, and release happens from the async generator's
    ``finally`` block.  A short critical section under a process lock keeps
    concurrent ASGI tasks from oversubscribing either counter.
    """

    def __init__(self, *, principal_limit: int, account_limit: int) -> None:
        self.principal_limit = max(1, int(principal_limit))
        self.account_limit = max(1, int(account_limit))
        self._lock = threading.Lock()
        self._principal_counts: dict[str, int] = {}
        self._account_counts: dict[str, int] = {}

    def acquire(self, principal_key: str, account_key: str) -> bool:
        principal_key = str(principal_key or "anonymous")
        account_key = str(account_key or "default")
        with self._lock:
            if self._principal_counts.get(principal_key, 0) >= self.principal_limit:
                return False
            if self._account_counts.get(account_key, 0) >= self.account_limit:
                return False
            self._principal_counts[principal_key] = self._principal_counts.get(principal_key, 0) + 1
            self._account_counts[account_key] = self._account_counts.get(account_key, 0) + 1
            return True

    def release(self, principal_key: str, account_key: str) -> None:
        principal_key = str(principal_key or "anonymous")
        account_key = str(account_key or "default")
        with self._lock:
            principal_count = self._principal_counts.get(principal_key, 0) - 1
            account_count = self._account_counts.get(account_key, 0) - 1
            if principal_count > 0:
                self._principal_counts[principal_key] = principal_count
            else:
                self._principal_counts.pop(principal_key, None)
            if account_count > 0:
                self._account_counts[account_key] = account_count
            else:
                self._account_counts.pop(account_key, None)

    def counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """Return snapshots for diagnostics/tests without exposing mutable maps."""
        with self._lock:
            return dict(self._principal_counts), dict(self._account_counts)


_JOB_STREAM_CONNECTION_GUARD = _JobStreamConnectionGuard(
    principal_limit=_MAX_JOB_STREAMS_PER_PRINCIPAL,
    account_limit=_MAX_JOB_STREAMS_PER_ACCOUNT,
)


def _principal_identity(principal: dict | None, account_id: str) -> str:
    """Resolve an authenticated identity from server-issued principal fields."""
    if isinstance(principal, dict):
        for key in ("principal_id", "id", "user_id", "subject", "sub", "actor_id"):
            value = str(principal.get(key) or "").strip()
            if value:
                return value
    # A principal without an id still cannot create an unbounded number of
    # streams: fall back to its server-derived account scope.
    return f"account:{str(account_id or 'default').strip() or 'default'}"


def _invalid_cursor(value: str) -> bool:
    return len(value) > _MAX_CURSOR_LENGTH or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _serialize_event(row: dict) -> str:
    """Convert a DB row to a JSON-safe dict."""
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return json.dumps(out, default=str)


async def _event_generator(
    bl,
    scope: ScopeContext,
    cursor: str,
    project_id: str | None,
    event_type: str | None,
) -> AsyncGenerator[str, None]:
    """Async generator yielding SSE-formatted events."""
    current_cursor = str(cursor or "").strip()

    # Initial retry directive
    yield "retry: 5000\n\n"

    while True:
        try:
            rows = await run_in_threadpool(
                bl.list_events_since,
                current_cursor,
                scope.account_id,
                project_id=project_id,
                event_type=event_type,
                allowed_project_ids=list(scope.allowed_project_ids)
                if scope.allowed_project_ids
                else None,
                limit=50,
            )
        except Exception:
            rows = []

        emitted = False
        for raw_row in rows:
            if not isinstance(raw_row, dict):
                continue
            row = dict(raw_row)
            row.setdefault("account_id", scope.account_id)
            if scope.allowed_workflow_ids:
                workflow_id = str(row.get("workflow_id") or "").strip()
                if not workflow_id or workflow_id not in scope.allowed_workflow_ids:
                    continue
            try:
                require_event_access(scope, row)
            except Exception:
                continue
            event_id = str(row.get("id") or current_cursor)
            data = _serialize_event(row)
            yield f"id: {event_id}\ndata: {data}\n\n"
            current_cursor = event_id
            emitted = True

        if not emitted:
            # Keep-alive comment to prevent proxy/client timeout
            yield ": keepalive\n\n"

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def _is_external_principal(principal: dict | None) -> bool:
    if not isinstance(principal, dict):
        return False
    source = str(principal.get("auth_source") or "").strip().lower()
    kind = str(principal.get("principal_type") or principal.get("actor_type") or "").strip().lower()
    return source in {"machine", "pat", "token"} or kind == "service_account"


def _stream_error() -> str:
    return (
        "event: stream.error\ndata: "
        '{"schema_version": "1", "code": "stream_unavailable", '
        '"message": "Public job stream is temporarily unavailable", '
        '"retryable": true}\n\n'
    )


def _snapshot_json(snapshot: WorkflowRunStateV1 | object) -> str:
    if hasattr(snapshot, "model_dump"):
        return json.dumps(snapshot.model_dump(mode="json"), default=str)
    if hasattr(snapshot, "snapshot"):
        return _snapshot_json(snapshot.snapshot)
    return json.dumps(snapshot, default=str, separators=(",", ":"))


def _reconcile_public_job_snapshot(bl, job: dict) -> WorkflowRunStateV1:
    """Adapt route dependencies to the application snapshot query."""
    return reconcile_public_job_snapshot(
        bl,
        job,
        get_thread_state_snapshot=deps.get_thread_state_snapshot,
        get_committed_checkpoint_values=deps.get_committed_checkpoint_values,
        to_json_obj=deps.to_json_obj,
        resolve_artifact=deps.resolve_artifact,
    )


async def _job_event_generator(
    stream_service,
    scope: ScopeContext,
    job_id: str,
    cursor: str,
    *,
    project_id: str | None = None,
    on_close=None,
    request: Request | None = None,
) -> AsyncGenerator[str, None]:
    """Stream only the durable public WorkflowRunStateV1 projection for one job."""
    if not isinstance(stream_service, PublicWorkflowStreamService):
        repository = stream_service
        stream_service = PublicWorkflowStreamService(
            repository,
            reconcile_snapshot=lambda job: _reconcile_public_job_snapshot(repository, job),
            require_project_access_fn=require_project_access,
            require_workflow_access_fn=require_workflow_access,
        )
    try:

        async def _disconnected() -> bool:
            return await _request_is_disconnected(request)

        async for event in stream_service.events(
            scope=scope,
            job_id=job_id,
            cursor=cursor,
            project_id=project_id,
            poll_interval_seconds=_POLL_INTERVAL_SECONDS,
            is_disconnected=_disconnected,
            logger=logger,
            sleep=asyncio.sleep,
        ):
            if event.kind == "retry":
                yield "retry: 5000\n\n"
            elif event.kind == "keepalive":
                yield ": keepalive\n\n"
            elif event.kind == "state":
                yield (
                    f"event: job.state\nid: {event.marker_id}\n"
                    f"data: {_snapshot_json(event.snapshot)}\n\n"
                )
            elif event.kind == "error":
                yield _stream_error()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("job-scoped SSE initial read failed job_id=%s", job_id)
        yield _stream_error()
    finally:
        if on_close is not None:
            try:
                on_close()
            except Exception:
                logger.exception("job-scoped SSE connection cleanup failed job_id=%s", job_id)


async def _request_is_disconnected(request: Request | None) -> bool:
    """Best-effort disconnect probe; ASGI cancellation remains authoritative."""
    if request is None:
        return False
    checker = getattr(request, "is_disconnected", None)
    if not callable(checker):
        return False
    try:
        result = checker()
        if isawaitable(result):
            result = await result
        return result is True
    except Exception:
        # A disconnect probe must never turn a healthy stream into a public
        # ``stream.error``; cancellation will still run the finally cleanup.
        return False


@router.get("/api/events/stream", operation_id="streamJobEvents")
async def events_stream(
    request: Request,
    cursor: Annotated[str, Query(description="Resume from this opaque event ID")] = "",
    project_id: Annotated[str | None, Query(description="Filter by project")] = None,
    event_type: Annotated[str | None, Query(description="Filter by event type")] = None,
    job_id: Annotated[str | None, Query(description="Public job-scoped stream target")] = None,
):
    """Server-Sent Events stream of domain events with cursor/resume support.

    The ``cursor`` parameter is the last event ``id`` the client received.
    On reconnect the browser automatically sends ``Last-Event-ID`` which
    maps to the ``id:`` field in the SSE payload, but we also accept it as
    a query parameter for simpler clients.
    """
    # Honour Last-Event-ID header (browser auto-reconnect)
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        cursor = str(last_event_id)
    raw_cursor = str(cursor or "")
    if _invalid_cursor(raw_cursor):
        return JSONResponse({"error": "Invalid cursor", "code": "invalid_cursor"}, status_code=400)
    cursor = raw_cursor.strip()

    user = getattr(request.state, "auth_user", None)
    principal = user if isinstance(user, dict) else None
    if job_id is not None:
        job_id = str(job_id).strip()
        if not job_id:
            job_id = None
    if _is_external_principal(principal) and not job_id:
        return JSONResponse(
            {"error": "job_id is required", "code": "job_id_required"}, status_code=400
        )
    if job_id and (project_id is not None or event_type is not None):
        return JSONResponse(
            {
                "error": "project_id and event_type are not supported for job streams",
                "code": "unsupported_job_stream_filter",
            },
            status_code=400,
        )
    scope = scope_from_principal(user if isinstance(user, dict) else None, project_id=project_id)

    bl = deps.get_logger_only()

    if job_id:
        if _is_external_principal(principal) and not principal_has_required_scopes(
            principal, {"jobs.read"}
        ):
            return JSONResponse({"error": "jobs.read scope required"}, status_code=403)
        if bl is None:
            return JSONResponse({"error": "Database unavailable"}, status_code=503)
        principal_key = _principal_identity(principal, scope.account_id)
        account_key = str(scope.account_id or "default")
        if not _JOB_STREAM_CONNECTION_GUARD.acquire(principal_key, account_key):
            return JSONResponse(
                {
                    "error": "Too many job-scoped SSE connections",
                    "code": "stream_connection_limit",
                },
                status_code=429,
                headers={"Retry-After": "1"},
            )

        lease_released = False

        def _release_lease() -> None:
            nonlocal lease_released
            if lease_released:
                return
            lease_released = True
            _JOB_STREAM_CONNECTION_GUARD.release(principal_key, account_key)

        stream_service = PublicWorkflowStreamService(
            bl,
            reconcile_snapshot=lambda job: _reconcile_public_job_snapshot(bl, job),
            require_project_access_fn=require_project_access,
            require_workflow_access_fn=require_workflow_access,
        )
        try:
            target = await run_in_threadpool(
                stream_service.prepare,
                scope=scope,
                job_id=job_id,
                cursor=cursor,
            )
            resolved_project_id = target.project_id
        except asyncio.CancelledError:
            _release_lease()
            raise
        except PublicWorkflowStreamCursorExpired as exc:
            _release_lease()
            return JSONResponse(
                {"error": str(exc), "code": exc.code},
                status_code=410,
            )
        except ServiceError as exc:
            _release_lease()
            if exc.status_code in {401, 403}:
                payload = {"error": _MESSAGE_ACCESS_DENIED}
            else:
                payload = dict(exc.payload or {"error": str(exc)})
            return JSONResponse(payload, status_code=exc.status_code)
        except PermissionError:
            _release_lease()
            return JSONResponse({"error": _MESSAGE_ACCESS_DENIED}, status_code=403)
        except Exception as exc:
            from core.application.jobs.workflow_run_projection import (
                MultipleApprovalGatesUnsupported,
            )

            if isinstance(exc, MultipleApprovalGatesUnsupported):
                _release_lease()
                return JSONResponse(
                    {"error": str(exc), "code": exc.code},
                    status_code=409,
                )
            logger.exception("job-scoped SSE authorization failed job_id=%s", job_id)
            _release_lease()
            return JSONResponse(
                {"error": "Stream unavailable", "code": "stream_unavailable"}, status_code=503
            )

        try:
            generator = _job_event_generator(
                stream_service,
                scope,
                job_id,
                cursor,
                project_id=resolved_project_id,
                on_close=_release_lease,
                request=request,
            )
            return StreamingResponse(
                generator,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                background=BackgroundTask(_release_lease),
            )
        except Exception:
            _release_lease()
            raise

    # Fine-grained scoping: if a project filter is requested, verify the caller
    # owns that project and, for restricted principals, is a member of it.
    if project_id is not None and bl is not None:
        project = await run_in_threadpool(bl.get_project, project_id)
        if not project:
            return JSONResponse({"error": "Not found"}, status_code=404)
        try:
            require_project_access(scope, project)
        except Exception:
            return JSONResponse({"error": _MESSAGE_ACCESS_DENIED}, status_code=403)

    # For restricted principals (service accounts / scoped PATs with an explicit
    # allowed_project_ids set), pass that set to the DB query so they cannot see
    # events outside their membership even when no project_id filter is given.
    gen = _event_generator(bl, scope, cursor, project_id, event_type)

    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
