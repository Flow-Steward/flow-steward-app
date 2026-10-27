"""Application orchestration for a durable, job-scoped public event stream."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PublicWorkflowStreamEvent:
    kind: str
    marker_id: str = ""
    snapshot: Any = None


def read_public_snapshot_marker(
    bl: Any,
    scope: Any,
    job_id: str,
    *,
    project_id: str | None,
    attempts: int = 3,
) -> tuple[Any, Any, dict[str, Any] | None]:
    """Read an exact snapshot/marker pair without crossing revisions."""
    last_pair = None
    for _ in range(max(1, attempts)):
        revision = bl.get_public_progress_snapshot(job_id)
        if revision is None:
            raise LookupError("public progress snapshot is unavailable")
        snapshot = getattr(revision, "snapshot", revision)
        if snapshot is None:
            raise LookupError("public progress snapshot is unavailable")
        raw_marker_id = getattr(revision, "marker_id", "")
        snapshot_marker_id = (
            str(raw_marker_id).strip() if isinstance(raw_marker_id, (str, int)) else ""
        )
        if snapshot_marker_id:
            marker = bl.get_public_state_marker(
                snapshot_marker_id,
                job_id,
                scope.account_id,
                project_id=project_id,
            )
            marker_id = str((marker or {}).get("id") or "").strip()
            if marker_id != snapshot_marker_id:
                last_pair = (revision, snapshot, marker)
                continue
            latest = bl.get_latest_public_state_marker(
                job_id,
                scope.account_id,
                project_id=project_id,
            )
            if str((latest or {}).get("id") or "").strip() != snapshot_marker_id:
                last_pair = (revision, snapshot, marker)
                continue
        else:
            marker = bl.get_latest_public_state_marker(
                job_id,
                scope.account_id,
                project_id=project_id,
            )
        snapshot_revision = getattr(revision, "revision", None)
        marker_detail = (marker or {}).get("detail") if isinstance(marker, dict) else None
        if isinstance(marker_detail, str):
            try:
                marker_detail = json.loads(marker_detail)
            except (TypeError, ValueError):
                marker_detail = None
        marker_revision = marker_detail.get("revision") if isinstance(marker_detail, dict) else None
        if (
            isinstance(snapshot_revision, int)
            and isinstance(marker_revision, int)
            and snapshot_revision != marker_revision
        ):
            last_pair = (revision, snapshot, marker)
            continue
        return revision, snapshot, marker
    if last_pair is not None:
        raise RuntimeError("public projection changed while reading")
    raise RuntimeError("public projection unavailable")


async def stream_public_workflow(
    bl: Any,
    scope: Any,
    job_id: str,
    cursor: str,
    *,
    project_id: str | None,
    poll_interval_seconds: float,
    is_disconnected: Callable[[], Awaitable[bool]],
    logger: logging.Logger,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncGenerator[PublicWorkflowStreamEvent, None]:
    """Poll durable markers and emit transport-neutral stream events."""
    current_cursor = str(cursor or "").strip()
    yield PublicWorkflowStreamEvent("retry")
    if await is_disconnected():
        return
    try:
        _, snapshot, marker = await asyncio.to_thread(
            read_public_snapshot_marker,
            bl,
            scope,
            job_id,
            project_id=project_id,
        )
        marker_id = str((marker or {}).get("id") or "").strip()
        if not marker_id:
            raise RuntimeError("public snapshot has no durable marker")
        if not current_cursor or marker_id != current_cursor or bool(snapshot.terminal):
            yield PublicWorkflowStreamEvent("state", marker_id, snapshot)
            current_cursor = marker_id
            if bool(snapshot.terminal):
                return

        while True:
            if await is_disconnected():
                return
            await sleep(poll_interval_seconds)
            if await is_disconnected():
                return
            latest = await asyncio.to_thread(
                bl.get_latest_public_state_marker,
                job_id,
                scope.account_id,
                project_id=project_id,
            )
            latest_id = str((latest or {}).get("id") or "").strip()
            if not latest_id or latest_id == current_cursor:
                yield PublicWorkflowStreamEvent("keepalive")
                continue
            _, snapshot, marker = await asyncio.to_thread(
                read_public_snapshot_marker,
                bl,
                scope,
                job_id,
                project_id=project_id,
            )
            marker_id = str((marker or {}).get("id") or "").strip()
            if not marker_id or marker_id == current_cursor:
                yield PublicWorkflowStreamEvent("keepalive")
                continue
            yield PublicWorkflowStreamEvent("state", marker_id, snapshot)
            current_cursor = marker_id
            if bool(snapshot.terminal):
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("job-scoped SSE read failed job_id=%s", job_id)
        yield PublicWorkflowStreamEvent("error")


__all__ = [
    "PublicWorkflowStreamEvent",
    "read_public_snapshot_marker",
    "stream_public_workflow",
]
