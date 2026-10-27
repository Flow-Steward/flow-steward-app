"""Deterministic loopback-only Streamable HTTP MCP fixture servers."""

from __future__ import annotations

import base64
import copy
import json
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MCP_LEGACY_VERSION = "2025-11-25"
MCP_MODERN_VERSION = "2026-07-28"


@dataclass(frozen=True)
class McpRequestRecord:
    """One complete request observed by a fixture server."""

    http_method: str
    path: str
    headers: dict[str, str]
    payload: dict[str, Any] | None


@dataclass
class LegacySessionRecord:
    """Server-side state retained for one deterministic legacy session."""

    session_id: str
    negotiated_version: str = MCP_LEGACY_VERSION
    initialized: bool = False
    expired: bool = False
    closed: bool = False
    current_url: str = ""
    clicks: list[str] = field(default_factory=list)


@dataclass
class LegacyMcpFixture:
    """Observable state for a legacy stateful fixture server."""

    pages: list[list[dict[str, Any]]]
    cursors: list[str]
    expire_session_once: bool = False
    accepted_authorizations: frozenset[str] = frozenset()
    request_delay_seconds: float = 0.0
    result_text: str = "fixture-final-result"
    discovery_http_status: int = 200
    discovery_error_code: int = -32601
    discovery_error_message: str = "Method not found"
    discovery_error_data: dict[str, Any] | None = None
    discovery_raw_body: str | None = None
    url: str = ""
    requests: list[McpRequestRecord] = field(default_factory=list)
    sessions: list[LegacySessionRecord] = field(default_factory=list)
    delete_count: int = 0
    max_active_requests: int = 0
    violations: list[str] = field(default_factory=list)
    _active_requests: int = 0
    _expiry_issued: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def session(self, session_id: str) -> LegacySessionRecord | None:
        with self._lock:
            return next(
                (record for record in self.sessions if record.session_id == session_id),
                None,
            )


@dataclass
class ModernMcpFixture:
    """Observable state for a modern stateless fixture server."""

    pages: list[list[dict[str, Any]]]
    cursors: list[str]
    unauthorized_once_for: str = ""
    accepted_authorizations: frozenset[str] = frozenset()
    request_delay_seconds: float = 0.0
    expected_user_agent: str = ""
    tools_list_http_status: int = 0
    tools_list_raw_body: str | None = None
    url: str = ""
    requests: list[McpRequestRecord] = field(default_factory=list)
    sessions: list[LegacySessionRecord] = field(default_factory=list)
    delete_count: int = 0
    max_active_requests: int = 0
    violations: list[str] = field(default_factory=list)
    _active_requests: int = 0
    _unauthorized_issued: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


def _default_pages() -> list[list[dict[str, Any]]]:
    return [
        [
            {
                "name": "browser_navigate",
                "description": "Navigate the fixture browser.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            }
        ],
        [
            {
                "name": "browser_click",
                "description": "Click in the fixture browser.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"selector": {"type": "string"}},
                    "required": ["selector"],
                },
            }
        ],
    ]


def _copy_pages(pages: Sequence[Sequence[dict[str, Any]]] | None) -> list[list[dict[str, Any]]]:
    selected = pages if pages is not None else _default_pages()
    normalized = [[copy.deepcopy(tool) for tool in page] for page in selected]
    return normalized or [[]]


def _copy_cursors(page_count: int, cursors: Sequence[str] | None) -> list[str]:
    if cursors is None:
        return [f"cursor-{index + 2}" for index in range(max(page_count - 1, 0))]
    normalized = [str(cursor) for cursor in cursors]
    if len(normalized) != max(page_count - 1, 0):
        raise ValueError("MCP fixture cursors must connect every adjacent page")
    return normalized


def _request_headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    return {key.lower(): value for key, value in handler.headers.items()}


def _read_payload(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _write_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    *,
    payload: dict[str, Any] | None = None,
    body: str = "",
    content_type: str = "application/json",
    headers: dict[str, str] | None = None,
) -> None:
    encoded = body.encode("utf-8") if body else b""
    if payload is not None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    if encoded:
        handler.send_header("Content-Type", content_type)
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    if encoded:
        handler.wfile.write(encoded)


