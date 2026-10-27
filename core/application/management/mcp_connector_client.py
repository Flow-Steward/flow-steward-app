"""Streamable HTTP MCP sessions and per-execution client ownership."""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from core.application.extensions import url_safety
from core.infrastructure.config.version import APP_VERSION

from .errors import (
    ServiceUnavailableError,
    ServiceValidationError,
)
from .mcp_connector_http import (
    _connector_rpc_url,
    _encode_mcp_http_value,
    _json_rpc_post_with_metadata,
    _with_mcp_platform_headers,
)
from .mcp_connector_observability import (
    _ACTIVE_MCP_OBSERVABILITY,
    _mcp_log_extra,
    _McpObservabilityState,
    logger,
)
from .mcp_connector_protocol import (
    _MCP_MODERN_METHODS,
    _MESSAGE_MCP_REQUEST_PARAMS_MUST_BE_AN_OBJECT,
    _TOKEN_SERVER_DISCOVER,
    MCP_LEGACY_PREFERRED_VERSION,
    MCP_LEGACY_VERSIONS,
    MCP_MODERN_VERSION,
    McpExecutionScopeInactiveError,
    McpProtocolEra,
    McpProtocolError,
    McpSessionExpiredError,
    _next_json_rpc_id,
    _normalized_mcp_request_params,
)


@dataclass
class _LegacyMcpState:
    negotiated_version: str = ""
    session_id: str = ""
    opened: bool = False
    closed: bool = False


@dataclass(frozen=True)
class _ExecutionMcpRegistryKey:
    attempt_token: str
    account_id: str
    job_id: str
    connector_id: str


@dataclass
class _ExecutionMcpScopeState:
    attempt_token: str
    account_id: str
    job_id: str
    closing: bool = False


_ACTIVE_MCP_EXECUTION_SCOPE: ContextVar[_ExecutionMcpScopeState | None] = ContextVar(
    "active_mcp_execution_scope",
    default=None,
)


_EXECUTION_MCP_CLIENTS: dict[_ExecutionMcpRegistryKey, StreamableHttpMcpClient] = {}


_EXECUTION_MCP_CLIENTS_LOCK = threading.RLock()


