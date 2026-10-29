"""Tests for MCP Connector model and services."""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
import urllib.error
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import copy_context
from copy import deepcopy
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth import (
    mcp_connector_oauth_provider,
    mcp_connector_oauth_repository,
    mcp_connector_oauth_rpc,
    mcp_connector_oauth_tokens,
)
from core.application.auth.oauth_client import OAuthConnectionError
from core.application.management import (
    mcp_connector_client,
    mcp_connector_common,
    mcp_connector_http,
    mcp_connector_observability,
    mcp_connector_protocol,
)
from core.application.management.command_models import (
    McpConnectorCreateInput,
    McpConnectorUpdateInput,
)
from core.application.management.command_services import ToolCommandService
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.mcp_connector import (
    McpConnector,
    McpConnectorAuthMode,
    McpConnectorStatus,
)
from core.application.management.mcp_connector_services import (
    McpConnectorCommandService,
    McpConnectorQueryService,
    McpConnectorStore,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.api.integrations_connectors import create_integrations_connectors_router
from tests.fixtures.mcp_streamable_http import legacy_mcp_server, modern_mcp_server

_LIVE_PROTOBUF_STRUCT_OUTPUT_SCHEMA = {
    "description": (
        "Represents a JSON object. An unordered key-value map, intending to perfectly "
        "capture the semantics of a JSON object. This enables parsing any arbitrary JSON "
        "payload as a message field in ProtoJSON format. This follows RFC 8259 guidelines "
        "for interoperable JSON: notably this type cannot represent large Int64 values or "
        "`NaN`/`Infinity` numbers, since the JSON format generally does not support those "
        "values in its number type. If you do not intend to parse arbitrary JSON into your "
        "message, a custom typed message should be preferred instead of using this type."
    ),
    "properties": {
        "fields": {
            "additionalProperties": {},
            "description": "Unordered map of dynamically typed values.",
            "type": "object",
        }
    },
    "type": "object",
}


def test_mcp_remote_body_reader_rejects_oversized_stream_without_echo() -> None:
    marker = b"must-not-echo-mcp-body"

    class _Stream:
        def __init__(self):
            self.read_sizes: list[int] = []

        def read(self, size: int) -> bytes:
            self.read_sizes.append(size)
            return (marker * size)[:size]

    stream = _Stream()
    with pytest.raises(ServiceValidationError) as captured:
        mcp_connector_http.read_bounded_http_body(
            stream,
            max_bytes=32,
            purpose="MCP server response",
        )

    assert stream.read_sizes == [33]
    assert "size limit" in str(captured.value)
    assert marker.decode() not in str(captured.value)


def test_discovered_tools_are_unavailable_when_current_grant_misses_requested_scope() -> None:
    from core.application.management import mcp_connector_oauth_contract

    connector = {
        "auth_mode": "oauth2",
        "oauth_metadata_jsonb": {
            "requested_scopes": ["gmail.readonly", "gmail.compose", "gmail.modify"],
            "token": {"granted_scopes": ["gmail.readonly", "gmail.compose"]},
        },
    }
    tools = [
        {
            "remote_tool_name": "label_message",
            "projected_tool_id": "gmail.label_message",
        }
    ]

    [tool] = mcp_connector_oauth_contract._annotate_oauth_scope_availability(connector, tools)

    assert tool["can_publish"] is False
    assert tool["availability_reason_code"] == "oauth_reauthorization_required"
    assert tool["missing_oauth_scopes"] == ["gmail.modify"]
    assert "Reconnect" in tool["availability_reason_text"]


_MCP_CONNECTOR_HTTP_FIELDS = {
    "connector_id",
    "project_id",
    "display_name",
    "description",
    "transport",
    "base_url",
    "auth_mode",
    "namespace_prefix",
    "allowlisted_tools",
    "discovered_tools_jsonb",
    "projected_tools",
    "status",
    "auth_status",
    "oauth_status",
    "last_synced_at",
    "last_sync_status",
    "last_sync_error",
    "enabled",
    "created_at",
    "updated_at",
    "has_client_id",
    "has_client_secret",
    "oauth_credential_revision",
    "oauth_authorization_parameters",
    "oauth_requested_scopes",
    "oauth_refresh_token_required",
    "catalog_item_id",
    "catalog_remote_id",
    "catalog_item_revision",
}


def _rpc_context_from_responses(
    responses: list[dict[str, Any]],
    *,
    requests: list[dict[str, Any]] | None = None,
    entries: list[dict[str, Any]] | None = None,
):
    remaining = iter(responses)

    @contextmanager
    def _context(
        _conn,
        *,
        connector_row,
        timeout_seconds,
        runtime_context=None,
    ):
        if entries is not None:
            entries.append(
                {
                    "connector_row": connector_row,
                    "timeout_seconds": timeout_seconds,
                    "runtime_context": runtime_context,
                }
            )

        def _post(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
            if requests is not None:
                requests.append(dict(payload))
            return next(remaining), 1

        yield _post

    return _context


@pytest.fixture(autouse=True)
def _stable_mcp_connector_dns(monkeypatch):
    monkeypatch.setattr(
        "core.application.management.mcp_connector_http._resolve_hostname_ips",
        lambda _hostname, *, port: {"93.184.216.34"},
    )
    monkeypatch.setattr(
        "core.application.extensions.url_safety._resolve_hostname_ips",
        lambda _hostname, *, port: {"93.184.216.34"},
    )


class _InMemoryMcpConnectorStore:
    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}

    def insert(self, row: dict[str, Any]) -> None:
        self.rows[row["connector_id"]] = dict(row)

    def get(
        self,
        connector_id: str,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        include_trashed: bool = False,
    ) -> dict[str, Any] | None:
        row = self.rows.get(connector_id)
        if row is None:
            return None
        if account_id and row.get("account_id") != account_id:
            return None
        if project_id and row.get("project_id") != project_id:
            return None
        if tenant_id and row.get("tenant_id") != tenant_id:
            return None
        if row.get("deleted_at") and not include_trashed:
            return None
        return dict(row)

    def list(
        self,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.rows.values()]
        if account_id:
            rows = [row for row in rows if row.get("account_id") == account_id]
        if project_id:
            rows = [row for row in rows if row.get("project_id") == project_id]
        if tenant_id:
            rows = [row for row in rows if row.get("tenant_id") == tenant_id]
        rows.sort(key=lambda row: str(row.get("created_at") or ""))
        return rows

    def update(
        self,
        connector_id: str,
        updates: dict[str, Any],
        *,
        account_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        row = self.rows.get(connector_id)
        if row is None:
            return
        if account_id and row.get("account_id") != account_id:
            return
        if project_id and row.get("project_id") != project_id:
            return
        if tenant_id and row.get("tenant_id") != tenant_id:
            return
        row.update(dict(updates))
        row["updated_at"] = datetime.now(UTC).isoformat()

    def delete(self, connector_id: str, *, account_id: str | None = None) -> None:
        row = self.rows.get(connector_id)
        if row is None:
            return
        if account_id and row.get("account_id") != account_id:
            return
        self.rows.pop(connector_id, None)

    def soft_delete(
        self,
        connector_id: str,
        *,
        account_id: str | None = None,
        deleted_by: str = "system",
        child_tool_ids: list[str] | None = None,
    ) -> None:
        row = self.rows.get(connector_id)
        if row is None or (account_id and row.get("account_id") != account_id):
            return
        row.update(
            {
                "enabled": False,
                "auth_status": "auth_required",
                "headers": {},
                "oauth_metadata_jsonb": {},
                "oauth_secret_refs_jsonb": {},
                "deleted_at": datetime.now(UTC).isoformat(),
                "deleted_by": deleted_by,
                "lifecycle_child_tool_ids_jsonb": sorted(child_tool_ids or []),
            }
        )


class _FakeHeaderSecretVault:
    """In-memory stand-in for the encrypted ``secret_refs`` header storage."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = {}
        self.generation = 0
        self.saves: list[dict[str, str]] = []
        if initial:
            self.save(None, connector_id="mcp_fs", headers=initial)

    def save(self, _conn, *, connector_id, headers, **_lineage) -> dict:
        self.generation += 1
        self.saves.append(dict(headers or {}))
        prefix = f"mcp_connector:{connector_id}:header:"
        self.values = {
            ref: value for ref, value in self.values.items() if not ref.startswith(prefix)
        }
        refs = {}
        for name, value in sorted((headers or {}).items()):
            ref = f"mcp_connector:{connector_id}:header:g{self.generation}:{name}"
            self.values[ref] = str(value)
            refs[name] = ref
        return {"refs": refs} if refs else {}

    def resolve(self, _conn, row) -> dict[str, str]:
        if isinstance(row.get("headers"), dict):
            return dict(row["headers"])
        refs = (row.get("header_secret_refs_jsonb") or {}).get("refs") or {}
        return {name: self.values[ref] for name, ref in refs.items()}

    def patch(self):
        stack = ExitStack()
        for target in (
            "core.application.management.mcp_connector_services.save_connector_header_secrets",
            "core.application.management.mcp_connector_services.resolve_connector_headers",
            "core.application.auth.mcp_connector_oauth_rpc.resolve_connector_headers",
        ):
            replacement = (
                self.save if target.endswith("save_connector_header_secrets") else self.resolve
            )
            stack.enter_context(patch(target, side_effect=replacement))
        return stack


class _FakeRegistryDbStore:
    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}
        self.workflows: list[dict[str, Any]] = []
        self.tool_agents: dict[str, list[str]] = {}

    def get_tool(self, tool_id: str) -> dict[str, Any] | None:
        row = self.rows.get(tool_id)
        return dict(row) if row and not row.get("deleted_at") else None

    def list_tools(
        self, enabled_only: bool = False, include_trashed: bool = False
    ) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.rows.values()]
        if not include_trashed:
            rows = [row for row in rows if not row.get("deleted_at")]
        if enabled_only:
            rows = [row for row in rows if row.get("enabled", True)]
        rows.sort(key=lambda row: str(row.get("tool_id") or ""))
        return rows

    def save_tool(self, data: dict[str, Any], author: str = "system") -> None:
        _ = author
        from core.application.registry.tool_manifest import ToolManifest

        manifest = ToolManifest.model_validate(data)
        row = manifest.to_registry_payload()
        row["tool_id"] = manifest.tool_id
        row["display_name"] = manifest.display_name
        row["enabled"] = manifest.enabled
        row["availability_status"] = data.get("availability_status", "available")
        row["availability_reason_code"] = data.get("availability_reason_code", "none")
        row["availability_reason_text"] = data.get("availability_reason_text", "")
        row["deleted_at"] = None
        row["deleted_by"] = None
        self.rows[manifest.tool_id] = row

    def delete_tool(self, tool_id: str, author: str = "system") -> None:
        row = self.rows.get(tool_id)
        if row is None or row.get("deleted_at"):
            raise KeyError(f"Tool '{tool_id}' not found")
        row["deleted_at"] = datetime.now(UTC).isoformat()
        row["deleted_by"] = author

    def get_agents_using_tool(self, tool_id: str) -> list[str]:
        return list(self.tool_agents.get(tool_id, []))

    def list_workflows(
        self, *, enabled_only: bool = False, include_definitions: bool = False
    ) -> list[dict[str, Any]]:
        _ = include_definitions
        rows = [dict(row) for row in self.workflows]
        if enabled_only:
            rows = [row for row in rows if row.get("enabled", True)]
        return rows

    def mark_tool_source_unavailable(
        self,
        tool_ids: list[str],
        *,
        reason_code: str,
        reason_text: str,
    ) -> int:
        # Mirrors the store method: availability lives outside the manifest
        # round-trip that save_tool performs, so it is written separately.
        touched = 0
        for tool_id in tool_ids:
            row = self.rows.get(tool_id)
            if row is None:
                continue
            row["availability_status"] = "unavailable"
            row["availability_reason_code"] = reason_code
            row["availability_reason_text"] = reason_text
            touched += 1
        return touched


class _FakeRegistryManager:
    def __init__(self):
        self.db_store = _FakeRegistryDbStore()
        self._manifests: dict[str, Any] = {}
        self.tools = SimpleNamespace(get_tool=self.get_tool)

    def get_tool(self, tool_id: str):
        return self._manifests.get(tool_id)

    def reload_all(self) -> None:
        from core.application.registry.tool_manifest import ToolManifest

        self._manifests = {
            tool_id: ToolManifest.from_db_row(row) for tool_id, row in self.db_store.rows.items()
        }


def _service_error_response(exc, *_args, **_kwargs) -> JSONResponse:
    raw_payload = getattr(exc, "payload", None)
    payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
    payload.setdefault("error", getattr(exc, "message", str(exc)))
    return JSONResponse(
        payload,
        status_code=int(getattr(exc, "status_code", 500) or 500),
    )


def _api_error_response(exc, *_args, **_kwargs) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=500)


def test_connector_view_coerces_uuid_account_id() -> None:
    account_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    bl = MagicMock()
    bl.create_mcp_connector.return_value = {
        "connector_id": "conn-1",
        "account_id": account_id,
        "project_id": "project-1",
        "tenant_id": "tenant-1",
        "display_name": "Demo",
        "description": "",
        "transport": "http",
        "base_url": "https://example.test/mcp",
        "auth_mode": "none",
        "namespace_prefix": "demo",
        "allowlisted_tools": [],
        "status": "active",
        "enabled": True,
        "created_at": "2026-05-05T00:00:00Z",
        "updated_at": "2026-05-05T00:00:00Z",
    }
    service = McpConnectorCommandService(get_logger=lambda: bl)

    payload = service.create_connector(
        McpConnectorCreateInput(
            display_name="Demo",
            transport="http",
            base_url="https://example.test/mcp",
            auth_mode="none",
            namespace_prefix="demo",
        ),
        scope=ScopeContext(
            account_id=str(account_id),
            project_id="project-1",
            tenant_id="tenant-1",
            actor_id="admin",
            principal_type="admin",
        ),
    ).dump()

    assert payload.get("account_id", "") != str(account_id)
    assert payload.get("tenant_id", "") != "tenant-1"
    assert payload["project_id"] == "project-1"


def test_mcp_http_error_body_is_not_exposed(monkeypatch) -> None:
    class _FakeOpener:
        def open(self, _request, timeout):
            _ = timeout
            raise urllib.error.HTTPError(
                "https://fs.example/mcp",
                500,
                "Server Error",
                hdrs=None,
                fp=io.BytesIO(b"reflected Authorization: Bearer leaked-token"),
            )

    monkeypatch.setattr(
        mcp_connector_http.urllib.request,
        "build_opener",
        lambda *args: _FakeOpener(),
    )

    with pytest.raises(ServiceUnavailableError) as exc_info:
        mcp_connector_client._json_rpc_post_with_latency(
            url="https://fs.example/mcp",
            headers={"Authorization": "Bearer submitted-token"},
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout_seconds=5.0,
        )

    message = str(exc_info.value)
    assert message == "MCP request failed with HTTP 500"
    assert "leaked-token" not in message
    assert "submitted-token" not in message


@pytest.mark.parametrize(
    ("connector_headers", "expected_user_agent"),
    [
        ({}, mcp_connector_http._DEFAULT_USER_AGENT),
        ({"uSeR-aGeNt": "connector-client/1.0"}, "connector-client/1.0"),
        ({"User-Agent": "   "}, mcp_connector_http._DEFAULT_USER_AGENT),
        (
            {"User-Agent": "", "user-agent": "connector-client/2.0"},
            "connector-client/2.0",
        ),
    ],
)
def test_mcp_http_post_sets_a_default_or_connector_user_agent(
    monkeypatch,
    connector_headers,
    expected_user_agent,
) -> None:
    captured_requests = []

    class _Response:
        def __init__(self, request_id: str) -> None:
            self.headers = {"Content-Type": "application/json"}
            self.request_id = request_id

        def read(self, size: int = -1):
            payload = json.dumps({"jsonrpc": "2.0", "id": self.request_id, "result": {}}).encode()
            return payload if size < 0 else payload[:size]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

    class _FakeOpener:
        def open(self, request, timeout):
            _ = timeout
            captured_requests.append(request)
            return _Response(json.loads(request.data.decode())["id"])

    monkeypatch.setattr(
        mcp_connector_http.urllib.request,
        "build_opener",
        lambda *args: _FakeOpener(),
    )

    mcp_connector_http._json_rpc_post_with_metadata(
        url="https://fs.example/mcp",
        headers=connector_headers,
        payload={"jsonrpc": "2.0", "id": "request-1", "method": "tools/list"},
        timeout_seconds=5.0,
        expected_request_id="request-1",
    )

    request_headers = {key.lower(): value for key, value in captured_requests[0].header_items()}
    assert request_headers["user-agent"] == expected_user_agent
    assert (
        sum(key.lower() == "user-agent" for key, _value in captured_requests[0].header_items()) == 1
    )


def test_mcp_http_post_includes_only_explicit_protocol_headers(monkeypatch) -> None:
    captured_requests = []

    class _Response:
        def __init__(self, request_id: str) -> None:
            self.headers = {"Content-Type": "application/json"}
            self.request_id = request_id

        def read(self, size: int = -1):
            payload = json.dumps({"jsonrpc": "2.0", "id": self.request_id, "result": {}}).encode()
            return payload if size < 0 else payload[:size]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

    class _FakeOpener:
        def open(self, request, timeout):
            _ = timeout
            captured_requests.append(request)
            return _Response(json.loads(request.data.decode())["id"])

    monkeypatch.setattr(
        mcp_connector_http.urllib.request,
        "build_opener",
        lambda *args: _FakeOpener(),
    )

    mcp_connector_http._json_rpc_post_with_metadata(
        url="https://fs.example/mcp",
        headers={
            "aCcEpT": "text/plain",
            "CONTENT-TYPE": "text/plain",
            "mCp-SeSsIoN-iD": "attacker-session",
            "mcp-PROTOCOL-version": "attacker-version",
            "MCP-METHOD": "tools/call",
            "mcp-NAME": "attacker.name",
            "user-agent": "connector-client/9.0",
        },
        payload={"jsonrpc": "2.0", "id": "request-1", "method": "tools/list"},
        timeout_seconds=5.0,
        expected_request_id="request-1",
        protocol_version="2026-07-28",
        method_name="tools/list",
        entity_name="catalog.read",
    )

    request_headers = {key.lower(): value for key, value in captured_requests[0].header_items()}
    assert request_headers["accept"] == "application/json, text/event-stream"
    assert request_headers["content-type"] == "application/json"
    assert request_headers["user-agent"] == "connector-client/9.0"
    assert "mcp-session-id" not in request_headers
    assert request_headers["mcp-protocol-version"] == "2026-07-28"
    assert request_headers["mcp-method"] == "tools/list"
    assert request_headers["mcp-name"] == "catalog.read"

    mcp_connector_http._json_rpc_post_with_metadata(
        url="https://fs.example/mcp",
        headers={
            "McP-SeSsIoN-Id": "attacker-session",
            "MCP-PROTOCOL-VERSION": "attacker-version",
            "mcp-method": "tools/call",
            "Mcp-Name": "attacker.name",
        },
        payload={"jsonrpc": "2.0", "id": "request-2", "method": "tools/list"},
        timeout_seconds=5.0,
        expected_request_id="request-2",
    )

    default_headers = {key.lower(): value for key, value in captured_requests[1].header_items()}
    assert "mcp-protocol-version" not in default_headers
    assert "mcp-method" not in default_headers
    assert "mcp-name" not in default_headers
    assert "mcp-session-id" not in default_headers


def test_mcp_json_rpc_ids_are_unique_across_concurrent_calls() -> None:
    ids: list[str] = []
    lock = threading.Lock()

    def _collect_id() -> None:
        request_id = mcp_connector_protocol._next_json_rpc_id()
        with lock:
            ids.append(request_id)

    threads = [threading.Thread(target=_collect_id) for _ in range(64)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(ids) == 64
    assert len(set(ids)) == 64
    assert all(str(uuid.UUID(request_id)) == request_id for request_id in ids)


def test_mcp_sse_decoder_uses_multiline_matching_response_and_skips_nonresponses() -> None:
    raw = """\
: keepalive

event: message
data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{}}

event: message
data: {\"jsonrpc\":\"2.0\",\"id\":\"other-request\",\"result\":{}}

event: message
data: {\"jsonrpc\":\"2.0\",
data: \"id\":\"request-1\",
data: \"result\":{\"tools\":[]}}
"""

    decoded = mcp_connector_http._decode_streamable_http_response(
        raw,
        "text/event-stream; charset=utf-8",
        expected_request_id="request-1",
    )

    assert decoded == {"jsonrpc": "2.0", "id": "request-1", "result": {"tools": []}}


def test_mcp_sse_decoder_rejects_when_no_response_matches_request_id() -> None:
    raw = """\
data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}

data: {\"jsonrpc\":\"2.0\",\"id\":\"other-request\",\"result\":{}}
"""

    with pytest.raises(ServiceValidationError, match="matching JSON-RPC response"):
        mcp_connector_http._decode_streamable_http_response(
            raw,
            "text/event-stream",
            expected_request_id="request-1",
        )


def test_mcp_json_decoder_requires_matching_request_id() -> None:
    with pytest.raises(ServiceValidationError, match="matching JSON-RPC response"):
        mcp_connector_http._decode_streamable_http_response(
            '{"jsonrpc":"2.0","id":"other-request","result":{}}',
            "application/json",
            expected_request_id="request-1",
        )


def test_mcp_session_generates_and_correlates_ids_for_response_requests(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def _post(**kwargs):
        calls.append(kwargs)
        payload = kwargs["payload"]
        if payload["method"] == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {"mcp-session-id": "s1"},
                0,
            )
        if payload["method"] == "notifications/initialized":
            return None, {}, 0
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}}, {}, 0

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    session = mcp_connector_client.StreamableHttpMcpSession(
        url="https://fs.example/mcp",
        headers={},
        timeout_seconds=5.0,
    )

    response, _latency_ms = session.request(
        {"jsonrpc": "2.0", "id": "caller-supplied-id", "method": "tools/list"}
    )

    initialize_call, notification_call, request_call = calls
    assert response == {
        "jsonrpc": "2.0",
        "id": request_call["payload"]["id"],
        "result": {"tools": []},
    }
    assert str(uuid.UUID(initialize_call["payload"]["id"])) == initialize_call["payload"]["id"]
    assert initialize_call["expected_request_id"] == initialize_call["payload"]["id"]
    assert initialize_call["payload"]["params"]["protocolVersion"] == "2025-11-25"
    assert initialize_call.get("protocol_version", "") == ""
    assert str(uuid.UUID(request_call["payload"]["id"])) == request_call["payload"]["id"]
    assert request_call["payload"]["id"] != "caller-supplied-id"
    assert request_call["expected_request_id"] == request_call["payload"]["id"]
    assert request_call["protocol_version"] == "2025-11-25"
    assert request_call["session_id"] == "s1"
    assert "id" not in notification_call["payload"]
    assert "expected_request_id" not in notification_call
    assert notification_call["protocol_version"] == "2025-11-25"
    assert notification_call["session_id"] == "s1"


@pytest.mark.parametrize(
    ("connector_headers", "expected_user_agent"),
    [
        ({}, mcp_connector_http._DEFAULT_USER_AGENT),
        ({"user-agent": "connector-client/1.0"}, "connector-client/1.0"),
    ],
)
def test_mcp_session_close_applies_platform_user_agent(
    monkeypatch,
    connector_headers,
    expected_user_agent,
) -> None:
    captured_requests = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

    class _FakeOpener:
        def open(self, request, timeout):
            _ = timeout
            captured_requests.append(request)
            return _Response()

    monkeypatch.setattr(
        mcp_connector_http.urllib.request,
        "build_opener",
        lambda *args: _FakeOpener(),
    )
    session = mcp_connector_client.StreamableHttpMcpSession(
        url="https://fs.example/mcp",
        headers={
            **connector_headers,
            "aCcEpT": "text/plain",
            "Content-TYPE": "text/plain",
            "MCP-session-ID": "attacker-session",
            "mcp-PROTOCOL-version": "attacker-version",
            "Mcp-Method": "tools/call",
            "Mcp-Name": "attacker.name",
        },
        timeout_seconds=5.0,
    )
    session.session_id = "session-1"
    session._state.negotiated_version = "2025-11-25"

    session.close()

    request_headers = {key.lower(): value for key, value in captured_requests[0].header_items()}
    assert request_headers["user-agent"] == expected_user_agent
    assert request_headers["accept"] == "application/json, text/event-stream"
    assert request_headers["content-type"] == "application/json"
    assert request_headers["mcp-session-id"] == "session-1"
    assert request_headers["mcp-protocol-version"] == "2025-11-25"
    assert "mcp-method" not in request_headers
    assert "mcp-name" not in request_headers


def test_mcp_session_close_uses_private_pinned_transport_for_credentials(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

    def _open_pinned_url(request, *, timeout_seconds, purpose, require_private=False):
        captured.update(
            request=request,
            timeout_seconds=timeout_seconds,
            purpose=purpose,
            require_private=require_private,
        )
        return _Response()

    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    monkeypatch.setattr(mcp_connector_http.url_safety, "open_pinned_url", _open_pinned_url)
    session = mcp_connector_client.StreamableHttpMcpSession(
        url="http://10.0.0.5/mcp",
        headers={"Authorization": "Bearer must-not-leak"},
        timeout_seconds=5.0,
    )
    session.session_id = "session-1"
    session._state.negotiated_version = "2025-11-25"

    session.close()

    request = captured["request"]
    assert request.get_method() == "DELETE"
    assert request.get_header("Authorization") == "Bearer must-not-leak"
    assert captured["require_private"] is True
    assert captured["purpose"] == "MCP connector base_url"


def _legacy_signal_error() -> mcp_connector_protocol.McpProtocolError:
    return mcp_connector_protocol.McpProtocolError(
        "MCP server requires legacy protocol negotiation",
        code="-32601",
        legacy_signal=True,
    )


def _http_service_error(status: int) -> ServiceUnavailableError:
    error = ServiceUnavailableError(f"MCP request failed with HTTP {status}")
    error.__cause__ = urllib.error.HTTPError(
        "https://fs.example/mcp",
        status,
        "failure",
        hdrs={},
        fp=None,
    )
    return error


def test_modern_client_sends_exact_discovery_list_and_call_wire_contract(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    opened_http_methods: list[str] = []

    def _post(**kwargs):
        calls.append(kwargs)
        payload = kwargs["payload"]
        result = (
            {"supportedVersions": ["2026-07-28"], "capabilities": {}}
            if payload["method"] == "server/discover"
            else {"tools": []}
        )
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}, {}, 3

    class _UnexpectedResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

    class _RecordingOpener:
        def open(self, request, timeout):
            _ = timeout
            opened_http_methods.append(request.get_method())
            return _UnexpectedResponse()

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    monkeypatch.setattr(
        mcp_connector_http.urllib.request,
        "build_opener",
        lambda *args: _RecordingOpener(),
    )
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp",
        headers={"X-Connector": "fixture"},
        timeout_seconds=5.0,
    )

    discovery, _ = client.request("server/discover", None)
    listed, _ = client.request("tools/list", {"_meta": {"caller": "preserved"}})
    called, _ = client.request(
        "tools/call",
        {"name": "  catalog.read  ", "arguments": {"query": "safe"}},
        name="  catalog.read  ",
    )
    client.close()

    assert discovery["result"]["supportedVersions"] == ["2026-07-28"]
    assert listed["result"] == {"tools": []}
    assert called["result"] == {"tools": []}
    assert [call["payload"]["method"] for call in calls] == [
        "server/discover",
        "tools/list",
        "tools/call",
    ]
    reserved_meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {
            "name": "flow-steward",
            "version": mcp_connector_client.APP_VERSION,
        },
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    assert calls[0]["payload"]["params"] == {"_meta": reserved_meta}
    assert calls[1]["payload"]["params"] == {"_meta": {"caller": "preserved", **reserved_meta}}
    assert calls[2]["payload"]["params"] == {
        "name": "  catalog.read  ",
        "arguments": {"query": "safe"},
        "_meta": reserved_meta,
    }
    assert [call["protocol_version"] for call in calls] == ["2026-07-28"] * 3
    assert [call["method_name"] for call in calls] == [
        "server/discover",
        "tools/list",
        "tools/call",
    ]
    encoded_name = f"=?base64?{base64.b64encode(b'  catalog.read  ').decode()}?="
    assert [call["entity_name"] for call in calls] == ["", "", encoded_name]
    assert all(call["session_id"] == "" for call in calls)
    assert all(call["headers"] == {"X-Connector": "fixture"} for call in calls)
    assert opened_http_methods == []


@pytest.mark.parametrize("negotiated_version", sorted(mcp_connector_protocol.MCP_LEGACY_VERSIONS))
def test_legacy_client_offers_preferred_version_and_uses_negotiated_headers(
    monkeypatch,
    negotiated_version,
) -> None:
    calls: list[dict[str, Any]] = []

    def _post(**kwargs):
        calls.append(kwargs)
        payload = kwargs["payload"]
        if payload["method"] == "server/discover":
            raise _legacy_signal_error()
        if payload["method"] == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": negotiated_version},
                },
                {"mcp-session-id": "legacy-session"},
                1,
            )
        if payload["method"] == "notifications/initialized":
            return None, {}, 1
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp",
        headers={},
        timeout_seconds=5.0,
    )

    response, _ = client.request(
        "tools/call",
        {"name": "  catalog.read  ", "arguments": {}},
        name="  catalog.read  ",
    )

    assert response["result"] == {"tools": []}
    discover_call, initialize_call, initialized_call, request_call = calls
    assert discover_call["protocol_version"] == "2026-07-28"
    assert initialize_call["payload"]["params"]["protocolVersion"] == "2025-11-25"
    assert initialize_call.get("protocol_version", "") == ""
    assert initialize_call.get("method_name", "") == ""
    assert initialize_call.get("entity_name", "") == ""
    assert initialize_call.get("session_id", "") == ""
    assert initialized_call["protocol_version"] == negotiated_version
    assert initialized_call["session_id"] == "legacy-session"
    assert initialized_call.get("method_name", "") == ""
    assert initialized_call.get("entity_name", "") == ""
    assert request_call["protocol_version"] == negotiated_version
    assert request_call["session_id"] == "legacy-session"
    assert request_call.get("method_name", "") == ""
    assert request_call.get("entity_name", "") == ""
    assert request_call["payload"]["params"]["name"] == "  catalog.read  "


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("catalog.read", False),
        (" café ", True),
        ("line\nfeed", True),
        ("tab\tvalue", True),
        ("=?base64?Y2F0YWxvZw==?=", True),
    ],
)
def test_modern_mcp_name_uses_required_http_value_encoding(monkeypatch, value, encoded) -> None:
    calls: list[dict[str, Any]] = []

    def _post(**kwargs):
        calls.append(kwargs)
        payload = kwargs["payload"]
        result = (
            {"supportedVersions": ["2026-07-28"], "capabilities": {}}
            if payload["method"] == "server/discover"
            else {"content": []}
        )
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    client.request("tools/call", {"name": value, "arguments": {}}, name=value)

    call = calls[-1]
    assert call["payload"]["params"]["name"] == value
    expected = value
    if encoded:
        expected = f"=?base64?{base64.b64encode(value.encode()).decode()}?="
    assert call["entity_name"] == expected


def test_supported_versions_is_authoritative_over_receiver_leniency_aliases() -> None:
    assert mcp_connector_client._advertised_modern_versions(
        {
            "supportedVersions": ["2026-07-28"],
            "capabilities": {},
            "protocolVersions": "malformed-alias",
        }
    ) == ["2026-07-28"]


@pytest.mark.parametrize(
    "discovery_result",
    [
        {"supportedVersions": ["2026-07-28"]},
        {"supportedVersions": ["2026-07-28"], "capabilities": []},
        {"supportedVersions": ["2026-07-28"], "capabilities": None},
    ],
)
def test_discovery_missing_or_non_object_capabilities_falls_back_to_legacy(
    monkeypatch,
    discovery_result,
) -> None:
    methods: list[str] = []

    def _post(**kwargs):
        payload = kwargs["payload"]
        method = payload["method"]
        methods.append(method)
        if method == "server/discover":
            return (
                {"jsonrpc": "2.0", "id": payload["id"], "result": discovery_result},
                {},
                1,
            )
        if method == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {},
                1,
            )
        if method == "notifications/initialized":
            return None, {}, 1
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    client.request("tools/list", {})

    assert methods == [
        "server/discover",
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


def test_discovery_empty_capabilities_object_selects_modern(monkeypatch) -> None:
    methods: list[str] = []

    def _post(**kwargs):
        payload = kwargs["payload"]
        methods.append(payload["method"])
        result = (
            {"supportedVersions": ["2026-07-28"], "capabilities": {}}
            if payload["method"] == "server/discover"
            else {"tools": []}
        )
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    client.request("tools/list", {})

    assert client.era is mcp_connector_protocol.McpProtocolEra.MODERN
    assert methods == ["server/discover", "tools/list"]


def test_mcp_param_headers_use_trusted_schema_and_omit_missing_or_null_values() -> None:
    schema = {
        "type": "object",
        "properties": {
            "tenant": {"type": "string", "x-mcp-header": "Tenant"},
            "enabled": {"type": "boolean", "x-mcp-header": "Enabled"},
            "count": {"type": "integer", "x-mcp-header": "Count"},
            "optional": {"type": "string", "x-mcp-header": "Optional"},
        },
    }

    headers = mcp_connector_http.mcp_tool_transport_headers(
        schema,
        {"tenant": " café ", "enabled": False, "count": 7, "optional": None},
    )

    assert headers == {
        "Mcp-Param-Tenant": f"=?base64?{base64.b64encode(' café '.encode()).decode()}?=",
        "Mcp-Param-Enabled": "false",
        "Mcp-Param-Count": "7",
    }


def test_mcp_param_integer_accepts_integral_json_number() -> None:
    headers = mcp_connector_http.mcp_tool_transport_headers(
        {
            "type": "object",
            "properties": {"count": {"type": "integer", "x-mcp-header": "Count"}},
        },
        {"count": 42.0},
    )

    assert headers == {"Mcp-Param-Count": "42"}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"context": {"region": "eu-west"}}, {"Mcp-Param-Region": "eu-west"}),
        ({"context": {}}, {}),
        ({"context": {"region": None}}, {}),
    ],
)
def test_mcp_param_nested_properties_path_is_supported(arguments, expected) -> None:
    schema = {
        "type": "object",
        "properties": {
            "context": {
                "type": "object",
                "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
            }
        },
    }

    assert mcp_connector_http.mcp_tool_transport_headers(schema, arguments) == expected


@pytest.mark.parametrize(
    "value",
    [42.5, float("nan"), float("inf"), float("-inf"), True, 2**53, -(2**53)],
)
def test_mcp_param_integer_rejects_non_integer_or_unsafe_json_number(value) -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer", "x-mcp-header": "Count"}},
    }

    with pytest.raises(mcp_connector_protocol.McpProtocolError):
        mcp_connector_http.mcp_tool_transport_headers(schema, {"count": value})


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "x-mcp-header": "Root", "properties": {}},
        {
            "type": "object",
            "properties": {"value": {"type": "number", "x-mcp-header": "Value"}},
        },
        {
            "type": "object",
            "properties": {
                "first": {"type": "string", "x-mcp-header": "Tenant"},
                "second": {"type": "string", "x-mcp-header": "tenant"},
            },
        },
        {
            "type": "object",
            "properties": {"values": {"type": "array", "items": {"x-mcp-header": "Bad"}}},
        },
        {
            "type": "object",
            "properties": {
                "direct": {"type": "string", "x-mcp-header": "Tenant"},
                "nested": {
                    "type": "object",
                    "properties": {"value": {"type": "string", "x-mcp-header": "tenant"}},
                },
            },
        },
        {
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "allOf": [{"type": "string", "x-mcp-header": "Bad"}],
                }
            },
        },
        {
            "type": "object",
            "$defs": {"value": {"type": "string", "x-mcp-header": "Bad"}},
        },
        {
            "type": "object",
            "properties": {
                "value": {
                    "$ref": {"x-mcp-header": "Bad"},
                }
            },
        },
        {"type": "object", "properties": [{"x-mcp-header": "Bad"}]},
    ],
)
def test_invalid_mcp_header_annotations_exclude_only_affected_tools(schema, caplog) -> None:
    secret = "annotation-secret-value"  # pragma: allowlist secret - redaction sentinel
    with caplog.at_level(logging.WARNING):
        tools, _cursor = mcp_connector_common._extract_tools_page(
            {
                "result": {
                    "tools": [
                        {"name": "invalid", "description": secret, "inputSchema": schema},
                        {"name": "valid", "inputSchema": {"type": "object"}},
                    ]
                }
            }
        )

    assert [tool["name"] for tool in tools] == ["valid"]
    assert secret not in caplog.text


def test_generated_mcp_param_header_overrides_connector_case_variants() -> None:
    headers = mcp_connector_http._with_mcp_platform_headers(
        {"mCp-PaRaM-tEnAnT": "connector-secret"},
        protocol_version="2026-07-28",
        method_name="tools/call",
        entity_name="catalog.read",
        generated_transport_headers={"Mcp-Param-Tenant": "trusted-value"},
    )

    lowered = {key.lower(): value for key, value in headers.items()}
    assert lowered["mcp-param-tenant"] == "trusted-value"
    assert "connector-secret" not in lowered.values()


def test_mcp_param_validation_error_does_not_expose_argument_value() -> None:
    secret = "mirrored-argument-secret"  # pragma: allowlist secret - redaction sentinel
    schema = {
        "type": "object",
        "properties": {"tenant": {"type": "integer", "x-mcp-header": "Tenant"}},
    }

    with pytest.raises(mcp_connector_protocol.McpProtocolError) as exc_info:
        mcp_connector_http.mcp_tool_transport_headers(schema, {"tenant": secret})

    assert secret not in str(exc_info.value)


def test_legacy_client_rejects_unadvertised_negotiated_version(monkeypatch) -> None:
    def _post(**kwargs):
        payload = kwargs["payload"]
        if payload["method"] == "server/discover":
            raise _legacy_signal_error()
        return (
            {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"protocolVersion": "2024-11-05"},
            },
            {},
            1,
        )

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    with pytest.raises(
        mcp_connector_protocol.McpProtocolError, match="unsupported protocol version"
    ):
        client.request("tools/list", None)


def test_modern_discovery_advertising_only_legacy_versions_falls_back(monkeypatch) -> None:
    methods: list[str] = []

    def _post(**kwargs):
        payload = kwargs["payload"]
        methods.append(payload["method"])
        if payload["method"] == "server/discover":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"supportedVersions": ["2025-11-25"], "capabilities": {}},
                },
                {},
                1,
            )
        if payload["method"] == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {},
                1,
            )
        if payload["method"] == "notifications/initialized":
            return None, {}, 1
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    client.request("tools/list", None)

    assert methods == [
        "server/discover",
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


@pytest.mark.parametrize(
    "rpc_error",
    [
        {"code": -32601, "message": "Method not found"},
        {"code": -32022, "message": "Unsupported version"},
        {"code": "unsupported_protocol_version", "message": "unsupported"},
        {"code": -32002, "message": "Server not initialized"},
    ],
)
def test_modern_discovery_falls_back_for_unrecognized_probe_errors(
    monkeypatch,
    rpc_error,
) -> None:
    methods: list[str] = []

    def _post(**kwargs):
        payload = kwargs["payload"]
        methods.append(payload["method"])
        if payload["method"] == "server/discover":
            raise mcp_connector_protocol._mcp_discovery_error_from_json_rpc(rpc_error)
        if payload["method"] == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {},
                1,
            )
        if payload["method"] == "notifications/initialized":
            return None, {}, 1
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    client.request("tools/list", None)

    assert methods == [
        "server/discover",
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


@pytest.mark.parametrize(
    "rpc_error",
    [
        "initialization required",
        {"code": -32000, "message": "Other failure", "data": {"detail": "initialize first"}},
        {"code": -32000, "message": {"detail": "initialization required"}},
        {"code": {}, "message": "unsupported protocol version"},
    ],
)
def test_modern_discovery_malformed_or_nested_errors_fall_back(
    monkeypatch,
    rpc_error,
) -> None:
    methods: list[str] = []

    def _post(**kwargs):
        method = kwargs["payload"]["method"]
        methods.append(method)
        if method == "server/discover":
            raise mcp_connector_protocol._mcp_discovery_error_from_json_rpc(rpc_error)
        if method == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": kwargs["payload"]["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {},
                1,
            )
        if method == "notifications/initialized":
            return None, {}, 1
        return {"jsonrpc": "2.0", "id": kwargs["payload"]["id"], "result": {}}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    client.request("tools/list", None)

    assert methods == ["server/discover", "initialize", "notifications/initialized", "tools/list"]


@pytest.mark.parametrize(
    "rpc_error",
    [
        {"code": -32002, "message": "Request rejected"},
        {"code": -32000, "message": "Request rejected"},
    ],
)
def test_modern_discovery_falls_back_for_arbitrary_in_band_errors(
    monkeypatch,
    rpc_error,
) -> None:
    methods: list[str] = []

    def _post(**kwargs):
        method = kwargs["payload"]["method"]
        methods.append(method)
        if method == "server/discover":
            raise mcp_connector_protocol._mcp_discovery_error_from_json_rpc(rpc_error)
        if method == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": kwargs["payload"]["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {},
                1,
            )
        if method == "notifications/initialized":
            return None, {}, 1
        return {"jsonrpc": "2.0", "id": kwargs["payload"]["id"], "result": {}}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    client.request("tools/list", None)

    assert methods == ["server/discover", "initialize", "notifications/initialized", "tools/list"]


@pytest.mark.parametrize(
    "advertised_versions",
    [
        [{}],
        [20260728],
        [""],
        ["   "],
        "2026-07-28",
        {"current": "2026-07-28"},
    ],
)
def test_modern_discovery_malformed_result_falls_back(
    monkeypatch,
    advertised_versions,
) -> None:
    methods: list[str] = []

    def _post(**kwargs):
        payload = kwargs["payload"]
        methods.append(payload["method"])
        if payload["method"] == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {},
                1,
            )
        if payload["method"] == "notifications/initialized":
            return None, {}, 1
        if payload["method"] != "server/discover":
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {}}, {}, 1
        return (
            {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"supportedVersions": advertised_versions, "capabilities": {}},
            },
            {},
            1,
        )

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    client.request("tools/list", None)

    assert methods == ["server/discover", "initialize", "notifications/initialized", "tools/list"]


def test_discovery_unsupported_version_with_mutual_modern_retries_once() -> None:
    error = mcp_connector_protocol._mcp_discovery_error_from_json_rpc(
        {
            "code": -32022,
            "message": "Unsupported protocol version",
            "data": {"supported": ["2026-07-28", "2025-11-25"]},
        }
    )
    calls = 0

    def _post(**kwargs):
        nonlocal calls
        calls += 1
        payload = kwargs["payload"]
        if calls == 1:
            raise error
        result = (
            {"supportedVersions": ["2026-07-28"], "capabilities": {}}
            if payload["method"] == "server/discover"
            else {"tools": []}
        )
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}, {}, 1

    with patch.object(mcp_connector_client, "_json_rpc_post_with_metadata", side_effect=_post):
        client = mcp_connector_client.StreamableHttpMcpClient(
            url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
        )
        response, _latency = client.request("tools/list", {})

    assert response["result"] == {"tools": []}
    assert calls == 3


def test_discovery_unsupported_version_retry_has_loop_guard() -> None:
    error = mcp_connector_protocol._mcp_discovery_error_from_json_rpc(
        {
            "code": -32022,
            "message": "Unsupported protocol version",
            "data": {"supported": ["2026-07-28", "2025-11-25"]},
        }
    )
    calls = 0

    def _post(**_kwargs):
        nonlocal calls
        calls += 1
        raise error

    with patch.object(mcp_connector_client, "_json_rpc_post_with_metadata", side_effect=_post):
        client = mcp_connector_client.StreamableHttpMcpClient(
            url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
        )
        with pytest.raises(mcp_connector_protocol.McpProtocolError) as exc_info:
            client.request("tools/list", {})

    assert exc_info.value.code == "-32022"
    assert calls == 2


@pytest.mark.parametrize(
    ("supported", "expect_legacy"),
    [
        (["2027-01-01"], False),
        (["2025-11-25"], True),
        (["2024-11-05", "2025-11-25"], True),
        (["2027-01-01", "2025-11-25"], True),
        (["2024-11-05"], False),
        ([], True),
        ("2026-07-28", True),
        (None, True),
    ],
)
def test_discovery_unsupported_version_classifies_supported_versions(
    monkeypatch,
    supported,
    expect_legacy,
) -> None:
    methods: list[str] = []
    rpc_error: dict[str, Any] = {
        "code": -32022,
        "message": "Unsupported protocol version",
    }
    if supported is not None:
        rpc_error["data"] = {"supported": supported}

    def _post(**kwargs):
        payload = kwargs["payload"]
        method = payload["method"]
        methods.append(method)
        if method == "server/discover":
            raise mcp_connector_protocol._mcp_discovery_error_from_json_rpc(rpc_error)
        if method == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {},
                1,
            )
        if method == "notifications/initialized":
            return None, {}, 1
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {}}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    if expect_legacy:
        client.request("tools/list", {})
        assert methods == [
            "server/discover",
            "initialize",
            "notifications/initialized",
            "tools/list",
        ]
    else:
        with pytest.raises(mcp_connector_protocol.McpProtocolError) as exc_info:
            client.request("tools/list", {})
        assert exc_info.value.code == "-32022"
        assert methods == ["server/discover"]


@pytest.mark.parametrize("error_code", ["-32020", "-32022", True])
def test_discovery_non_integer_error_code_is_conservative_legacy(
    monkeypatch,
    error_code,
) -> None:
    methods: list[str] = []
    rpc_error = {
        "code": error_code,
        "message": "Unsupported protocol version",
        "data": {"supported": ["2026-07-28"]},
    }

    def _post(**kwargs):
        payload = kwargs["payload"]
        method = payload["method"]
        methods.append(method)
        if method == "server/discover":
            raise mcp_connector_protocol._mcp_discovery_error_from_json_rpc(rpc_error)
        if method == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {},
                1,
            )
        if method == "notifications/initialized":
            return None, {}, 1
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {}}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    client.request("tools/list", {})

    assert methods == [
        "server/discover",
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


@pytest.mark.parametrize("modern", [True, False], ids=["modern", "legacy"])
def test_protocol_client_rejects_name_header_that_conflicts_with_body(
    monkeypatch,
    modern,
) -> None:
    methods: list[str] = []

    def _post(**kwargs):
        payload = kwargs["payload"]
        method = payload["method"]
        methods.append(method)
        if method == "server/discover":
            if modern:
                return (
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "supportedVersions": ["2026-07-28"],
                            "capabilities": {},
                        },
                    },
                    {},
                    1,
                )
            raise _legacy_signal_error()
        if method == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {"mcp-session-id": "legacy-session"},
                1,
            )
        if method == "notifications/initialized":
            return None, {}, 1
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )
    if modern:
        client.request("server/discover", None)
    else:
        client.request("tools/list", None)
    methods_before_conflict = list(methods)

    with pytest.raises(mcp_connector_protocol.McpProtocolError, match=r"name.*does not match"):
        client.request(
            "tools/call",
            {"name": "catalog.read", "arguments": {}},
            name="catalog.write",
        )

    assert methods == methods_before_conflict


@pytest.mark.parametrize(
    "probe_error",
    [
        _http_service_error(401),
        _http_service_error(403),
        _http_service_error(500),
        ServiceUnavailableError("MCP request failed: timed out"),
        ServiceUnavailableError("MCP request failed: network unavailable"),
    ],
)
def test_modern_discovery_does_not_fallback_for_transport_or_malformed_failures(
    monkeypatch,
    probe_error,
) -> None:
    calls = 0

    def _post(**_kwargs):
        nonlocal calls
        calls += 1
        raise probe_error

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    with pytest.raises(type(probe_error)):
        client.request("tools/list", None)

    assert calls == 1


def test_legacy_session_404_reinitializes_and_retries_once(monkeypatch) -> None:
    methods: list[tuple[str, str]] = []
    initialize_count = 0
    request_count = 0

    def _post(**kwargs):
        nonlocal initialize_count, request_count
        payload = kwargs["payload"]
        method = payload["method"]
        methods.append((method, kwargs.get("session_id", "")))
        if method == "server/discover":
            raise _legacy_signal_error()
        if method == "initialize":
            initialize_count += 1
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {"mcp-session-id": f"session-{initialize_count}"},
                1,
            )
        if method == "notifications/initialized":
            return None, {}, 1
        request_count += 1
        if request_count == 1:
            raise _http_service_error(404)
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    response, _ = client.request("tools/list", None)

    assert response["result"] == {"tools": []}
    assert initialize_count == 2
    assert request_count == 2
    assert methods[-1] == ("tools/list", "session-2")


def test_legacy_session_second_404_is_not_reinitialized_again(monkeypatch) -> None:
    initialize_count = 0
    request_count = 0

    def _post(**kwargs):
        nonlocal initialize_count, request_count
        payload = kwargs["payload"]
        method = payload["method"]
        if method == "server/discover":
            raise _legacy_signal_error()
        if method == "initialize":
            initialize_count += 1
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {"mcp-session-id": f"session-{initialize_count}"},
                1,
            )
        if method == "notifications/initialized":
            return None, {}, 1
        request_count += 1
        raise _http_service_error(404)

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    with pytest.raises(ServiceUnavailableError, match="HTTP 404"):
        client.request("tools/list", None)

    assert initialize_count == 2
    assert request_count == 2


def test_legacy_tools_call_404_reinitializes_but_does_not_replay(monkeypatch) -> None:
    initialize_count = 0
    call_count = 0

    def _post(**kwargs):
        nonlocal initialize_count, call_count
        payload = kwargs["payload"]
        method = payload["method"]
        if method == "server/discover":
            raise _legacy_signal_error()
        if method == "initialize":
            initialize_count += 1
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
                {"mcp-session-id": f"session-{initialize_count}"},
                1,
            )
        if method == "notifications/initialized":
            return None, {}, 1
        call_count += 1
        if call_count == 1:
            raise _http_service_error(404)
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {}}, {}, 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    with pytest.raises(
        mcp_connector_protocol.McpSessionExpiredError,
        match="non-retryable",
    ):
        client.request(
            "tools/call",
            {"name": "catalog.write", "arguments": {"value": 1}},
            name="catalog.write",
        )

    assert initialize_count == 2
    assert call_count == 1
    response, _ = client.request("tools/list", None)
    assert response["result"] == {}
    assert initialize_count == 2
    assert call_count == 2


@pytest.mark.parametrize("failure", ["unsupported_version", "notification"])
def test_legacy_initialize_failure_deletes_provisional_session(
    monkeypatch,
    failure,
) -> None:
    delete_requests = []

    def _post(**kwargs):
        payload = kwargs["payload"]
        method = payload["method"]
        if method == "server/discover":
            raise _legacy_signal_error()
        if method == "initialize":
            version = "2024-11-05" if failure == "unsupported_version" else "2025-11-25"
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": version},
                },
                {"mcp-session-id": "provisional-session"},
                1,
            )
        if method == "notifications/initialized":
            raise ServiceUnavailableError("initialized notification failed")
        pytest.fail(f"unexpected MCP method {method}")

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

    class _FakeOpener:
        def open(self, request, timeout):
            _ = timeout
            delete_requests.append(request)
            return _Response()

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    monkeypatch.setattr(
        mcp_connector_http.urllib.request,
        "build_opener",
        lambda *args: _FakeOpener(),
    )
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    with pytest.raises((mcp_connector_protocol.McpProtocolError, ServiceUnavailableError)):
        client.request("tools/list", None)

    assert len(delete_requests) == 1
    headers = {key.lower(): value for key, value in delete_requests[0].header_items()}
    assert headers["mcp-session-id"] == "provisional-session"
    assert client._legacy_session is not None
    assert client._legacy_session.session_id == ""


def test_legacy_close_deletes_at_most_once_and_includes_negotiated_headers(monkeypatch) -> None:
    def _post(**kwargs):
        payload = kwargs["payload"]
        if payload["method"] == "server/discover":
            raise _legacy_signal_error()
        if payload["method"] == "initialize":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-06-18"},
                },
                {"mcp-session-id": "delete-once"},
                1,
            )
        if payload["method"] == "notifications/initialized":
            return None, {}, 1
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {}}, {}, 1

    requests = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

    class _FakeOpener:
        def open(self, request, timeout):
            _ = timeout
            requests.append(request)
            return _Response()

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    monkeypatch.setattr(
        mcp_connector_http.urllib.request,
        "build_opener",
        lambda *args: _FakeOpener(),
    )
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )
    client.request("tools/list", None)

    client.close()
    client.close()

    assert len(requests) == 1
    headers = {key.lower(): value for key, value in requests[0].header_items()}
    assert headers["mcp-session-id"] == "delete-once"
    assert headers["mcp-protocol-version"] == "2025-06-18"


def test_execution_scope_reuses_browser_client_and_isolates_registry_keys() -> None:
    created = []

    class _FakeClient:
        def __init__(self) -> None:
            self.page = ""
            self.close_count = 0
            created.append(self)

        def close(self) -> None:
            self.close_count += 1

    def _get(account_id: str, job_id: str, connector_id: str):
        return mcp_connector_client.get_execution_mcp_client(
            account_id=account_id,
            job_id=job_id,
            connector_id=connector_id,
            client_factory=_FakeClient,
        )

    with mcp_connector_client.mcp_execution_scope("acc-a", "job-a"):
        browser = _get("acc-a", "job-a", "browser")
        browser.page = "https://fs.example/catalog"
        assert _get("acc-a", "job-a", "browser").page == "https://fs.example/catalog"
        assert _get("acc-a", "job-a", "search") is not browser
    with mcp_connector_client.mcp_execution_scope("acc-a", "job-b"):
        assert _get("acc-a", "job-b", "browser") is not browser
    with mcp_connector_client.mcp_execution_scope("acc-b", "job-a"):
        assert _get("acc-b", "job-a", "browser") is not browser

    assert len(created) == 4
    assert all(client.close_count == 1 for client in created)


def test_execution_scope_rejects_runtime_identity_mismatch() -> None:
    _raises_input_2110_1 = MagicMock()
    with (
        mcp_connector_client.mcp_execution_scope("acc-a", "job-a"),
        pytest.raises(ServiceValidationError, match="identity does not match"),
    ):
        mcp_connector_client.get_execution_mcp_client(
            account_id="acc-b",
            job_id="job-a",
            connector_id="browser",
            client_factory=_raises_input_2110_1,
        )


def test_execution_client_without_active_scope_raises_typed_inactive_error() -> None:
    client_factory = MagicMock()

    with pytest.raises(
        mcp_connector_protocol.McpExecutionScopeInactiveError,
        match="not active",
    ) as exc_info:
        mcp_connector_client.get_execution_mcp_client(
            account_id="acc-a",
            job_id="job-a",
            connector_id="browser",
            client_factory=client_factory,
        )

    assert isinstance(exc_info.value, ServiceValidationError)
    client_factory.assert_not_called()


def test_execution_scope_close_removes_clients_before_best_effort_close() -> None:
    close_calls = 0

    class _FailingCloseClient:
        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1
            raise RuntimeError("advisory close failure")

    with mcp_connector_client.mcp_execution_scope("acc-a", "job-a"):
        mcp_connector_client.get_execution_mcp_client(
            account_id="acc-a",
            job_id="job-a",
            connector_id="browser",
            client_factory=_FailingCloseClient,
        )
        mcp_connector_client.close_execution_mcp_clients()
        with pytest.raises(ServiceValidationError, match="closing"):
            mcp_connector_client.get_execution_mcp_client(
                account_id="acc-a",
                job_id="job-a",
                connector_id="browser",
                client_factory=_FailingCloseClient,
            )

    assert close_calls == 1


def test_protocol_client_serializes_parallel_requests(monkeypatch) -> None:
    active = 0
    maximum_active = 0
    active_lock = threading.Lock()
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def _post(**kwargs):
        nonlocal active, maximum_active
        payload = kwargs["payload"]
        with active_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.01)
            result = (
                {"supportedVersions": ["2026-07-28"], "capabilities": {}}
                if payload["method"] == "server/discover"
                else {"tools": []}
            )
            return {"jsonrpc": "2.0", "id": payload["id"], "result": result}, {}, 1
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )
    client.request("server/discover", None)
    maximum_active = 0

    def _request_tools() -> None:
        try:
            response, _latency_ms = client.request("tools/list", None)
        except BaseException as exc:
            with active_lock:
                errors.append(exc)
        else:
            with active_lock:
                results.append(response)

    threads = [threading.Thread(target=_request_tools) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert maximum_active == 1
    assert errors == []
    assert len(results) == 4
    assert all(result["result"] == {"tools": []} for result in results)


def _fixture_connector_row(url: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "connector_id": "mcp_browser_fixture",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "transport": "streamable_http",
        "base_url": url,
        "auth_mode": "none",
        "headers": {},
    }
    row.update(overrides)
    return row


def _fixture_rpc(
    row: dict[str, Any],
    *,
    job_id: str,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    from core.application.auth.mcp_connector_oauth import mcp_connector_rpc_post

    return mcp_connector_rpc_post(
        object(),
        connector_row=row,
        payload={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout_seconds=2.0,
        runtime_context={
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "job_id": job_id,
        },
    )


def test_legacy_fixture_reuses_state_and_correlates_final_sse_response(monkeypatch) -> None:
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")

    with legacy_mcp_server() as fixture:
        row = _fixture_connector_row(fixture.url)
        with mcp_connector_client.mcp_execution_scope("acc_alpha", "job-stateful"):
            navigated = _fixture_rpc(
                row,
                job_id="job-stateful",
                method="tools/call",
                params={
                    "name": "browser_navigate",
                    "arguments": {"url": "https://example.test/catalog"},
                },
            )
            clicked = _fixture_rpc(
                row,
                job_id="job-stateful",
                method="tools/call",
                params={
                    "name": "browser_click",
                    "arguments": {"selector": "#checkout"},
                },
            )

        assert navigated["result"]["structuredContent"] == {"url": "https://example.test/catalog"}
        assert clicked["result"]["content"] == [{"type": "text", "text": "fixture-final-result"}]
        assert clicked["result"]["structuredContent"] == {
            "url": "https://example.test/catalog",
            "selector": "#checkout",
        }
        assert len(fixture.sessions) == 1
        assert fixture.sessions[0].clicks == ["#checkout"]
        assert fixture.sessions[0].closed is True
        assert fixture.delete_count == 1
        assert fixture.violations == []


def test_legacy_fixture_isolates_sessions_across_jobs(monkeypatch) -> None:
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")

    with legacy_mcp_server() as fixture:
        row = _fixture_connector_row(fixture.url)
        for job_id, url in (
            ("job-alpha", "https://example.test/alpha"),
            ("job-beta", "https://example.test/beta"),
        ):
            with mcp_connector_client.mcp_execution_scope("acc_alpha", job_id):
                _fixture_rpc(
                    row,
                    job_id=job_id,
                    method="tools/call",
                    params={"name": "browser_navigate", "arguments": {"url": url}},
                )

        assert [session.current_url for session in fixture.sessions] == [
            "https://example.test/alpha",
            "https://example.test/beta",
        ]
        assert len({session.session_id for session in fixture.sessions}) == 2
        assert all(session.closed for session in fixture.sessions)
        assert fixture.delete_count == 2


def test_legacy_fixture_serializes_same_job_calls_over_real_http(monkeypatch) -> None:
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    errors: list[BaseException] = []
    results: list[dict[str, Any]] = []
    result_lock = threading.Lock()
    barrier = threading.Barrier(4)

    with legacy_mcp_server(request_delay_seconds=0.03) as fixture:
        row = _fixture_connector_row(fixture.url)
        with mcp_connector_client.mcp_execution_scope("acc_alpha", "job-serialized"):
            _fixture_rpc(
                row,
                job_id="job-serialized",
                method="tools/call",
                params={
                    "name": "browser_navigate",
                    "arguments": {"url": "https://example.test/shared"},
                },
            )
            fixture.max_active_requests = 0

            def _call_click(index: int) -> None:
                try:
                    barrier.wait()
                    response = _fixture_rpc(
                        row,
                        job_id="job-serialized",
                        method="tools/call",
                        params={
                            "name": "browser_click",
                            "arguments": {"selector": f"#item-{index}"},
                        },
                    )
                except BaseException as exc:
                    with result_lock:
                        errors.append(exc)
                else:
                    with result_lock:
                        results.append(response)

            contexts = [copy_context() for _ in range(4)]
            threads = [
                threading.Thread(target=context.run, args=(_call_click, index))
                for index, context in enumerate(contexts)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5.0)
                assert not thread.is_alive()

        assert errors == []
        assert len(results) == 4
        assert fixture.max_active_requests == 1
        assert len(fixture.sessions) == 1
        assert len(fixture.sessions[0].clicks) == 4
        assert fixture.delete_count == 1


def test_modern_fixture_stays_stateless_and_validates_wire_contract(monkeypatch) -> None:
    from core.application.auth.mcp_connector_oauth import mcp_connector_rpc_context

    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    with modern_mcp_server() as fixture:
        row = _fixture_connector_row(
            fixture.url,
            headers={"mCp-PaRaM-tEnAnT": "connector-override"},
        )
        monkeypatch.setattr(
            mcp_connector_oauth_repository.McpConnectorStore,
            "get_connector_row",
            lambda *_args, **_kwargs: row,
        )
        with mcp_connector_rpc_context(
            object(),
            connector_row=row,
            timeout_seconds=2.0,
        ) as rpc_post:
            first, _ = rpc_post({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            second, _ = rpc_post(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {"cursor": first["result"]["nextCursor"]},
                }
            )
            called, _ = rpc_post(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "browser_click", "arguments": {"selector": "#ok"}},
                },
                generated_transport_headers={"Mcp-Param-Tenant": "trusted-tenant"},
            )

        assert [tool["name"] for tool in first["result"]["tools"]] == ["browser_navigate"]
        assert [tool["name"] for tool in second["result"]["tools"]] == ["browser_click"]
        assert called["result"]["structuredContent"] == {"name": "browser_click"}
        assert [
            record.payload["method"] for record in fixture.requests if record.payload is not None
        ] == ["server/discover", "tools/list", "tools/list", "tools/call"]
        assert fixture.sessions == []
        assert fixture.delete_count == 0
        assert fixture.violations == []
        call_record = next(
            record
            for record in fixture.requests
            if record.payload and record.payload.get("method") == "tools/call"
        )
        assert call_record.headers["mcp-param-tenant"] == "trusted-tenant"
        assert "connector-override" not in call_record.headers.values()


def test_generated_tool_headers_cannot_cross_public_plain_http_send_path(monkeypatch) -> None:
    from core.application.auth import mcp_connector_oauth

    sent: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, **_kwargs):
            self._fs_connector_headers_provider = None

        def request(self, *_args, **kwargs):
            sent.append(kwargs)
            return {"result": {}}, 1

        def close(self):
            return None

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    row = _fixture_connector_row("http://public.example.test/mcp")
    row["auth_mode"] = "none"
    row["headers"] = {}

    with (
        mcp_connector_oauth.mcp_connector_rpc_context(
            object(),
            connector_row=row,
            timeout_seconds=2.0,
        ) as rpc_post,
        pytest.raises(ServiceValidationError, match="must use an https"),
    ):
        rpc_post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "lookup", "arguments": {}},
            },
            generated_transport_headers={"Mcp-Param-Api-Key": "per-call-secret"},
        )

    assert sent == []


@pytest.mark.parametrize(
    "query_key",
    [
        "api_key",
        "dbPassword",
        "dbPasswd",
        "client_secret",
        "access_token",
        "credential",
        "credentials",
        "bearer_auth",
    ],
)
def test_credential_query_cannot_cross_public_plain_http_send_path(
    monkeypatch,
    query_key: str,
) -> None:
    from core.application.extensions import url_safety
    from core.application.management.mcp_connector_http import _json_rpc_post_with_metadata

    opener = MagicMock()
    monkeypatch.setattr(url_safety, "open_pinned_url", opener)
    secret_value = "must-not-appear-in-errors"

    with pytest.raises(ServiceValidationError) as exc_info:
        _json_rpc_post_with_metadata(
            url=f"http://public.example.test/mcp?{query_key}={secret_value}",
            headers={},
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            timeout_seconds=1.0,
            expected_request_id=1,
        )

    assert "must not include credentials in query parameters" in str(exc_info.value)
    assert secret_value not in str(exc_info.value)
    assert secret_value not in str(getattr(exc_info.value, "payload", {}))
    opener.assert_not_called()


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize(
    "query_key",
    [
        "api_key",
        "client_secret",
        "access_token",
        "key",
        "auth",
        "access_key",
        "signature",
        "sig",
    ],
)
def test_connector_endpoint_rejects_credential_query_parameters_without_echoing_values(
    monkeypatch,
    scheme: str,
    query_key: str,
) -> None:
    from core.application.management import (
        mcp_connector_http,
    )

    monkeypatch.setattr(
        mcp_connector_http,
        "_classify_mcp_host",
        lambda _host, *, port: (False, False, False),
    )
    canary_value = "query-canary-unit-8a42d1"

    with pytest.raises(ServiceValidationError) as exc_info:
        mcp_connector_http._connector_rpc_url(
            {
                "transport": "streamable_http",
                "base_url": (f"{scheme}://public.example.test/mcp?{query_key}={canary_value}"),
                "auth_mode": "none",
                "headers": {},
            }
        )

    assert exc_info.value.payload == {
        "error": "mcp_endpoint_query_credentials_forbidden",
        "field": "base_url",
        "message": (
            "MCP connector endpoint must not include credentials in query parameters; "
            "use Headers or OAuth settings."
        ),
    }
    assert canary_value not in str(exc_info.value)
    assert canary_value not in str(exc_info.value.payload)


def test_connector_endpoint_allows_non_secret_query_parameters(monkeypatch) -> None:
    from core.application.management import (
        mcp_connector_http,
    )

    monkeypatch.setattr(
        mcp_connector_http,
        "_classify_mcp_host",
        lambda _host, *, port: (False, False, False),
    )
    endpoint = "https://public.example.test/mcp?region=eu&version=1"

    assert (
        mcp_connector_http._connector_rpc_url(
            {
                "transport": "streamable_http",
                "base_url": endpoint,
                "auth_mode": "none",
                "headers": {},
            }
        )
        == endpoint
    )


@pytest.mark.parametrize(
    ("payload", "expected_request_id"),
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, 1),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, None),
    ],
)
def test_legacy_session_id_cannot_cross_public_plain_http_post(
    payload,
    expected_request_id,
) -> None:
    from core.application.management.mcp_connector_http import _json_rpc_post_with_metadata

    with pytest.raises(ServiceValidationError, match="must use an https"):
        _json_rpc_post_with_metadata(
            url="http://public.example.test/mcp",
            headers={},
            payload=payload,
            timeout_seconds=1.0,
            session_id="server-issued-session-capability",
            expected_request_id=expected_request_id,
        )


def test_legacy_session_id_cannot_cross_public_plain_http_delete(monkeypatch) -> None:
    from core.application.extensions import url_safety
    from core.application.management.mcp_connector_client import StreamableHttpMcpSession

    opener = MagicMock()
    monkeypatch.setattr(url_safety, "open_pinned_url", opener)
    session = StreamableHttpMcpSession(
        url="http://public.example.test/mcp",
        timeout_seconds=1.0,
    )

    session._terminate_session(
        session_id="server-issued-session-capability",
        protocol_version="2025-03-26",
    )

    opener.assert_not_called()


@pytest.mark.parametrize(
    ("http_status", "error_code"),
    [(404, -32601), (400, -32022)],
)
def test_http_level_modern_discovery_errors_start_bounded_legacy_fallback(
    monkeypatch,
    http_status,
    error_code,
) -> None:
    marker = "private-4xx-body-marker"  # pragma: allowlist secret - redaction sentinel
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    with legacy_mcp_server(
        discovery_http_status=http_status,
        discovery_error_code=error_code,
        discovery_error_message=f"Method not found {marker}",
    ) as fixture:
        client = mcp_connector_client.StreamableHttpMcpClient(
            url=fixture.url,
            headers={},
            timeout_seconds=2.0,
        )
        response, _latency = client.request("tools/list", {})
        client.close()

    assert response["result"]["tools"]
    assert [
        record.payload["method"] for record in fixture.requests if record.payload is not None
    ] == ["server/discover", "initialize", "notifications/initialized", "tools/list"]
    assert marker not in str(response)
    assert fixture.violations == []


@pytest.mark.parametrize("http_status", [200, 400, 404, 405])
def test_http_unrecognized_json_rpc_error_falls_back_without_exposing_body(
    monkeypatch,
    caplog,
    http_status,
) -> None:
    marker = "unmarked-4xx-body-marker"  # pragma: allowlist secret - redaction sentinel
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    with legacy_mcp_server(
        discovery_http_status=http_status,
        discovery_error_code=-32000,
        discovery_error_message=marker,
    ) as fixture:
        client = mcp_connector_client.StreamableHttpMcpClient(
            url=fixture.url,
            headers={},
            timeout_seconds=2.0,
        )
        response, _latency = client.request("tools/list", {})
        client.close()

    assert [
        record.payload["method"] for record in fixture.requests if record.payload is not None
    ] == ["server/discover", "initialize", "notifications/initialized", "tools/list"]
    assert response["result"]["tools"]
    assert marker not in str(response)
    assert marker not in caplog.text


@pytest.mark.parametrize("http_status", [400, 404, 405])
@pytest.mark.parametrize("raw_body", ["", "not-json malformed-4xx-body-marker"])
def test_http_empty_or_malformed_discovery_body_falls_back_without_exposure(
    monkeypatch,
    caplog,
    http_status,
    raw_body,
) -> None:
    marker = "malformed-4xx-body-marker"  # pragma: allowlist secret - redaction sentinel
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    with legacy_mcp_server(
        discovery_http_status=http_status,
        discovery_raw_body=raw_body,
    ) as fixture:
        client = mcp_connector_client.StreamableHttpMcpClient(
            url=fixture.url,
            headers={},
            timeout_seconds=2.0,
        )
        response, _latency = client.request("tools/list", {})
        client.close()

    assert [
        record.payload["method"] for record in fixture.requests if record.payload is not None
    ] == ["server/discover", "initialize", "notifications/initialized", "tools/list"]
    assert response["result"]["tools"]
    assert marker not in str(response)
    assert marker not in caplog.text


@pytest.mark.parametrize("http_status", [200, 400, 404, 405])
@pytest.mark.parametrize("error_code", [-32020, -32021])
def test_http_recognized_modern_discovery_error_does_not_fallback(
    monkeypatch,
    error_code,
    http_status,
) -> None:
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    with legacy_mcp_server(
        discovery_http_status=http_status,
        discovery_error_code=error_code,
        discovery_error_message="recognized modern error",
    ) as fixture:
        client = mcp_connector_client.StreamableHttpMcpClient(
            url=fixture.url,
            headers={},
            timeout_seconds=2.0,
        )
        with pytest.raises(mcp_connector_protocol.McpProtocolError) as exc_info:
            client.request("tools/list", {})

    assert exc_info.value.code == str(error_code)
    assert exc_info.value.legacy_signal is False
    assert [
        record.payload["method"] for record in fixture.requests if record.payload is not None
    ] == ["server/discover"]


@pytest.mark.parametrize("http_status", [401, 403, 500, 503])
def test_modern_http_auth_or_outage_errors_do_not_fallback(monkeypatch, http_status) -> None:
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    with legacy_mcp_server(discovery_http_status=http_status) as fixture:
        client = mcp_connector_client.StreamableHttpMcpClient(
            url=fixture.url,
            headers={},
            timeout_seconds=2.0,
        )
        with pytest.raises(ServiceUnavailableError) as exc_info:
            client.request("tools/list", {})

    assert isinstance(exc_info.value.__cause__, urllib.error.HTTPError)
    assert exc_info.value.__cause__.code == http_status
    assert len(fixture.requests) == 1


def test_post_negotiation_http_400_malformed_body_does_not_start_legacy_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    with modern_mcp_server(
        tools_list_http_status=400,
        tools_list_raw_body="not-a-modern-error",
    ) as fixture:
        client = mcp_connector_client.StreamableHttpMcpClient(
            url=fixture.url,
            headers={},
            timeout_seconds=2.0,
        )
        with pytest.raises(ServiceUnavailableError, match="HTTP 400"):
            client.request("tools/list", {})
        client.close()

    assert [
        record.payload["method"] for record in fixture.requests if record.payload is not None
    ] == ["server/discover", "tools/list"]
    assert fixture.delete_count == 0
    assert client.era is mcp_connector_protocol.McpProtocolEra.MODERN


@pytest.mark.parametrize("error_code", [-32000, -32020, -32021, -32022])
def test_post_negotiation_json_rpc_errors_never_demote_modern_era(
    monkeypatch,
    error_code,
) -> None:
    methods: list[str] = []

    def _post(**kwargs):
        payload = kwargs["payload"]
        method = payload["method"]
        methods.append(method)
        if method == "server/discover":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "supportedVersions": ["2026-07-28"],
                        "capabilities": {},
                    },
                },
                {},
                1,
            )
        raise mcp_connector_protocol._mcp_protocol_error_from_json_rpc(
            {"code": error_code, "message": "post-negotiation error"}
        )

    monkeypatch.setattr(mcp_connector_client, "_json_rpc_post_with_metadata", _post)
    client = mcp_connector_client.StreamableHttpMcpClient(
        url="https://fs.example/mcp", headers={}, timeout_seconds=5.0
    )

    with pytest.raises(mcp_connector_protocol.McpProtocolError):
        client.request("tools/list", {})

    assert client.era is mcp_connector_protocol.McpProtocolEra.MODERN
    assert methods == ["server/discover", "tools/list"]


def test_modern_fixture_accepts_documented_custom_user_agent(monkeypatch) -> None:
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    custom_user_agent = "connector-client/9.0"
    with modern_mcp_server(expected_user_agent=custom_user_agent) as fixture:
        client = mcp_connector_client.StreamableHttpMcpClient(
            url=fixture.url,
            headers={"user-agent": custom_user_agent},
            timeout_seconds=2.0,
        )
        response, _ = client.request("tools/list", {})
        client.close()

    assert response["result"]["tools"]
    assert {
        record.headers.get("user-agent")
        for record in fixture.requests
        if record.payload is not None
    } == {custom_user_agent}
    assert fixture.violations == []


def test_remote_tool_identity_is_exact_through_projection_resolver_and_modern_wire(
    monkeypatch,
) -> None:
    from core.application.orchestration.tools import tool_resolver

    raw_name = " catalog.read "
    runtime_bl = SimpleNamespace(cursor=object())
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    with modern_mcp_server() as fixture:
        connector_row = _fixture_connector_row(
            fixture.url,
            display_name="Fixture MCP",
            namespace_prefix="fs",
            enabled=True,
            allowlisted_tools=[],
        )
        page_tools, _cursor = mcp_connector_common._extract_tools_page(
            {
                "result": {
                    "tools": [
                        {
                            "name": raw_name,
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                }
            }
        )
        [normalized_tool] = mcp_connector_common._normalize_discovered_tools(
            connector_row,
            page_tools,
        )
        projected_id = normalized_tool["projected_tool_id"]
        connector_row["discovered_tools_jsonb"] = [normalized_tool]
        connector_row["allowlisted_tools"] = [projected_id]
        service = McpConnectorCommandService(get_logger=lambda: runtime_bl)
        projected_manifest = service._build_projected_tool_manifest(
            connector_row,
            normalized_tool,
            enabled=True,
        )
        implementation_payload = projected_manifest["implementation"]
        implementation = SimpleNamespace(
            **implementation_payload,
            to_dict=lambda: dict(implementation_payload),
        )
        manifest = SimpleNamespace(
            tool_id=projected_id,
            implementation=implementation,
            config=projected_manifest["config"],
        )
        monkeypatch.setattr(
            McpConnectorStore,
            "get_connector_row",
            lambda _self, _conn, _connector_id, **_kwargs: dict(connector_row),
        )

        result = tool_resolver._invoke_remote_mcp_tool(
            manifest,
            function_name=projected_id,
            arguments={},
            runtime_context={
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
                "tenant_id": "tenant_alpha",
                "job_id": "job-exact-name",
                "logger": runtime_bl,
            },
        )

    assert normalized_tool["remote_tool_name"] == raw_name
    assert projected_manifest["implementation"]["remote_tool_name"] == raw_name
    assert raw_name not in projected_id
    assert result == {"name": raw_name}
    call_record = next(
        record
        for record in fixture.requests
        if record.payload and record.payload.get("method") == "tools/call"
    )
    assert call_record.payload["params"]["name"] == raw_name
    assert call_record.headers["mcp-name"].startswith("=?base64?")
    assert fixture.violations == []


def test_projected_protobuf_struct_output_contract_matches_protojson_whole_object() -> None:
    raw_tool = {
        "name": "get_spreadsheet",
        "inputSchema": {
            "type": "object",
            "properties": {"spreadsheetId": {"type": "string"}},
            "required": ["spreadsheetId"],
        },
        "outputSchema": deepcopy(_LIVE_PROTOBUF_STRUCT_OUTPUT_SCHEMA),
    }
    connector = {
        "connector_id": "mcp_sheets",
        "namespace_prefix": "sheets",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "display_name": "Sheets MCP",
    }

    [normalized] = mcp_connector_common._normalize_discovered_tools(connector, [raw_tool])
    projected = McpConnectorCommandService(
        get_logger=lambda: object()
    )._build_projected_tool_manifest(
        connector,
        normalized,
        enabled=True,
    )

    assert normalized["output_schema"] == _LIVE_PROTOBUF_STRUCT_OUTPUT_SCHEMA
    assert "mcp_output_projection" not in normalized
    assert raw_tool["outputSchema"] == _LIVE_PROTOBUF_STRUCT_OUTPUT_SCHEMA
    assert projected["interface"]["functions"][0]["returns"] == {
        "type": "object",
        "properties": {
            "result": {
                "type": "object",
                "description": _LIVE_PROTOBUF_STRUCT_OUTPUT_SCHEMA["description"],
                "additionalProperties": {},
            }
        },
        "required": ["result"],
    }
    assert projected["config"]["output_schema"] == _LIVE_PROTOBUF_STRUCT_OUTPUT_SCHEMA
    assert projected["config"]["mcp_output_projection"] == "whole_object_result"


def test_projected_real_fields_output_without_protobuf_signature_stays_typed() -> None:
    fields_schema = {
        "type": "object",
        "description": "A stable record envelope.",
        "properties": {
            "fields": {
                "type": "object",
                "description": "The record's named fields.",
                "additionalProperties": {"type": "string"},
            }
        },
        "required": ["fields"],
    }
    connector = {
        "connector_id": "mcp_records",
        "namespace_prefix": "records",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "display_name": "Records MCP",
    }
    [normalized] = mcp_connector_common._normalize_discovered_tools(
        connector,
        [
            {
                "name": "get_record",
                "inputSchema": {},
                "outputSchema": fields_schema,
                "mcp_output_projection": "whole_object_result",
            }
        ],
    )

    projected = McpConnectorCommandService(
        get_logger=lambda: object()
    )._build_projected_tool_manifest(
        connector,
        normalized,
        enabled=True,
    )

    assert projected["interface"]["functions"][0]["returns"] == fields_schema
    assert normalized["mcp_output_projection"] == "whole_object_result"
    assert "mcp_output_projection" not in projected["config"]


def test_projected_explicit_dynamic_object_uses_whole_result_contract() -> None:
    open_schema = {
        "type": "object",
        "description": "Dynamic labels keyed by provider-defined names.",
        "properties": {},
        "additionalProperties": {"type": "string"},
    }
    connector = {
        "connector_id": "mcp_labels",
        "namespace_prefix": "labels",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "display_name": "Labels MCP",
    }
    [normalized] = mcp_connector_common._normalize_discovered_tools(
        connector,
        [{"name": "list_labels", "inputSchema": {}, "outputSchema": open_schema}],
    )

    projected = McpConnectorCommandService(
        get_logger=lambda: object()
    )._build_projected_tool_manifest(
        connector,
        normalized,
        enabled=True,
    )

    assert projected["interface"]["functions"][0]["returns"] == {
        "type": "object",
        "properties": {"result": open_schema},
        "required": ["result"],
    }
    assert projected["config"]["mcp_output_projection"] == "whole_object_result"


@pytest.mark.parametrize(
    "stable_contract",
    [
        {"required": ["stable_output"]},
        {"allOf": [{"required": ["stable_output"]}]},
        {"$ref": "#/$defs/stableOutput", "$defs": {"stableOutput": {"type": "object"}}},
    ],
)
def test_projected_open_object_with_stable_contract_keywords_stays_typed(
    stable_contract: dict[str, Any],
) -> None:
    raw_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": {},
        **stable_contract,
    }

    projected_schema, projection = mcp_connector_common._project_mcp_output_contract(raw_schema)

    assert projected_schema == raw_schema
    assert projection == ""


def test_projected_protobuf_lookalike_with_composition_stays_typed() -> None:
    raw_schema = {
        **deepcopy(_LIVE_PROTOBUF_STRUCT_OUTPUT_SCHEMA),
        "allOf": [{"required": ["stable_output"]}],
    }

    projected_schema, projection = mcp_connector_common._project_mcp_output_contract(raw_schema)

    assert projected_schema == raw_schema
    assert projection == ""


def test_projected_typed_get_values_output_stays_top_level() -> None:
    typed_schema = {
        "type": "object",
        "properties": {
            "range": {"type": "string"},
            "values": {
                "type": "array",
                "items": {"type": "array", "items": {}},
            },
        },
    }
    connector = {
        "connector_id": "mcp_sheets",
        "namespace_prefix": "sheets",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "display_name": "Sheets MCP",
    }
    [normalized] = mcp_connector_common._normalize_discovered_tools(
        connector,
        [{"name": "get_values", "inputSchema": {}, "outputSchema": typed_schema}],
    )

    projected = McpConnectorCommandService(
        get_logger=lambda: object()
    )._build_projected_tool_manifest(
        connector,
        normalized,
        enabled=True,
    )

    assert projected["interface"]["functions"][0]["returns"] == typed_schema
    assert "mcp_output_projection" not in projected["config"]


def test_projected_ids_keep_safe_names_and_separate_edge_whitespace_identity() -> None:
    tools = mcp_connector_common._normalize_discovered_tools(
        {"connector_id": "mcp_fs", "namespace_prefix": "fs"},
        [
            {"name": "foo", "inputSchema": {}},
            {"name": " foo ", "inputSchema": {}},
        ],
    )

    by_name = {tool["remote_tool_name"]: tool["projected_tool_id"] for tool in tools}
    assert by_name["foo"] == "fs.foo"
    assert by_name[" foo "] != by_name["foo"]
    assert by_name[" foo "].startswith("fs.remote_unsafe_")
    assert " foo " not in by_name[" foo "]


def test_reserved_projected_alias_name_cannot_collide_and_invokes_exact_identity(
    monkeypatch,
) -> None:
    from core.application.orchestration.tools import tool_resolver

    unsafe_name = " unsafe "
    unsafe_projected_id = mcp_connector_common._projected_tool_id_for_values(
        connector_id="mcp_fs",
        namespace_prefix="fs",
        remote_tool_name=unsafe_name,
    )
    reserved_remote_name = unsafe_projected_id.removeprefix("fs.")
    source_tools = [
        {"name": unsafe_name, "inputSchema": {"type": "object", "properties": {}}},
        {
            "name": reserved_remote_name,
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    connector_identity = {"connector_id": "mcp_fs", "namespace_prefix": "fs"}
    forward = mcp_connector_common._normalize_discovered_tools(
        connector_identity,
        source_tools,
    )
    reverse = mcp_connector_common._normalize_discovered_tools(
        connector_identity,
        list(reversed(source_tools)),
    )
    forward_ids = {tool["remote_tool_name"]: tool["projected_tool_id"] for tool in forward}
    reverse_ids = {tool["remote_tool_name"]: tool["projected_tool_id"] for tool in reverse}

    assert forward_ids == reverse_ids
    assert len(set(forward_ids.values())) == 2
    assert forward_ids[unsafe_name] == unsafe_projected_id
    assert forward_ids[reserved_remote_name].startswith("fs.remote_unsafe_r_")

    runtime_bl = SimpleNamespace(cursor=object())
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    with modern_mcp_server() as fixture:
        connector_row = _fixture_connector_row(
            fixture.url,
            display_name="Fixture MCP",
            namespace_prefix="fs",
            enabled=True,
            allowlisted_tools=list(forward_ids.values()),
        )
        connector_row["discovered_tools_jsonb"] = forward
        service = McpConnectorCommandService(get_logger=lambda: runtime_bl)
        manifests: list[SimpleNamespace] = []
        for tool in forward:
            projected = service._build_projected_tool_manifest(
                connector_row,
                tool,
                enabled=True,
            )
            implementation_payload = projected["implementation"]
            manifests.append(
                SimpleNamespace(
                    tool_id=tool["projected_tool_id"],
                    implementation=SimpleNamespace(
                        **implementation_payload,
                        to_dict=lambda payload=implementation_payload: dict(payload),
                    ),
                    config=projected["config"],
                )
            )
        monkeypatch.setattr(
            McpConnectorStore,
            "get_connector_row",
            lambda _self, _conn, _connector_id, **_kwargs: dict(connector_row),
        )

        results = [
            tool_resolver._invoke_remote_mcp_tool(
                manifest,
                function_name=manifest.tool_id,
                arguments={},
                runtime_context={
                    "account_id": "acc_alpha",
                    "project_id": "project_alpha",
                    "tenant_id": "tenant_alpha",
                    "job_id": "job-alias-collision",
                    "logger": runtime_bl,
                },
            )
            for manifest in manifests
        ]

    assert [result["name"] for result in results] == [unsafe_name, reserved_remote_name]
    assert [
        record.payload["params"]["name"]
        for record in fixture.requests
        if record.payload and record.payload.get("method") == "tools/call"
    ] == [unsafe_name, reserved_remote_name]
    assert fixture.violations == []


def test_lifecycle_logs_are_structured_and_exclude_sensitive_values(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:

    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    argument_secret = "argument-secret-value"  # pragma: allowlist secret - redaction sentinel
    authorization_secret = (
        "Bearer authorization-secret-value"  # pragma: allowlist secret - redaction sentinel
    )
    result_secret = "result-secret-value"  # pragma: allowlist secret - redaction sentinel

    with (
        legacy_mcp_server(
            expire_session_once=True,
            accepted_authorizations=[authorization_secret],
            result_text=result_secret,
        ) as fixture,
        caplog.at_level(
            logging.INFO,
            logger="core.application.management.mcp_connector_observability",
        ),
    ):
        row = _fixture_connector_row(
            fixture.url,
            auth_mode="bearer",
            headers={"Authorization": authorization_secret},
        )
        monkeypatch.setattr(
            mcp_connector_oauth_repository.McpConnectorStore,
            "get_connector_row",
            lambda *_args, **_kwargs: row,
        )
        with mcp_connector_client.mcp_execution_scope("acc_alpha", "job-observed"):
            _fixture_rpc(
                row,
                job_id="job-observed",
                method="tools/list",
                params={},
            )
            _fixture_rpc(
                row,
                job_id="job-observed",
                method="tools/call",
                params={
                    "name": "browser_navigate",
                    "arguments": {"url": f"https://example.test/{argument_secret}"},
                },
            )
            _fixture_rpc(
                row,
                job_id="job-observed",
                method="tools/call",
                params={"name": "browser_click", "arguments": {"selector": "#observed"}},
            )

    lifecycle_records = [
        record
        for record in caplog.records
        if record.getMessage()
        in {
            "MCP protocol era selected",
            "MCP legacy session opened",
            "MCP legacy session reinitialized",
            "MCP legacy session closed",
        }
    ]
    assert [record.getMessage() for record in lifecycle_records] == [
        "MCP protocol era selected",
        "MCP legacy session opened",
        "MCP legacy session reinitialized",
        "MCP legacy session closed",
    ]
    assert all(record.connector_id == "mcp_browser_fixture" for record in lifecycle_records)
    assert all(record.job_id == "job-observed" for record in lifecycle_records)
    assert all(record.protocol_era == "legacy" for record in lifecycle_records)
    assert [record.protocol_version for record in lifecycle_records] == [
        "",
        "2025-11-25",
        "2025-11-25",
        "2025-11-25",
    ]
    assert [record.session_event for record in lifecycle_records[1:]] == [
        "open",
        "reinitialize",
        "close",
    ]
    captured = "\n".join(repr(sorted(record.__dict__.items())) for record in lifecycle_records)
    for sensitive in (
        argument_secret,
        authorization_secret,
        result_secret,
        *(session.session_id for session in fixture.sessions),
    ):
        assert sensitive not in captured


@contextmanager
def _mcp_connector_api_client():
    store = _InMemoryMcpConnectorStore()
    store.vault = _FakeHeaderSecretVault()
    registry_manager = _FakeRegistryManager()
    app = FastAPI()
    bl = MagicMock()

    def _request_scope(request: Request, **kwargs):
        _ = kwargs
        return ScopeContext(
            account_id=str(request.headers.get("x-test-account-id") or "acc_default").strip(),
            project_id=str(
                request.query_params.get("project_id") or "00000000-0000-0000-0000-000000000001"
            ).strip(),
            tenant_id=str(request.headers.get("x-test-tenant-id") or "tenant_alpha").strip(),
            actor_id=str(request.headers.get("x-test-actor") or "api").strip(),
            principal_type=str(request.headers.get("x-test-role") or "admin").strip().lower(),
        )

    app.include_router(
        create_integrations_connectors_router(
            get_logger=lambda: bl,
            get_extension_install_store=lambda: MagicMock(),
            get_extension_runtime_store=lambda: MagicMock(),
            extension_query_service=lambda: MagicMock(),
            request_scope=_request_scope,
            project_query_service=lambda: MagicMock(),
            project_command_service=lambda: MagicMock(),
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
            err_not_found="Not found",
        )
    )

    with ExitStack() as stack:
        stack.enter_context(store.vault.patch())
        stack.enter_context(
            patch.object(
                McpConnectorCommandService,
                "_insert_connector",
                lambda self, bl, row: store.insert(row),
            )
        )
        stack.enter_context(
            patch.object(
                McpConnectorCommandService,
                "_get_connector_row",
                lambda self, bl, connector_id, account_id=None, project_id=None, tenant_id=None, **_kwargs: (
                    store.get(
                        connector_id,
                        account_id=account_id,
                        project_id=project_id,
                        tenant_id=tenant_id,
                    )
                ),
            )
        )
        stack.enter_context(
            patch.object(
                McpConnectorCommandService,
                "_list_connector_rows",
                lambda self, bl, account_id=None, project_id=None, tenant_id=None, **_kwargs: (
                    store.list(
                        account_id=account_id,
                        project_id=project_id,
                        tenant_id=tenant_id,
                    )
                ),
            )
        )
        stack.enter_context(
            patch.object(
                McpConnectorCommandService,
                "_update_connector_row",
                lambda self, bl, connector_id, updates, account_id=None, project_id=None, tenant_id=None, **_kwargs: (
                    store.update(
                        connector_id,
                        updates,
                        account_id=account_id,
                        project_id=project_id,
                        tenant_id=tenant_id,
                    )
                ),
            )
        )
        stack.enter_context(
            patch.object(
                McpConnectorCommandService,
                "_soft_delete_connector_row",
                lambda self, bl, connector_id, account_id=None, deleted_by="system", child_tool_ids=None, **_kwargs: (
                    store.soft_delete(
                        connector_id,
                        account_id=account_id,
                        deleted_by=deleted_by,
                        child_tool_ids=child_tool_ids,
                    )
                ),
            )
        )
        stack.enter_context(
            patch.object(
                McpConnectorStore,
                "get_connector_row",
                lambda self, bl, connector_id, account_id=None, project_id=None, tenant_id=None, **_kwargs: (
                    store.get(
                        connector_id,
                        account_id=account_id,
                        project_id=project_id,
                        tenant_id=tenant_id,
                    )
                ),
            )
        )
        stack.enter_context(
            patch.object(
                McpConnectorStore,
                "list_connector_rows",
                lambda self, bl, account_id=None, project_id=None, tenant_id=None, **_kwargs: (
                    store.list(
                        account_id=account_id,
                        project_id=project_id,
                        tenant_id=tenant_id,
                    )
                ),
            )
        )
        stack.enter_context(
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry_manager,
            )
        )
        with TestClient(app, raise_server_exceptions=False) as client:

            class _ScopedClient:
                def __init__(self, inner):
                    self._inner = inner

                @staticmethod
                def _path(path: str) -> str:
                    if path.startswith("/api/mcp") and "project_id=" not in path:
                        return f"{path}?project_id=00000000-0000-0000-0000-000000000001"
                    return path

                def get(self, path, *args, **kwargs):
                    return self._inner.get(self._path(path), *args, **kwargs)

                def post(self, path, *args, **kwargs):
                    return self._inner.post(self._path(path), *args, **kwargs)

                def put(self, path, *args, **kwargs):
                    return self._inner.put(self._path(path), *args, **kwargs)

                def patch(self, path, *args, **kwargs):
                    return self._inner.patch(self._path(path), *args, **kwargs)

                def delete(self, path, *args, **kwargs):
                    return self._inner.delete(self._path(path), *args, **kwargs)

            yield _ScopedClient(client), store


def test_mcp_provider_error_callback_returns_to_connector_without_provider_detail() -> None:
    return_path = "/registry/mcp/mcp_alpha?project_id=project_alpha"
    with (
        _mcp_connector_api_client() as (client, _store),
        patch("core.application.auth.oauth_client.oauth_state_type", return_value="mcp_connector"),
        patch(
            "core.application.auth.mcp_connector_oauth.mcp_oauth_callback_context",
            return_value={"return_path": return_path},
        ),
        patch(
            "core.application.auth.mcp_connector_oauth.handle_mcp_oauth_provider_error",
            return_value={"return_path": return_path},
        ) as provider_handler,
        patch.object(McpConnectorCommandService, "sync_connector") as sync_connector,
    ):
        response = client.get(
            "/api/integrations/oauth/callback?state=opaque&error=access_denied&error_description=must-not-leak",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "/registry/mcp/mcp_alpha?project_id=project_alpha&oauth=error"
    )
    assert "oauth_failed" in response.headers["location"]
    assert "access_denied" not in response.headers["location"]
    assert "must-not-leak" not in response.headers["location"]
    provider_handler.assert_called_once()
    sync_connector.assert_not_called()


def test_mcp_oauth_callback_syncs_once_with_validated_state_scope() -> None:
    return_path = "/registry/mcp/mcp_alpha?project_id=project_alpha"
    callback_context = {
        "return_path": return_path,
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
    }
    with (
        _mcp_connector_api_client() as (client, _store),
        patch("core.application.auth.oauth_client.oauth_state_type", return_value="mcp_connector"),
        patch(
            "core.application.auth.mcp_connector_oauth.mcp_oauth_callback_context",
            return_value=callback_context,
        ),
        patch(
            "core.application.auth.mcp_connector_oauth.handle_mcp_oauth_callback",
            return_value={"return_path": return_path},
        ),
        patch.object(McpConnectorCommandService, "sync_connector") as sync_connector,
    ):
        response = client.get(
            "/api/integrations/oauth/callback?state=signed-state&code=opaque-code",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/registry/mcp/mcp_alpha?project_id=project_alpha&oauth=success&sync=success"
    )
    sync_connector.assert_called_once()
    assert sync_connector.call_args.args == ("mcp_alpha",)
    scope = sync_connector.call_args.kwargs["scope"]
    assert scope == ScopeContext(
        account_id="acc_alpha",
        project_id="project_alpha",
        tenant_id="tenant_alpha",
        actor_id="mcp_oauth_callback",
        principal_type="system_actor",
        auth_source="oauth_callback",
        allowed_project_ids=frozenset({"project_alpha"}),
    )


def test_mcp_oauth_callback_keeps_oauth_success_when_sync_fails() -> None:
    return_path = "/registry/mcp/mcp_alpha?project_id=project_alpha"
    callback_context = {
        "return_path": return_path,
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
    }
    sensitive_detail = "provider-response-must-not-leak"
    with (
        _mcp_connector_api_client() as (client, _store),
        patch("core.application.auth.oauth_client.oauth_state_type", return_value="mcp_connector"),
        patch(
            "core.application.auth.mcp_connector_oauth.mcp_oauth_callback_context",
            return_value=callback_context,
        ),
        patch(
            "core.application.auth.mcp_connector_oauth.handle_mcp_oauth_callback",
            return_value={"return_path": return_path, "status": "connected"},
        ) as callback_handler,
        patch.object(
            McpConnectorCommandService,
            "sync_connector",
            side_effect=ServiceUnavailableError(sensitive_detail),
        ) as sync_connector,
    ):
        response = client.get(
            "/api/integrations/oauth/callback?state=signed-state&code=opaque-code",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/registry/mcp/mcp_alpha?project_id=project_alpha&oauth=success&sync=failed"
    )
    assert sensitive_detail not in response.headers["location"]
    callback_handler.assert_called_once()
    sync_connector.assert_called_once()


def test_mcp_token_exchange_failure_returns_to_connector_detail() -> None:
    return_path = "/registry/mcp/mcp_alpha?project_id=project_alpha"
    with (
        _mcp_connector_api_client() as (client, _store),
        patch("core.application.auth.oauth_client.oauth_state_type", return_value="mcp_connector"),
        patch(
            "core.application.auth.mcp_connector_oauth.mcp_oauth_callback_context",
            return_value={"return_path": return_path},
        ),
        patch(
            "core.application.auth.mcp_connector_oauth.handle_mcp_oauth_callback",
            side_effect=OAuthConnectionError("token endpoint rejected must-not-leak"),
        ),
        patch.object(McpConnectorCommandService, "sync_connector") as sync_connector,
    ):
        response = client.get(
            "/api/integrations/oauth/callback?state=opaque&code=opaque-code",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "/registry/mcp/mcp_alpha?project_id=project_alpha&oauth=error"
    )
    assert "oauth_failed" in response.headers["location"]
    assert "must-not-leak" not in response.headers["location"]
    sync_connector.assert_not_called()


@pytest.mark.parametrize("failure", ["invalid", "replayed", "lineage-mismatch"])
def test_mcp_oauth_callback_context_failure_never_syncs(failure: str) -> None:
    with (
        _mcp_connector_api_client() as (client, _store),
        patch("core.application.auth.oauth_client.oauth_state_type", return_value="mcp_connector"),
        patch(
            "core.application.auth.mcp_connector_oauth.mcp_oauth_callback_context",
            side_effect=OAuthConnectionError(failure),
        ),
        patch.object(McpConnectorCommandService, "sync_connector") as sync_connector,
    ):
        response = client.get(
            "/api/integrations/oauth/callback?state=untrusted-state&code=opaque-code",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/settings/integrations?oauth=error&reason=oauth_failed"
    )
    assert failure not in response.headers["location"]
    sync_connector.assert_not_called()


class TestMcpConnectorModel:
    def test_connector_id_auto_generated(self):
        c = McpConnector(
            account_id="acc_default",
            project_id="project_default",
            tenant_id="tenant_default",
            display_name="Test MCP",
            base_url="http://localhost:8200/mcp",
        )
        assert c.connector_id.startswith("mcp_")

    def test_default_values(self):
        c = McpConnector(
            account_id="acc_default",
            project_id="project_default",
            tenant_id="tenant_default",
            display_name="Test",
            base_url="http://localhost:8200",
        )
        assert c.transport == "streamable_http"
        assert c.auth_mode == McpConnectorAuthMode.none
        assert c.status == McpConnectorStatus.unchecked
        assert c.enabled is True
        assert c.last_sync_status == "never"

    def test_auth_modes(self):
        for mode in ("none", "bearer", "api_key", "oauth2"):
            c = McpConnector(
                account_id="acc_default",
                project_id="project_default",
                tenant_id="tenant_default",
                display_name="Test",
                base_url="http://localhost",
                auth_mode=mode,
            )
            assert c.auth_mode == mode

    def test_allowlisted_tools(self):
        c = McpConnector(
            account_id="acc_default",
            project_id="project_default",
            tenant_id="tenant_default",
            display_name="Test",
            base_url="http://localhost",
            allowlisted_tools=["search", "query"],
            namespace_prefix="fs",
        )
        assert c.allowlisted_tools == ["search", "query"]
        assert c.namespace_prefix == "fs"


class TestMcpConnectorCommandService:
    @pytest.mark.parametrize(
        ("config", "field"),
        [
            ({"requested_scopes": "files:read"}, "requested_scopes"),
            ({"refresh_token_required": "yes"}, "refresh_token_required"),
        ],
    )
    def test_oauth_config_errors_address_the_exact_form_field(self, config, field):
        from core.application.management import mcp_connector_oauth_contract

        with pytest.raises(ServiceValidationError) as exc_info:
            mcp_connector_oauth_contract._oauth_metadata_from_config(config)

        assert exc_info.value.payload["field"] == field

    def _make_service(self):
        bl = MagicMock()
        bl.cursor.return_value.execute = MagicMock()
        bl.cursor.return_value.fetchone = MagicMock(return_value=None)
        bl.cursor.return_value.fetchall = MagicMock(return_value=[])
        return McpConnectorCommandService(get_logger=lambda: bl), bl

    def _configured_sync_service(
        self,
        *,
        discovered_tools: list[dict[str, Any]] | None = None,
        auth_mode: str = "none",
    ):
        svc, _bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "display_name": "Flow Steward MCP",
            "description": "",
            "transport": "streamable_http",
            "base_url": "https://fs.example/mcp",
            "auth_mode": auth_mode,
            "headers": {},
            "allowlisted_tools": [],
            "namespace_prefix": "fs",
            "status": "unchecked",
            "discovered_tools_jsonb": list(discovered_tools or []),
            "last_synced_at": None,
            "last_sync_status": "never",
            "last_sync_error": "",
            "enabled": True,
            "created_at": "2026-05-20T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z",
        }
        captured_updates: list[dict[str, Any]] = []

        def _get_row(_bl, _connector_id, account_id=None, **_kwargs):
            if account_id and account_id != row["account_id"]:
                return None
            return dict(row)

        def _update_row(_bl, _connector_id, updates, account_id=None, **_kwargs):
            assert account_id == "acc_alpha"
            captured_updates.append(dict(updates))
            row.update(dict(updates))

        svc._get_connector_row = MagicMock(side_effect=_get_row)
        svc._update_connector_row = MagicMock(side_effect=_update_row)
        return svc, scope, row, captured_updates

    def test_extract_tools_page_accepts_empty_catalog_and_preserves_cursor(self):
        tools, next_cursor = mcp_connector_common._extract_tools_page(
            {"result": {"tools": [], "nextCursor": "  page-2  "}}
        )

        assert tools == []
        assert next_cursor == "  page-2  "

    def test_extract_tools_page_distinguishes_absent_and_empty_cursor(self):
        _tools, absent_cursor = mcp_connector_common._extract_tools_page({"result": {"tools": []}})
        _tools, empty_cursor = mcp_connector_common._extract_tools_page(
            {"result": {"tools": [], "nextCursor": ""}}
        )

        assert absent_cursor is None
        assert empty_cursor == ""

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"result": None},
            {"result": {}},
            {"result": {"tools": {}}},
            {"result": {"tools": ["not-a-tool"]}},
            {"result": {"tools": [], "nextCursor": {"cursor": "bad"}}},
        ],
    )
    def test_extract_tools_page_rejects_malformed_pages(self, payload):
        with pytest.raises(ServiceValidationError, match="malformed"):
            mcp_connector_common._extract_tools_page(payload)

    def test_normalized_discovery_preserves_compatible_remote_fields_and_refs(self):
        source = {
            "name": "catalog.read",
            "title": "Catalog Read",
            "description": "Read catalog rows.",
            "inputSchema": {
                "$ref": "https://schemas.example.test/catalog-input.json",
                "x-input-extension": {"mode": "strict"},
            },
            "outputSchema": {
                "$ref": "https://schemas.example.test/catalog-output.json",
            },
            "annotations": {"readOnlyHint": True},
            "icons": [{"src": "https://cdn.example.test/catalog.png"}],
            "x-server-extension": {"nested": [1, 2, 3]},
        }

        [normalized] = mcp_connector_common._normalize_discovered_tools(
            {"connector_id": "mcp_fs", "namespace_prefix": "fs"},
            [source],
        )

        assert normalized["remote_tool_name"] == "catalog.read"
        assert normalized["projected_tool_id"] == "fs.catalog.read"
        assert normalized["display_name"] == "Catalog Read"
        assert normalized["inputSchema"] == source["inputSchema"]
        assert normalized["input_schema"] == source["inputSchema"]
        assert normalized["outputSchema"] == source["outputSchema"]
        assert normalized["output_schema"] == source["outputSchema"]
        assert normalized["annotations"] == {"readOnlyHint": True}
        assert normalized["icons"] == source["icons"]
        assert normalized["x-server-extension"] == source["x-server-extension"]
        assert normalized["inputSchema"] is not source["inputSchema"]
        assert normalized["x-server-extension"] is not source["x-server-extension"]

    def test_raw_discovery_collisions_cannot_override_canonical_identity_or_schemas(self):
        raw_tools = [
            {
                "name": "catalog.first",
                "title": "Catalog First",
                "remote_tool_name": "shared.attacker.value",
                "projected_tool_id": "attacker.projected",
                "display_name": "Attacker Display",
                "inputSchema": {"type": "object", "properties": {"first": {}}},
                "input_schema": {"type": "string", "description": "attacker alias"},
                "outputSchema": {"type": "object", "properties": {"firstResult": {}}},
                "output_schema": {"type": "string", "description": "attacker alias"},
                "x-server-extension": {"preserved": "first"},
            },
            {
                "name": "catalog.second",
                "title": "Catalog Second",
                "remote_tool_name": "shared.attacker.value",
                "projected_tool_id": "attacker.projected",
                "display_name": "Attacker Display",
                "inputSchema": {"type": "object", "properties": {"second": {}}},
                "input_schema": {"type": "integer"},
                "outputSchema": {"type": "array", "items": {"type": "string"}},
                "output_schema": {"type": "boolean"},
                "x-server-extension": {"preserved": "second"},
            },
        ]

        normalized = mcp_connector_common._normalize_discovered_tools(
            {"connector_id": "mcp_fs", "namespace_prefix": "fs"},
            raw_tools,
        )

        assert [tool["remote_tool_name"] for tool in normalized] == [
            "catalog.first",
            "catalog.second",
        ]
        assert [tool["projected_tool_id"] for tool in normalized] == [
            "fs.catalog.first",
            "fs.catalog.second",
        ]
        assert [tool["display_name"] for tool in normalized] == [
            "Catalog First",
            "Catalog Second",
        ]
        assert normalized[0]["input_schema"] == raw_tools[0]["inputSchema"]
        assert normalized[0]["output_schema"] == raw_tools[0]["outputSchema"]
        assert normalized[1]["input_schema"] == raw_tools[1]["inputSchema"]
        assert normalized[1]["output_schema"] == raw_tools[1]["outputSchema"]
        assert [tool["x-server-extension"]["preserved"] for tool in normalized] == [
            "first",
            "second",
        ]

    def test_persisted_normalized_discovery_uses_snake_case_compatibility_shape(self):
        persisted = {
            "remote_tool_name": "catalog.persisted",
            "projected_tool_id": "legacy.projected",
            "display_name": "Persisted Catalog",
            "description": "Persisted record without a canonical MCP name field.",
            "input_schema": {"type": "object", "properties": {"persisted": {}}},
            "output_schema": {"type": "array", "items": {"type": "number"}},
            "x-persisted-extension": True,
        }

        [normalized] = mcp_connector_common._normalize_discovered_tools(
            {"connector_id": "mcp_fs", "namespace_prefix": "fs"},
            [persisted],
        )

        assert normalized["remote_tool_name"] == "catalog.persisted"
        assert normalized["projected_tool_id"] == "fs.catalog.persisted"
        assert normalized["display_name"] == "Persisted Catalog"
        assert normalized["input_schema"] == persisted["input_schema"]
        assert normalized["output_schema"] == persisted["output_schema"]
        assert normalized["x-persisted-extension"] is True

    def test_sync_aggregates_pages_in_order_and_deduplicates_by_remote_name(self):
        svc, scope, row, _updates = self._configured_sync_service(auth_mode="oauth2")
        requests: list[dict[str, Any]] = []
        entries: list[dict[str, Any]] = []
        responses = [
            {
                "result": {
                    "tools": [
                        {"name": "catalog.first", "description": "first copy"},
                        {"name": "catalog.shared", "description": "keep this copy"},
                    ],
                    "nextCursor": "cursor-2",
                }
            },
            {
                "result": {
                    "tools": [
                        {"name": "catalog.shared", "description": "discard duplicate"},
                        {"name": "catalog.last"},
                    ],
                    "nextCursor": "cursor-3",
                }
            },
            {"result": {"tools": [{"name": "catalog.final"}]}},
        ]

        with patch(
            "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
            side_effect=_rpc_context_from_responses(
                responses,
                requests=requests,
                entries=entries,
            ),
        ):
            result = svc.sync_connector("mcp_fs", scope=scope)

        assert [tool["remote_tool_name"] for tool in result.discovered_tools_jsonb] == [
            "catalog.first",
            "catalog.shared",
            "catalog.last",
            "catalog.final",
        ]
        assert row["discovered_tools_jsonb"][1]["description"] == "keep this copy"
        assert [request["params"] for request in requests] == [
            {},
            {"cursor": "cursor-2"},
            {"cursor": "cursor-3"},
        ]
        assert len(entries) == 1
        assert entries[0]["connector_row"]["connector_id"] == "mcp_fs"
        assert entries[0]["timeout_seconds"] == 30.0
        assert entries[0]["runtime_context"] is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("transport", "sse"),
            ("base_url", "https://changed.example/mcp"),
            ("auth_mode", "bearer"),
            (
                "header_secret_refs_jsonb",
                {"refs": {"Authorization": "mcp_connector:mcp_fs:header:changed:0"}},
            ),
            ("oauth_metadata_jsonb", {"requested_scopes": ["changed:scope"]}),
            (
                "oauth_secret_refs_jsonb",
                {"refs": {"_oauth_client_id": "ref:changed-client-id"}},
            ),
            ("oauth_credential_revision", 2),
            ("namespace_prefix", "changed-concurrently"),
            ("allowlisted_tools", ["fs.catalog.list"]),
        ],
    )
    def test_sync_aborts_without_projection_when_configuration_changes_after_remote_read(
        self,
        field,
        value,
    ):
        svc, scope, row, updates = self._configured_sync_service()
        projection = MagicMock()
        svc._sync_projected_registry_tools = projection

        @contextmanager
        def _rpc_context(*_args, **_kwargs):
            def _post(_payload):
                row[field] = value
                row["updated_at"] = "2026-05-20T00:00:01Z"
                return {"result": {"tools": [{"name": "catalog.list"}]}}, 1

            yield _post

        with (
            patch(
                "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
                _rpc_context,
            ),
            pytest.raises(ServiceConflictError, match="changed while sync was in progress"),
        ):
            svc.sync_connector("mcp_fs", scope=scope)

        projection.assert_not_called()
        assert updates == []

    def test_sync_accepts_oauth_token_refresh_inside_real_rpc_context(self, monkeypatch):
        from core.application.auth import mcp_connector_oauth

        svc, scope, row, updates = self._configured_sync_service(auth_mode="oauth2")
        row.update(
            {
                "auth_status": "connected",
                "oauth_credential_revision": 1,
                "oauth_connected_identity": "operator@example.test",
                "oauth_metadata_jsonb": {
                    "requested_scopes": ["tools:read"],
                    "authorization_parameters": {"prompt": "consent"},
                    "refresh_token_required": True,
                    "discovery": {
                        "resource": "https://fs.example/mcp",
                        "issuer": "https://auth.example.test",
                        "authorization_server": {
                            "issuer": "https://auth.example.test",
                            "token_endpoint": "https://auth.example.test/token",
                        },
                    },
                    "registration": {
                        "mode": "manual",
                        "token_endpoint_auth_method": "none",
                    },
                    "token": {
                        "connector_endpoint": "https://fs.example/mcp",
                        "expires_at": 1.0,
                    },
                },
                "oauth_secret_refs_jsonb": {
                    "refs": {
                        "_oauth_access_token": "ref:access",
                        "_oauth_refresh_token": "ref:refresh",
                        "_oauth_client_id": "ref:client-id",
                        "_oauth_expires_at": "ref:expires-at",
                        "_oauth_granted_scopes": "ref:granted-scopes",
                        "_oauth_token_type": "ref:token-type",
                    }
                },
            }
        )
        secret_values = {
            "_oauth_access_token": "expired-access",
            "_oauth_refresh_token": "stored-refresh",
            "_oauth_client_id": "client-alpha",
            "_oauth_expires_at": "1",
            "_oauth_granted_scopes": '["tools:read"]',
            "_oauth_token_type": "Bearer",
        }

        @contextmanager
        def _refresh_lock(_conn, *, lock_name):
            assert lock_name == "mcp_connector_oauth_refresh:acc_alpha:mcp_fs"
            yield True, _conn

        def _save_secrets(_conn, *, secrets_to_save, **_kwargs):
            secret_values.update(secrets_to_save)
            return {
                "refs": {
                    name: f"ref:{name.removeprefix('_oauth_').replace('_', '-')}"
                    for name in secrets_to_save
                }
            }

        def _update_oauth(_conn, _connector_id, *, updates, **_kwargs):
            row.update(updates)
            row["updated_at"] = "2026-05-20T00:00:01Z"

        class _RefreshingClient:
            def __init__(self, *, headers_provider, **_kwargs):
                self._headers_provider = headers_provider
                self._fs_connector_headers_provider = headers_provider

            def request(self, method, _params, **_kwargs):
                assert method == "tools/list"
                assert self._headers_provider()["Authorization"] == "Bearer refreshed-access"
                return {"result": {"tools": [{"name": "catalog.list"}]}}, 1

            def close(self):
                return None

        monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _RefreshingClient)
        monkeypatch.setattr(
            mcp_connector_oauth_repository.McpConnectorStore,
            "get_connector_row",
            lambda *_args, **_kwargs: dict(row),
        )
        monkeypatch.setattr(
            mcp_connector_oauth_tokens,
            "_resolve_connector_oauth_secrets",
            lambda *_args, **_kwargs: dict(secret_values),
        )
        monkeypatch.setattr(
            mcp_connector_oauth_tokens,
            "_require_oauth_credentials_bound_to_connector",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            mcp_connector_oauth.mcp_connector_oauth_store,
            "refresh_advisory_lock",
            _refresh_lock,
        )
        monkeypatch.setattr(
            mcp_connector_oauth_provider,
            "_locked_connector_row",
            lambda *_args, **_kwargs: dict(row),
        )
        monkeypatch.setattr(
            mcp_connector_oauth_repository,
            "_locked_connector_row",
            mcp_connector_oauth_provider._locked_connector_row,
        )
        monkeypatch.setattr(
            mcp_connector_oauth_rpc,
            "_locked_connector_row",
            mcp_connector_oauth_provider._locked_connector_row,
        )
        monkeypatch.setattr(
            mcp_connector_oauth_tokens,
            "_locked_connector_row",
            mcp_connector_oauth_provider._locked_connector_row,
        )
        monkeypatch.setattr(
            mcp_connector_oauth_repository,
            "_latest_connector_row",
            lambda *_args, **_kwargs: dict(row),
        )
        monkeypatch.setattr(
            mcp_connector_oauth_tokens,
            "_latest_connector_row",
            mcp_connector_oauth_repository._latest_connector_row,
        )
        monkeypatch.setattr(
            mcp_connector_oauth_tokens,
            "_exchange_token",
            lambda **_kwargs: {
                "access_token": "refreshed-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
                "scope": "tools:read",
                "token_type": "Bearer",
            },
        )
        monkeypatch.setattr(
            mcp_connector_oauth_tokens, "_save_connector_oauth_secrets", _save_secrets
        )
        monkeypatch.setattr(mcp_connector_oauth_provider, "_update_connector_oauth", _update_oauth)
        monkeypatch.setattr(
            mcp_connector_oauth_repository,
            "_update_connector_oauth",
            mcp_connector_oauth_provider._update_connector_oauth,
        )
        monkeypatch.setattr(
            mcp_connector_oauth_rpc,
            "_update_connector_oauth",
            mcp_connector_oauth_provider._update_connector_oauth,
        )
        monkeypatch.setattr(
            mcp_connector_oauth_tokens,
            "_update_connector_oauth",
            mcp_connector_oauth_provider._update_connector_oauth,
        )

        result = svc.sync_connector("mcp_fs", scope=scope)

        assert result.last_sync_status == "success"
        assert result.discovered_tools_jsonb[0]["remote_tool_name"] == "catalog.list"
        assert secret_values["_oauth_access_token"] == "refreshed-access"
        assert secret_values["_oauth_refresh_token"] == "rotated-refresh"
        assert row["oauth_credential_revision"] == 1
        assert row["auth_status"] == "connected"
        assert updates[-1]["last_sync_status"] == "success"

    def test_sync_paginates_over_real_modern_http_and_logs_safe_count(
        self,
        monkeypatch,
        caplog: pytest.LogCaptureFixture,
    ):
        monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
        pages = [
            [
                {"name": "catalog.first", "inputSchema": {}},
                {"name": "catalog.shared", "description": "keep", "inputSchema": {}},
            ],
            [
                {"name": "catalog.shared", "description": "discard", "inputSchema": {}},
                {"name": "catalog.last", "inputSchema": {}},
            ],
        ]
        svc, scope, row, _updates = self._configured_sync_service()

        with (
            modern_mcp_server(pages=pages) as fixture,
            caplog.at_level(
                logging.INFO,
                logger="core.application.management.mcp_connector_observability",
            ),
        ):
            row["base_url"] = fixture.url
            result = svc.sync_connector("mcp_fs", scope=scope)

        assert [tool["remote_tool_name"] for tool in result.discovered_tools_jsonb] == [
            "catalog.first",
            "catalog.shared",
            "catalog.last",
        ]
        assert [
            record.payload["method"] for record in fixture.requests if record.payload is not None
        ] == ["server/discover", "tools/list", "tools/list"]
        assert fixture.violations == []
        assert fixture.delete_count == 0
        [pagination_record] = [
            record
            for record in caplog.records
            if record.getMessage() == "MCP connector discovery pagination completed"
        ]
        assert pagination_record.connector_id == "mcp_fs"
        assert pagination_record.job_id == ""
        assert pagination_record.protocol_era == "modern"
        assert pagination_record.protocol_version == "2026-07-28"
        assert pagination_record.page_count == 2

    @pytest.mark.parametrize("opaque_cursor", ["  opaque cursor  ", ""])
    def test_sync_forwards_present_opaque_cursor_exactly_over_real_http(
        self,
        monkeypatch,
        opaque_cursor,
    ):
        monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
        pages = [
            [{"name": "catalog.first", "inputSchema": {}}],
            [{"name": "catalog.second", "inputSchema": {}}],
        ]
        svc, scope, row, _updates = self._configured_sync_service()

        with modern_mcp_server(pages=pages, cursors=[opaque_cursor]) as fixture:
            row["base_url"] = fixture.url
            result = svc.sync_connector("mcp_fs", scope=scope)

        list_requests = [
            record
            for record in fixture.requests
            if record.payload and record.payload.get("method") == "tools/list"
        ]
        assert "cursor" not in list_requests[0].payload["params"]
        assert list_requests[1].payload["params"]["cursor"] == opaque_cursor
        assert [tool["remote_tool_name"] for tool in result.discovered_tools_jsonb] == [
            "catalog.first",
            "catalog.second",
        ]

    def test_sync_rejects_repeated_empty_cursor_over_real_http(self, monkeypatch):
        monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
        pages = [
            [{"name": "catalog.first", "inputSchema": {}}],
            [{"name": "catalog.second", "inputSchema": {}}],
            [{"name": "catalog.unreachable", "inputSchema": {}}],
        ]
        svc, scope, row, _updates = self._configured_sync_service()

        with (
            modern_mcp_server(pages=pages, cursors=["", ""]) as fixture,
            pytest.raises(ServiceValidationError, match="sync failed; verify"),
        ):
            row["base_url"] = fixture.url
            svc.sync_connector("mcp_fs", scope=scope)

        list_requests = [
            record
            for record in fixture.requests
            if record.payload and record.payload.get("method") == "tools/list"
        ]
        assert len(list_requests) == 2
        assert list_requests[1].payload["params"]["cursor"] == ""
        assert row["last_sync_status"] == "failed"

    def test_sync_accepts_empty_catalog_and_clears_stale_discovery(self):
        svc, scope, row, _updates = self._configured_sync_service(
            discovered_tools=[{"remote_tool_name": "stale.tool"}]
        )

        with patch(
            "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
            side_effect=_rpc_context_from_responses([{"result": {"tools": []}}]),
        ):
            result = svc.sync_connector("mcp_fs", scope=scope)

        assert result.last_sync_status == "success"
        assert result.discovered_tools_jsonb == []
        assert row["discovered_tools_jsonb"] == []

    @pytest.mark.parametrize(
        ("remaining_tools", "expected_allowlist"),
        [
            ([{"name": "catalog.read", "inputSchema": {}}], ["fs.catalog.read"]),
            ([], []),
        ],
        ids=["partial-shrink", "empty-catalog"],
    )
    def test_sync_reconciles_allowlist_and_disables_removed_projected_tools(
        self,
        remaining_tools,
        expected_allowlist,
    ):
        svc, scope, row, _updates = self._configured_sync_service()
        registry_manager = _FakeRegistryManager()
        discovered = mcp_connector_common._normalize_discovered_tools(
            row,
            [
                {"name": "catalog.read", "inputSchema": {}},
                {"name": "catalog.write", "inputSchema": {}},
            ],
        )
        row["discovered_tools_jsonb"] = discovered
        row["allowlisted_tools"] = ["fs.catalog.read", "fs.catalog.write"]

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry_manager,
        ):
            svc._sync_projected_registry_tools(dict(row))
            with patch(
                "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
                side_effect=_rpc_context_from_responses([{"result": {"tools": remaining_tools}}]),
            ):
                result = svc.sync_connector("mcp_fs", scope=scope)

        assert result.allowlisted_tools == expected_allowlist
        assert row["allowlisted_tools"] == expected_allowlist
        read_tool = registry_manager.db_store.get_tool("fs.catalog.read")
        write_tool = registry_manager.db_store.get_tool("fs.catalog.write")
        assert read_tool is not None
        assert write_tool is not None
        assert read_tool["enabled"] is bool(expected_allowlist)
        assert write_tool["enabled"] is False

    def test_sync_rejects_repeated_cursor_without_requesting_another_page(self):
        svc, scope, row, _updates = self._configured_sync_service()
        requests: list[dict[str, Any]] = []

        with (
            patch(
                "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
                side_effect=_rpc_context_from_responses(
                    [
                        {"result": {"tools": [], "nextCursor": "repeat"}},
                        {"result": {"tools": [], "nextCursor": "repeat"}},
                    ],
                    requests=requests,
                ),
            ),
            pytest.raises(ServiceValidationError, match="sync failed; verify"),
        ):
            svc.sync_connector("mcp_fs", scope=scope)

        assert len(requests) == 2
        assert row["last_sync_status"] == "failed"

    def test_sync_accepts_exactly_one_hundred_discovery_pages(self):
        svc, scope, row, _updates = self._configured_sync_service()
        requests: list[dict[str, Any]] = []
        responses = [
            {
                "result": {
                    "tools": [{"name": f"tool.{page}"}],
                    **({"nextCursor": f"cursor-{page + 1}"} if page < 100 else {}),
                }
            }
            for page in range(1, 101)
        ]

        with patch(
            "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
            side_effect=_rpc_context_from_responses(responses, requests=requests),
        ):
            svc.sync_connector("mcp_fs", scope=scope)

        assert len(requests) == 100
        assert len(row["discovered_tools_jsonb"]) == 100
        assert row["last_sync_status"] == "success"

    def test_sync_rejects_page_one_hundred_and_one_before_requesting_it(self):
        svc, scope, row, _updates = self._configured_sync_service()
        requests: list[dict[str, Any]] = []
        responses = [
            {
                "result": {
                    "tools": [{"name": f"tool.{page}"}],
                    "nextCursor": f"cursor-{page + 1}",
                }
            }
            for page in range(1, 101)
        ]

        with (
            patch(
                "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
                side_effect=_rpc_context_from_responses(responses, requests=requests),
            ),
            pytest.raises(ServiceValidationError, match="sync failed; verify"),
        ):
            svc.sync_connector("mcp_fs", scope=scope)

        assert len(requests) == 100
        assert row["last_sync_status"] == "failed"

    def test_sync_completes_a_streamable_http_session_before_listing_tools(self, monkeypatch):
        """AUDIT-025: a compliant MCP server rejects tools/list before initialize."""
        received: list[tuple[dict[str, str], dict[str, object]]] = []
        closed_session_ids: list[str] = []

        class _CompliantMcpHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                headers = {key.lower(): value for key, value in self.headers.items()}
                received.append((headers, payload))
                method = payload.get("method")

                if method == "server/discover":
                    body = {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                elif method == "initialize":
                    body = {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fixture", "version": "1.0"},
                        },
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Mcp-Session-Id", "fixture-session")
                elif method == "notifications/initialized":
                    self.send_response(202)
                    body = None
                elif method == "tools/list" and headers.get("mcp-session-id") == "fixture-session":
                    body = {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {"tools": [{"name": "catalog.read", "inputSchema": {}}]},
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                else:
                    body = {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -1}}
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")

                if body is not None:
                    encoded = json.dumps(body).encode("utf-8")
                    self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                if body is not None:
                    self.wfile.write(encoded)

            def do_DELETE(self):
                closed_session_ids.append(str(self.headers.get("Mcp-Session-Id") or ""))
                self.send_response(202)
                self.end_headers()

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _CompliantMcpHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
        svc, _bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        row = {
            "connector_id": "mcp_fixture",
            "account_id": "acc_alpha",
            "display_name": "Protocol fixture",
            "transport": "streamable_http",
            "base_url": f"http://127.0.0.1:{server.server_port}/mcp",
            "auth_mode": "none",
            "headers": {},
            "allowlisted_tools": [],
            "namespace_prefix": "fixture",
            "status": "unchecked",
            "discovered_tools_jsonb": [],
            "last_sync_status": "never",
            "last_sync_error": "",
            "enabled": True,
        }
        svc._get_connector_row = MagicMock(side_effect=lambda *_args, **_kwargs: dict(row))
        svc._update_connector_row = MagicMock(
            side_effect=lambda _bl, _id, updates, **_kw: row.update(updates)
        )
        try:
            result = svc.sync_connector("mcp_fixture", scope=scope)
        finally:
            server.shutdown()
            thread.join()

        assert result.last_sync_status == "success"
        assert [payload["method"] for _headers, payload in received] == [
            "server/discover",
            "initialize",
            "notifications/initialized",
            "tools/list",
        ]
        assert all(
            "application/json" in headers["accept"] and "text/event-stream" in headers["accept"]
            for headers, _payload in received
        )
        assert received[0][0]["mcp-protocol-version"] == "2026-07-28"
        assert received[0][0]["mcp-method"] == "server/discover"
        assert received[1][1]["params"]["protocolVersion"] == "2025-11-25"
        assert not {
            "mcp-protocol-version",
            "mcp-method",
            "mcp-name",
            "mcp-session-id",
        }.intersection(received[1][0])
        assert received[2][0]["mcp-protocol-version"] == "2025-03-26"
        assert received[2][0]["mcp-session-id"] == "fixture-session"
        assert "mcp-method" not in received[2][0]
        assert "mcp-name" not in received[2][0]
        assert received[-1][0]["mcp-protocol-version"] == "2025-03-26"
        assert received[-1][0]["mcp-session-id"] == "fixture-session"
        assert "mcp-method" not in received[-1][0]
        assert "mcp-name" not in received[-1][0]
        assert closed_session_ids == ["fixture-session"]

    def test_sync_reinitializes_after_a_streamable_http_session_expires(self, monkeypatch):
        """A 404 for a session-bound request must restart the MCP lifecycle once."""
        received: list[tuple[str, str]] = []
        initialized = 0

        class _ExpiringMcpHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal initialized
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
                method = str(payload.get("method") or "")
                session_id = str(self.headers.get("Mcp-Session-Id") or "")
                received.append((method, session_id))
                if method == "server/discover":
                    body = {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {"code": -32002, "message": "Server not initialized"},
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                elif method == "initialize":
                    initialized += 1
                    body = {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                        },
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Mcp-Session-Id", f"session-{initialized}")
                elif method == "notifications/initialized":
                    body = None
                    self.send_response(202)
                elif method == "tools/list" and session_id == "session-1":
                    body = {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -1}}
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                else:
                    body = {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {"tools": [{"name": "catalog.read", "inputSchema": {}}]},
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                if body is not None:
                    encoded = json.dumps(body).encode()
                    self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                if body is not None:
                    self.wfile.write(encoded)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _ExpiringMcpHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
        try:
            from core.application.management.mcp_connector_client import (
                streamable_http_request_with_latency,
            )

            payload, _latency = streamable_http_request_with_latency(
                url=f"http://127.0.0.1:{server.server_port}/mcp",
                headers={},
                payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                timeout_seconds=2,
            )
        finally:
            server.shutdown()
            thread.join()

        assert payload["result"]["tools"][0]["name"] == "catalog.read"
        assert received == [
            ("server/discover", ""),
            ("initialize", ""),
            ("notifications/initialized", "session-1"),
            ("tools/list", "session-1"),
            ("initialize", ""),
            ("notifications/initialized", "session-2"),
            ("tools/list", "session-2"),
        ]

    def test_oauth_tool_call_uses_session_lifecycle_and_final_sse_event(self, monkeypatch):
        """AUDIT-025: OAuth tools/call must retain its MCP session through SSE."""
        received: list[tuple[str, str, str]] = []
        deleted_sessions: list[str] = []

        class _OauthSseMcpHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
                method = str(payload.get("method") or "")
                session_id = str(self.headers.get("Mcp-Session-Id") or "")
                authorization = str(self.headers.get("Authorization") or "")
                received.append((method, session_id, authorization))
                if method == "server/discover":
                    body = {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                elif method == "initialize":
                    body = {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                        },
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Mcp-Session-Id", "oauth-session")
                elif method == "notifications/initialized":
                    body = None
                    self.send_response(202)
                else:
                    body = (
                        "event: message\n"
                        'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
                        "event: message\n"
                        + "data: "
                        + json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": payload["id"],
                                "result": {"content": [{"type": "text", "text": "final"}]},
                            }
                        )
                        + "\n\n"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                if body is not None:
                    encoded = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                    self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                if body is not None:
                    self.wfile.write(encoded)

            def do_DELETE(self):
                deleted_sessions.append(str(self.headers.get("Mcp-Session-Id") or ""))
                self.send_response(202)
                self.end_headers()

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _OauthSseMcpHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
        from core.application.auth import mcp_connector_oauth

        monkeypatch.setattr(
            mcp_connector_oauth_rpc,
            "_headers_for_connector",
            lambda _conn, *, connector_row, force_refresh=False: {"Authorization": "Bearer access"},
        )
        try:
            payload, _latency_ms = mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
                object(),
                connector_row={
                    "auth_mode": "oauth2",
                    "base_url": f"http://127.0.0.1:{server.server_port}/mcp",
                },
                payload={
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {"name": "catalog.read", "arguments": {}},
                },
                timeout_seconds=2,
            )
        finally:
            server.shutdown()
            thread.join()

        assert payload["result"]["content"][0]["text"] == "final"
        assert received == [
            ("server/discover", "", "Bearer access"),
            ("initialize", "", "Bearer access"),
            ("notifications/initialized", "oauth-session", "Bearer access"),
            ("tools/call", "oauth-session", "Bearer access"),
        ]
        assert deleted_sessions == ["oauth-session"]

    def test_create_validates_required_fields(self):
        svc, _ = self._make_service()
        inp = McpConnectorCreateInput(display_name="", base_url="http://test")
        _raises_input_4756_1 = ScopeContext(
            account_id="acc_default", project_id="project_default", tenant_id="tenant_default"
        )
        with pytest.raises(ServiceValidationError, match="display_name"):
            svc.create_connector(inp, scope=_raises_input_4756_1)

    def test_create_validates_base_url(self):
        svc, _ = self._make_service()
        inp = McpConnectorCreateInput(display_name="Test", base_url="")
        _raises_input_4769_1 = ScopeContext(
            account_id="acc_default", project_id="project_default", tenant_id="tenant_default"
        )
        with pytest.raises(ServiceValidationError, match="base_url"):
            svc.create_connector(inp, scope=_raises_input_4769_1)

    def test_create_rejects_unsupported_transport(self):
        svc, _ = self._make_service()
        inp = McpConnectorCreateInput(
            display_name="Flow Steward MCP",
            base_url="https://fs.example/mcp",
            transport="sse",
        )
        _raises_input_4786_1 = ScopeContext(
            account_id="acc_default", project_id="project_default", tenant_id="tenant_default"
        )
        with pytest.raises(ServiceValidationError, match="streamable_http"):
            svc.create_connector(inp, scope=_raises_input_4786_1)

    def test_create_connector_success(self):
        svc, _ = self._make_service()
        inp = McpConnectorCreateInput(
            display_name="Flow Steward MCP",
            base_url="https://fs.example/mcp",
            auth_mode="bearer",
        )
        result = svc.create_connector(
            inp,
            scope=ScopeContext(
                account_id="acc_default", project_id="project_default", tenant_id="tenant_default"
            ),
        )
        assert result.connector_id.startswith("mcp_")
        assert result.display_name == "Flow Steward MCP"
        assert result.auth_mode == "bearer"
        assert result.allowlisted_tools == []
        assert result.discovered_tools_jsonb == []
        assert result.last_sync_status == "never"

    def test_create_oauth2_connector_starts_auth_required(self):
        svc, _ = self._make_service()
        inp = McpConnectorCreateInput(
            display_name="Flow Steward MCP",
            base_url="https://fs.example/mcp",
            auth_mode="oauth2",
        )

        result = svc.create_connector(
            inp,
            scope=ScopeContext(
                account_id="acc_default", project_id="project_default", tenant_id="tenant_default"
            ),
        )

        assert result.auth_mode == "oauth2"
        assert result.auth_status == "auth_required"

    def test_create_connector_rejects_localhost_base_url(self):
        svc, _ = self._make_service()
        inp = McpConnectorCreateInput(
            display_name="Local MCP",
            base_url="http://localhost:8300/mcp",
            auth_mode="none",
        )

        _raises_input_4842_1 = ScopeContext(
            account_id="acc_default", project_id="project_default", tenant_id="tenant_default"
        )
        with pytest.raises(ServiceValidationError, match="host is not allowed"):
            svc.create_connector(inp, scope=_raises_input_4842_1)

    def test_delete_not_found_raises(self):
        svc, _ = self._make_service()
        _raises_input_4854_1 = ScopeContext(
            account_id="acc_default", project_id="project_default", tenant_id="tenant_default"
        )
        with pytest.raises(ServiceNotFoundError):
            svc.delete_connector("mcp_nonexistent", scope=_raises_input_4854_1)

    def test_no_logger_raises_unavailable(self):
        svc = McpConnectorCommandService(get_logger=lambda: None)
        inp = McpConnectorCreateInput(display_name="Test", base_url="http://test")
        from core.application.management.errors import ServiceUnavailableError

        _raises_input_4869_1 = ScopeContext(
            account_id="acc_default", project_id="project_default", tenant_id="tenant_default"
        )
        with pytest.raises(ServiceUnavailableError):
            svc.create_connector(inp, scope=_raises_input_4869_1)

    def test_viewer_cannot_create_connectors(self):
        svc, _ = self._make_service()
        scope = ScopeContext(
            account_id="acc_default",
            project_id="project_default",
            tenant_id="tenant_default",
            actor_id="viewer",
            principal_type="viewer",
        )
        inp = McpConnectorCreateInput(display_name="Test", base_url="http://test")

        with pytest.raises(ServiceForbiddenError, match="Operator role required"):
            svc.create_connector(inp, scope=scope)

    def test_operator_delete_uses_account_scoped_lookup(self):
        svc, bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        svc._get_connector_row = MagicMock(return_value=None)

        with pytest.raises(ServiceNotFoundError):
            svc.delete_connector("mcp_nonexistent", scope=scope)

        svc._get_connector_row.assert_called_once_with(
            bl,
            "mcp_nonexistent",
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
        )

    def test_operator_health_check_uses_account_scoped_lookup(self):
        svc, bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        svc._get_connector_row = MagicMock(return_value=None)

        with pytest.raises(ServiceNotFoundError):
            svc.check_health("mcp_nonexistent", scope=scope)

        svc._get_connector_row.assert_called_once_with(
            bl,
            "mcp_nonexistent",
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
        )

    def test_health_probe_binds_connector_observability_context(self, monkeypatch):
        svc, scope, row, _updates = self._configured_sync_service()
        observed: list[tuple[str, str]] = []

        def _probe(**_kwargs):
            state = mcp_connector_observability._ACTIVE_MCP_OBSERVABILITY.get()
            assert state is not None
            observed.append((state.connector_id, state.job_id))
            return {"result": {"tools": []}}, 7

        monkeypatch.setattr(
            "core.application.management.mcp_connector_services._json_rpc_post_with_latency",
            _probe,
        )

        result = svc.check_health("mcp_fs", scope=scope)

        assert result.status == "active"
        assert result.latency_ms == 7
        assert observed == [("mcp_fs", "")]
        assert row["status"] == "active"

    def test_health_and_sync_redact_untrusted_exception_details(self, monkeypatch):
        marker = "secret@internal.example.test/path?token=leaked"
        svc, scope, row, updates = self._configured_sync_service()
        monkeypatch.setattr(
            "core.application.management.mcp_connector_services._json_rpc_post_with_latency",
            MagicMock(side_effect=RuntimeError(marker)),
        )

        health = svc.check_health("mcp_fs", scope=scope)

        assert marker not in str(health.error)
        assert health.error == "MCP connector health check failed; verify the endpoint and retry"

        with (
            patch(
                "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
                side_effect=RuntimeError(marker),
            ),
            pytest.raises(ServiceUnavailableError) as exc_info,
        ):
            svc.sync_connector("mcp_fs", scope=scope)

        assert marker not in str(exc_info.value)
        assert row["last_sync_error"] == "MCP connector sync failed; verify the endpoint and retry"
        assert marker not in str(updates)

    def test_create_redacts_persistence_exception_details(self):
        marker = "postgres://operator:secret@internal.example.test/private"
        svc, _bl = self._make_service()
        svc._insert_connector = MagicMock(side_effect=RuntimeError(marker))
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )

        _raises_input_4997_1 = McpConnectorCreateInput(
            display_name="Redacted failure", base_url="https://public.example.test/mcp"
        )
        with pytest.raises(ServiceUnavailableError) as exc_info:
            svc.create_connector(_raises_input_4997_1, scope=scope)

        assert marker not in str(exc_info.value)

    def test_update_endpoint_change_clears_oauth_state(self):
        svc, _bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "display_name": "Flow Steward MCP",
            "description": "",
            "transport": "streamable_http",
            "base_url": "https://fs.example/mcp",
            "auth_mode": "oauth2",
            "headers": {},
            "allowlisted_tools": [],
            "namespace_prefix": "fs",
            "status": "active",
            "auth_status": "connected",
            "oauth_credential_revision": 4,
            "oauth_metadata_jsonb": {
                "requested_scopes": ["files:read"],
                "authorization_parameters": {"prompt": "consent"},
                "refresh_token_required": True,
                "discovery": {"resource": "https://fs.example/mcp"},
                "registration": {"issuer": "https://auth.example", "mode": "dynamic"},
                "token": {"resource": "https://fs.example/mcp"},
            },
            "oauth_secret_refs_jsonb": {
                "refs": {
                    "_oauth_access_token": "mcp_connector:mcp_fs:_oauth_access_token",
                    "_oauth_refresh_token": "mcp_connector:mcp_fs:_oauth_refresh_token",
                    "_oauth_client_id": "mcp_connector:mcp_fs:_oauth_client_id",
                    "_oauth_client_secret": "mcp_connector:mcp_fs:_oauth_client_secret",
                    "_oauth_registration_access_token": "mcp_connector:mcp_fs:_oauth_registration_access_token",
                }
            },
            "oauth_connected_identity": "alice@example.test",
            "oauth_last_error": "",
            "discovered_tools_jsonb": [],
            "last_synced_at": None,
            "last_sync_status": "never",
            "last_sync_error": "",
            "enabled": True,
            "created_at": "2026-05-20T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z",
        }
        deleted_refs: list[str] = []

        def _get_row(_bl, _connector_id, account_id=None, **_kwargs):
            if account_id and account_id != row["account_id"]:
                return None
            return dict(row)

        def _update_row(_bl, _connector_id, updates, account_id=None, **_kwargs):
            _ = account_id
            row.update(dict(updates))

        svc._get_connector_row = MagicMock(side_effect=_get_row)
        svc._update_connector_row = MagicMock(side_effect=_update_row)

        with (
            patch(
                "core.application.auth.mcp_connector_oauth._locked_connector_row",
                side_effect=lambda _conn, _row: dict(row),
            ),
            patch(
                "core.application.auth.mcp_connector_oauth.model_provider_store.delete_secret_ref",
                side_effect=lambda _conn, *, ref_key, **_kwargs: deleted_refs.append(ref_key),
            ),
        ):
            result = svc.update_connector(
                "mcp_fs",
                McpConnectorUpdateInput(base_url="https://fs-new.example/mcp"),
                scope=scope,
            )

        assert result.base_url == "https://fs-new.example/mcp"
        assert result.auth_status == "auth_required"
        assert result.oauth_status["last_error"] == (
            "MCP OAuth reconnect required after connector transport, endpoint, auth mode, or headers change"
        )
        assert row["oauth_secret_refs_jsonb"] == {}
        assert row["oauth_metadata_jsonb"] == {
            "requested_scopes": ["files:read"],
            "authorization_parameters": {"prompt": "consent"},
            "refresh_token_required": True,
        }
        assert row["oauth_connected_identity"] == ""
        assert row["status"] == "degraded"
        assert sorted(deleted_refs) == [
            "mcp_connector:mcp_fs:_oauth_access_token",
            "mcp_connector:mcp_fs:_oauth_client_id",
            "mcp_connector:mcp_fs:_oauth_client_secret",
            "mcp_connector:mcp_fs:_oauth_refresh_token",
            "mcp_connector:mcp_fs:_oauth_registration_access_token",
        ]
        last_updates = dict(svc._update_connector_row.call_args.args[2])
        assert last_updates["base_url"] == "https://fs-new.example/mcp"
        assert last_updates["oauth_secret_refs_jsonb"] == {}
        assert last_updates["oauth_metadata_jsonb"] == {
            "requested_scopes": ["files:read"],
            "authorization_parameters": {"prompt": "consent"},
            "refresh_token_required": True,
        }
        assert last_updates["oauth_credential_revision"] == 5
        assert last_updates["auth_status"] == "auth_required"
        assert svc._update_connector_row.call_args.kwargs == {
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
        }

    def test_update_headers_supports_explicit_keep_replace_and_clear(self):
        svc, _bl = self._make_service()
        vault = _FakeHeaderSecretVault()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "display_name": "Flow Steward MCP",
            "description": "",
            "transport": "streamable_http",
            "base_url": "https://fs.example/mcp",
            "auth_mode": "bearer",
            "header_secret_refs_jsonb": vault.save(
                None,
                connector_id="mcp_fs",
                headers={"Authorization": "Bearer retained-secret"},
            ),
            "allowlisted_tools": [],
            "namespace_prefix": "fs",
            "status": "active",
            "auth_status": "not_configured",
            "oauth_metadata_jsonb": {},
            "oauth_secret_refs_jsonb": {},
            "oauth_connected_identity": "",
            "oauth_last_error": "",
            "discovered_tools_jsonb": [],
            "last_synced_at": None,
            "last_sync_status": "never",
            "last_sync_error": "",
            "enabled": True,
            "created_at": "2026-05-20T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z",
        }
        svc._get_connector_row = MagicMock(side_effect=lambda *_args, **_kwargs: dict(row))
        svc._update_connector_row = MagicMock(
            side_effect=lambda _bl, _connector_id, updates, **_kwargs: row.update(updates)
        )

        retained_refs = row["header_secret_refs_jsonb"]
        with vault.patch():
            svc.update_connector(
                "mcp_fs",
                McpConnectorUpdateInput(display_name="Renamed", headers_mode="keep"),
                scope=scope,
            )
            assert row["header_secret_refs_jsonb"] == retained_refs
            assert vault.resolve(None, row) == {"Authorization": "Bearer retained-secret"}

            svc.update_connector(
                "mcp_fs",
                McpConnectorUpdateInput(
                    headers_mode="replace",
                    headers={"Authorization": "Bearer retained-secret"},
                ),
                scope=scope,
            )
            assert row["header_secret_refs_jsonb"] == retained_refs

            svc.update_connector(
                "mcp_fs",
                McpConnectorUpdateInput(
                    headers_mode="replace",
                    headers={"X-Api-Key": "replacement-secret"},
                ),
                scope=scope,
            )
            assert "headers" not in row
            assert "replacement-secret" not in json.dumps(row)
            assert vault.resolve(None, row) == {"X-Api-Key": "replacement-secret"}

            svc.update_connector(
                "mcp_fs",
                McpConnectorUpdateInput(headers_mode="clear"),
                scope=scope,
            )
            assert row["header_secret_refs_jsonb"] == {}
        assert vault.saves[1:] == [{"X-Api-Key": "replacement-secret"}, {}]

    def test_switching_credential_auth_mode_requires_explicit_clean_headers(self):
        svc, _bl = self._make_service()
        vault = _FakeHeaderSecretVault()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "display_name": "Flow Steward MCP",
            "description": "",
            "transport": "streamable_http",
            "base_url": "https://fs.example/mcp",
            "auth_mode": "bearer",
            "header_secret_refs_jsonb": vault.save(
                None,
                connector_id="mcp_fs",
                headers={"Authorization": "Bearer retained-secret"},
            ),
            "allowlisted_tools": [],
            "namespace_prefix": "fs",
            "status": "active",
            "auth_status": "not_configured",
            "oauth_metadata_jsonb": {},
            "oauth_secret_refs_jsonb": {},
            "oauth_connected_identity": "",
            "oauth_last_error": "",
            "discovered_tools_jsonb": [],
            "last_synced_at": None,
            "last_sync_status": "never",
            "last_sync_error": "",
            "enabled": True,
            "created_at": "2026-05-20T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z",
        }
        svc._get_connector_row = MagicMock(side_effect=lambda *_args, **_kwargs: dict(row))
        svc._update_connector_row = MagicMock(
            side_effect=lambda _bl, _connector_id, updates, **_kwargs: row.update(updates)
        )

        _raises_input_5232_1 = McpConnectorUpdateInput(auth_mode="oauth2")
        with (
            vault.patch(),
            pytest.raises(ServiceValidationError, match="requires headers_mode clear or replace"),
        ):
            svc.update_connector("mcp_fs", _raises_input_5232_1, scope=scope)

        with (
            vault.patch(),
            patch(
                "core.application.auth.mcp_connector_oauth.prepare_mcp_connector_oauth_reset",
                side_effect=lambda _conn, *, connector_row: connector_row,
            ),
        ):
            svc.update_connector(
                "mcp_fs",
                McpConnectorUpdateInput(auth_mode="oauth2", headers_mode="clear"),
                scope=scope,
            )
        assert row["auth_mode"] == "oauth2"
        assert row["header_secret_refs_jsonb"] == {}

    def test_update_oauth_configuration_clears_tokens_and_pending_state_but_preserves_client(self):
        svc, _bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "display_name": "Flow Steward MCP",
            "description": "",
            "transport": "streamable_http",
            "base_url": "https://fs.example/mcp",
            "auth_mode": "oauth2",
            "headers": {},
            "allowlisted_tools": [],
            "namespace_prefix": "fs",
            "status": "active",
            "auth_status": "connected",
            "oauth_credential_revision": 7,
            "oauth_metadata_jsonb": {
                "requested_scopes": ["files:read"],
                "authorization_parameters": {"prompt": "consent"},
                "refresh_token_required": True,
                "discovery": {"issuer": "https://auth.example"},
                "registration": {"mode": "manual", "issuer": "https://auth.example"},
                "token": {"resource": "https://fs.example/mcp"},
            },
            "oauth_secret_refs_jsonb": {
                "refs": {
                    "_oauth_access_token": "mcp_connector:mcp_fs:_oauth_access_token",
                    "_oauth_refresh_token": "mcp_connector:mcp_fs:_oauth_refresh_token",
                    "_oauth_client_id": "mcp_connector:mcp_fs:_oauth_client_id",
                    "_oauth_client_secret": "mcp_connector:mcp_fs:_oauth_client_secret",
                }
            },
            "oauth_connected_identity": "alice@example.test",
            "oauth_last_error": "",
            "discovered_tools_jsonb": [],
            "last_synced_at": None,
            "last_sync_status": "never",
            "last_sync_error": "",
            "enabled": True,
            "created_at": "2026-05-20T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z",
        }
        deleted_refs: list[str] = []
        deleted_states: list[tuple[str, str, str, str]] = []
        lifecycle_events: list[str] = []

        svc._get_connector_row = MagicMock(return_value=dict(row))
        svc._update_connector_row = MagicMock(
            side_effect=lambda _bl, _connector_id, updates, **_kwargs: row.update(updates)
        )

        with (
            patch(
                "core.application.auth.mcp_connector_oauth._locked_connector_row",
                side_effect=lambda _conn, _row: dict(row),
            ),
            patch(
                "core.application.auth.mcp_connector_oauth.model_provider_store.delete_secret_ref",
                side_effect=lambda _conn, *, ref_key, **_kwargs: (
                    lifecycle_events.append(f"delete:{ref_key}"),
                    deleted_refs.append(ref_key),
                )[-1],
            ),
            patch(
                "core.application.auth.mcp_connector_oauth.mcp_connector_oauth_store.delete_connector_oauth_states",
                side_effect=lambda _conn, *, connector_id, account_id, project_id, tenant_id: (
                    deleted_states.append((connector_id, account_id, project_id, tenant_id))
                ),
            ),
        ):
            svc.update_connector(
                "mcp_fs",
                McpConnectorUpdateInput(
                    oauth_config={
                        "requested_scopes": ["files:write"],
                        "authorization_parameters": {"access_type": "offline"},
                        "refresh_token_required": False,
                    }
                ),
                scope=scope,
            )

        assert deleted_states == [("mcp_fs", "acc_alpha", "project_alpha", "tenant_alpha")]
        assert all(event.startswith("delete:") for event in lifecycle_events)
        assert deleted_refs == [
            "mcp_connector:mcp_fs:_oauth_access_token",
            "mcp_connector:mcp_fs:_oauth_refresh_token",
        ]
        assert row["oauth_secret_refs_jsonb"] == {
            "refs": {
                "_oauth_client_id": "mcp_connector:mcp_fs:_oauth_client_id",
                "_oauth_client_secret": "mcp_connector:mcp_fs:_oauth_client_secret",
            }
        }
        assert row["oauth_metadata_jsonb"]["requested_scopes"] == ["files:write"]
        assert row["oauth_metadata_jsonb"]["authorization_parameters"] == {"access_type": "offline"}
        assert row["oauth_metadata_jsonb"]["refresh_token_required"] is False
        assert "token" not in row["oauth_metadata_jsonb"]
        assert row["oauth_metadata_jsonb"]["registration"]["mode"] == "manual"
        assert row["oauth_credential_revision"] == 8

    def test_sync_persists_discovery_payload_and_sync_state(self):
        svc, _bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "display_name": "Flow Steward MCP",
            "description": "",
            "transport": "streamable_http",
            "base_url": "https://fs.example/mcp",
            "auth_mode": "none",
            "headers": {},
            "allowlisted_tools": [],
            "namespace_prefix": "fs",
            "status": "unchecked",
            "discovered_tools_jsonb": [],
            "last_synced_at": None,
            "last_sync_status": "never",
            "last_sync_error": "",
            "enabled": True,
            "created_at": "2026-05-20T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z",
        }
        captured_updates: list[dict[str, Any]] = []

        def _get_row(_bl, _connector_id, account_id=None, **_kwargs):
            if account_id and account_id != row["account_id"]:
                return None
            return dict(row)

        def _update_row(_bl, _connector_id, updates, account_id=None, **_kwargs):
            _ = account_id
            captured_updates.append(dict(updates))
            row.update(dict(updates))

        svc._get_connector_row = MagicMock(side_effect=_get_row)
        svc._update_connector_row = MagicMock(side_effect=_update_row)

        with patch(
            "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
            side_effect=_rpc_context_from_responses(
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "tools": [
                                {
                                    "name": "catalog.read",
                                    "title": "Catalog Read",
                                    "description": "Read catalog rows.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"limit": {"type": "integer"}},
                                        "required": ["limit"],
                                    },
                                }
                            ]
                        },
                    }
                ]
            ),
        ):
            result = svc.sync_connector("mcp_fs", scope=scope)

        assert result.last_sync_status == "success"
        assert result.discovered_tools_jsonb[0]["remote_tool_name"] == "catalog.read"
        assert result.discovered_tools_jsonb[0]["projected_tool_id"] == "fs.catalog.read"
        assert captured_updates[0]["last_sync_status"] == "success"
        assert captured_updates[0]["last_sync_error"] == ""
        assert captured_updates[0]["discovered_tools_jsonb"][0]["display_name"] == "Catalog Read"

    def test_allowlist_save_projects_tools_and_rejects_ambiguous_deselection(self):
        svc, _bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        registry_manager = _FakeRegistryManager()
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "display_name": "Flow Steward MCP",
            "description": "",
            "transport": "streamable_http",
            "base_url": "https://fs.example/mcp",
            "auth_mode": "none",
            "headers": {},
            "allowlisted_tools": [],
            "namespace_prefix": "fs",
            "status": "active",
            "discovered_tools_jsonb": [
                {
                    "remote_tool_name": "catalog.read",
                    "projected_tool_id": "fs.catalog.read",
                    "display_name": "Catalog Read",
                    "description": "Read catalog rows.",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                    "annotations": {},
                },
                {
                    "remote_tool_name": "catalog.inspect",
                    "projected_tool_id": "fs.catalog.inspect",
                    "display_name": "Catalog Inspect",
                    "description": "Inspect one catalog row.",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                    "annotations": {},
                },
            ],
            "last_synced_at": "2026-05-20T01:00:00Z",
            "last_sync_status": "success",
            "last_sync_error": "",
            "enabled": True,
            "created_at": "2026-05-20T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z",
        }

        def _get_row(_bl, _connector_id, account_id=None, **_kwargs):
            if account_id and account_id != row["account_id"]:
                return None
            return dict(row)

        def _update_row(_bl, _connector_id, updates, account_id=None, **_kwargs):
            _ = account_id
            row.update(dict(updates))
            row["updated_at"] = datetime.now(UTC).isoformat()

        svc._get_connector_row = MagicMock(side_effect=_get_row)
        svc._update_connector_row = MagicMock(side_effect=_update_row)

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry_manager,
        ):
            first = svc.update_connector(
                "mcp_fs",
                McpConnectorUpdateInput(allowlisted_tools=["fs.catalog.read"]),
                scope=scope,
            )
            _raises_input_5511_1 = McpConnectorUpdateInput(allowlisted_tools=["fs.catalog.inspect"])
            with pytest.raises(ServiceConflictError, match="moved to Trash"):
                svc.update_connector("mcp_fs", _raises_input_5511_1, scope=scope)

        first_tool = registry_manager.db_store.get_tool("fs.catalog.read")
        second_tool = registry_manager.db_store.get_tool("fs.catalog.inspect")
        assert first.allowlisted_tools == ["fs.catalog.read"]
        assert first_tool is not None
        assert first_tool["enabled"] is True
        assert row["allowlisted_tools"] == ["fs.catalog.read"]
        assert second_tool is None

    def test_allowlist_save_rejects_projected_tool_id_collisions(self):
        svc, _bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        registry_manager = _FakeRegistryManager()
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "display_name": "Flow Steward MCP",
            "description": "",
            "transport": "streamable_http",
            "base_url": "https://fs.example/mcp",
            "auth_mode": "none",
            "headers": {},
            "allowlisted_tools": [],
            "namespace_prefix": "fs",
            "status": "active",
            "discovered_tools_jsonb": [
                {
                    "remote_tool_name": "catalog.read",
                    "projected_tool_id": "fs.catalog.read",
                    "display_name": "Catalog Read",
                    "description": "Read catalog rows.",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                    "annotations": {},
                }
            ],
            "last_synced_at": "2026-05-20T01:00:00Z",
            "last_sync_status": "success",
            "last_sync_error": "",
            "enabled": True,
            "created_at": "2026-05-20T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z",
        }

        svc._get_connector_row = MagicMock(return_value=dict(row))
        svc._update_connector_row = MagicMock(
            side_effect=AssertionError("connector row must not persist")
        )
        registry_manager.db_store.save_tool(
            {
                "tool_id": "fs.catalog.read",
                "version": "1.0.0",
                "display_name": "Other Connector / Catalog Read",
                "description": "Existing projected tool",
                "enabled": True,
                "category": "other",
                "prompt": "",
                "interface": {
                    "functions": [
                        {
                            "name": "fs.catalog.read",
                            "parameters": [],
                            "returns": {"name": "result", "type": "json", "required": False},
                        }
                    ]
                },
                "implementation": {
                    "type": "mcp",
                    "connector_id": "mcp_other",
                    "remote_tool_name": "catalog.read",
                },
                "config": {"remote_tool_name": "catalog.read"},
                "tags": ["mcp", "derived"],
                "builder_metadata_jsonb": {},
            }
        )

        _raises_input_5600_1 = McpConnectorUpdateInput(allowlisted_tools=["fs.catalog.read"])
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry_manager,
            ),
            pytest.raises(
                ServiceValidationError, match="already owned by MCP connector 'mcp_other'"
            ),
        ):
            svc.update_connector("mcp_fs", _raises_input_5600_1, scope=scope)

        existing_tool = registry_manager.db_store.get_tool("fs.catalog.read")
        assert existing_tool is not None
        assert existing_tool["implementation"]["connector_id"] == "mcp_other"
        svc._update_connector_row.assert_not_called()

    def test_discovery_marks_same_account_projected_tombstone_in_trash(self):
        registry_manager = _FakeRegistryManager()
        registry_manager.db_store.rows["fs.catalog.read"] = {
            "tool_id": "fs.catalog.read",
            "account_id": "acc_alpha",
            "display_name": "Catalog Read",
            "enabled": False,
            "implementation": {"type": "mcp", "connector_id": "mcp_fs"},
            "deleted_at": "2026-08-22T12:00:00Z",
            "deleted_by": "admin-b",
        }
        connector = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "namespace_prefix": "fs",
            "allowlisted_tools": [],
            "discovered_tools_jsonb": [
                {
                    "remote_tool_name": "catalog.read",
                    "projected_tool_id": "fs.catalog.read",
                    "display_name": "Catalog Read",
                    "input_schema": {},
                    "output_schema": {},
                    "annotations": {},
                }
            ],
        }

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry_manager,
        ):
            payload = mcp_connector_common._sanitize_connector_row_for_view(
                connector,
                include_deleted_actor=True,
            )

        discovered = payload["discovered_tools_jsonb"][0]
        assert discovered["lifecycle_state"] == "in_trash"
        assert discovered["can_publish"] is False
        assert discovered["availability_reason_code"] == "projected_tool_in_trash"
        assert discovered["recovery_action"] == "open_trash"
        assert discovered["trash_key"] == "registry_tool:fs.catalog.read"
        assert discovered["deleted_by"] == "admin-b"

        registry_manager.db_store.rows.pop("fs.catalog.read")
        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry_manager,
        ):
            after_purge = mcp_connector_common._sanitize_connector_row_for_view(connector)[
                "discovered_tools_jsonb"
            ][0]
        assert after_purge["lifecycle_state"] == "available_to_add"
        assert after_purge["can_publish"] is True

    def test_discovery_conceals_cross_account_projected_tombstone_owner(self):
        registry_manager = _FakeRegistryManager()
        registry_manager.db_store.rows["fs.catalog.read"] = {
            "tool_id": "fs.catalog.read",
            "account_id": "acc_beta",
            "enabled": False,
            "implementation": {"type": "mcp", "connector_id": "mcp_foreign"},
            "deleted_at": "2026-08-22T12:00:00Z",
            "deleted_by": "foreign-admin",
        }
        connector = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "namespace_prefix": "fs",
            "allowlisted_tools": [],
            "discovered_tools_jsonb": [
                {
                    "remote_tool_name": "catalog.read",
                    "projected_tool_id": "fs.catalog.read",
                    "display_name": "Catalog Read",
                    "input_schema": {},
                    "output_schema": {},
                    "annotations": {},
                }
            ],
        }

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry_manager,
        ):
            discovered = mcp_connector_common._sanitize_connector_row_for_view(connector)[
                "discovered_tools_jsonb"
            ][0]

        assert discovered["lifecycle_state"] == "id_conflict"
        assert discovered["can_publish"] is False
        assert "deleted_by" not in discovered
        assert "foreign-admin" not in json.dumps(discovered)

    def test_allowlist_update_rejects_same_connector_tombstone_without_resurrection(self):
        svc, scope, row, _updates = self._configured_sync_service(
            discovered_tools=[
                {
                    "remote_tool_name": "catalog.read",
                    "projected_tool_id": "fs.catalog.read",
                    "display_name": "Catalog Read",
                    "input_schema": {},
                    "output_schema": {},
                    "annotations": {},
                }
            ]
        )
        registry_manager = _FakeRegistryManager()
        registry_manager.db_store.rows["fs.catalog.read"] = {
            "tool_id": "fs.catalog.read",
            "account_id": "acc_alpha",
            "implementation": {"type": "mcp", "connector_id": "mcp_fs"},
            "deleted_at": "2026-08-22T12:00:00Z",
            "deleted_by": "admin-a",
            "enabled": False,
        }

        _raises_input_5741_1 = McpConnectorUpdateInput(allowlisted_tools=["fs.catalog.read"])
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry_manager,
            ),
            pytest.raises(ServiceConflictError) as exc_info,
        ):
            svc.update_connector("mcp_fs", _raises_input_5741_1, scope=scope)

        assert exc_info.value.payload["error"] == "projected_tool_in_trash"
        assert row["allowlisted_tools"] == []
        assert registry_manager.db_store.rows["fs.catalog.read"]["deleted_at"]

    def test_sync_reconciles_legacy_allowlisted_tombstone_without_resurrection(self):
        svc, scope, row, _updates = self._configured_sync_service()
        row["allowlisted_tools"] = ["fs.catalog.read"]
        registry_manager = _FakeRegistryManager()
        registry_manager.db_store.rows["fs.catalog.read"] = {
            "tool_id": "fs.catalog.read",
            "account_id": "acc_alpha",
            "implementation": {"type": "mcp", "connector_id": "mcp_fs"},
            "deleted_at": "2026-08-22T12:00:00Z",
            "deleted_by": "admin-a",
            "enabled": False,
        }
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {
                        "name": "catalog.read",
                        "title": "Catalog Read",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            },
        }

        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry_manager,
            ),
            patch(
                "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
                side_effect=_rpc_context_from_responses([response]),
            ),
        ):
            result = svc.sync_connector("mcp_fs", scope=scope)

        assert result.last_sync_status == "success"
        assert result.allowlisted_tools == []
        assert "projected_tool_in_trash" in result.last_sync_error
        assert registry_manager.db_store.rows["fs.catalog.read"]["deleted_at"]

    def test_confirmed_delete_connector_moves_root_and_projected_tool_to_trash(self):
        svc, _bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_default",
            project_id="project_default",
            tenant_id="tenant_default",
            actor_id="ops",
            principal_type="operator",
        )
        registry_manager = _FakeRegistryManager()
        connector_store = _InMemoryMcpConnectorStore()

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    McpConnectorCommandService,
                    "_insert_connector",
                    lambda self, logger, row: connector_store.insert(row),
                )
            )
            stack.enter_context(
                patch.object(
                    McpConnectorCommandService,
                    "_get_connector_row",
                    lambda self, logger, connector_id, account_id=None, **_kwargs: (
                        connector_store.get(
                            connector_id,
                            account_id=account_id,
                        )
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    McpConnectorCommandService,
                    "_update_connector_row",
                    lambda self, logger, connector_id, updates, account_id=None, **_kwargs: (
                        connector_store.update(
                            connector_id,
                            updates,
                            account_id=account_id,
                        )
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    McpConnectorCommandService,
                    "_soft_delete_connector_row",
                    lambda self, logger, connector_id, account_id=None, deleted_by="system", child_tool_ids=None, **_kwargs: (
                        connector_store.soft_delete(
                            connector_id,
                            account_id=account_id,
                            deleted_by=deleted_by,
                            child_tool_ids=child_tool_ids,
                        )
                    ),
                )
            )
            stack.enter_context(
                patch(
                    "core.application.registry.registry_manager.RegistryManager.instance",
                    return_value=registry_manager,
                )
            )
            stack.enter_context(
                patch(
                    "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
                    side_effect=_rpc_context_from_responses(
                        [
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "result": {
                                    "tools": [
                                        {
                                            "name": "catalog.read",
                                            "title": "Catalog Read",
                                            "description": "Read catalog rows.",
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {},
                                            },
                                        }
                                    ]
                                },
                            },
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "result": {
                                    "tools": [
                                        {
                                            "name": "catalog.read",
                                            "title": "Catalog Read",
                                            "description": "Read catalog rows.",
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {},
                                            },
                                        }
                                    ]
                                },
                            },
                        ]
                    ),
                )
            )

            first = svc.create_connector(
                McpConnectorCreateInput(
                    display_name="Flow Steward MCP A",
                    base_url="https://fs-a.example/mcp",
                    namespace_prefix="fs",
                ),
                scope=scope,
            )
            svc.sync_connector(first.connector_id, scope=scope)
            svc.update_connector(
                first.connector_id,
                McpConnectorUpdateInput(allowlisted_tools=["fs.catalog.read"]),
                scope=scope,
            )

            orphaned_tool = registry_manager.db_store.get_tool("fs.catalog.read")
            assert orphaned_tool is not None
            assert orphaned_tool["enabled"] is True
            assert orphaned_tool["implementation"]["connector_id"] == first.connector_id
            registry_manager.db_store.workflows = [
                {
                    "workflow_id": "wf_uses_catalog",
                    "account_id": "acc_default",
                    "project_id": "project_default",
                    "tenant_id": "tenant_default",
                    "enabled": True,
                    "status": "active",
                    "tool_ids": ["fs.catalog.read"],
                    "steps": [
                        {
                            "step_kind": "connector",
                            "connector_id": "connector.fs.catalog.read",
                        }
                    ],
                }
            ]
            workflow_snapshot = deepcopy(registry_manager.db_store.workflows)

            plan = svc.preview_delete_connector(first.connector_id, scope=scope)
            assert [tool["tool_id"] for tool in plan["remove"]["tools"]] == ["fs.catalog.read"]
            assert [workflow["workflow_id"] for workflow in plan["affected"]["workflows"]] == [
                "wf_uses_catalog"
            ]
            deleted = svc.delete_connector(
                first.connector_id,
                plan_revision=plan["revision"],
                scope=scope,
            )
            assert deleted == {"status": "trashed", "connector_id": first.connector_id}
            assert registry_manager.db_store.get_tool("fs.catalog.read") is None
            trashed_tool = next(
                tool
                for tool in registry_manager.db_store.list_tools(include_trashed=True)
                if tool["tool_id"] == "fs.catalog.read"
            )
            assert trashed_tool["enabled"] is False
            assert trashed_tool["deleted_at"]
            assert trashed_tool["availability_status"] == "unavailable"
            assert trashed_tool["availability_reason_code"] == "source_connector_removed"
            assert first.connector_id in trashed_tool["availability_reason_text"]
            assert connector_store.get(first.connector_id) is None
            trashed_connector = connector_store.get(first.connector_id, include_trashed=True)
            assert trashed_connector is not None
            assert trashed_connector["enabled"] is False
            assert trashed_connector["headers"] == {}
            assert trashed_connector["lifecycle_child_tool_ids_jsonb"] == ["fs.catalog.read"]
            assert registry_manager.db_store.workflows == workflow_snapshot

            # Trash restore retains the safe orphan snapshot. It never makes a
            # projection executable while its source connector is absent.
            registry_manager.db_store.rows["fs.catalog.read"]["deleted_at"] = None
            tool_commands = ToolCommandService(
                get_db=lambda: registry_manager.db_store,
                reload_registry=registry_manager.reload_all,
                get_tool_validation=lambda current_db, tool_id: {
                    "valid": True,
                    "errors": [],
                    "warnings": [],
                },
                format_validation_error_detail=lambda **kwargs: kwargs,
            )
            with pytest.raises(ServiceValidationError, match="managed only"):
                tool_commands.toggle_tool("fs.catalog.read")

            from core.application.registry.tool_registry import ToolRegistry

            executable_registry = ToolRegistry(registry_manager.db_store)
            assert executable_registry.get_enabled_tool("fs.catalog.read") is None
            assert "fs.catalog.read" not in executable_registry.build_resolver().list_registered()

            second = svc.create_connector(
                McpConnectorCreateInput(
                    display_name="Flow Steward MCP B",
                    base_url="https://fs-b.example/mcp",
                    namespace_prefix="fs",
                ),
                scope=scope,
            )
            svc.sync_connector(second.connector_id, scope=scope)
            # A second administrator cannot silently resurrect or rebind the
            # tombstone through a crafted allowlist update.
            registry_manager.db_store.delete_tool("fs.catalog.read")
            _raises_input_6016_1 = McpConnectorUpdateInput(allowlisted_tools=["fs.catalog.read"])
            with pytest.raises(ServiceConflictError, match="reserved") as exc_info:
                svc.update_connector(second.connector_id, _raises_input_6016_1, scope=scope)

        assert exc_info.value.payload["error"] == "projected_tool_id_conflict"
        assert registry_manager.db_store.get_tool("fs.catalog.read") is None
        assert connector_store.get(second.connector_id)["allowlisted_tools"] == []

    def test_delete_connector_rolls_back_projected_tools_when_row_delete_fails(self):
        svc, _bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        registry_manager = _FakeRegistryManager()
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "display_name": "Flow Steward MCP",
            "description": "",
            "transport": "streamable_http",
            "base_url": "https://fs.example/mcp",
            "auth_mode": "none",
            "headers": {},
            "allowlisted_tools": ["fs.catalog.read"],
            "namespace_prefix": "fs",
            "status": "active",
            "discovered_tools_jsonb": [
                {
                    "remote_tool_name": "catalog.read",
                    "projected_tool_id": "fs.catalog.read",
                    "display_name": "Catalog Read",
                    "description": "Read catalog rows.",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                    "annotations": {},
                }
            ],
            "last_synced_at": "2026-05-20T01:00:00Z",
            "last_sync_status": "success",
            "last_sync_error": "",
            "enabled": True,
            "created_at": "2026-05-20T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z",
        }

        svc._get_connector_row = MagicMock(return_value=dict(row))
        svc._soft_delete_connector_row = MagicMock(
            side_effect=RuntimeError("connector row delete failed")
        )
        registry_manager.db_store.save_tool(
            {
                "tool_id": "fs.catalog.read",
                "version": "1.0.0",
                "display_name": "Flow Steward MCP / Catalog Read",
                "description": "Projected MCP tool",
                "enabled": True,
                "category": "other",
                "prompt": "",
                "interface": {
                    "functions": [
                        {
                            "name": "fs.catalog.read",
                            "parameters": [],
                            "returns": {"name": "result", "type": "json", "required": False},
                        }
                    ]
                },
                "implementation": {
                    "type": "mcp",
                    "connector_id": "mcp_fs",
                    "remote_tool_name": "catalog.read",
                },
                "config": {"remote_tool_name": "catalog.read"},
                "tags": ["mcp", "derived"],
                "builder_metadata_jsonb": {},
            }
        )

        def _raises_operation_6041():
            plan = svc.preview_delete_connector("mcp_fs", scope=scope)
            svc.delete_connector("mcp_fs", plan_revision=plan["revision"], scope=scope)

        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry_manager,
            ),
            pytest.raises(RuntimeError, match="connector row delete failed"),
        ):
            _raises_operation_6041()

        restored_tool = registry_manager.db_store.get_tool("fs.catalog.read")
        assert restored_tool is not None
        assert restored_tool["enabled"] is True

    def test_delete_connector_does_not_soft_delete_when_local_oauth_secret_cleanup_fails(self):
        svc, _bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "display_name": "OAuth MCP",
            "auth_mode": "oauth2",
            "allowlisted_tools": [],
            "discovered_tools_jsonb": [],
            "oauth_secret_refs_jsonb": {
                "refs": {"oauth_client_secret": "mcp_connector:mcp_fs:oauth_client_secret"}
            },
            "enabled": True,
            "status": "active",
            "updated_at": "2026-09-04T12:00:00Z",
        }
        svc._get_connector_row = MagicMock(return_value=dict(row))
        svc._soft_delete_connector_row = MagicMock()
        registry_manager = _FakeRegistryManager()

        def _raises_operation_6084():
            plan = svc.preview_delete_connector("mcp_fs", scope=scope)
            svc.delete_connector("mcp_fs", plan_revision=plan["revision"], scope=scope)

        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry_manager,
            ),
            patch(
                "core.application.auth.mcp_connector_oauth._resolve_connector_oauth_secrets",
                return_value={},
            ),
            patch(
                "core.application.auth.mcp_connector_oauth.model_provider_store.delete_secret_ref",
                side_effect=RuntimeError("injected local secret delete failure"),
            ),
            patch(
                "core.application.auth.mcp_connector_oauth._locked_connector_row",
                return_value=dict(row),
            ),
            pytest.raises(RuntimeError, match="injected local secret delete failure"),
        ):
            _raises_operation_6084()

        svc._soft_delete_connector_row.assert_not_called()

    def test_disabling_connector_keeps_projected_tool_live_and_out_of_trash(self):
        svc, scope, row, _updates = self._configured_sync_service(
            discovered_tools=[
                {
                    "remote_tool_name": "catalog.read",
                    "projected_tool_id": "fs.catalog.read",
                    "display_name": "Catalog Read",
                    "description": "Read catalog rows.",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                    "annotations": {},
                }
            ]
        )
        row["allowlisted_tools"] = ["fs.catalog.read"]
        registry_manager = _FakeRegistryManager()

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry_manager,
        ):
            svc._sync_projected_registry_tools(dict(row))
            result = svc.update_connector(
                "mcp_fs",
                McpConnectorUpdateInput(enabled=False),
                scope=scope,
            )

        tool = registry_manager.db_store.get_tool("fs.catalog.read")
        assert result.enabled is False
        assert tool is not None
        assert tool["enabled"] is False
        assert tool.get("deleted_at") is None
        assert [
            item["tool_id"] for item in registry_manager.db_store.list_tools(include_trashed=True)
        ] == ["fs.catalog.read"]

    def test_delete_plan_groups_direct_and_agent_workflow_impact_and_rejects_stale_revision(
        self,
    ):
        svc, _bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        registry_manager = _FakeRegistryManager()
        connector = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "display_name": "Flow Steward MCP",
            "auth_mode": "none",
            "allowlisted_tools": ["fs.catalog.read", "fs.catalog.write"],
            "discovered_tools_jsonb": [],
            "enabled": False,
            "status": "disabled",
            "updated_at": "2026-08-22T05:00:00Z",
        }
        svc._get_connector_row = MagicMock(return_value=dict(connector))
        svc._soft_delete_connector_row = MagicMock()
        for tool_id in ("fs.catalog.read", "fs.catalog.write"):
            registry_manager.db_store.save_tool(
                {
                    "tool_id": tool_id,
                    "version": "1.0.0",
                    "display_name": tool_id,
                    "description": "Projected MCP tool",
                    "enabled": False,
                    "category": "other",
                    "prompt": "",
                    "interface": {"functions": []},
                    "implementation": {
                        "type": "mcp",
                        "connector_id": "mcp_fs",
                        "remote_tool_name": tool_id,
                        "owner_account_id": "acc_alpha",
                        "owner_project_id": "project_alpha",
                        "owner_tenant_id": "tenant_alpha",
                    },
                    "config": {
                        "owner_account_id": "acc_alpha",
                        "owner_project_id": "project_alpha",
                        "owner_tenant_id": "tenant_alpha",
                    },
                    "account_id": "acc_alpha",
                    "project_id": "project_alpha",
                    "tenant_id": "tenant_alpha",
                    "scope_kind": "project",
                    "tags": ["mcp", "derived"],
                }
            )
        registry_manager.db_store.tool_agents["fs.catalog.write"] = ["writer"]
        registry_manager.db_store.workflows = [
            {
                "workflow_id": "wf_direct",
                "display_name": "Direct",
                "account_id": "acc_alpha",
                "enabled": True,
                "status": "active",
                "steps": [
                    {
                        "step_kind": "connector",
                        "connector_id": "connector.fs.catalog.read",
                    }
                ],
            },
            {
                "workflow_id": "wf_agent",
                "display_name": "Agent mediated",
                "account_id": "acc_alpha",
                "enabled": False,
                "status": "disabled",
                "steps": [{"step_kind": "agent", "agent_id": "writer"}],
            },
            {
                "workflow_id": "wf_unrelated",
                "account_id": "acc_alpha",
                "enabled": True,
                "steps": [{"step_kind": "agent", "agent_id": "planner"}],
            },
            {
                "workflow_id": "wf_foreign",
                "account_id": "acc_beta",
                "enabled": True,
                "steps": [{"step_kind": "agent", "agent_id": "writer"}],
            },
        ]

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry_manager,
        ):
            first = svc.preview_delete_connector("mcp_fs", scope=scope)
            second = svc.preview_delete_connector("mcp_fs", scope=scope)
            assert first == second
            assert [row["tool_id"] for row in first["remove"]["tools"]] == [
                "fs.catalog.read",
                "fs.catalog.write",
            ]
            assert first["affected"]["workflows"] == [
                {
                    "display_name": "Agent mediated",
                    "enabled": False,
                    "status": "disabled",
                    "tool_ids": ["fs.catalog.write"],
                    "via_connector": False,
                    "workflow_id": "wf_agent",
                },
                {
                    "display_name": "Direct",
                    "enabled": True,
                    "status": "active",
                    "tool_ids": ["fs.catalog.read"],
                    "via_connector": False,
                    "workflow_id": "wf_direct",
                },
            ]
            assert "wf_unrelated" not in json.dumps(first)
            assert "wf_foreign" not in json.dumps(first)

            registry_manager.db_store.workflows.append(
                {
                    "workflow_id": "wf_new_dependency",
                    "account_id": "acc_alpha",
                    "enabled": True,
                    "steps": [{"step_kind": "agent", "agent_id": "writer"}],
                }
            )
            with pytest.raises(ServiceConflictError, match="impact changed"):
                svc.delete_connector(
                    "mcp_fs",
                    plan_revision=first["revision"],
                    scope=scope,
                )

        assert svc._soft_delete_connector_row.call_count == 0
        assert registry_manager.db_store.get_tool("fs.catalog.read") is not None
        assert registry_manager.db_store.get_tool("fs.catalog.write") is not None

    def test_allowlist_save_rolls_back_projected_tools_when_connector_update_fails(self):
        svc, _bl = self._make_service()
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="ops",
            principal_type="operator",
        )
        registry_manager = _FakeRegistryManager()
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "display_name": "Flow Steward MCP",
            "description": "",
            "transport": "streamable_http",
            "base_url": "https://fs.example/mcp",
            "auth_mode": "none",
            "headers": {},
            "allowlisted_tools": [],
            "namespace_prefix": "fs",
            "status": "active",
            "discovered_tools_jsonb": [
                {
                    "remote_tool_name": "catalog.read",
                    "projected_tool_id": "fs.catalog.read",
                    "display_name": "Catalog Read",
                    "description": "Read catalog rows.",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                    "annotations": {},
                }
            ],
            "last_synced_at": "2026-05-20T01:00:00Z",
            "last_sync_status": "success",
            "last_sync_error": "",
            "enabled": True,
            "created_at": "2026-05-20T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z",
        }

        svc._get_connector_row = MagicMock(return_value=dict(row))
        svc._update_connector_row = MagicMock(
            side_effect=RuntimeError("connector row write failed")
        )

        _raises_input_6401_1 = McpConnectorUpdateInput(allowlisted_tools=["fs.catalog.read"])
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry_manager,
            ),
            pytest.raises(RuntimeError, match="connector row write failed"),
        ):
            svc.update_connector("mcp_fs", _raises_input_6401_1, scope=scope)

        assert registry_manager.db_store.get_tool("fs.catalog.read") is None


class TestMcpConnectorQueryService:
    def test_list_empty(self):
        bl = MagicMock()
        bl.cursor.return_value.execute = MagicMock()
        bl.cursor.return_value.fetchall = MagicMock(return_value=[])
        svc = McpConnectorQueryService(get_logger=lambda: bl)
        result = svc.list_connectors(
            scope=ScopeContext(
                account_id="acc_default",
                project_id="project_default",
                tenant_id="tenant_default",
                actor_id="viewer",
                principal_type="viewer",
            )
        )
        assert result.total == 0
        assert result.connectors == []

    def test_get_connector_uses_account_scoped_lookup(self, monkeypatch):
        bl = MagicMock()
        svc = McpConnectorQueryService(get_logger=lambda: bl)
        scope = ScopeContext(
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
            actor_id="alice",
            principal_type="viewer",
        )
        get_row = MagicMock(return_value=None)
        monkeypatch.setattr(McpConnectorStore, "get_connector_row", get_row)

        with pytest.raises(ServiceNotFoundError):
            svc.get_connector("mcp_ctx7", scope=scope)

        get_row.assert_called_once_with(
            bl,
            "mcp_ctx7",
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
        )


class TestMcpConnectorRuntimeResolver:
    def test_load_mcp_connector_row_uses_database_url(self):
        from core.application.orchestration.tools import tool_resolver

        runtime_bl = MagicMock()
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "enabled": True,
        }

        with (
            patch.dict(
                "os.environ",
                {"DATABASE_URL": "postgres://fs.example/runtime"},
                clear=True,
            ),
            patch(
                "core.infrastructure.db.platform_stores.PlatformIdentityStore.get_shared_instance",
                return_value=runtime_bl,
            ) as get_logger,
            patch.object(
                McpConnectorStore,
                "get_connector_row",
                return_value=dict(row),
            ) as get_row,
        ):
            loaded = tool_resolver._load_mcp_connector_row(
                "mcp_fs",
                runtime_context={
                    "account_id": "acc_alpha",
                    "project_id": "project_alpha",
                    "tenant_id": "tenant_alpha",
                },
            )

        assert loaded == row
        get_logger.assert_called_once_with("postgres://fs.example/runtime")
        get_row.assert_called_once_with(
            runtime_bl,
            "mcp_fs",
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
        )

    def test_load_mcp_connector_row_rejects_runtime_manifest_account_mismatch(self):
        from core.application.orchestration.tools import tool_resolver

        runtime_bl = MagicMock()
        manifest = SimpleNamespace(
            implementation=SimpleNamespace(owner_account_id="acc_alpha"),
            config={"owner_account_id": "acc_alpha"},
        )

        with (
            patch.object(McpConnectorStore, "get_connector_row") as get_row,
            pytest.raises(RuntimeError, match="account"),
        ):
            tool_resolver._load_mcp_connector_row(
                "mcp_fs",
                manifest=manifest,
                runtime_context={"account_id": "acc_beta", "logger": runtime_bl},
            )

        get_row.assert_not_called()

    def test_remote_mcp_tool_rejects_cross_account_connector_row(self):
        from core.application.orchestration.tools import tool_resolver

        runtime_bl = MagicMock()
        manifest = SimpleNamespace(
            tool_id="fs.catalog.read",
            implementation=SimpleNamespace(
                connector_id="mcp_fs",
                remote_tool_name="catalog.read",
                owner_account_id="acc_alpha",
                owner_project_id="project_alpha",
                owner_tenant_id="tenant_alpha",
            ),
            config={
                "remote_tool_name": "catalog.read",
                "owner_account_id": "acc_alpha",
                "owner_project_id": "project_alpha",
                "owner_tenant_id": "tenant_alpha",
            },
        )

        with (
            patch.object(
                McpConnectorStore,
                "get_connector_row",
                return_value={
                    "connector_id": "mcp_fs",
                    "account_id": "acc_beta",
                    "project_id": "project_alpha",
                    "tenant_id": "tenant_alpha",
                    "enabled": True,
                    "allowlisted_tools": ["fs.catalog.read"],
                    "namespace_prefix": "fs",
                },
            ) as get_row,
            patch("core.application.auth.mcp_connector_oauth.mcp_connector_rpc_post") as rpc_post,
            pytest.raises(RuntimeError, match="account"),
        ):
            tool_resolver._invoke_remote_mcp_tool(
                manifest,
                function_name="fs.catalog.read",
                arguments={},
                runtime_context={
                    "account_id": "acc_alpha",
                    "project_id": "project_alpha",
                    "tenant_id": "tenant_alpha",
                    "logger": runtime_bl,
                },
            )

        get_row.assert_called_once_with(
            runtime_bl,
            "mcp_fs",
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
        )
        rpc_post.assert_not_called()

    def test_tool_resolver_runtime_dispatch_scopes_mcp_connector_lookup_to_owner_account(self):
        from core.application.orchestration.tools import tool_resolver
        from core.application.orchestration.tools.tool_resolver import ToolResolver
        from core.application.registry.tool_manifest import ToolManifest

        runtime_bl = MagicMock()
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "transport": "streamable_http",
            "base_url": "https://fs.example/mcp",
            "headers": {},
            "enabled": True,
            "allowlisted_tools": ["fs.catalog.read"],
            "namespace_prefix": "fs",
        }
        manifest = ToolManifest.model_validate(
            {
                "tool_id": "fs.catalog.read",
                "display_name": "Catalog read",
                "enabled": True,
                "interface": {
                    "functions": [
                        {
                            "name": "fs.catalog.read",
                            "parameters": [{"name": "limit", "type": "integer", "required": True}],
                            "returns": {"name": "result", "type": "json", "required": False},
                        }
                    ]
                },
                "implementation": {
                    "type": "mcp",
                    "connector_id": "mcp_fs",
                    "remote_tool_name": "catalog.read",
                    "owner_account_id": "acc_alpha",
                    "owner_project_id": "project_alpha",
                    "owner_tenant_id": "tenant_alpha",
                },
                "config": {
                    "owner_account_id": "acc_alpha",
                    "owner_project_id": "project_alpha",
                    "owner_tenant_id": "tenant_alpha",
                    "remote_tool_name": "catalog.read",
                },
            }
        )
        resolver = ToolResolver()

        with (
            patch.dict(
                "os.environ",
                {"DATABASE_URL": "postgres://fs.example/runtime"},
                clear=True,
            ),
            patch(
                "core.infrastructure.db.platform_stores.PlatformIdentityStore.get_shared_instance",
                return_value=runtime_bl,
            ),
            patch.object(
                McpConnectorStore,
                "get_connector_row",
                return_value=dict(row),
            ) as get_row,
            patch(
                "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_post",
                return_value={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "structuredContent": {
                            "result": {"rows": [{"sku": "A1"}]},
                        }
                    },
                },
            ) as rpc_post,
            patch.object(
                tool_resolver,
                "_resolve_mcp_tools",
                side_effect=AssertionError(
                    "projected remote MCP tools must not import the local MCP SDK client"
                ),
            ) as resolve_local_mcp_tools,
        ):
            resolver.register_from_manifest(manifest)
            assert resolver.resolve(["fs.catalog.read"]) == []
            [tool] = resolver.resolve_runtime(
                ["fs.catalog.read"],
                runtime_context={
                    "account_id": "acc_alpha",
                    "project_id": "project_alpha",
                    "tenant_id": "tenant_alpha",
                },
            )
            result = json.loads(tool.invoke({"limit": 5}))

        assert result == {"result": {"rows": [{"sku": "A1"}]}}
        assert get_row.call_args_list == [
            (
                (runtime_bl, "mcp_fs"),
                {
                    "account_id": "acc_alpha",
                    "project_id": "project_alpha",
                    "tenant_id": "tenant_alpha",
                },
            ),
        ]
        rpc_post.assert_called_once()
        resolve_local_mcp_tools.assert_not_called()
        assert rpc_post.call_args.kwargs["runtime_context"] == {
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
        }

    def test_tool_resolver_catalog_dispatch_rejects_runtime_owner_account_mismatch(self):
        from core.application.orchestration.tools.tool_resolver import ToolResolver
        from core.application.registry.tool_manifest import ToolManifest

        runtime_bl = MagicMock()
        row = {
            "connector_id": "mcp_fs",
            "account_id": "acc_alpha",
            "transport": "streamable_http",
            "base_url": "https://fs.example/mcp",
            "headers": {},
            "enabled": True,
            "allowlisted_tools": ["fs.catalog.read"],
            "namespace_prefix": "fs",
        }
        manifest = ToolManifest.model_validate(
            {
                "tool_id": "fs.catalog.read",
                "display_name": "Catalog read",
                "enabled": True,
                "interface": {
                    "functions": [
                        {
                            "name": "fs.catalog.read",
                            "parameters": [],
                            "returns": {"name": "result", "type": "json", "required": False},
                        }
                    ]
                },
                "implementation": {
                    "type": "mcp",
                    "connector_id": "mcp_fs",
                    "remote_tool_name": "catalog.read",
                    "owner_account_id": "acc_alpha",
                    "owner_project_id": "project_alpha",
                    "owner_tenant_id": "tenant_alpha",
                },
                "config": {
                    "owner_account_id": "acc_alpha",
                    "owner_project_id": "project_alpha",
                    "owner_tenant_id": "tenant_alpha",
                    "remote_tool_name": "catalog.read",
                },
            }
        )
        resolver = ToolResolver()

        with (
            patch.dict(
                "os.environ",
                {"DATABASE_URL": "postgres://fs.example/runtime"},
                clear=True,
            ),
            patch(
                "core.infrastructure.db.platform_stores.PlatformIdentityStore.get_shared_instance",
                return_value=runtime_bl,
            ),
            patch.object(
                McpConnectorStore,
                "get_connector_row",
                return_value=dict(row),
            ) as get_row,
            patch("core.application.auth.mcp_connector_oauth.mcp_connector_rpc_post") as rpc_post,
        ):
            resolver.register_from_manifest(manifest)
            [tool] = resolver.resolve_catalog(
                [manifest.catalog_entry()],
                runtime_context={"account_id": "acc_beta"},
            )
            result = tool.invoke({})

        assert "[ERROR]" in result
        assert "account" in result
        get_row.assert_not_called()
        rpc_post.assert_not_called()

    def test_allowlist_validation_uses_database_url_when_fail_closed(self):
        from core.application.orchestration.tools import tool_resolver

        runtime_bl = MagicMock()
        manifest = SimpleNamespace(
            implementation=SimpleNamespace(
                server="",
                owner_account_id="acc_alpha",
                owner_project_id="project_alpha",
                owner_tenant_id="tenant_alpha",
            ),
            config={
                "owner_account_id": "acc_alpha",
                "owner_project_id": "project_alpha",
                "owner_tenant_id": "tenant_alpha",
            },
            interface=SimpleNamespace(all_function_names=["fs.catalog.read"]),
        )

        with (
            patch.dict(
                "os.environ",
                {
                    "DATABASE_URL": "postgres://fs.example/runtime",
                    "FS_MCP_ALLOWLIST_FAIL_CLOSED": "true",
                },
                clear=True,
            ),
            patch(
                "core.infrastructure.db.platform_stores.PlatformIdentityStore.get_shared_instance",
                return_value=runtime_bl,
            ) as get_logger,
            patch.object(
                McpConnectorStore,
                "get_connector_row",
                return_value={
                    "connector_id": "mcp_fs",
                    "account_id": "acc_alpha",
                    "project_id": "project_alpha",
                    "tenant_id": "tenant_alpha",
                    "enabled": True,
                    "allowlisted_tools": ["fs.catalog.read"],
                    "namespace_prefix": "fs",
                },
            ) as get_row,
        ):
            tool_resolver._validate_mcp_connector_tool_access("mcp_fs", manifest)

        get_logger.assert_called_once_with("postgres://fs.example/runtime")
        get_row.assert_called_once_with(
            runtime_bl,
            "mcp_fs",
            account_id="acc_alpha",
            project_id="project_alpha",
            tenant_id="tenant_alpha",
        )


class _UrlopenResponse:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        _ = (exc_type, exc, tb)
        return False


def test_mcp_json_rpc_rejects_redirect_to_blocked_host(monkeypatch):
    from core.application.management.mcp_connector_client import _json_rpc_post_with_latency

    class _RedirectingOpener:
        def open(self, _request, timeout):
            _ = timeout
            raise urllib.error.HTTPError(
                url="https://fs.example/mcp",
                code=302,
                msg="Found",
                hdrs={"Location": "http://127.0.0.1/mcp"},
                fp=None,
            )

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: _RedirectingOpener())

    with pytest.raises(ServiceValidationError, match="host is not allowed"):
        _json_rpc_post_with_latency(
            url="https://fs.example/mcp",
            headers={},
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            timeout_seconds=1.0,
        )


def test_mcp_json_rpc_rechecks_dns_before_post(monkeypatch):
    from core.application.management.mcp_connector_client import _json_rpc_post_with_latency

    monkeypatch.setattr(
        "core.application.management.mcp_connector_http._resolve_hostname_ips",
        lambda _hostname, *, port: {"127.0.0.1"},
    )

    class _UnexpectedOpener:
        def open(self, _request, timeout):
            _ = timeout
            pytest.fail("unsafe connector URL must be rejected before opening the request")

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: _UnexpectedOpener())

    with pytest.raises(ServiceValidationError, match="resolves to a blocked address"):
        _json_rpc_post_with_latency(
            url="https://fs.example/mcp",
            headers={"Authorization": "Bearer secret"},
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            timeout_seconds=1.0,
        )


def test_mcp_connector_connection_rejects_rebound_private_peer(monkeypatch):
    import http.client

    from core.application.management.mcp_connector_http import _ValidatingHTTPConnection

    class _FakeSocket:
        def __init__(self) -> None:
            self.closed = False

        def getpeername(self):
            return ("127.0.0.1", 8100)

        def close(self) -> None:
            self.closed = True

    sockets: list[_FakeSocket] = []

    def _fake_connect(self):
        sock = _FakeSocket()
        sockets.append(sock)
        self.sock = sock

    monkeypatch.setattr(http.client.HTTPConnection, "connect", _fake_connect)

    conn = _ValidatingHTTPConnection("rebind.example", 80)
    with pytest.raises(ServiceValidationError, match="connected to a blocked address"):
        conn.connect()
    assert sockets
    assert sockets[0].closed is True


def test_mcp_connector_connection_allows_public_peer(monkeypatch):
    import http.client

    from core.application.management.mcp_connector_http import _ValidatingHTTPConnection

    class _FakeSocket:
        def getpeername(self):
            return ("93.184.216.34", 80)

        def close(self) -> None:
            pytest.fail("public peer should not be closed")

    def _fake_connect(self):
        self.sock = _FakeSocket()

    monkeypatch.setattr(http.client.HTTPConnection, "connect", _fake_connect)

    conn = _ValidatingHTTPConnection("example.com", 80)
    conn.connect()


def test_mcp_private_cleartext_delete_rejects_public_peer_before_request_bytes(monkeypatch):
    from core.application.extensions import url_safety

    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")

    class _FakeSocket:
        def __init__(self) -> None:
            self.closed = False
            self.sent: list[bytes] = []

        def getpeername(self):
            return ("93.184.216.34", 80)

        def sendall(self, data: bytes) -> None:
            self.sent.append(data)

        def close(self) -> None:
            self.closed = True

    sock = _FakeSocket()
    http_cls, _https_cls = url_safety._pinned_connection_classes(
        ("10.0.0.5",),
        purpose="MCP connector base_url",
        require_private=True,
    )
    connection = http_cls("private.example", 80)
    monkeypatch.setattr(connection, "_new_sock", lambda _ip: sock)

    with pytest.raises(url_safety.PinnedPeerError, match="pinned address set"):
        connection.request(
            "DELETE",
            "/mcp",
            headers={"Authorization": "Bearer must-not-write"},
        )

    assert sock.sent == []
    assert sock.closed is True


def test_generated_header_cleartext_send_rejects_dns_rebind_before_request_bytes(monkeypatch):
    from core.application.management.mcp_connector_http import _json_rpc_post_with_metadata

    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    monkeypatch.setattr(
        mcp_connector_http,
        "_resolve_hostname_ips",
        lambda _hostname, *, port: {"10.0.0.5"},
    )
    monkeypatch.setattr(
        "core.application.extensions.url_safety._descriptor_io._resolve_hostname_ips",
        lambda _hostname, *, port: {"93.184.216.34"},
    )
    opened: list[bool] = []
    monkeypatch.setattr(
        "socket.create_connection",
        lambda *_args, **_kwargs: opened.append(True),
    )

    with pytest.raises(ServiceValidationError, match="resolve only to private"):
        _json_rpc_post_with_metadata(
            url="http://dev-mcp.example.test/mcp",
            headers={},
            generated_transport_headers={"Mcp-Param-Api-Key": "per-call-secret"},
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
            timeout_seconds=1.0,
        )

    assert opened == []


def test_update_rejects_adding_credentials_to_public_cleartext_connector(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_connector_http,
        "_resolve_hostname_ips",
        lambda _hostname, *, port: {"93.184.216.34"},
    )
    row = {
        **_fixture_connector_row("http://public.example.test/mcp"),
        "auth_mode": "none",
        "headers": {},
        "oauth_metadata_jsonb": {},
        "oauth_secret_refs_jsonb": {},
    }
    service = McpConnectorCommandService(get_logger=lambda: object())
    service._get_connector_row = MagicMock(return_value=row)

    _raises_input_7037_1 = McpConnectorUpdateInput(auth_mode="bearer")
    _raises_input_7037_2 = ScopeContext(
        account_id=row["account_id"],
        project_id=row["project_id"],
        tenant_id=row["tenant_id"],
        actor_id="ops",
        principal_type="operator",
    )
    with pytest.raises(ServiceValidationError, match="must use an https") as exc_info:
        service.update_connector(
            row["connector_id"], _raises_input_7037_1, scope=_raises_input_7037_2
        )

    assert exc_info.value.payload == {
        "error": "insecure_mcp_endpoint",
        "field": "base_url",
        "message": "Credential-bearing MCP connector requests must use an https base_url",
    }


def test_update_oauth_headers_invalidates_existing_authorization(monkeypatch) -> None:
    row = {
        **_fixture_connector_row("https://public.example.test/mcp"),
        "auth_mode": "oauth2",
        "headers": {},
        "oauth_credential_revision": 3,
        "oauth_metadata_jsonb": {"token": {"resource": "https://public.example.test/mcp"}},
        "oauth_secret_refs_jsonb": {"refs": {"_oauth_access_token": "access-ref"}},
        "oauth_connected_identity": "operator@example.test",
    }
    service = McpConnectorCommandService(get_logger=lambda: object())
    service._get_connector_row = MagicMock(side_effect=lambda *_args, **_kwargs: dict(row))
    service._update_connector_row = MagicMock(
        side_effect=lambda _bl, _connector_id, updates, **_kwargs: row.update(updates)
    )
    vault = _FakeHeaderSecretVault()
    monkeypatch.setattr(
        "core.application.management.mcp_connector_services.save_connector_header_secrets",
        vault.save,
    )
    monkeypatch.setattr(
        "core.application.auth.mcp_connector_oauth.mcp_connector_oauth_reset_updates",
        lambda *_args, **_kwargs: {
            "oauth_secret_refs_jsonb": {},
            "oauth_metadata_jsonb": {},
            "oauth_credential_revision": 4,
            "oauth_connected_identity": "",
            "oauth_last_error": "reconnect",
            "auth_status": "auth_required",
        },
    )

    result = service.update_connector(
        row["connector_id"],
        McpConnectorUpdateInput(headers={"X-Tenant": "beta"}),
        scope=ScopeContext(
            account_id=row["account_id"],
            project_id=row["project_id"],
            tenant_id=row["tenant_id"],
            actor_id="ops",
            principal_type="operator",
        ),
    )

    assert result.auth_status == "auth_required"
    assert row["oauth_credential_revision"] == 4
    assert row["oauth_secret_refs_jsonb"] == {}
    assert vault.saves == [{"X-Tenant": "beta"}]


class TestMcpConnectorApiHttpContracts:
    @pytest.mark.parametrize(
        "query_key", ["api_key", "key", "auth", "access_key", "signature", "sig"]
    )
    def test_http_create_and_update_reject_query_credentials_before_persistence(
        self,
        query_key: str,
    ):
        canary_value = "query-canary-api-3c96e5"
        expected_error = {
            "error": "mcp_endpoint_query_credentials_forbidden",
            "field": "base_url",
            "message": (
                "MCP connector endpoint must not include credentials in query parameters; "
                "use Headers or OAuth settings."
            ),
        }
        request_headers = {"x-test-account-id": "acc_alpha", "x-test-role": "operator"}

        with _mcp_connector_api_client() as (client, store):
            rejected_create = client.post(
                "/api/mcp",
                headers=request_headers,
                json={
                    "display_name": "Rejected query credential",
                    "base_url": (f"https://public.example.test/mcp?{query_key}={canary_value}"),
                },
            )

            assert rejected_create.status_code == 400
            assert rejected_create.json() == expected_error
            assert canary_value not in rejected_create.text
            assert store.rows == {}

            created = client.post(
                "/api/mcp",
                headers=request_headers,
                json={
                    "display_name": "Safe query parameters",
                    "base_url": "https://public.example.test/mcp?region=eu&version=1",
                },
            )
            assert created.status_code == 200
            connector_id = created.json()["connector_id"]
            original_url = store.rows[connector_id]["base_url"]

            rejected_update = client.put(
                f"/api/mcp/{connector_id}",
                headers=request_headers,
                json={"base_url": (f"http://public.example.test/mcp?{query_key}={canary_value}")},
            )

            assert rejected_update.status_code == 400
            assert rejected_update.json() == expected_error
            assert canary_value not in rejected_update.text
            assert store.rows[connector_id]["base_url"] == original_url

    def test_http_update_headers_modes_are_typed_and_reach_command_service(self):
        with _mcp_connector_api_client() as (client, store):
            created = client.post(
                "/api/mcp",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json={
                    "display_name": "Header lifecycle MCP",
                    "base_url": "https://headers.example/mcp",
                    "auth_mode": "bearer",
                    "headers": {"Authorization": "Bearer original"},
                },
            )
            connector_id = created.json()["connector_id"]
            endpoint = f"/api/mcp/{connector_id}"
            request_headers = {
                "x-test-account-id": "acc_alpha",
                "x-test-role": "operator",
            }

            kept = client.put(
                endpoint,
                headers=request_headers,
                json={"display_name": "Kept headers", "headers_mode": "keep"},
            )
            assert kept.status_code == 200
            assert store.vault.resolve(None, store.rows[connector_id]) == {
                "Authorization": "Bearer original"
            }

            replaced = client.put(
                endpoint,
                headers=request_headers,
                json={
                    "headers_mode": "replace",
                    "headers": {"Authorization": "Bearer replacement"},
                },
            )
            assert replaced.status_code == 200
            assert store.vault.resolve(None, store.rows[connector_id]) == {
                "Authorization": "Bearer replacement"
            }

            cleared = client.put(
                endpoint,
                headers=request_headers,
                json={"headers_mode": "clear"},
            )
            assert cleared.status_code == 200
            assert store.vault.resolve(None, store.rows[connector_id]) == {}

    @pytest.mark.parametrize(
        ("payload", "expected_status", "expected_field"),
        [
            ({"headers_mode": "discard"}, 422, "headers_mode"),
            (
                {"headers_mode": "keep", "headers": {"X-Tenant": "alpha"}},
                400,
                "headers",
            ),
            (
                {"headers_mode": "clear", "headers": {"X-Tenant": "alpha"}},
                400,
                "headers",
            ),
            ({"headers_mode": "replace", "headers": {}}, 400, "headers"),
        ],
    )
    def test_http_update_rejects_invalid_header_lifecycle_payloads(
        self,
        payload,
        expected_status,
        expected_field,
    ):
        with _mcp_connector_api_client() as (client, _store):
            created = client.post(
                "/api/mcp",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json={
                    "display_name": "Header validation MCP",
                    "base_url": "https://headers.example/mcp",
                },
            )
            connector_id = created.json()["connector_id"]
            response = client.put(
                f"/api/mcp/{connector_id}",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json=payload,
            )

        assert response.status_code == expected_status
        body = response.json()
        if expected_status == 422:
            assert any(detail.get("loc", [])[-1:] == [expected_field] for detail in body["detail"])
        else:
            assert body["field"] == expected_field

    def test_client_metadata_document_is_public_and_does_not_lookup_connector(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "core.application.auth.serve_auth_runtime.public_base_url",
            lambda: "https://flow.example.test",
        )
        with (
            _mcp_connector_api_client() as (client, _store),
            patch.object(
                McpConnectorStore,
                "get_connector_row",
                side_effect=AssertionError("client metadata must not perform tenant lookup"),
            ),
        ):
            response = client.get("/api/mcp/oauth/client-metadata/mcp_alpha.json")

        assert response.status_code == 200
        body = response.json()
        assert body["client_id"] == (
            "https://flow.example.test/api/mcp/oauth/client-metadata/mcp_alpha.json"
        )
        assert body["client_name"] == "Flow Steward MCP Client"

    def test_oauth_credentials_endpoint_enforces_scope_validation_and_write_only_response(self):
        with _mcp_connector_api_client() as (client, store):
            created = client.post(
                "/api/mcp",
                headers={
                    "x-test-account-id": "acc_alpha",
                    "x-test-tenant-id": "tenant_alpha",
                    "x-test-role": "operator",
                },
                json={
                    "display_name": "OAuth MCP",
                    "base_url": "https://oauth.example/mcp",
                    "auth_mode": "oauth2",
                },
            )
            connector_id = created.json()["connector_id"]
            project_id = "00000000-0000-0000-0000-000000000001"

            missing_secret = client.post(
                f"/api/mcp/{connector_id}/oauth/credentials?project_id={project_id}",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json={"client_id": "client-1"},
            )
            assert missing_secret.status_code == 422

            empty_id = client.post(
                f"/api/mcp/{connector_id}/oauth/credentials?project_id={project_id}",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json={"client_id": "   ", "client_secret": "secret"},
            )
            assert empty_id.status_code == 422

            with patch(
                "core.application.auth.mcp_connector_oauth.save_mcp_connector_oauth_credentials"
            ) as save_credentials:
                save_credentials.return_value = {
                    "saved": True,
                    "has_client_id": True,
                    "has_client_secret": True,
                }

                forbidden = client.post(
                    f"/api/mcp/{connector_id}/oauth/credentials?project_id={project_id}",
                    headers={"x-test-account-id": "acc_alpha", "x-test-role": "viewer"},
                    json={"client_id": "client-1", "client_secret": "secret"},
                )
                assert forbidden.status_code == 403
                save_credentials.assert_not_called()

                wrong_project = client.post(
                    f"/api/mcp/{connector_id}/oauth/credentials?project_id=00000000-0000-0000-0000-000000000002",
                    headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                    json={"client_id": "client-1", "client_secret": "secret"},
                )
                assert wrong_project.status_code == 404

                wrong_tenant = client.post(
                    f"/api/mcp/{connector_id}/oauth/credentials?project_id={project_id}",
                    headers={
                        "x-test-account-id": "acc_alpha",
                        "x-test-tenant-id": "tenant_other",
                        "x-test-role": "operator",
                    },
                    json={"client_id": "client-1", "client_secret": "secret"},
                )
                assert wrong_tenant.status_code == 404

                valid = client.post(
                    f"/api/mcp/{connector_id}/oauth/credentials?project_id={project_id}",
                    headers={
                        "x-test-account-id": "acc_alpha",
                        "x-test-tenant-id": "tenant_alpha",
                        "x-test-role": "operator",
                    },
                    json={
                        "client_id": " client-1 ",
                        "client_secret": "  opaque secret  ",
                    },
                )

            assert valid.status_code == 200
            assert valid.json() == {
                "saved": True,
                "has_client_id": True,
                "has_client_secret": True,
            }
            assert "opaque secret" not in valid.text
            saved_row = store.rows[connector_id]
            save_credentials.assert_called_once()
            assert save_credentials.call_args.kwargs == {
                "connector_row": saved_row,
                "client_id": "client-1",
                "client_secret": "  opaque secret  ",
            }

    def test_oauth_start_returns_structured_manual_credentials_requirement(self):
        from core.application.auth import mcp_connector_oauth

        with _mcp_connector_api_client() as (client, _store):
            created = client.post(
                "/api/mcp",
                headers={
                    "x-test-account-id": "acc_alpha",
                    "x-test-tenant-id": "tenant_alpha",
                    "x-test-role": "operator",
                },
                json={
                    "display_name": "OAuth MCP",
                    "base_url": "https://oauth.example/mcp",
                    "auth_mode": "oauth2",
                },
            )
            connector_id = created.json()["connector_id"]
            project_id = "00000000-0000-0000-0000-000000000001"

            with patch(
                "core.application.auth.mcp_connector_oauth.start_mcp_connector_oauth",
                side_effect=mcp_connector_oauth.McpOAuthManualCredentialsRequired(),
            ):
                response = client.post(
                    f"/api/mcp/{connector_id}/oauth/start?project_id={project_id}",
                    headers={
                        "x-test-account-id": "acc_alpha",
                        "x-test-tenant-id": "tenant_alpha",
                        "x-test-role": "operator",
                    },
                    json={},
                )

        assert response.status_code == 400
        assert response.json()["code"] == "manual_credentials_required"
        assert "client credentials" in response.json()["message"]

    def test_http_crud_readback_parity_and_secret_redaction(self):
        with _mcp_connector_api_client() as (client, store):
            created = client.post(
                "/api/mcp",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json={
                    "display_name": "Flow Steward MCP",
                    "description": "Registry MCP",
                    "transport": "streamable_http",
                    "base_url": "https://fs.example/mcp",
                    "auth_mode": "bearer",
                    "headers": {"Authorization": "Bearer top-secret"},
                    "allowlisted_tools": ["fs.catalog.read", "fs.catalog.inspect"],
                    "namespace_prefix": "fs",
                },
            )

            assert created.status_code == 200
            create_body = created.json()
            connector_id = create_body["connector_id"]
            assert set(create_body) == _MCP_CONNECTOR_HTTP_FIELDS
            assert create_body["display_name"] == "Flow Steward MCP"
            assert create_body["auth_mode"] == "bearer"
            assert create_body["allowlisted_tools"] == ["fs.catalog.read", "fs.catalog.inspect"]
            assert create_body["last_sync_status"] == "never"
            assert create_body["discovered_tools_jsonb"] == []
            assert create_body["projected_tools"] == []
            assert "headers" not in create_body
            assert store.vault.resolve(None, store.rows[connector_id]) == {
                "Authorization": "Bearer top-secret"
            }

            store.rows[connector_id]["discovered_tools_jsonb"] = [
                {
                    "remote_tool_name": "catalog.read",
                    "projected_tool_id": "fs.catalog.read",
                    "display_name": "Catalog Read",
                    "description": "Read catalog rows.",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                    "annotations": {},
                },
                {
                    "remote_tool_name": "catalog.inspect",
                    "projected_tool_id": "fs.catalog.inspect",
                    "display_name": "Catalog Inspect",
                    "description": "Inspect catalog rows.",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                    "annotations": {},
                },
            ]
            store.rows[connector_id]["last_sync_status"] = "success"
            store.rows[connector_id]["last_synced_at"] = "2026-05-20T05:00:00Z"

            listed = client.get(
                "/api/mcp",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "viewer"},
            )
            assert listed.status_code == 200
            list_body = listed.json()
            assert set(list_body) == {"connectors", "total"}
            assert list_body["total"] == 1
            assert len(list_body["connectors"]) == 1
            assert set(list_body["connectors"][0]) == _MCP_CONNECTOR_HTTP_FIELDS
            assert list_body["connectors"][0]["connector_id"] == connector_id
            assert "headers" not in list_body["connectors"][0]

            cross_account_list = client.get(
                "/api/mcp",
                headers={"x-test-account-id": "acc_beta", "x-test-role": "viewer"},
            )
            assert cross_account_list.status_code == 200
            assert cross_account_list.json()["total"] == 0

            detail = client.get(
                f"/api/mcp/{connector_id}",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "viewer"},
            )
            assert detail.status_code == 200
            detail_body = detail.json()
            assert set(detail_body) == _MCP_CONNECTOR_HTTP_FIELDS
            assert detail_body["display_name"] == "Flow Steward MCP"
            assert detail_body["namespace_prefix"] == "fs"
            assert detail_body["last_sync_status"] == "success"
            assert (
                detail_body["discovered_tools_jsonb"][0]["projected_tool_id"] == "fs.catalog.read"
            )
            assert "headers" not in detail_body

            updated = client.put(
                f"/api/mcp/{connector_id}",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json={
                    "display_name": "Flow Steward MCP Prod",
                    "description": "Prod registry MCP",
                    "namespace_prefix": "fs",
                    "allowlisted_tools": ["fs.catalog.read"],
                    "enabled": False,
                },
            )
            assert updated.status_code == 200
            update_body = updated.json()
            assert set(update_body) == _MCP_CONNECTOR_HTTP_FIELDS
            assert update_body["display_name"] == "Flow Steward MCP Prod"
            assert update_body["description"] == "Prod registry MCP"
            assert update_body["namespace_prefix"] == "fs"
            assert update_body["allowlisted_tools"] == ["fs.catalog.read"]
            assert update_body["enabled"] is False
            assert update_body["projected_tools"][0]["tool_id"] == "fs.catalog.read"
            assert update_body["projected_tools"][0]["enabled"] is False
            assert "headers" not in update_body

            readback = client.get(
                f"/api/mcp/{connector_id}",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "viewer"},
            )
            assert readback.status_code == 200
            readback_body = readback.json()
            assert readback_body["display_name"] == "Flow Steward MCP Prod"
            assert readback_body["description"] == "Prod registry MCP"
            assert readback_body["namespace_prefix"] == "fs"
            assert readback_body["allowlisted_tools"] == ["fs.catalog.read"]
            assert readback_body["enabled"] is False
            assert store.vault.resolve(None, store.rows[connector_id]) == {
                "Authorization": "Bearer top-secret"
            }

    def test_http_health_contract_persists_status_and_returns_typed_errors(self):
        with _mcp_connector_api_client() as (client, store):
            created = client.post(
                "/api/mcp",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json={
                    "display_name": "Flow Steward MCP",
                    "base_url": "https://fs.example",
                    "headers": {"X-API-Key": "secret-key"},
                },
            )
            connector_id = created.json()["connector_id"]
            captured_call: dict[str, Any] = {}

            def _healthy_probe(*, url, headers, payload, timeout_seconds):
                captured_call.update(
                    {
                        "url": url,
                        "headers": dict(headers),
                        "payload": dict(payload),
                        "timeout_seconds": timeout_seconds,
                    }
                )
                return (
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"tools": []},
                    },
                    42,
                )

            with patch(
                "core.application.management.mcp_connector_services._json_rpc_post_with_latency",
                side_effect=_healthy_probe,
            ):
                healthy = client.post(
                    f"/api/mcp/{connector_id}/health",
                    headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                    json={},
                )

            assert healthy.status_code == 200
            healthy_body = healthy.json()
            assert set(healthy_body) == {
                "connector_id",
                "status",
                "auth_status",
                "latency_ms",
                "error",
                "checked_at",
            }
            assert healthy_body["connector_id"] == connector_id
            assert healthy_body["status"] == "active"
            assert healthy_body["error"] is None
            assert healthy_body["latency_ms"] == 42
            assert isinstance(healthy_body["checked_at"], str)
            assert healthy_body["checked_at"]
            assert store.rows[connector_id]["status"] == "active"
            assert captured_call["url"] == "https://fs.example"
            assert captured_call["headers"]["X-API-Key"] == "secret-key"
            assert captured_call["payload"]["method"] == "tools/list"

            degraded_error = ServiceUnavailableError(
                "MCP request failed with HTTP 503: Service Unavailable"
            )
            degraded_error.__cause__ = urllib.error.HTTPError(
                url="https://fs.example",
                code=503,
                msg="Service Unavailable",
                hdrs=None,
                fp=None,
            )
            with patch(
                "core.application.management.mcp_connector_services._json_rpc_post_with_latency",
                side_effect=degraded_error,
            ):
                degraded = client.post(
                    f"/api/mcp/{connector_id}/health",
                    headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                    json={},
                )

            assert degraded.status_code == 200
            degraded_body = degraded.json()
            assert degraded_body["status"] == "degraded"
            assert degraded_body["error"] == "HTTP 503"
            assert store.rows[connector_id]["status"] == "degraded"

    def test_http_health_oauth_failure_is_degraded_and_returns_fresh_auth_status(self):
        with _mcp_connector_api_client() as (client, store):
            created = client.post(
                "/api/mcp",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json={
                    "display_name": "Flow Steward MCP",
                    "base_url": "https://fs.example/mcp",
                    "auth_mode": "oauth2",
                },
            )
            connector_id = created.json()["connector_id"]

            def _oauth_failure(_conn, *, connector_row, payload, timeout_seconds):
                _ = (payload, timeout_seconds)
                store.update(
                    connector_row["connector_id"],
                    {
                        "auth_status": "refresh_failed",
                        "oauth_last_error": "MCP OAuth token refresh failed",
                    },
                    account_id=connector_row["account_id"],
                )
                from core.application.auth.oauth_client import OAuthConnectionError

                raise OAuthConnectionError("MCP OAuth token refresh failed")

            with patch(
                "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_post_with_latency",
                side_effect=_oauth_failure,
            ):
                response = client.post(
                    f"/api/mcp/{connector_id}/health",
                    headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                    json={},
                )

            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "degraded"
            assert body["auth_status"] == "refresh_failed"
            assert body["error"] == (
                "MCP connector health check failed; verify the endpoint and retry"
            )
            assert store.rows[connector_id]["status"] == "degraded"

    def test_http_create_rejects_unsupported_transport(self):
        with _mcp_connector_api_client() as (client, _store):
            created = client.post(
                "/api/mcp",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json={
                    "display_name": "Flow Steward MCP",
                    "base_url": "https://fs.example/mcp",
                    "transport": "sse",
                },
            )

            assert created.status_code == 400
            assert "streamable_http" in created.json()["error"]

    def test_http_sync_contract_persists_discovery_state(self):
        with _mcp_connector_api_client() as (client, store):
            created = client.post(
                "/api/mcp",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json={
                    "display_name": "Flow Steward MCP",
                    "base_url": "https://fs.example/mcp",
                    "namespace_prefix": "fs",
                },
            )
            connector_id = created.json()["connector_id"]

            with patch(
                "core.application.auth.mcp_connector_oauth.mcp_connector_rpc_context",
                side_effect=_rpc_context_from_responses(
                    [
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {
                                "tools": [
                                    {
                                        "name": "catalog.read",
                                        "title": "Catalog Read",
                                        "description": "Read catalog rows.",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {"limit": {"type": "integer"}},
                                            "required": ["limit"],
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                ),
            ):
                synced = client.post(
                    f"/api/mcp/{connector_id}/sync",
                    headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                    json={},
                )

            assert synced.status_code == 200
            body = synced.json()
            assert body["last_sync_status"] == "success"
            assert body["last_sync_error"] == ""
            assert body["discovered_tools_jsonb"][0]["projected_tool_id"] == "fs.catalog.read"
            assert store.rows[connector_id]["last_sync_status"] == "success"
            assert (
                store.rows[connector_id]["discovered_tools_jsonb"][0]["remote_tool_name"]
                == "catalog.read"
            )

    def test_http_scope_forbidden_and_delete_contract(self):
        with _mcp_connector_api_client() as (client, store):
            forbidden = client.post(
                "/api/mcp",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "viewer"},
                json={"display_name": "Viewer Blocked", "base_url": "https://viewer.example/mcp"},
            )
            assert forbidden.status_code == 403
            assert "operator role required" in forbidden.json()["error"].lower()

            created = client.post(
                "/api/mcp",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json={"display_name": "Scoped Connector", "base_url": "https://scope.example/mcp"},
            )
            connector_id = created.json()["connector_id"]

            cross_account_get = client.get(
                f"/api/mcp/{connector_id}",
                headers={"x-test-account-id": "acc_beta", "x-test-role": "viewer"},
            )
            assert cross_account_get.status_code == 404

            cross_account_delete = client.delete(
                f"/api/mcp/{connector_id}",
                headers={"x-test-account-id": "acc_beta", "x-test-role": "operator"},
            )
            assert cross_account_delete.status_code == 404

            cross_account_health = client.post(
                f"/api/mcp/{connector_id}/health",
                headers={"x-test-account-id": "acc_beta", "x-test-role": "operator"},
                json={},
            )
            assert cross_account_health.status_code == 404

            unconfirmed = client.delete(
                f"/api/mcp/{connector_id}",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
            )
            assert unconfirmed.status_code == 400
            assert "confirmed impact plan revision" in unconfirmed.json()["error"]

            plan = client.get(
                f"/api/mcp/{connector_id}/delete-plan",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
            )
            assert plan.status_code == 200
            assert plan.json()["connector_id"] == connector_id
            assert plan.json()["remove"]["tools"] == []

            store.rows[connector_id]["updated_at"] = "2026-08-22T06:30:00Z"
            stale = client.post(
                f"/api/mcp/{connector_id}/delete",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                json={"revision": plan.json()["revision"]},
            )
            assert stale.status_code == 409
            assert "impact changed" in stale.json()["error"]

            plan = client.get(
                f"/api/mcp/{connector_id}/delete-plan",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
            )
            assert plan.status_code == 200

            deleted = client.delete(
                f"/api/mcp/{connector_id}",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "operator"},
                params={
                    "revision": plan.json()["revision"],
                    "project_id": "00000000-0000-0000-0000-000000000001",
                },
            )
            assert deleted.status_code == 200, deleted.json()
            assert deleted.json() == {
                "status": "trashed",
                "connector_id": connector_id,
            }

            missing_after_delete = client.get(
                f"/api/mcp/{connector_id}",
                headers={"x-test-account-id": "acc_alpha", "x-test-role": "viewer"},
            )
            assert missing_after_delete.status_code == 404