def _json_rpc_result(payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}


def _json_rpc_error(
    payload: dict[str, Any],
    code: int,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = copy.deepcopy(data)
    return {
        "jsonrpc": "2.0",
        "id": payload.get("id"),
        "error": error,
    }


def _page_result(
    pages: list[list[dict[str, Any]]],
    params: dict[str, Any],
    cursors: list[str],
) -> dict[str, Any] | None:
    if "cursor" not in params:
        page_index = 0
    else:
        cursor = params.get("cursor")
        if not isinstance(cursor, str) or cursor not in cursors:
            return None
        page_index = cursors.index(cursor) + 1
    if page_index < 0 or page_index >= len(pages):
        return None
    result: dict[str, Any] = {"tools": copy.deepcopy(pages[page_index])}
    if page_index + 1 < len(pages):
        result["nextCursor"] = cursors[page_index]
    return result


def _enter_request(fixture: LegacyMcpFixture | ModernMcpFixture) -> None:
    with fixture._lock:
        fixture._active_requests += 1
        fixture.max_active_requests = max(
            fixture.max_active_requests,
            fixture._active_requests,
        )


def _leave_request(fixture: LegacyMcpFixture | ModernMcpFixture) -> None:
    with fixture._lock:
        fixture._active_requests -= 1


def _record_post(
    fixture: LegacyMcpFixture | ModernMcpFixture,
    handler: BaseHTTPRequestHandler,
    payload: dict[str, Any] | None,
) -> dict[str, str]:
    headers = _request_headers(handler)
    record = McpRequestRecord(
        http_method="POST",
        path=handler.path,
        headers=headers,
        payload=copy.deepcopy(payload),
    )
    with fixture._lock:
        fixture.requests.append(record)
    return headers


def _authorization_is_allowed(
    fixture: LegacyMcpFixture | ModernMcpFixture,
    headers: dict[str, str],
) -> bool:
    if not fixture.accepted_authorizations:
        return True
    return str(headers.get("authorization") or "") in fixture.accepted_authorizations


def _unauthorized(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
    _write_response(
        handler,
        401,
        payload=_json_rpc_error(payload, -32001, "Unauthorized"),
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


def _legacy_handler(fixture: LegacyMcpFixture) -> type[BaseHTTPRequestHandler]:
    class LegacyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            _enter_request(fixture)
            try:
                payload = _read_payload(self)
                headers = _record_post(fixture, self, payload)
                if payload is None:
                    fixture.violations.append("request body was not a JSON object")
                    _write_response(self, 400, payload={"error": "invalid request"})
                    return
                if not _authorization_is_allowed(fixture, headers):
                    _unauthorized(self, payload)
                    return
                method = str(payload.get("method") or "")
                protocol_headers = {
                    key
                    for key in headers
                    if key in {"mcp-protocol-version", "mcp-method", "mcp-name", "mcp-session-id"}
                }
                if method == "initialize" and protocol_headers:
                    fixture.violations.append("legacy initialize received protocol headers")
                if method in {"notifications/initialized", "tools/list", "tools/call"} and (
                    "mcp-method" in headers or "mcp-name" in headers
                ):
                    fixture.violations.append("legacy traffic received modern transport headers")
                if method in {"tools/list", "tools/call"} and fixture.request_delay_seconds:
                    time.sleep(fixture.request_delay_seconds)
                if method == "server/discover":
                    if fixture.discovery_raw_body is not None:
                        _write_response(
                            self,
                            fixture.discovery_http_status,
                            body=fixture.discovery_raw_body,
                        )
                        return
                    _write_response(
                        self,
                        fixture.discovery_http_status,
                        payload=_json_rpc_error(
                            payload,
                            fixture.discovery_error_code,
                            fixture.discovery_error_message,
                            data=fixture.discovery_error_data,
                        ),
                    )
                    return
                if method == "initialize":
                    self._initialize(payload)
                    return
                if method == "notifications/initialized":
                    self._initialized(payload, headers)
                    return
                if method in {"tools/list", "tools/call"}:
                    self._session_request(payload, headers, method)
                    return
                _write_response(
                    self,
                    200,
                    payload=_json_rpc_error(payload, -32601, "Method not found"),
                )
            finally:
                _leave_request(fixture)

        def _initialize(self, payload: dict[str, Any]) -> None:
            with fixture._lock:
                session = LegacySessionRecord(
                    session_id=f"legacy-session-{len(fixture.sessions) + 1}",
                )
                fixture.sessions.append(session)
            _write_response(
                self,
                200,
                payload=_json_rpc_result(
                    payload,
                    {
                        "protocolVersion": MCP_LEGACY_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "legacy-fixture", "version": "1.0"},
                    },
                ),
                headers={"Mcp-Session-Id": session.session_id},
            )

        def _initialized(
            self,
            payload: dict[str, Any],
            headers: dict[str, str],
        ) -> None:
            session = fixture.session(str(headers.get("mcp-session-id") or ""))
            if session is None:
                _write_response(
                    self, 404, payload=_json_rpc_error(payload, -32002, "Unknown session")
                )
                return
            with fixture._lock:
                session.initialized = True
            _write_response(self, 202)

        def _session_request(
            self,
            payload: dict[str, Any],
            headers: dict[str, str],
            method: str,
        ) -> None:
            session = fixture.session(str(headers.get("mcp-session-id") or ""))
            if session is None or not session.initialized or session.closed:
                _write_response(
                    self, 404, payload=_json_rpc_error(payload, -32002, "Unknown session")
                )
                return
            with fixture._lock:
                should_expire = fixture.expire_session_once and not fixture._expiry_issued
                if should_expire:
                    fixture._expiry_issued = True
                    session.expired = True
            if should_expire:
                _write_response(
                    self, 404, payload=_json_rpc_error(payload, -32002, "Expired session")
                )
                return
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            if method == "tools/list":
                result = _page_result(fixture.pages, params, fixture.cursors)
                if result is None:
                    _write_response(
                        self, 400, payload=_json_rpc_error(payload, -32602, "Bad cursor")
                    )
                    return
                _write_response(self, 200, payload=_json_rpc_result(payload, result))
                return
            name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if name == "browser_navigate":
                with fixture._lock:
                    session.current_url = str(arguments.get("url") or "")
                _write_response(
                    self,
                    200,
                    payload=_json_rpc_result(
                        payload,
                        {
                            "content": [{"type": "text", "text": "navigated"}],
                            "structuredContent": {"url": session.current_url},
                        },
                    ),
                )
                return
            if name == "browser_click":
                selector = str(arguments.get("selector") or "")
                with fixture._lock:
                    session.clicks.append(selector)
                    current_url = session.current_url
                response_id = payload.get("id")
                events = [
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {"progress": 1},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": f"unrelated-{response_id}",
                        "result": {"content": [{"type": "text", "text": "unrelated"}]},
                    },
                    _json_rpc_result(
                        payload,
                        {
                            "content": [{"type": "text", "text": fixture.result_text}],
                            "structuredContent": {
                                "url": current_url,
                                "selector": selector,
                            },
                        },
                    ),
                ]
                body = "".join(
                    f"event: message\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
                    for event in events
                )
                _write_response(self, 200, body=body, content_type="text/event-stream")
                return
            _write_response(self, 200, payload=_json_rpc_error(payload, -32601, "Unknown tool"))

        def do_DELETE(self) -> None:
            headers = _request_headers(self)
            record = McpRequestRecord(
                http_method="DELETE",
                path=self.path,
                headers=headers,
                payload=None,
            )
            with fixture._lock:
                fixture.requests.append(record)
                fixture.delete_count += 1
                session = fixture.session(str(headers.get("mcp-session-id") or ""))
                if session is not None:
                    session.closed = True
            _write_response(self, 202)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return LegacyHandler


def _modern_contract_violations(
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    expected_user_agent: str = "",
) -> list[str]:
    violations: list[str] = []
    method = str(payload.get("method") or "")
    params = payload.get("params")
    if method == "initialize":
        violations.append("modern fixture received initialize")
    if headers.get("mcp-session-id"):
        violations.append("modern fixture received a session header")
    if "application/json" not in str(headers.get("accept") or ""):
        violations.append("Accept header omitted application/json")
    if "text/event-stream" not in str(headers.get("accept") or ""):
        violations.append("Accept header omitted text/event-stream")
    if "application/json" not in str(headers.get("content-type") or ""):
        violations.append("Content-Type header was not application/json")
    actual_user_agent = str(headers.get("user-agent") or "").strip()
    if not actual_user_agent:
        violations.append("User-Agent header was empty")
    elif expected_user_agent and actual_user_agent != expected_user_agent:
        violations.append("User-Agent header did not match the expected agent")
    if headers.get("mcp-protocol-version") != MCP_MODERN_VERSION:
        violations.append("MCP-Protocol-Version header was not modern")
    if headers.get("mcp-method") != method:
        violations.append("Mcp-Method header did not match the JSON-RPC method")
    expected_name = ""
    if isinstance(params, dict) and isinstance(params.get("name"), str):
        expected_name = str(params.get("name") or "")
    actual_name = str(headers.get("mcp-name") or "")
    if actual_name.startswith("=?base64?") and actual_name.endswith("?="):
        try:
            actual_name = base64.b64decode(actual_name[9:-2], validate=True).decode("utf-8")
        except Exception:
            violations.append("Mcp-Name header used invalid value encoding")
    if actual_name != expected_name:
        violations.append("Mcp-Name header did not match params.name")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict):
        violations.append("params._meta was not an object")
        return violations
    if meta.get("io.modelcontextprotocol/protocolVersion") != MCP_MODERN_VERSION:
        violations.append("reserved protocol metadata was not modern")
    client_info = meta.get("io.modelcontextprotocol/clientInfo")
    if not (
        isinstance(client_info, dict)
        and client_info.get("name") == "flow-steward"
        and isinstance(client_info.get("version"), str)
        and client_info.get("version")
    ):
        violations.append("reserved client metadata was invalid")
    if meta.get("io.modelcontextprotocol/clientCapabilities") != {}:
        violations.append("reserved client capabilities metadata was invalid")
    return violations


