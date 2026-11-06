"""The ``workflow_run_report`` MCP tool and its viewer resource (ADR 0059 task 3).

The application's model-safe part is the tool's content; the snapshot travels
only in ``_meta`` and only to a client that negotiated MCP Apps. Every read
while waiting carries the caller's bearer, so the API's own checks apply.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
import pytest
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from core.application.jobs.workflow_run_models import PublicWorkflowResultV1
from core.application.jobs.workflow_run_report import workflow_run_report
from core.application.management.query_models import JobStatusView
from core.entrypoints.mcp import server_api_tool, server_workflow_report_tools
from core.entrypoints.mcp.server_api_client import MCP_TOOL_ROUTES, AppApiError
from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES

SNAPSHOT = {"kind": "fs.report", "title": "Weekly", "datasets": [{"id": "d", "rows": [["ROW"]]}]}


def _answer(state: str, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "model": {
            "job": {"id": "job-1", "status": "completed", "outcome": "completed", "terminal": True},
            "state": state,
            "report_output": "weekly" if snapshot else None,
            "report": {"title": "Weekly"} if snapshot else None,
            "text": "Weekly",
        },
        "snapshot": snapshot,
    }


class _Client:
    responses: ClassVar[list[object]] = []
    requests: ClassVar[list[dict[str, Any]]] = []

    async def request_async(self, method: str, path: str, **kwargs: Any) -> object:
        self.requests.append({"method": method, "path": path, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _ctx(*, apps: bool) -> SimpleNamespace:
    extensions = {EXTENSION_ID: {"mimeTypes": [APP_MIME_TYPE]}} if apps else None
    capabilities = SimpleNamespace(extensions=extensions)
    return SimpleNamespace(session=SimpleNamespace(client_capabilities=capabilities))


@pytest.fixture(autouse=True)
def _api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_Client, "responses", [])
    monkeypatch.setattr(_Client, "requests", [])
    monkeypatch.setattr(server_workflow_report_tools, "AppApiClient", _Client)
    monkeypatch.setattr(server_workflow_report_tools, "current_caller_bearer", lambda: "caller")
    monkeypatch.setattr(server_workflow_report_tools, "_POLL_SECONDS", 0.01)


def _call(*, apps: bool, wait_seconds: int = 0, job_id: str = "job-1") -> Any:
    return asyncio.run(
        server_workflow_report_tools.workflow_run_report(
            _ctx(apps=apps), job_id=job_id, wait_seconds=wait_seconds
        )
    )


def test_an_apps_client_gets_the_snapshot_only_in_meta() -> None:
    _Client.responses.append(_answer("ready", SNAPSHOT))
    result = _call(apps=True)

    assert result.meta == {server_workflow_report_tools.REPORT_META_KEY: SNAPSHOT}
    assert result.structured_content["ok"] is True
    assert result.structured_content["data"]["state"] == "ready"
    assert "ROW" not in json.dumps(result.structured_content)
    assert "ROW" not in result.content[0].text
    assert json.loads(result.content[0].text) == result.structured_content
    request = _Client.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/api/jobs/job-1/workflow-report"
    assert request["bearer"] == "caller"


def test_a_client_without_apps_gets_the_model_safe_part_alone() -> None:
    _Client.responses.append(_answer("ready", SNAPSHOT))
    result = _call(apps=False)

    assert result.meta is None
    assert result.structured_content["data"]["report"] == {"title": "Weekly"}


def test_it_waits_for_a_running_job_with_a_fresh_authorized_read_each_time() -> None:
    _Client.responses.extend([_answer("pending"), _answer("pending"), _answer("ready", SNAPSHOT)])
    result = _call(apps=True, wait_seconds=5)

    assert result.structured_content["data"]["state"] == "ready"
    assert [request["bearer"] for request in _Client.requests] == ["caller"] * 3


def test_without_wait_a_running_job_is_reported_pending_after_one_read() -> None:
    _Client.responses.append(_answer("pending"))
    result = _call(apps=True)

    assert result.structured_content["data"]["state"] == "pending"
    assert result.meta is None
    assert len(_Client.requests) == 1


def test_a_refusal_while_waiting_ends_the_call_with_the_shared_error_envelope() -> None:
    _Client.responses.extend([_answer("pending"), AppApiError(401, {"error": "invalid_token"})])
    with pytest.raises(ToolError) as raised:
        _call(apps=True, wait_seconds=5)

    envelope = json.loads(str(raised.value))
    assert envelope["ok"] is False
    assert envelope["tool"] == "workflow_run_report"
    assert envelope["error"]["status_code"] == 401


class _Clock:
    """Virtual time for the wait loop: sleeps and slow reads advance it instantly."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.read_budgets: list[float] = []
        self.slow_read_seconds: float | None = None
        monkeypatch.setattr(server_workflow_report_tools, "_POLL_SECONDS", 1.0)
        monkeypatch.setattr(server_workflow_report_tools, "_monotonic", lambda: self.now)
        monkeypatch.setattr(server_workflow_report_tools, "_sleep", self._sleep)
        monkeypatch.setattr(server_workflow_report_tools, "_wait_for", self._wait_for)

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    async def _wait_for(self, read: Any, timeout: float) -> Any:
        self.read_budgets.append(timeout)
        if self.slow_read_seconds is not None and self.slow_read_seconds > timeout:
            read.close()
            self.now += timeout
            raise TimeoutError
        return await read


