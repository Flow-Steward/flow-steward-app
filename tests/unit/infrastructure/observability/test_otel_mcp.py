from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace

import pytest

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("mcp")

from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

from core.infrastructure.observability import otel_instruments, otel_mcp
from tests.unit.test_workflow_telemetry import _Telemetry
from tests.unit.test_workflow_telemetry import telemetry as telemetry

_TRACE = "1" * 32
_SPAN = "2" * 16
_CARRIER = {"traceparent": f"00-{_TRACE}-{_SPAN}-01"}


def _ctx(method="tools/call", params=None, meta=None, request_id=7):
    return SimpleNamespace(
        method=method,
        params={"name": "list_projects", "arguments": {"token": "s3cret"}}
        if params is None
        else params,
        meta=meta,
        request_id=request_id,
    )


def _run(ctx, call_next):
    """Run the middleware in one task and report the context it leaves behind."""

    async def task():
        before = trace.get_current_span().get_span_context()
        try:
            return await otel_mcp.McpTraceMiddleware()(ctx, call_next)
        finally:
            # The same task continues after the middleware: nothing may leak.
            assert trace.get_current_span().get_span_context() == before

    return asyncio.run(task())


def test_the_pinned_sdk_ships_its_middleware_and_fs_mcp_replaces_it() -> None:
    from mcp.server import MCPServer

    server = MCPServer(name="probe", middleware=[object()])
    # Guards the provisional middleware API of the pinned SDK version.
    assert any(type(item).__name__ == "OpenTelemetryMiddleware" for item in server.middleware)

    otel_mcp.install_mcp_tracing(server)
    otel_mcp.install_mcp_tracing(server)

    names = [type(item).__name__ for item in server.middleware]
    assert names[0] == "McpTraceMiddleware"
    assert names.count("McpTraceMiddleware") == 1
    assert "OpenTelemetryMiddleware" not in names


def test_a_request_span_continues_the_client_context_and_is_current(
    telemetry: _Telemetry,
) -> None:
    seen = {}

    async def call_next(ctx):
        seen["current"] = trace.get_current_span().get_span_context()
        return {"content": []}

    _run(_ctx(meta=dict(_CARRIER)), call_next)

    span = telemetry.span("tools/call list_projects")
    assert span.kind == SpanKind.SERVER
    assert format(span.context.trace_id, "032x") == _TRACE
    assert format(span.parent.span_id, "016x") == _SPAN
    assert seen["current"].span_id == span.context.span_id
    assert dict(span.attributes) == {
        "mcp.method.name": "tools/call",
        "gen_ai.tool.name": "list_projects",
    }
    assert "s3cret" not in repr(span.to_json())


def test_errors_keep_a_bounded_type_and_no_payload(telemetry: _Telemetry) -> None:
    class FakeMcpError(Exception):
        def __init__(self) -> None:
            super().__init__("secret message")
            self.error = SimpleNamespace(code=-32601, message="secret message")

    async def raises(ctx):
        raise FakeMcpError()

    async def crashes(ctx):
        raise RuntimeError("secret detail")

    async def tool_error(ctx):
        return {"isError": True, "content": [{"type": "text", "text": "secret output"}]}

    with pytest.raises(FakeMcpError):
        _run(_ctx(method="tools/list", params={}), raises)
    with pytest.raises(RuntimeError):
        _run(_ctx(method="tools/list", params={}), crashes)
    _run(_ctx(), tool_error)

    spans = telemetry.finished()
    assert [s.attributes.get("error.type") for s in spans] == ["-32601", "_OTHER", "tool_error"]
    for span in spans:
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description is None
        assert span.events == ()
        assert "secret" not in repr(span.to_json())


def test_cancellation_restores_the_context(telemetry: _Telemetry) -> None:
    async def cancelled(ctx):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run(_ctx(), cancelled)

    assert telemetry.span("tools/call list_projects").attributes["error.type"] == "cancelled"


def test_notifications_and_unsafe_names_stay_bounded(telemetry: _Telemetry) -> None:
    async def ok(ctx):
        return None

    _run(_ctx(method="notifications/initialized", request_id=None), ok)
    assert telemetry.finished() == []

    _run(_ctx(method="Tools/Call; DROP", params={"name": "../../etc"}), ok)
    (span,) = telemetry.finished()
    assert span.name == "other"
    assert dict(span.attributes) == {"mcp.method.name": "other"}


def test_sdk_client_spans_export_only_the_allowed_shape(telemetry: _Telemetry) -> None:
    tracer = telemetry.tracer_provider.get_tracer("mcp-python-sdk")
    with (
        pytest.raises(RuntimeError),
        tracer.start_as_current_span(
            "MCP send tools/call list_projects",
            kind=SpanKind.CLIENT,
            attributes={
                "mcp.method.name": "tools/call",
                "jsonrpc.request.id": "3",
                "mcp.session.id": "secret-session",
            },
        ),
    ):
        raise RuntimeError("secret detail")
    with tracer.start_as_current_span("tools/call x", kind=SpanKind.SERVER):
        pass

    (client,) = telemetry.finished()
    assert client.kind == SpanKind.CLIENT
    assert client.name == "MCP send tools/call list_projects"
    assert dict(client.attributes) == {
        "mcp.method.name": "tools/call",
        "jsonrpc.request.id": "3",
        "error.type": "_OTHER",
    }
    assert client.events == ()
    assert client.status.description is None


def test_the_sync_runner_keeps_the_caller_context_in_its_thread() -> None:
    from core.application.orchestration.tools import mcp_tools_client

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="")

    async def read_marker():
        return marker.get()

    async def caller():
        marker.set("caller")
        return mcp_tools_client._run_mcp_coro_sync(read_marker())

    assert asyncio.run(caller()) == "caller"


def test_an_external_connector_call_runs_outside_the_trace(
    telemetry: _Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.application.management import mcp_connector_client

    client = mcp_connector_client.StreamableHttpMcpClient.__new__(
        mcp_connector_client.StreamableHttpMcpClient
    )
    seen = {}

    def fake_request(method, params, *, name="", generated_transport_headers=None):
        seen["inside"] = trace.get_current_span().get_span_context().is_valid
        return {"result": {}}, 3

    monkeypatch.setattr(client, "_request", fake_request, raising=False)
    tracer = telemetry.tracer_provider.get_tracer(otel_instruments.FLOW_STEWARD_SCOPE)
    with tracer.start_as_current_span("POST /api/integrations/mcp/test") as outer:
        client.request("tools/call", {"name": "x"})

    assert seen["inside"] is False
    span = telemetry.span("mcp tools/call")
    assert span.kind == SpanKind.CLIENT
    assert span.parent.span_id == outer.get_span_context().span_id
    assert dict(span.attributes) == {"mcp.method.name": "tools/call"}


def test_external_modern_meta_carries_no_trace_context(telemetry: _Telemetry) -> None:
    from core.application.management.mcp_connector_client import _modern_meta

    tracer = telemetry.tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("request"):
        meta = _modern_meta({"name": "x", "_meta": {"progressToken": 1}})["_meta"]

    assert "traceparent" not in meta
    assert "tracestate" not in meta
    assert meta["progressToken"] == 1