def _modern_handler(fixture: ModernMcpFixture) -> type[BaseHTTPRequestHandler]:
    class ModernHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            _enter_request(fixture)
            try:
                payload = _read_payload(self)
                headers = _record_post(fixture, self, payload)
                if payload is None:
                    fixture.violations.append("request body was not a JSON object")
                    _write_response(self, 400, payload={"error": "invalid request"})
                    return
                violations = _modern_contract_violations(
                    payload,
                    headers,
                    expected_user_agent=fixture.expected_user_agent,
                )
                if violations:
                    with fixture._lock:
                        fixture.violations.extend(violations)
                    _write_response(
                        self,
                        400,
                        payload=_json_rpc_error(payload, -32600, "Invalid modern request"),
                    )
                    return
                method = str(payload.get("method") or "")
                with fixture._lock:
                    issue_unauthorized = (
                        method == fixture.unauthorized_once_for and not fixture._unauthorized_issued
                    )
                    if issue_unauthorized:
                        fixture._unauthorized_issued = True
                if issue_unauthorized:
                    _unauthorized(self, payload)
                    return
                if not _authorization_is_allowed(fixture, headers):
                    _unauthorized(self, payload)
                    return
                if method in {"tools/list", "tools/call"} and fixture.request_delay_seconds:
                    time.sleep(fixture.request_delay_seconds)
                params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                if method == "tools/list" and fixture.tools_list_http_status:
                    _write_response(
                        self,
                        fixture.tools_list_http_status,
                        body=fixture.tools_list_raw_body or "",
                    )
                    return
                if method == "server/discover":
                    _write_response(
                        self,
                        200,
                        payload=_json_rpc_result(
                            payload,
                            {
                                "supportedVersions": [MCP_MODERN_VERSION],
                                "capabilities": {},
                                "serverInfo": {"name": "modern-fixture", "version": "1.0"},
                            },
                        ),
                    )
                    return
                if method == "tools/list":
                    result = _page_result(fixture.pages, params, fixture.cursors)
                    if result is None:
                        _write_response(
                            self,
                            400,
                            payload=_json_rpc_error(payload, -32602, "Bad cursor"),
                        )
                        return
                    _write_response(self, 200, payload=_json_rpc_result(payload, result))
                    return
                if method == "tools/call":
                    _write_response(
                        self,
                        200,
                        payload=_json_rpc_result(
                            payload,
                            {
                                "content": [{"type": "text", "text": "modern-result"}],
                                "structuredContent": {"name": params.get("name")},
                            },
                        ),
                    )
                    return
                _write_response(
                    self,
                    400,
                    payload=_json_rpc_error(payload, -32601, "Method not found"),
                )
            finally:
                _leave_request(fixture)

        def do_DELETE(self) -> None:
            headers = _request_headers(self)
            with fixture._lock:
                fixture.delete_count += 1
                fixture.requests.append(
                    McpRequestRecord(
                        http_method="DELETE",
                        path=self.path,
                        headers=headers,
                        payload=None,
                    )
                )
                fixture.violations.append("modern fixture received DELETE")
            _write_response(self, 405)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return ModernHandler