def test_a_zero_wait_reads_once_without_a_budget_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(monkeypatch)
    _Client.responses.append(_answer("pending"))
    result = _call(apps=True, wait_seconds=0)

    assert result.structured_content["data"]["state"] == "pending"
    assert len(_Client.requests) == 1
    assert clock.sleeps == []
    assert clock.read_budgets == []


def test_no_read_starts_after_the_last_sleep_spends_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(monkeypatch)
    _Client.responses.extend([_answer("pending")] * 3)
    result = _call(apps=True, wait_seconds=2)

    assert result.structured_content["data"]["state"] == "pending"
    # Read, sleep 1 s, read with 1 s left, sleep the last second, stop.
    assert len(_Client.requests) == 2
    assert clock.sleeps == [1.0, 1.0]
    assert clock.read_budgets == [1.0]
    assert clock.now == 1002.0


def test_the_last_sleep_is_cut_to_the_budget_left_by_a_slow_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(monkeypatch)

    class _SlowRead(_Client):
        async def request_async(self, method: str, path: str, **kwargs: Any) -> object:
            clock.now += 1.5
            return await super().request_async(method, path, **kwargs)

    monkeypatch.setattr(server_workflow_report_tools, "AppApiClient", _SlowRead)
    _Client.responses.extend([_answer("pending")] * 3)
    _call(apps=True, wait_seconds=2)

    # The first read took 1.5 s, so 0.5 s is left: one short sleep and no read after it.
    assert clock.sleeps == [0.5]
    assert len(_Client.requests) == 1
    assert clock.now == 1002.0


def test_a_slow_read_is_cut_off_at_the_deadline_with_the_last_pending_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(monkeypatch)
    clock.slow_read_seconds = 30.0
    _Client.responses.extend([_answer("pending"), _answer("ready", SNAPSHOT)])
    result = _call(apps=True, wait_seconds=3)

    assert result.structured_content["data"]["state"] == "pending"
    assert result.meta is None
    assert clock.sleeps == [1.0]
    assert clock.read_budgets == [2.0]
    assert clock.now == 1003.0


def test_a_ready_answer_within_the_budget_ends_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(monkeypatch)
    _Client.responses.extend([_answer("pending"), _answer("ready", SNAPSHOT)])
    result = _call(apps=True, wait_seconds=30)

    assert result.structured_content["data"]["state"] == "ready"
    assert result.meta == {server_workflow_report_tools.REPORT_META_KEY: SNAPSHOT}
    assert clock.sleeps == [1.0]
    assert clock.read_budgets == [29.0]


CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "assets"
    / "frontend"
    / "src"
    / "report-viewer"
    / "__fixtures__"
    / "workflow-run-report-results.json"
)
SALES_SNAPSHOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "workflow_reports"
    / "sales_report_snapshot.json"
)


def _status(status: str, *, report: bool) -> JobStatusView:
    snapshot = json.loads(SALES_SNAPSHOT.read_text("utf-8"))
    result = (
        PublicWorkflowResultV1(
            outputs={"weekly": snapshot, "note": "done"}, artifacts={}, report_output="weekly"
        )
        if report
        else None
    )
    return JobStatusView(id="job-1", status=status, task_summary="Weekly", workflow_result=result)