class StreamableHttpMcpSession:
    """Compatibility wrapper for one negotiated legacy Streamable HTTP session."""

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str] | None = None,
        timeout_seconds: float,
        headers_provider: Callable[[], dict[str, str]] | None = None,
        observability_state: _McpObservabilityState | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout_seconds = timeout_seconds
        self._headers_provider = headers_provider
        self._observability_state = observability_state or _McpObservabilityState()
        self._state = _LegacyMcpState()

    @property
    def session_id(self) -> str:
        return self._state.session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self._state.session_id = str(value or "").strip()

    @property
    def negotiated_version(self) -> str:
        return self._state.negotiated_version

    @property
    def _opened(self) -> bool:
        return self._state.opened

    @_opened.setter
    def _opened(self, value: bool) -> None:
        self._state.opened = bool(value)

    def _current_headers(self) -> dict[str, str]:
        if self._headers_provider is None:
            return dict(self.headers)
        return dict(self._headers_provider())

    def open(self, *, lifecycle_event: str = "open") -> None:
        if self._state.closed:
            raise McpProtocolError("MCP client is closed")
        if self._state.opened:
            return
        initialize_request_id = _next_json_rpc_id()
        initialize, response_headers, _latency_ms = _json_rpc_post_with_metadata(
            url=self.url,
            headers=self._current_headers(),
            payload={
                "jsonrpc": "2.0",
                "id": initialize_request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_LEGACY_PREFERRED_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "flow-steward", "version": APP_VERSION},
                },
            },
            timeout_seconds=self.timeout_seconds,
            expected_request_id=initialize_request_id,
        )
        session_id = str(response_headers.get("mcp-session-id") or "").strip()
        self._state.session_id = session_id
        try:
            result = (initialize or {}).get("result")
            if not isinstance(result, dict):
                raise McpProtocolError("MCP initialize returned a malformed result payload")
            negotiated_version = str(result.get("protocolVersion") or "").strip()
            if negotiated_version not in MCP_LEGACY_VERSIONS:
                raise McpProtocolError("MCP initialize returned an unsupported protocol version")
            self._state.negotiated_version = negotiated_version
            _json_rpc_post_with_metadata(
                url=self.url,
                headers=self._current_headers(),
                payload={"jsonrpc": "2.0", "method": "notifications/initialized"},
                timeout_seconds=self.timeout_seconds,
                session_id=session_id,
                allow_empty_response=True,
                protocol_version=negotiated_version,
            )
            self._state.opened = True
        except Exception:
            self._terminate_session(
                session_id=session_id,
                protocol_version=self._state.negotiated_version or MCP_LEGACY_PREFERRED_VERSION,
            )
            self._state.session_id = ""
            self._state.negotiated_version = ""
            self._state.opened = False
            raise
        self._observability_state.protocol_era = McpProtocolEra.LEGACY.value
        self._observability_state.protocol_version = negotiated_version
        logger.info(
            (
                "MCP legacy session reinitialized"
                if lifecycle_event == "reinitialize"
                else "MCP legacy session opened"
            ),
            extra=_mcp_log_extra(
                self._observability_state,
                session_event=lifecycle_event,
            ),
        )

    def request(
        self,
        payload: dict[str, Any],
        *,
        name: str = "",
    ) -> tuple[dict[str, Any], int]:
        method = str(payload.get("method") or "").strip()
        raw_params = payload.get("params")
        if raw_params is not None and not isinstance(raw_params, dict):
            raise McpProtocolError(_MESSAGE_MCP_REQUEST_PARAMS_MUST_BE_AN_OBJECT)
        normalized_params, _entity_name = _normalized_mcp_request_params(
            method,
            raw_params if isinstance(raw_params, dict) else None,
            name,
        )
        normalized_payload = dict(payload)
        normalized_payload["method"] = method
        normalized_payload["params"] = normalized_params
        self.open()

        def _post_request() -> tuple[dict[str, Any] | None, dict[str, str], int]:
            request_payload = dict(normalized_payload)
            expected_request_id: object | None = None
            if "id" in request_payload:
                expected_request_id = _next_json_rpc_id()
                request_payload["id"] = expected_request_id
            return _json_rpc_post_with_metadata(
                url=self.url,
                headers=self._current_headers(),
                payload=request_payload,
                timeout_seconds=self.timeout_seconds,
                session_id=self._state.session_id,
                expected_request_id=expected_request_id,
                protocol_version=self._state.negotiated_version,
            )

        try:
            response, _headers, latency_ms = _post_request()
        except ServiceUnavailableError as exc:
            cause = exc.__cause__
            if not (
                self._state.session_id
                and isinstance(cause, urllib.error.HTTPError)
                and int(cause.code) == 404
            ):
                raise
            self._state.session_id = ""
            self._state.negotiated_version = ""
            self._state.opened = False
            self.open(lifecycle_event="reinitialize")
            if method != "tools/list":
                raise McpSessionExpiredError(
                    "MCP session expired during a non-retryable request"
                ) from exc
            response, _headers, latency_ms = _post_request()
        if response is None:
            raise McpProtocolError("MCP request returned an empty response")
        return response, latency_ms

    def close(self) -> None:
        if self._state.closed:
            return
        self._state.closed = True
        session_id = self._state.session_id
        negotiated_version = self._state.negotiated_version
        was_opened = self._state.opened
        self._state.session_id = ""
        self._state.opened = False
        if negotiated_version:
            self._observability_state.protocol_version = negotiated_version
        if not session_id:
            if was_opened:
                logger.info(
                    "MCP legacy session closed",
                    extra=_mcp_log_extra(
                        self._observability_state,
                        session_event="close",
                    ),
                )
            return
        self._terminate_session(
            session_id=session_id,
            protocol_version=negotiated_version,
        )
        logger.info(
            "MCP legacy session closed",
            extra=_mcp_log_extra(
                self._observability_state,
                session_event="close",
            ),
        )

    def _terminate_session(self, *, session_id: str, protocol_version: str) -> None:
        if not session_id:
            return
        try:
            connector_headers = self._current_headers()
            credential_bearing = bool(connector_headers) or bool(session_id)
            request_headers = _with_mcp_platform_headers(
                connector_headers,
                session_id=session_id,
                protocol_version=protocol_version,
            )
            safe_url = _connector_rpc_url(
                {
                    "transport": "streamable_http",
                    "base_url": self.url,
                    "headers": connector_headers if credential_bearing else {},
                },
                session_id=session_id,
            )
            request = urllib.request.Request(  # nosec B310
                safe_url,
                headers=request_headers,
                method="DELETE",
            )
            response_context = url_safety.open_pinned_url(
                request,
                timeout_seconds=self.timeout_seconds,
                purpose="MCP connector base_url",
                require_private=(
                    credential_bearing and urllib.parse.urlsplit(safe_url).scheme == "http"
                ),
            )
            with response_context as response:
                response.read(0)
        except Exception:
            # Session termination is advisory: servers may reject DELETE with 405.
            pass


