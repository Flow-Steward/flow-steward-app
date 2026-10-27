"""Integration coverage for remote extension runtime execution paths."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

pytestmark = [pytest.mark.integration]

SCOPE_FIELDS = ["group_id", "for_each_id", "step_id", "step_execution_id"]


class _RuntimeHandler(BaseHTTPRequestHandler):
    routes: ClassVar[dict[tuple[str, str], tuple[int, dict]]] = {}
    requests: ClassVar[list[dict]] = []

    def _handle(self) -> None:
        path = str(self.path or "").split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        payload: dict | None = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
                payload = parsed if isinstance(parsed, dict) else {"raw": parsed}
            except Exception:
                payload = {"raw": raw.decode("utf-8", errors="replace")}

        self.__class__.requests.append(
            {"method": self.command, "path": path, "payload": payload or {}}
        )
        from core.application.extensions.host_contract import envelope_contract_error

        # A runtime refuses an envelope of a foreign host contract before it
        # executes anything.
        contract_error = envelope_contract_error(payload) if self.command == "POST" else ""
        if contract_error:
            status, response = (
                400,
                {"ok": False, "error": {"code": "capability_mismatch", "message": contract_error}},
            )
        else:
            status, response = self.__class__.routes.get(
                (self.command, path),
                (404, {"ok": False, "error": {"message": "not found"}}),
            )
        body = json.dumps(response or {}).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format, *args):
        _ = (format, args)


@pytest.fixture
def remote_runtime_server(monkeypatch):
    # The stub runtime binds to loopback; opt into private/loopback remote URLs
    # so the SSRF preflight allows dialing 127.0.0.1 in this integration test.
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    _RuntimeHandler.routes = {
        ("POST", "/handshake"): (
            200,
            {
                "ok": True,
                "result": {
                    "status": "ok",
                    "contract_version": "extension_host_v2",
                    "execution_scope_fields": SCOPE_FIELDS,
                },
            },
        ),
        ("GET", "/health"): (200, {"ok": True, "result": {"status": "healthy"}}),
        ("POST", "/queries/list_accounts"): (
            200,
            {"ok": True, "result": {"items": [{"id": "acc_1"}], "next_cursor": "", "total": 1}},
        ),
        ("POST", "/actions/sync_account"): (200, {"ok": True, "result": {"status": "done"}}),
        ("POST", "/events"): (200, {"ok": True, "result": {"accepted": True}}),
    }
    _RuntimeHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RuntimeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    http_base_url = f"http://127.0.0.1:{server.server_port}"
    base_url = f"https://runtime.test:{server.server_port}"
    from tests._remote_https_test_transport import install_loopback_https_runtime_transport

    install_loopback_https_runtime_transport(
        monkeypatch,
        https_base_url=base_url,
        http_base_url=http_base_url,
    )
    try:
        yield {"base_url": base_url, "handler": _RuntimeHandler}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _remote_extension(base_url: str, **overrides) -> dict:
    payload = {
        "extension_key": "remote.demo",
        "runtime": {"contract_version": "extension_host_v2"},
        "runtime_mode": "remote_https",
        "remote_base_url": base_url,
        "runtime_status": "compatible",
        "handshake_status": "ok",
        "health_status": "healthy",
        "query_definitions": [{"query_id": "list_accounts", "handler": {"mode": "extension"}}],
        "action_definitions": [{"action_id": "sync_account", "handler": {"mode": "extension"}}],
        "supported_events": ["external.comment.received"],
    }
    payload.update(overrides)
    return payload


def test_remote_runtime_executes_query_action_and_event(remote_runtime_server):
    from core.infrastructure.extension_runtime.extension_runtime import (
        emit_extension_event,
        run_extension_action,
        run_extension_query,
    )

    extension = _remote_extension(remote_runtime_server["base_url"])

    query = run_extension_query(
        extension,
        query_id="list_accounts",
        context={"project_id": 7},
        params={"limit": 10},
        runtime_context={
            "job_id": 42,
            "group_id": "per_item",
            "for_each_id": None,
            "step_id": "fetch",
            "step_execution_id": "11111111-1111-4111-8111-111111111111",
        },
    )
    assert query["status"] == "succeeded"
    assert query.get("mode") == "query"
    assert query.get("handler_mode") == "remote_https"
    assert (query.get("response") or {}).get("ok") is True

    action = run_extension_action(
        extension,
        action_id="sync_account",
        input_payload={"account_id": "acc_1"},
        context={"project_id": 7},
        runtime_context={
            "job_id": 42,
            "group_id": None,
            "for_each_id": "each_account",
            "step_id": "sync",
            "step_execution_id": "22222222-2222-4222-8222-222222222222",
        },
    )
    assert action["status"] == "succeeded"
    assert action.get("mode") == "action"
    assert action.get("handler_mode") == "remote_https"

    emitted = emit_extension_event(
        extension,
        event_type="external.comment.received",
        payload={"message_id": "m_1"},
        runtime_context={"project_id": 7},
    )
    assert emitted["status"] == "accepted"
    assert emitted["handler_mode"] == "remote_https"
    assert (emitted.get("remote_event") or {}).get("status") == "succeeded"

    requests = remote_runtime_server["handler"].requests
    paths = [item["path"] for item in requests]
    assert "/queries/list_accounts" in paths
    assert "/actions/sync_account" in paths
    assert "/events" in paths
    by_path = {item["path"]: item["payload"] for item in requests}
    for path in ("/queries/list_accounts", "/actions/sync_account", "/events"):
        assert by_path[path]["contract_version"] == "extension_host_v2"
        assert "phase_id" not in by_path[path]["scope"]
    assert {key: by_path["/queries/list_accounts"]["scope"][key] for key in SCOPE_FIELDS} == {
        "group_id": "per_item",
        "for_each_id": None,
        "step_id": "fetch",
        "step_execution_id": "11111111-1111-4111-8111-111111111111",
    }
    assert by_path["/actions/sync_account"]["scope"]["group_id"] is None
    assert by_path["/actions/sync_account"]["scope"]["for_each_id"] == "each_account"
    assert by_path["/actions/sync_account"]["scope"]["step_id"] == "sync"
    # A workflow-level event carries all four execution fields as null.
    assert {key: by_path["/events"]["scope"][key] for key in SCOPE_FIELDS} == dict.fromkeys(
        SCOPE_FIELDS
    )


def test_remote_handshake_negotiates_the_host_contract(remote_runtime_server):
    from core.infrastructure.db.remote_extension_runtime import remote_handshake

    invocation = remote_handshake(_remote_extension(remote_runtime_server["base_url"]))

    assert invocation["status"] == "succeeded"
    assert invocation["contract_compatible"] is True
    request = remote_runtime_server["handler"].requests[-1]
    assert request["path"] == "/handshake"
    assert request["payload"]["contract_version"] == "extension_host_v2"


@pytest.mark.parametrize(
    "result",
    [{"status": "ok"}, {"status": "ok", "contract_version": "extension_host_v1"}],
    ids=["missing-confirmation", "foreign-contract"],
)
def test_remote_handshake_without_v2_confirmation_is_incompatible(remote_runtime_server, result):
    from core.infrastructure.db.remote_extension_runtime import remote_handshake

    remote_runtime_server["handler"].routes[("POST", "/handshake")] = (
        200,
        {"ok": True, "result": result},
    )
    invocation = remote_handshake(_remote_extension(remote_runtime_server["base_url"]))

    assert invocation["status"] == "failed"
    assert invocation["detail"] == "capability_mismatch"
    assert invocation["contract_compatible"] is False
    paths = [item["path"] for item in remote_runtime_server["handler"].requests]
    # One negotiation, no retry with another token.
    assert paths == ["/handshake"]


def test_unconfirmed_runtime_receives_no_operation(remote_runtime_server):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_query

    remote_runtime_server["handler"].routes[("POST", "/handshake")] = (
        200,
        {"ok": True, "result": {"status": "ok"}},
    )
    extension = _remote_extension(remote_runtime_server["base_url"], handshake_status="unknown")

    query = run_extension_query(extension, query_id="list_accounts", params={"limit": 1})

    assert query["status"] == "failed"
    assert query["detail"] == "capability_mismatch"
    paths = [item["path"] for item in remote_runtime_server["handler"].requests]
    assert paths == ["/handshake"]


def test_confirmed_runtime_is_negotiated_before_the_first_operation(remote_runtime_server):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_query

    extension = _remote_extension(remote_runtime_server["base_url"], handshake_status="unknown")

    query = run_extension_query(extension, query_id="list_accounts", params={"limit": 1})

    assert query["status"] == "succeeded"
    paths = [item["path"] for item in remote_runtime_server["handler"].requests]
    assert paths == ["/handshake", "/queries/list_accounts"]


def test_release_declaring_another_contract_is_never_called(remote_runtime_server):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_query

    extension = _remote_extension(
        remote_runtime_server["base_url"],
        runtime={"contract_version": "extension_host_v1"},
    )

    query = run_extension_query(extension, query_id="list_accounts", params={"limit": 1})

    assert query["status"] == "failed"
    assert remote_runtime_server["handler"].requests == []


def test_runtime_rejects_an_envelope_of_a_foreign_contract(remote_runtime_server):
    from core.infrastructure.db import remote_extension_runtime

    client = remote_extension_runtime._build_remote_client(
        _remote_extension(remote_runtime_server["base_url"])
    )
    response = client.invoke_query(
        "list_accounts",
        {"contract_version": "extension_host_v1", "kind": "query", "scope": {}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "capability_mismatch"


def test_remote_runtime_health_failure_does_not_fallback_to_local(remote_runtime_server):
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_health

    remote_runtime_server["handler"].routes[("GET", "/health")] = (
        500,
        {"ok": False, "error": {"message": "health check failed"}},
    )
    extension = _remote_extension(remote_runtime_server["base_url"])
    health = run_extension_health(extension)

    assert health["status"] == "failed"
    assert str(health.get("detail") or "").strip()
    assert health.get("command") == []


def test_remote_handshake_failure_returns_failed_invocation(remote_runtime_server):
    from core.infrastructure.db.remote_extension_runtime import remote_handshake

    remote_runtime_server["handler"].routes[("POST", "/handshake")] = (
        500,
        {"ok": False, "error": {"message": "handshake failed"}},
    )
    extension = _remote_extension(remote_runtime_server["base_url"])
    invocation = remote_handshake(extension, payload={"ping": True})

    assert invocation["status"] == "failed"
    assert invocation["mode"] == "handshake"
    assert invocation["operation_id"] == "handshake"


def test_remote_runtime_preserves_safe_extension_error_code(remote_runtime_server):
    from core.infrastructure.db.remote_extension_runtime import remote_handshake

    remote_runtime_server["handler"].routes[("POST", "/handshake")] = (
        502,
        {"ok": False, "errors": [{"code": "rate_limited", "message": "upstream limited"}]},
    )
    invocation = remote_handshake(_remote_extension(remote_runtime_server["base_url"]))

    assert invocation["status"] == "failed"
    assert invocation["detail"] == "rate_limited"


def test_remote_runtime_replaces_untrusted_error_text_with_safe_category(
    remote_runtime_server,
):
    from core.infrastructure.db.remote_extension_runtime import remote_handshake

    remote_runtime_server["handler"].routes[("POST", "/handshake")] = (
        502,
        {
            "ok": False,
            "error": {
                "code": "dummy-secret-value",
                "message": "dummy-secret-value",
                "detail": "dummy-secret-value",
            },
            "detail": "dummy-secret-value",
        },
    )

    invocation = remote_handshake(_remote_extension(remote_runtime_server["base_url"]))

    assert invocation["status"] == "failed"
    assert invocation["detail"] == "upstream_error"
    assert "dummy-secret-value" not in json.dumps(invocation)


def test_incompatible_remote_install_is_blocked_without_network_call(remote_runtime_server):
    from core.infrastructure.extension_runtime.extension_runtime import (
        emit_extension_event,
        run_extension_action,
        run_extension_query,
    )

    extension = _remote_extension(
        remote_runtime_server["base_url"],
        runtime_status="incompatible",
    )

    query = run_extension_query(extension, query_id="list_accounts", params={"limit": 1})
    action = run_extension_action(extension, action_id="sync_account", input_payload={"x": 1})
    event = emit_extension_event(
        extension,
        event_type="external.comment.received",
        payload={"x": 1},
        runtime_context={"project_id": 1},
    )

    assert query["status"] == "failed"
    assert query["detail"] == "model_unavailable"
    assert action["status"] == "failed"
    assert action["detail"] == "model_unavailable"
    assert event["status"] == "failed"
    assert event["detail"] == "model_unavailable"

    paths = [item["path"] for item in remote_runtime_server["handler"].requests]
    assert "/queries/list_accounts" not in paths
    assert "/actions/sync_account" not in paths
    assert "/events" not in paths


def test_extension_chat_model_tool_round_trip_and_strict_structured_result(
    remote_runtime_server, monkeypatch
):
    """Exercise the adapter over the remote runtime boundary, not a local fallback."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from core.application.orchestration.extension_chat_model import ExtensionChatModel

    remote_runtime_server["handler"].routes.update(
        {
            (
                "POST",
                "/actions/llm.chat",
            ): (
                200,
                {
                    "ok": True,
                    "result": {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_probe",
                                    "name": "flow_steward_capability_probe",
                                    "arguments": {"key": "probe"},
                                }
                            ],
                            "continuation": {"opaque": "continue-1"},
                        },
                        "finish_reason": "tool_calls",
                    },
                },
            ),
            (
                "POST",
                "/actions/llm.chat_structured",
            ): (
                200,
                {
                    "ok": True,
                    "result": {"structured_output": {"status": "ok", "tool_value": "probe-ok"}},
                },
            ),
        }
    )
    adapter = ExtensionChatModel(
        account_id="acc-1",
        provider_key="llmext_test",
        model_id="verified-model",
        chat_action_id="llm.chat",
        structured_chat_action_id="llm.chat_structured",
    )
    monkeypatch.setattr(
        adapter, "_runtime_extension", lambda: _remote_extension(remote_runtime_server["base_url"])
    )
    first = adapter.bind_tools(
        [
            {
                "name": "flow_steward_capability_probe",
                "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}},
            }
        ]
    ).invoke([HumanMessage(content="call probe")])
    assert first.tool_calls[0]["id"] == "call_probe"
    assert first.response_metadata["continuation"] == {"opaque": "continue-1"}

    schema = {
        "type": "object",
        "properties": {"status": {"const": "ok"}, "tool_value": {"const": "probe-ok"}},
        "required": ["status", "tool_value"],
        "additionalProperties": False,
    }
    parsed = adapter.with_structured_output(schema).invoke(
        [
            HumanMessage(content="call probe"),
            AIMessage(
                content="", tool_calls=first.tool_calls, response_metadata=first.response_metadata
            ),
            ToolMessage(content='{"value":"probe-ok"}', tool_call_id="call_probe"),
        ]
    )
    assert parsed == {"status": "ok", "tool_value": "probe-ok"}

    calls = remote_runtime_server["handler"].requests
    assert calls[-2]["payload"]["input"]["tools"][0]["function"]["name"] == (
        "flow_steward_capability_probe"
    )
    final_messages = calls[-1]["payload"]["input"]["messages"]
    assert final_messages[-1]["tool_call_id"] == "call_probe"
    assert final_messages[-2]["continuation"] == {"opaque": "continue-1"}
