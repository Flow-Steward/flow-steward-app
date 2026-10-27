"""Job WebSocket stream endpoint extracted from serve.py."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.application.jobs.public_approval_serializer import serialize_approval_history_entry
from core.application.management.errors import ServiceForbiddenError
from core.application.management.guards import require_project_access
from core.application.management.scope import scope_from_principal

_MESSAGE_ACCESS_DENIED = "Access denied"

_SEND_AFTER_CLOSE_FRAGMENT = 'Cannot call "send" once a close message has been sent.'

# How often an open stream checks again that its session is live and still has
# access to the job's project. Logout, revocation, expiry, a disabled user or a
# removed membership end the stream within this time.
REVALIDATE_SECONDS = 10.0


def _project_access_denied(bl: Any, principal: Any, job: dict[str, Any]) -> bool:
    """Whether ``principal`` may not read ``job``'s project."""
    if not isinstance(principal, dict):
        return False
    project_id = str(job.get("project_id") or "").strip()
    if not project_id:
        return True
    project = bl.get_project(project_id) if hasattr(bl, "get_project") else None
    if not project:
        return True
    try:
        require_project_access(scope_from_principal(principal, project_id=project_id), project)
    except ServiceForbiddenError:
        return True
    return False


async def _send_frame(websocket: WebSocket, payload: dict[str, Any]) -> bool:
    """Send one frame unless the client has already closed the socket."""
    try:
        await websocket.send_json(payload)
        return True
    except WebSocketDisconnect:
        return False
    except RuntimeError as exc:
        if _SEND_AFTER_CLOSE_FRAGMENT in str(exc):
            return False
        raise


def _status_frame_data(job: dict[str, Any], status: str, ws_terminal: set[str]) -> dict[str, Any]:
    status_data: dict[str, Any] = {"status": status}
    if status in ws_terminal and job.get("error"):
        status_data["error"] = job["error"]
    return status_data


def create_jobs_websocket_router(
    *,
    ws_authenticate: Callable[..., Any],
    get_logger_only: Callable[[], Any],
    serialize_log_row: Callable[[dict, Any], dict],
    serialize_event_row: Callable[[dict], dict],
    serialize_agent_message_row: Callable[[dict], dict],
    ws_terminal: set[str],
    ws_get_approval_payload: Callable[[dict], dict | None],
    approval_fallback_payload: Callable[[dict], dict],
    build_runtime_payload: Callable[[dict, Any], dict],
    logger: Any,
) -> APIRouter:
    router = APIRouter(tags=["ws-jobs"])

    @router.websocket("/ws/jobs/{job_id}")
    async def job_websocket(websocket: WebSocket, job_id: str):
        """WebSocket endpoint for live job updates."""
        try:
            principal = await ws_authenticate(websocket)
        except PermissionError as exc:
            await websocket.close(code=4401, reason=str(exc))
            return

        bl = get_logger_only()
        if not bl:
            await websocket.close(code=1011, reason="Database not configured")
            return
        job = bl.get_job(job_id)
        if not job:
            await websocket.close(code=4404, reason="Job not found")
            return
        if _project_access_denied(bl, principal, job):
            await websocket.close(code=4403, reason=_MESSAGE_ACCESS_DENIED)
            return

        await websocket.accept()
        last_authorized = time.monotonic()

        last_log_id = ""
        last_event_id = ""
        last_message_id = ""
        last_status = None
        last_cost_key = None
        last_hist_len = -1
        last_runtime_key = None
        last_approval_key = None
        first_poll = True

        try:
            while True:
                bl = get_logger_only()
                if not bl:
                    break

                job = bl.get_job(job_id)
                if not job:
                    break
                if time.monotonic() - last_authorized >= REVALIDATE_SECONDS:
                    # The session and the membership are checked again, not
                    # remembered from the moment the stream opened.
                    try:
                        principal = await ws_authenticate(websocket)
                    except PermissionError as exc:
                        await websocket.close(code=4401, reason=str(exc))
                        return
                    if _project_access_denied(bl, principal, job):
                        await websocket.close(code=4403, reason=_MESSAGE_ACCESS_DENIED)
                        return
                    last_authorized = time.monotonic()

                status = str(job.get("status") or "unknown")
                if first_poll and status in ws_terminal:
                    status_data = _status_frame_data(job, status, ws_terminal)
                    last_status = status
                    if not await _send_frame(websocket, {"type": "status", "data": status_data}):
                        return
                    await _send_frame(websocket, {"type": "done", "data": {"status": status}})
                    break

                new_logs = bl.get_logs(job_id, since_id=last_log_id)
                for row in new_logs:
                    row_data = serialize_log_row(dict(row), job_id)
                    if not await _send_frame(websocket, {"type": "log", "data": row_data}):
                        return
                    last_log_id = str(row_data.get("id") or last_log_id)

                new_events = bl.get_events(job_id, since_id=last_event_id)
                for ev in new_events:
                    ev_data = serialize_event_row(dict(ev))
                    if not await _send_frame(websocket, {"type": "event", "data": ev_data}):
                        return
                    last_event_id = str(ev_data.get("id") or last_event_id)

                new_messages = bl.get_agent_messages(job_id, since_id=last_message_id)
                for msg in new_messages:
                    msg_data = serialize_agent_message_row(dict(msg))
                    if not await _send_frame(websocket, {"type": "message", "data": msg_data}):
                        return
                    last_message_id = str(msg_data.get("id") or last_message_id)

                if status != last_status:
                    last_status = status
                    status_data = _status_frame_data(job, status, ws_terminal)
                    if not await _send_frame(websocket, {"type": "status", "data": status_data}):
                        return

                if status == "awaiting_approval":
                    payload = ws_get_approval_payload(job) or approval_fallback_payload(job)
                    approval_key = json.dumps(payload, sort_keys=True, default=str)
                    if approval_key != last_approval_key:
                        last_approval_key = approval_key
                        if not await _send_frame(websocket, {"type": "approval", "data": payload}):
                            return
                else:
                    last_approval_key = None

                try:
                    runtime_payload = build_runtime_payload(job, bl)
                    runtime_key = json.dumps(runtime_payload, sort_keys=True, default=str)
                    if runtime_key != last_runtime_key:
                        last_runtime_key = runtime_key
                        if not await _send_frame(
                            websocket, {"type": "runtime", "data": runtime_payload}
                        ):
                            return
                except Exception:
                    pass

                job_meta = job.get("meta") or {}
                if isinstance(job_meta, str):
                    job_meta = json.loads(job_meta)
                hist = [
                    serialize_approval_history_entry(entry)
                    for entry in job_meta.get("approvals") or []
                    if isinstance(entry, Mapping)
                ]
                if len(hist) != last_hist_len:
                    last_hist_len = len(hist)
                    if not await _send_frame(websocket, {"type": "approval_history", "data": hist}):
                        return

                try:
                    cost = bl.get_job_cost_summary(job_id)
                    cost_key = (cost.get("tokens_total"), cost.get("cost_usd"))
                    if cost_key != last_cost_key and cost.get("tokens_total"):
                        last_cost_key = cost_key
                        if not await _send_frame(websocket, {"type": "cost", "data": cost}):
                            return
                except Exception:
                    pass

                if status in ws_terminal:
                    await _send_frame(websocket, {"type": "done", "data": {"status": status}})
                    break

                first_poll = False
                await asyncio.sleep(2)

        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning("job_websocket job_id=%s: %s", job_id, e)
            with suppress(Exception):
                await websocket.close()

    return router