def _modern_meta(params: dict[str, Any] | None) -> dict[str, Any]:
    normalized = dict(params or {})
    raw_meta = normalized.get("_meta")
    meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    meta.update(
        {
            "io.modelcontextprotocol/protocolVersion": MCP_MODERN_VERSION,
            "io.modelcontextprotocol/clientInfo": {
                "name": "flow-steward",
                "version": APP_VERSION,
            },
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    )
    normalized["_meta"] = meta
    return normalized


def _advertised_modern_versions(result: dict[str, Any]) -> list[str]:
    if not isinstance(result.get("capabilities"), dict):
        raise McpProtocolError("MCP discovery returned malformed server capabilities")
    raw_versions = result.get("supportedVersions")
    if not isinstance(raw_versions, list):
        raise McpProtocolError("MCP discovery returned malformed advertised protocol versions")
    if any(
        not isinstance(version, str) or not version.strip() or version != version.strip()
        for version in raw_versions
    ):
        raise McpProtocolError("MCP discovery returned malformed advertised protocol versions")
    return list(raw_versions)


class StreamableHttpMcpClient:
    """Serialized, dual-era MCP client whose state remains process-local."""

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str] | None = None,
        timeout_seconds: float,
        headers_provider: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout_seconds = timeout_seconds
        self._headers_provider = headers_provider
        self._observability_state = _ACTIVE_MCP_OBSERVABILITY.get() or _McpObservabilityState()
        self._era: McpProtocolEra | None = None
        self._legacy_session: StreamableHttpMcpSession | None = None
        self._modern_discovery: tuple[dict[str, Any], int] | None = None
        self._closed = False
        self._lock = threading.RLock()

    @property
    def era(self) -> McpProtocolEra | None:
        return self._era

    def _current_headers(self) -> dict[str, str]:
        if self._headers_provider is None:
            return dict(self.headers)
        return dict(self._headers_provider())

    def _bind_observability(self, *, connector_id: str, job_id: str) -> None:
        self._observability_state.connector_id = str(connector_id or "").strip()
        self._observability_state.job_id = str(job_id or "").strip()

    def _select_legacy(self) -> None:
        self._era = McpProtocolEra.LEGACY
        self._observability_state.protocol_era = self._era.value
        self._observability_state.protocol_version = ""
        logger.info(
            "MCP protocol era selected",
            extra=_mcp_log_extra(self._observability_state, era_event="selected"),
        )
        self._legacy_session = StreamableHttpMcpSession(
            url=self.url,
            headers=self.headers,
            headers_provider=self._headers_provider,
            timeout_seconds=self.timeout_seconds,
            observability_state=self._observability_state,
        )

    def _negotiate(self) -> None:
        response: dict[str, Any] | None = None
        latency_ms = 0
        for probe_attempt in range(2):
            request_id = _next_json_rpc_id()
            try:
                response, _response_headers, latency_ms = _json_rpc_post_with_metadata(
                    url=self.url,
                    headers=self._current_headers(),
                    payload={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": _TOKEN_SERVER_DISCOVER,
                        "params": _modern_meta(None),
                    },
                    timeout_seconds=self.timeout_seconds,
                    session_id="",
                    expected_request_id=request_id,
                    protocol_version=MCP_MODERN_VERSION,
                    method_name=_TOKEN_SERVER_DISCOVER,
                    entity_name="",
                )
            except McpProtocolError as exc:
                if exc.code == "-32022" and MCP_MODERN_VERSION in exc.supported_versions:
                    if probe_attempt == 0:
                        continue
                    raise
                if not exc.legacy_signal:
                    raise
                self._select_legacy()
                return
            break
        result = (response or {}).get("result")
        if not isinstance(result, dict):
            self._select_legacy()
            return
        try:
            advertised_versions = _advertised_modern_versions(result)
        except McpProtocolError:
            self._select_legacy()
            return
        if MCP_MODERN_VERSION not in advertised_versions:
            self._select_legacy()
            return
        self._era = McpProtocolEra.MODERN
        self._observability_state.protocol_era = self._era.value
        self._observability_state.protocol_version = MCP_MODERN_VERSION
        logger.info(
            "MCP protocol era selected",
            extra=_mcp_log_extra(self._observability_state, era_event="selected"),
        )
        self._modern_discovery = response, latency_ms

    def request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        name: str = "",
        generated_transport_headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], int]:
        with self._lock:
            if self._closed:
                raise McpProtocolError("MCP client is closed")
            normalized_method = str(method or "").strip()
            if not normalized_method:
                raise McpProtocolError("MCP request method is required")
            if params is not None and not isinstance(params, dict):
                raise McpProtocolError(_MESSAGE_MCP_REQUEST_PARAMS_MUST_BE_AN_OBJECT)
            normalized_params, entity_name = _normalized_mcp_request_params(
                normalized_method,
                params,
                name,
            )
            if self._era is None:
                self._negotiate()
            if normalized_method == _TOKEN_SERVER_DISCOVER:
                if self._era is not McpProtocolEra.MODERN or self._modern_discovery is None:
                    raise McpProtocolError("MCP server does not support modern discovery")
                return self._modern_discovery
            if self._era is McpProtocolEra.MODERN:
                if normalized_method not in _MCP_MODERN_METHODS:
                    raise McpProtocolError("MCP method is unsupported for the modern protocol era")
                request_id = _next_json_rpc_id()
                response, _response_headers, latency_ms = _json_rpc_post_with_metadata(
                    url=self.url,
                    headers=self._current_headers(),
                    payload={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": normalized_method,
                        "params": _modern_meta(normalized_params),
                    },
                    timeout_seconds=self.timeout_seconds,
                    session_id="",
                    expected_request_id=request_id,
                    protocol_version=MCP_MODERN_VERSION,
                    method_name=normalized_method,
                    entity_name=_encode_mcp_http_value(entity_name) if entity_name else "",
                    generated_transport_headers=generated_transport_headers,
                )
                if response is None:
                    raise McpProtocolError("MCP request returned an empty response")
                return response, latency_ms
            if self._legacy_session is None:
                raise McpProtocolError("MCP legacy protocol state is unavailable")
            request_id = _next_json_rpc_id()
            return self._legacy_session.request(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": normalized_method,
                    "params": normalized_params,
                },
                name=entity_name,
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            legacy_session = self._legacy_session
            self._legacy_session = None
            if legacy_session is not None:
                legacy_session.close()


