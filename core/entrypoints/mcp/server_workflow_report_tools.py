"""The workflow run report tool and its MCP Apps viewer (ADR 0059, task 3).

``workflow_run_report`` reads one job's report from the application API with
the caller's bearer, so job scope, workflow access and token revocation are the
API's own checks, repeated on every read while it waits. The application splits
the answer: the model-safe part (job state and the bounded report preview)
becomes the tool's content, and the full snapshot goes only into the result
``_meta`` under ``flowsteward.org/report``, which hosts give to the viewer and
keep from the model. A client that did not negotiate MCP Apps gets the
model-safe part alone.

The viewer is the single-file build of ``assets/frontend/report-viewer.html``.
The UI images ship it in ``assets/frontend/dist``; this server reads it once
from the application's public ``/static`` mount and serves it as the
``ui://`` resource the tool names.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from pydantic import Field

from core.entrypoints.mcp.server_api_auth import app_api_base_url
from core.entrypoints.mcp.server_api_client import MCP_TOOL_ROUTES, AppApiClient, AppApiError
from core.entrypoints.mcp.server_api_tool import api_tool_envelope, api_tool_error
from core.entrypoints.mcp.server_caller_identity import current_caller_bearer
from mcp.server.apps import APP_MIME_TYPE, Apps, client_supports_apps
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ResourceError
from mcp.server.mcpserver.resources import FunctionResource
from mcp.types import CallToolResult, TextContent

TOOL = "workflow_run_report"
REPORT_VIEWER_URI = "ui://flow-steward/report-viewer/v1"
#: The ``_meta`` key of the snapshot; the viewer bridge reads the same key.
REPORT_META_KEY = "flowsteward.org/report"
#: Where the UI build puts the viewer, under the app's public ``/static`` mount.
VIEWER_STATIC_PATH = "/static/frontend/dist/report-viewer/report-viewer.html"
MAX_WAIT_SECONDS = 30
_POLL_SECONDS = 1.0
_STATE_PENDING = "pending"

_viewer_html: str | None = None
_viewer_lock = asyncio.Lock()


async def report_viewer_html() -> str:
    """The viewer document, read once per process from the application."""
    global _viewer_html
    async with _viewer_lock:
        if _viewer_html is None:
            try:
                async with httpx.AsyncClient(
                    base_url=app_api_base_url(), timeout=httpx.Timeout(30.0)
                ) as client:
                    response = await client.get(VIEWER_STATIC_PATH)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise ResourceError("The report viewer is not available.") from exc
            text = response.text
            if 'id="fs-third-party-notices"' not in text:
                raise ResourceError("The report viewer build is incomplete.")
            _viewer_html = text
    return _viewer_html


report_apps = Apps()
report_apps.add_resource(
    FunctionResource(
        uri=REPORT_VIEWER_URI,
        name="flow-steward-report-viewer",
        title="Flow Steward report",
        description="Read-only viewer of the report a workflow run returned.",
        mime_type=APP_MIME_TYPE,
        meta={"ui": {"prefersBorder": True}},
        fn=report_viewer_html,
    )
)


#: Timing seams, replaced by deterministic fakes in tests.
_monotonic = time.monotonic
_sleep = asyncio.sleep
_wait_for = asyncio.wait_for


async def _read_once(
    client: AppApiClient, path: str, bearer: str, correlation_id: str
) -> dict[str, Any]:
    try:
        payload = await client.request_async(
            "GET", path, bearer=bearer, correlation_id=correlation_id
        )
    except AppApiError as exc:
        raise api_tool_error(TOOL, exc) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), dict):
        raise api_tool_error(TOOL, AppApiError(502, {"error": "Unexpected report answer"}))
    return payload


async def _read_report(job_id: str, wait_seconds: int, correlation_id: str) -> dict[str, Any]:
    """Read once, then re-read a pending report until ``wait_seconds`` is spent.

    The first read is always made and bounded only by the client's own timeout.
    Every later read starts only while budget remains and is cut off when the
    budget ends; the caller then gets the last pending answer.
    """
    path = MCP_TOOL_ROUTES[TOOL].path.format(job_id=quote(job_id, safe=""))
    bearer = current_caller_bearer()
    client = AppApiClient()
    deadline = _monotonic() + wait_seconds
    payload = await _read_once(client, path, bearer, correlation_id)
    while payload["model"].get("state") == _STATE_PENDING:
        remaining = deadline - _monotonic()
        if remaining <= 0:
            break
        await _sleep(min(_POLL_SECONDS, remaining))
        remaining = deadline - _monotonic()
        if remaining <= 0:
            break
        try:
            payload = await _wait_for(
                _read_once(client, path, bearer, correlation_id), timeout=remaining
            )
        except TimeoutError:
            break
    return payload


@report_apps.tool(
    resource_uri=REPORT_VIEWER_URI,
    name=TOOL,
    title="Workflow run report",
    description=(
        "Show the report one workflow run returned (the Interface's report output), "
        "after workflow_trigger gave its job_id. You receive the report's title, summary "
        "text and counts; the user sees the full report in the viewer. wait_seconds "
        f"(0-{MAX_WAIT_SECONDS}) waits for a running job; state 'pending' means call "
        "again. Not operational analytics (see report_generate)."
    ),
)
async def workflow_run_report(
    ctx: Context,
    job_id: str,
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS)] = 0,
) -> CallToolResult:
    """Return the model-safe report part, and the snapshot for the viewer only."""
    correlation_id = uuid4().hex
    payload = await _read_report(str(job_id).strip(), wait_seconds, correlation_id)
    envelope = api_tool_envelope(TOOL, payload["model"], correlation_id)
    snapshot = payload.get("snapshot")
    meta = (
        {REPORT_META_KEY: snapshot}
        if isinstance(snapshot, dict) and client_supports_apps(ctx)
        else None
    )
    text = json.dumps(envelope, ensure_ascii=False, sort_keys=True, default=str)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=envelope,
        _meta=meta,
    )


__all__ = [
    "MAX_WAIT_SECONDS",
    "REPORT_META_KEY",
    "REPORT_VIEWER_URI",
    "VIEWER_STATIC_PATH",
    "report_apps",
    "report_viewer_html",
    "workflow_run_report",
]