@pytest.mark.parametrize(
    ("case", "status", "report"),
    [("ready", "completed", True), ("pending", "running", False), ("no_report", "failed", False)],
)
def test_the_viewer_contract_fixture_is_what_the_tool_sends(
    monkeypatch: pytest.MonkeyPatch, case: str, status: str, report: bool
) -> None:
    """The viewer's tests read this fixture; it must be the tool's actual wire result."""
    monkeypatch.setattr(
        server_workflow_report_tools, "uuid4", lambda: SimpleNamespace(hex="contract")
    )
    _Client.responses.append(workflow_run_report(_status(status, report=report)))
    result = _call(apps=True)
    wire = {
        "content": [
            item.model_dump(by_alias=True, mode="json", exclude_none=True)
            for item in result.content
        ],
        "structuredContent": result.structured_content,
    }
    if result.meta is not None:
        wire["_meta"] = result.meta

    assert wire == json.loads(CONTRACT.read_text("utf-8"))[case]
    assert "EMEA" not in json.dumps(wire["structuredContent"])
    assert "EMEA" not in wire["content"][0]["text"]


def test_the_job_id_cannot_leave_the_job_route() -> None:
    _Client.responses.append(_answer("pending"))
    _call(apps=False, job_id="../projects/x")

    assert _Client.requests[0]["path"] == "/api/jobs/..%2Fprojects%2Fx/workflow-report"


def test_the_tool_is_bound_to_the_viewer_resource_and_listed_with_a_jobs_read_scope() -> None:
    apps = server_workflow_report_tools.report_apps
    (tool,) = apps.tools()
    assert tool.meta == {"ui": {"resourceUri": server_workflow_report_tools.REPORT_VIEWER_URI}}
    (resource,) = apps.resources()
    assert resource.resource.uri == server_workflow_report_tools.REPORT_VIEWER_URI
    assert resource.resource.mime_type == APP_MIME_TYPE
    assert MCP_TOOL_ROUTES["workflow_run_report"].path == "/api/jobs/{job_id}/workflow-report"
    assert MCP_TOOL_SCOPES["workflow_run_report"] == MCP_TOOL_SCOPES["job_result"]


def test_the_generic_trigger_names_no_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FS_MCP_AUTH_DISABLED", "1")
    from core.entrypoints.mcp.server import mcp

    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert "ui" not in (tools["workflow_trigger"].meta or {})
    assert tools["workflow_run_report"].meta["ui"]["resourceUri"].startswith("ui://")


def test_the_job_result_tool_reads_the_model_view() -> None:
    _method, _path, params, _body, _headers = server_api_tool._delegation_parts(
        "job_result",
        route_method="GET",
        route_path="/api/jobs/job-1/result",
        arguments={"include_messages_limit": 100, "include_logs_limit": 50},
        project_id="",
    )
    assert params == {"include_messages_limit": 100, "include_logs_limit": 50, "view": "model"}


class _Viewer:
    def __init__(self, handler: Any) -> None:
        self.transport = httpx.MockTransport(handler)

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, **kwargs)


def _viewer(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[str]:
    paths: list[str] = []

    def recorded(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return handler(request)

    viewer = _Viewer(recorded)
    monkeypatch.setattr(server_workflow_report_tools, "_viewer_html", None)
    monkeypatch.setattr(server_workflow_report_tools.httpx, "AsyncClient", viewer.client)
    return paths


def test_the_viewer_is_read_once_from_the_app_static_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    html = '<!doctype html><script type="text/plain" id="fs-third-party-notices"></script>'
    paths = _viewer(monkeypatch, lambda _request: httpx.Response(200, text=html))

    async def read_twice() -> tuple[str, str]:
        return (
            await server_workflow_report_tools.report_viewer_html(),
            await server_workflow_report_tools.report_viewer_html(),
        )

    assert asyncio.run(read_twice()) == (html, html)
    assert paths == [server_workflow_report_tools.VIEWER_STATIC_PATH]


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404, text="missing"), httpx.Response(200, text="<!doctype html>")],
    ids=["missing", "incomplete"],
)
def test_a_missing_or_incomplete_viewer_is_a_resource_error(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    _viewer(monkeypatch, lambda _request: response)
    with pytest.raises(ResourceError):
        asyncio.run(server_workflow_report_tools.report_viewer_html())
    assert server_workflow_report_tools._viewer_html is None