def _active_execution_scope_for_identity(
    *,
    account_id: str,
    job_id: str,
) -> _ExecutionMcpScopeState:
    active_scope = _ACTIVE_MCP_EXECUTION_SCOPE.get()
    if active_scope is None:
        raise McpExecutionScopeInactiveError("MCP execution scope is not active")
    normalized_account_id = str(account_id or "").strip()
    normalized_job_id = str(job_id or "").strip()
    if active_scope.account_id != normalized_account_id or active_scope.job_id != normalized_job_id:
        raise ServiceValidationError(
            "MCP runtime identity does not match the active execution scope"
        )
    return active_scope


def get_execution_mcp_client(
    *,
    account_id: str,
    job_id: str,
    connector_id: str,
    client_factory: Callable[[], StreamableHttpMcpClient],
) -> StreamableHttpMcpClient:
    """Return the one client owned by the active execution attempt and connector."""

    active_scope = _active_execution_scope_for_identity(account_id=account_id, job_id=job_id)
    normalized_connector_id = str(connector_id or "").strip()
    if not normalized_connector_id:
        raise ServiceValidationError("MCP connector identity is required")
    key = _ExecutionMcpRegistryKey(
        attempt_token=active_scope.attempt_token,
        account_id=active_scope.account_id,
        job_id=active_scope.job_id,
        connector_id=normalized_connector_id,
    )
    with _EXECUTION_MCP_CLIENTS_LOCK:
        if active_scope.closing:
            raise ServiceValidationError("MCP execution scope is closing")
        client = _EXECUTION_MCP_CLIENTS.get(key)
        if client is None:
            client = client_factory()
            bind_observability = getattr(client, "_bind_observability", None)
            if callable(bind_observability):
                bind_observability(
                    connector_id=normalized_connector_id,
                    job_id=active_scope.job_id,
                )
            _EXECUTION_MCP_CLIENTS[key] = client
        return client