@contextmanager
def legacy_mcp_server(
    *,
    pages: Sequence[Sequence[dict[str, Any]]] | None = None,
    cursors: Sequence[str] | None = None,
    expire_session_once: bool = False,
    accepted_authorizations: Sequence[str] = (),
    request_delay_seconds: float = 0.0,
    result_text: str = "fixture-final-result",
    discovery_http_status: int = 200,
    discovery_error_code: int = -32601,
    discovery_error_message: str = "Method not found",
    discovery_error_data: dict[str, Any] | None = None,
    discovery_raw_body: str | None = None,
) -> Iterator[LegacyMcpFixture]:
    """Run a deterministic legacy stateful server on an ephemeral loopback port."""

    fixture_pages = _copy_pages(pages)
    fixture = LegacyMcpFixture(
        pages=fixture_pages,
        cursors=_copy_cursors(len(fixture_pages), cursors),
        expire_session_once=expire_session_once,
        accepted_authorizations=frozenset(accepted_authorizations),
        request_delay_seconds=request_delay_seconds,
        result_text=result_text,
        discovery_http_status=discovery_http_status,
        discovery_error_code=discovery_error_code,
        discovery_error_message=discovery_error_message,
        discovery_error_data=copy.deepcopy(discovery_error_data),
        discovery_raw_body=discovery_raw_body,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _legacy_handler(fixture))
    fixture.url = f"http://127.0.0.1:{server.server_port}/mcp"
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        name="legacy-mcp-fixture",
        daemon=True,
    )
    thread.start()
    try:
        yield fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
        if thread.is_alive():
            raise RuntimeError("legacy MCP fixture server did not stop")


