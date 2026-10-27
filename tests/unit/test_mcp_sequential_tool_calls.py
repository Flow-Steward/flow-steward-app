"""Sequential MCP tool calls in one session must each get their own response.

The workflow E2E ledger recorded `MCP server returned no matching JSON-RPC
response` on a remote tool after earlier tools in the same session had succeeded,
and asked for a sequential multi-tool regression. Driving the shipped
`StreamableHttpMcpClient` against the lab's browser MCP server (24 tools,
protocol `2025-06-18`) did not reproduce it: eight sequential `tools/call`
requests, including ones the server answered with `isError`, all correlated. The
regression is therefore pinned here against a stubbed transport so it runs
without the lab, and the correlation rules the fixture depends on are asserted
explicitly:

* a response is matched by request id, one request at a time, in one session;
* the *first* object carrying our id wins, so a duplicate later in the stream
  cannot overwrite the real response;
* only an actual JSON-RPC response (`result` or `error`) counts as the match, so
  a notification that echoes our id cannot be mistaken for one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.management import mcp_connector_client as mcp
from core.application.management.errors import ServiceValidationError
from core.application.management.mcp_connector_http import _decode_streamable_http_response
from core.application.management.mcp_connector_protocol import (
    MCP_LEGACY_PREFERRED_VERSION,
    McpProtocolError,
)


def _sse(*objects: dict) -> str:
    return "".join(f"event: message\ndata: {json.dumps(obj)}\n\n" for obj in objects)


# ---------------------------------------------------------------------------
# Correlation rules
# ---------------------------------------------------------------------------


def test_response_is_matched_by_request_id():
    decoded = _decode_streamable_http_response(
        _sse({"jsonrpc": "2.0", "id": "req-2", "result": {"ok": True}}),
        "text/event-stream",
        expected_request_id="req-2",
    )
    assert decoded["result"] == {"ok": True}


def test_a_foreign_response_in_the_stream_is_ignored():
    decoded = _decode_streamable_http_response(
        _sse(
            {"jsonrpc": "2.0", "id": "req-1", "result": {"stale": True}},
            {"jsonrpc": "2.0", "id": "req-2", "result": {"ok": True}},
        ),
        "text/event-stream",
        expected_request_id="req-2",
    )
    assert decoded["result"] == {"ok": True}


def test_only_a_foreign_response_is_a_correlation_failure():
    _raises_input_65_1 = _sse({"jsonrpc": "2.0", "id": "req-1", "result": {"stale": True}})
    with pytest.raises(ServiceValidationError, match="no matching JSON-RPC response"):
        _decode_streamable_http_response(
            _raises_input_65_1, "text/event-stream", expected_request_id="req-2"
        )


def test_notifications_around_the_response_are_ignored():
    decoded = _decode_streamable_http_response(
        _sse(
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}},
            {"jsonrpc": "2.0", "id": "req-2", "result": {"ok": True}},
            {"jsonrpc": "2.0", "method": "notifications/message", "params": {}},
        ),
        "text/event-stream",
        expected_request_id="req-2",
    )
    assert decoded["result"] == {"ok": True}


def test_a_notification_echoing_our_id_is_not_taken_as_the_response():
    """Only `result`/`error` is a response; an echoed id on a notification is not."""
    decoded = _decode_streamable_http_response(
        _sse(
            {"jsonrpc": "2.0", "id": "req-2", "method": "notifications/progress", "params": {}},
            {"jsonrpc": "2.0", "id": "req-2", "result": {"ok": True}},
        ),
        "text/event-stream",
        expected_request_id="req-2",
    )
    assert decoded["result"] == {"ok": True}


def test_the_first_response_for_our_id_wins():
    """A duplicate later in the stream must not overwrite the real response."""
    decoded = _decode_streamable_http_response(
        _sse(
            {"jsonrpc": "2.0", "id": "req-2", "result": {"ordinal": "first"}},
            {"jsonrpc": "2.0", "id": "req-2", "result": {"ordinal": "second"}},
        ),
        "text/event-stream",
        expected_request_id="req-2",
    )
    assert decoded["result"] == {"ordinal": "first"}


def test_an_error_response_still_correlates():
    decoded = _decode_streamable_http_response(
        _sse({"jsonrpc": "2.0", "id": "req-2", "error": {"code": -32602, "message": "bad args"}}),
        "text/event-stream",
        expected_request_id="req-2",
    )
    assert decoded["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# A whole session: initialize, then several tool calls, then close
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """Stand-in for the Streamable HTTP transport, one response per request."""

    def __init__(self, tool_names: list[str]):
        self._tool_names = tool_names
        self.session_ids: list[str] = []
        self.methods: list[str] = []
        self.request_ids: list[object] = []
        self.deleted_sessions: list[str] = []

    def __call__(self, **kwargs):
        payload = kwargs["payload"]
        method = str(payload.get("method") or "")
        request_id = payload.get("id")
        self.methods.append(method)
        self.session_ids.append(str(kwargs.get("session_id") or ""))
        if request_id is not None:
            self.request_ids.append(request_id)

        if method == "server/discover":
            raise McpProtocolError("modern discovery unsupported", legacy_signal=True)
        if method == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": MCP_LEGACY_PREFERRED_VERSION,
                        "capabilities": {},
                        "serverInfo": {"name": "stub", "version": "1"},
                    },
                },
                {"mcp-session-id": "session-abc"},
                1,
            )
        if method == "notifications/initialized":
            return None, {}, 1
        if method == "tools/list":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {"name": name, "inputSchema": {"type": "object"}}
                            for name in self._tool_names
                        ]
                    },
                },
                {},
                1,
            )
        if method == "tools/call":
            tool = str((payload.get("params") or {}).get("name") or "")
            return (
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": f"ran {tool}"}]},
                },
                {},
                1,
            )
        raise AssertionError(f"unexpected method {method}")


def test_sequential_tool_calls_share_one_session_and_stay_correlated(monkeypatch):
    """navigate -> type -> type -> click -> snapshot in one session, each answered once."""
    tools = ["browser_navigate", "browser_type", "browser_click", "browser_snapshot"]
    transport = _RecordingTransport(tools)
    monkeypatch.setattr(mcp, "_json_rpc_post_with_metadata", transport)

    client = mcp.StreamableHttpMcpClient(
        url="https://mcp.example.test/mcp", headers={}, timeout_seconds=30
    )
    try:
        listed, _ms = client.request("tools/list", {})
        assert [row["name"] for row in listed["result"]["tools"]] == tools

        sequence = [
            ("browser_navigate", {"url": "https://example.test/login"}),
            ("browser_type", {"ref": "#user", "text": "operator"}),
            ("browser_type", {"ref": "#pass", "text": "*****"}),
            ("browser_click", {"ref": "#submit"}),
            ("browser_snapshot", {}),
        ]
        for tool, arguments in sequence:
            response, _ms = client.request(
                "tools/call", {"name": tool, "arguments": arguments}, name=tool
            )
            assert response["result"]["content"][0]["text"] == f"ran {tool}"
    finally:
        client.close()

    # One session, established once and reused by every later request.
    assert transport.methods[:3] == ["server/discover", "initialize", "notifications/initialized"]
    assert transport.methods.count("initialize") == 1
    post_init_sessions = transport.session_ids[3:]
    assert post_init_sessions
    assert set(post_init_sessions) == {"session-abc"}

    # Every correlated request got a distinct id.
    assert len(transport.request_ids) == len(set(transport.request_ids))


def test_client_is_closed_after_use_and_refuses_further_requests(monkeypatch):
    """Cleanup must leave the client unusable rather than half-open."""
    transport = _RecordingTransport(["browser_snapshot"])
    monkeypatch.setattr(mcp, "_json_rpc_post_with_metadata", transport)

    client = mcp.StreamableHttpMcpClient(
        url="https://mcp.example.test/mcp", headers={}, timeout_seconds=30
    )
    client.request("tools/list", {})
    client.close()
    client.close()  # idempotent

    with pytest.raises(McpProtocolError, match="closed"):
        client.request("tools/list", {})


@pytest.mark.parametrize(
    "raised",
    [
        RuntimeError("tool blew up"),
        TimeoutError("tool timed out"),
        KeyboardInterrupt("cancelled"),
    ],
)
def test_execution_scope_closes_its_clients_on_failure_timeout_and_cancellation(
    monkeypatch, raised
):
    """A failed, timed-out or cancelled attempt must not leak its MCP clients."""
    closed: list[str] = []

    class _Client:
        def __init__(self, label):
            self.label = label

        def close(self):
            closed.append(self.label)

    def _raises_operation_265():
        mcp.get_execution_mcp_client(
            account_id="account-1",
            job_id="job-1",
            connector_id="connector.example_browser",
            client_factory=lambda: _Client("browser"),
        )
        raise raised

    with pytest.raises(type(raised)), mcp.mcp_execution_scope("account-1", "job-1"):
        _raises_operation_265()

    assert closed == ["browser"]