def close_execution_mcp_clients(
    *,
    attempt_token: str | None = None,
    account_id: str = "",
    job_id: str = "",
) -> None:
    """Atomically detach and then best-effort close one attempt's owned clients."""

    active_scope = _ACTIVE_MCP_EXECUTION_SCOPE.get()
    selected_token = str(attempt_token or "").strip()
    if active_scope is not None:
        if account_id or job_id:
            _active_execution_scope_for_identity(account_id=account_id, job_id=job_id)
        if selected_token and selected_token != active_scope.attempt_token:
            raise ServiceValidationError("MCP execution attempt token does not match active scope")
        selected_token = active_scope.attempt_token
    if not selected_token:
        return
    with _EXECUTION_MCP_CLIENTS_LOCK:
        if active_scope is not None and active_scope.attempt_token == selected_token:
            active_scope.closing = True
        keys = [key for key in _EXECUTION_MCP_CLIENTS if key.attempt_token == selected_token]
        clients = [_EXECUTION_MCP_CLIENTS.pop(key) for key in keys]
    for client in clients:
        with suppress(Exception):
            client.close()


@contextmanager
def mcp_execution_scope(account_id: str, job_id: str) -> Iterator[str]:
    """Activate process-local outbound MCP ownership for one execution attempt."""

    normalized_account_id = str(account_id or "").strip()
    normalized_job_id = str(job_id or "").strip()
    if not normalized_account_id or not normalized_job_id:
        raise ServiceValidationError("MCP execution scope requires account and job identity")
    scope_state = _ExecutionMcpScopeState(
        attempt_token=str(uuid.uuid4()),
        account_id=normalized_account_id,
        job_id=normalized_job_id,
    )
    reset_token = _ACTIVE_MCP_EXECUTION_SCOPE.set(scope_state)
    try:
        yield scope_state.attempt_token
    finally:
        try:
            close_execution_mcp_clients(attempt_token=scope_state.attempt_token)
        finally:
            _ACTIVE_MCP_EXECUTION_SCOPE.reset(reset_token)


def streamable_http_request_with_latency(
    *, url: str, headers: dict[str, str], payload: dict[str, Any], timeout_seconds: float
) -> tuple[dict[str, Any], int]:
    """Run one request through a short-lived dual-era protocol client."""

    method = str(payload.get("method") or "").strip()
    raw_params = payload.get("params")
    if raw_params is not None and not isinstance(raw_params, dict):
        raise McpProtocolError(_MESSAGE_MCP_REQUEST_PARAMS_MUST_BE_AN_OBJECT)
    params = raw_params if isinstance(raw_params, dict) else None
    name = str((params or {}).get("name") or "").strip()
    client = StreamableHttpMcpClient(
        url=url,
        headers=headers,
        timeout_seconds=timeout_seconds,
    )
    try:
        return client.request(method, params, name=name)
    finally:
        client.close()


def _json_rpc_post_with_latency(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> tuple[dict[str, Any], int]:
    return streamable_http_request_with_latency(
        url=url,
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )


def _json_rpc_post(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    payload, _latency_ms = _json_rpc_post_with_latency(
        url=url,
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )
    return payload