@contextmanager
def modern_mcp_server(
    *,
    pages: Sequence[Sequence[dict[str, Any]]] | None = None,
    cursors: Sequence[str] | None = None,
    unauthorized_once_for: str = "",
    accepted_authorizations: Sequence[str] = (),
    request_delay_seconds: float = 0.0,
    expected_user_agent: str = "",
    tools_list_http_status: int = 0,
    tools_list_raw_body: str | None = None,
) -> Iterator[ModernMcpFixture]:
    """Run a deterministic modern stateless server on an ephemeral loopback port."""

    fixture_pages = _copy_pages(pages)
    fixture = ModernMcpFixture(
        pages=fixture_pages,
        cursors=_copy_cursors(len(fixture_pages), cursors),
        unauthorized_once_for=str(unauthorized_once_for or "").strip(),
        accepted_authorizations=frozenset(accepted_authorizations),
        request_delay_seconds=request_delay_seconds,
        expected_user_agent=str(expected_user_agent or "").strip(),
        tools_list_http_status=tools_list_http_status,
        tools_list_raw_body=tools_list_raw_body,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _modern_handler(fixture))
    fixture.url = f"http://127.0.0.1:{server.server_port}/mcp"
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        name="modern-mcp-fixture",
        daemon=True,
    )
    thread.start()
    try:
        yield fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
        if thread.is_alive():
            raise RuntimeError("modern MCP fixture server did not stop")
