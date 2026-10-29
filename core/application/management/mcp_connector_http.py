"""Bounded MCP HTTP transport and remote address validation."""

from __future__ import annotations

import base64
import http.client
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from core.application.extensions import url_safety
from core.application.extensions.lifecycle_descriptor_io import (
    _allow_private_remote_urls,
    _is_blocked_ip,
    _resolve_hostname_ips,
)
from core.application.management.mcp_connector_header_secrets import connector_has_headers
from core.infrastructure.config.version import APP_VERSION

from .errors import (
    ServiceUnavailableError,
    ServiceValidationError,
)
from .mcp_connector_protocol import (
    _TOKEN_SERVER_DISCOVER,
    MCP_MODERN_VERSION,
    McpProtocolError,
    _mcp_discovery_error_from_json_rpc,
    _mcp_protocol_error_from_json_rpc,
    _normalize_transport,
)

_MESSAGE_X_MCP_HEADER_IS_OUTSIDE_A_PROPERTIES_ONLY_PATH = (
    "x-mcp-header is outside a properties-only path"
)


_BLOCKED_MCP_HOSTS = {"localhost", "localhost.localdomain"}


_DEFAULT_USER_AGENT = f"flow-steward/{APP_VERSION}"


_MAX_MCP_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024


_MCP_CREDENTIAL_QUERY_MARKERS = (
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "passwd",
    "password",
    "privatekey",
    "secret",
    "token",
)


_MCP_CREDENTIAL_QUERY_EXACT_IDENTITIES = frozenset(
    {
        "accesskey",
        "auth",
        "key",
        "sig",
        "signature",
    }
)


def read_bounded_http_body(response: Any, *, max_bytes: int, purpose: str) -> bytes:
    """Read at most ``max_bytes`` without buffering an untrusted response."""
    limit = max(1, int(max_bytes))
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ServiceValidationError(f"{purpose} exceeded the response size limit")
    return payload


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, _newurl):
        return None


def _validate_connected_peer_address(address: str) -> None:
    if _allow_private_remote_urls():
        return
    if _is_blocked_ip(str(address or "").strip()):
        raise ServiceValidationError("MCP connector connected to a blocked address")


class _ValidatingHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        super().connect()
        try:
            peer_host = str((self.sock.getpeername() or ("",))[0])
            _validate_connected_peer_address(peer_host)
        except Exception:
            if self.sock is not None:
                self.sock.close()
            raise


class _ValidatingHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        super().connect()
        try:
            peer_host = str((self.sock.getpeername() or ("",))[0])
            _validate_connected_peer_address(peer_host)
        except Exception:
            if self.sock is not None:
                self.sock.close()
            raise


class _ValidatingHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_ValidatingHTTPConnection, req)


class _ValidatingHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_ValidatingHTTPSConnection, req)


def _classify_mcp_host(host: str, *, port: int) -> tuple[bool, bool, bool]:
    """Return (private_only, contains_private, resolved_hostname)."""

    if host in _BLOCKED_MCP_HOSTS or host.endswith((".localhost", ".local")):
        return True, True, False
    try:
        blocked = _is_blocked_ip(host)
        return url_safety.is_development_private_ip(host), blocked, False
    except ValueError:
        try:
            resolved_ips = _resolve_hostname_ips(host, port=port)
        except Exception as exc:
            raise ServiceValidationError(
                "MCP connector base_url host could not be resolved"
            ) from exc
        if not resolved_ips:
            raise ServiceValidationError(
                "MCP connector base_url host could not be resolved"
            ) from None
        private_only = all(
            url_safety.is_development_private_ip(address) for address in resolved_ips
        )
        contains_blocked = any(_is_blocked_ip(address) for address in resolved_ips)
        return private_only, contains_blocked, True


def _mcp_url_query_contains_credentials(parsed: urllib.parse.SplitResult) -> bool:
    for key, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        identity = re.sub(r"[^a-zA-Z0-9]", "", key).lower()
        if identity in _MCP_CREDENTIAL_QUERY_EXACT_IDENTITIES or any(
            marker in identity for marker in _MCP_CREDENTIAL_QUERY_MARKERS
        ):
            return True
    return False


def _connector_rpc_url(
    row: dict[str, Any],
    *,
    generated_transport_headers: dict[str, str] | None = None,
    session_id: str = "",
) -> str:
    _normalize_transport(row.get("transport"))
    base_url = str(row.get("base_url") or "").strip()
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ServiceValidationError(
            "MCP connector base_url must be a valid http(s) URL",
            payload={
                "error": "invalid_mcp_endpoint",
                "field": "base_url",
                "message": "MCP connector base_url must be a valid http(s) URL",
            },
        )
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ServiceValidationError(
            "MCP connector base_url has an invalid port",
            payload={
                "error": "invalid_mcp_endpoint",
                "field": "base_url",
                "message": "MCP connector base_url has an invalid port",
            },
        ) from exc
    if parsed.username or parsed.password:
        raise ServiceValidationError(
            "MCP connector base_url must not include credentials",
            payload={
                "error": "invalid_mcp_endpoint",
                "field": "base_url",
                "message": "MCP connector base_url must not include credentials",
            },
        )
    if _mcp_url_query_contains_credentials(parsed):
        raise ServiceValidationError(
            "MCP connector endpoint must not include credentials in query parameters; "
            "use Headers or OAuth settings.",
            payload={
                "error": "mcp_endpoint_query_credentials_forbidden",
                "field": "base_url",
                "message": (
                    "MCP connector endpoint must not include credentials in query parameters; "
                    "use Headers or OAuth settings."
                ),
            },
        )
    auth_mode = str(row.get("auth_mode") or "none").strip().lower()
    credential_bearing = (
        auth_mode in {"bearer", "api_key", "oauth2"}
        or connector_has_headers(row)
        or bool(generated_transport_headers)
        or bool(str(session_id or "").strip())
    )
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    allow_private = _allow_private_remote_urls()
    private_only, contains_private, resolved_hostname = _classify_mcp_host(host, port=port)
    if credential_bearing and parsed.scheme != "https" and not (allow_private and private_only):
        raise ServiceValidationError(
            "Credential-bearing MCP connector requests must use an https base_url",
            payload={
                "error": "insecure_mcp_endpoint",
                "field": "base_url",
                "message": "Credential-bearing MCP connector requests must use an https base_url",
            },
        )
    if not allow_private and contains_private:
        from core.application.extensions.url_safety import private_addresses_hint

        if resolved_hostname:
            raise ServiceValidationError(
                f"MCP connector base_url resolves to a blocked address. {private_addresses_hint()}"
            ) from None
        raise ServiceValidationError(
            f"MCP connector base_url host is not allowed. {private_addresses_hint()}"
        )
    return base_url


def _with_platform_user_agent(headers: dict[str, str]) -> dict[str, str]:
    normalized = dict(headers)
    user_agent = next(
        (
            value
            for key, value in normalized.items()
            if key.lower() == "user-agent" and value.strip()
        ),
        _DEFAULT_USER_AGENT,
    )
    for key in [key for key in normalized if key.lower() == "user-agent"]:
        normalized.pop(key)
    normalized["User-Agent"] = user_agent
    return normalized


_MCP_PLATFORM_HEADER_NAMES = frozenset(
    {
        "accept",
        "content-type",
        "mcp-session-id",
        "mcp-protocol-version",
        "mcp-method",
        "mcp-name",
    }
)


_MCP_HEADER_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


_MCP_BASE64_SENTINEL_RE = re.compile(r"^=\?base64\?.*\?=$", re.DOTALL)


_MCP_SAFE_INTEGER_MAX = (2**53) - 1


def _encode_mcp_http_value(value: str) -> str:
    plain = (
        value == value.strip(" \t")
        and all(" " <= character <= "~" for character in value)
        and _MCP_BASE64_SENTINEL_RE.fullmatch(value) is None
    )
    if plain:
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"=?base64?{encoded}?="


def _mcp_header_annotations(input_schema: dict[str, Any]) -> list[tuple[tuple[str, ...], str, str]]:
    annotations: list[tuple[tuple[str, ...], str, str]] = []
    seen_headers: set[str] = set()

    def _contains_annotation(value: Any) -> bool:
        if isinstance(value, dict):
            return "x-mcp-header" in value or any(
                _contains_annotation(item) for item in value.values()
            )
        if isinstance(value, list):
            return any(_contains_annotation(item) for item in value)
        return False

    def _walk(schema: dict[str, Any], path: tuple[str, ...], *, property_schema: bool) -> None:
        if "x-mcp-header" in schema:
            if not property_schema:
                raise McpProtocolError(_MESSAGE_X_MCP_HEADER_IS_OUTSIDE_A_PROPERTIES_ONLY_PATH)
            header_suffix = schema.get("x-mcp-header")
            schema_type = schema.get("type")
            if not isinstance(header_suffix, str) or not _MCP_HEADER_TOKEN_RE.fullmatch(
                header_suffix
            ):
                raise McpProtocolError("x-mcp-header has an invalid header name")
            if schema_type not in {"string", "integer", "boolean"}:
                raise McpProtocolError("x-mcp-header annotates a non-primitive property")
            full_header = f"mcp-param-{header_suffix}".lower()
            if full_header in seen_headers:
                raise McpProtocolError("x-mcp-header names are not case-insensitively unique")
            seen_headers.add(full_header)
            annotations.append((path, header_suffix, schema_type))

        properties = schema.get("properties")
        if isinstance(properties, dict):
            for property_name, property_value in properties.items():
                if not isinstance(property_name, str) or not isinstance(property_value, dict):
                    if _contains_annotation(property_value):
                        raise McpProtocolError(
                            _MESSAGE_X_MCP_HEADER_IS_OUTSIDE_A_PROPERTIES_ONLY_PATH
                        )
                    continue
                _walk(property_value, (*path, property_name), property_schema=True)
        elif _contains_annotation(properties):
            raise McpProtocolError(_MESSAGE_X_MCP_HEADER_IS_OUTSIDE_A_PROPERTIES_ONLY_PATH)
        if any(
            _contains_annotation(value)
            for key, value in schema.items()
            if key not in {"x-mcp-header", "properties"}
        ):
            raise McpProtocolError(_MESSAGE_X_MCP_HEADER_IS_OUTSIDE_A_PROPERTIES_ONLY_PATH)

    _walk(input_schema, (), property_schema=False)
    return annotations


def mcp_tool_transport_headers(
    input_schema: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, str]:
    """Build transient modern transport headers from trusted discovered schema metadata."""

    generated: dict[str, str] = {}
    for path, header_suffix, schema_type in _mcp_header_annotations(input_schema):
        value: Any = arguments
        present = True
        for component in path:
            if not isinstance(value, dict) or component not in value:
                present = False
                break
            value = value[component]
        if not present or value is None:
            continue
        if schema_type == "string" and isinstance(value, str):
            rendered = value
        elif schema_type == "integer" and (
            (isinstance(value, int) and not isinstance(value, bool))
            or (isinstance(value, float) and math.isfinite(value) and value.is_integer())
        ):
            integer_value = int(value)
            if abs(integer_value) > _MCP_SAFE_INTEGER_MAX:
                raise McpProtocolError("MCP header argument integer is outside the safe range")
            rendered = str(integer_value)
        elif schema_type == "boolean" and isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            raise McpProtocolError("MCP header argument does not match its annotated type")
        generated[f"Mcp-Param-{header_suffix}"] = _encode_mcp_http_value(rendered)
    return generated


def _with_mcp_platform_headers(
    headers: dict[str, str],
    *,
    session_id: str = "",
    protocol_version: str = "",
    method_name: str = "",
    entity_name: str = "",
    generated_transport_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Apply transport-owned headers after removing case-variant connector values."""

    sanitized = {
        key: value
        for key, value in headers.items()
        if key.lower() not in _MCP_PLATFORM_HEADER_NAMES
        and not key.lower().startswith("mcp-param-")
    }
    request_headers = _with_platform_user_agent(sanitized)
    request_headers["Accept"] = "application/json, text/event-stream"
    request_headers["Content-Type"] = "application/json"
    if session_id:
        request_headers["Mcp-Session-Id"] = session_id
    if protocol_version:
        request_headers["MCP-Protocol-Version"] = protocol_version
    if method_name:
        request_headers["Mcp-Method"] = method_name
    if entity_name:
        request_headers["Mcp-Name"] = entity_name
    generated_names: set[str] = set()
    for key, value in (generated_transport_headers or {}).items():
        normalized_key = key.lower()
        suffix = key[len("mcp-param-") :] if normalized_key.startswith("mcp-param-") else ""
        if (
            not suffix
            or not _MCP_HEADER_TOKEN_RE.fullmatch(suffix)
            or normalized_key in generated_names
        ):
            raise McpProtocolError("Generated MCP transport header name is invalid")
        generated_names.add(normalized_key)
        request_headers[key] = value
    return request_headers


def _decode_streamable_http_response(
    raw: str,
    content_type: str,
    *,
    expected_request_id: object | None,
) -> dict[str, Any]:
    def _matching_response(candidate: dict[str, Any]) -> bool:
        # A JSON-RPC response carries `result` or `error`; a notification that happens
        # to echo the request id is not the response to it.
        if "result" not in candidate and "error" not in candidate:
            return False
        return expected_request_id is None or candidate.get("id") == expected_request_id

    if "text/event-stream" in content_type.lower():
        matching_response: dict[str, Any] | None = None
        data_lines: list[str] = []

        def _decode_event() -> None:
            nonlocal matching_response
            if matching_response is not None or not data_lines:
                # First response for our id wins: a duplicate later in the stream must
                # not overwrite the answer this request was correlated to.
                return
            try:
                candidate = json.loads("\n".join(data_lines))
            except Exception:
                return
            if isinstance(candidate, dict) and _matching_response(candidate):
                matching_response = candidate

        for line in raw.splitlines():
            if not line:
                _decode_event()
                data_lines.clear()
                continue
            if line.startswith(":") or not line.startswith("data:"):
                continue
            data = line.removeprefix("data:")
            data_lines.append(data.removeprefix(" "))
        _decode_event()

        if matching_response is None:
            if expected_request_id is not None:
                raise ServiceValidationError("MCP server returned no matching JSON-RPC response")
            raise ServiceValidationError("MCP server returned an invalid SSE response")
        return matching_response
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise ServiceValidationError("MCP server returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ServiceValidationError("MCP server returned a malformed JSON-RPC payload")
    if not _matching_response(parsed):
        raise ServiceValidationError("MCP server returned no matching JSON-RPC response")
    return parsed


def _json_rpc_post_with_metadata(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    session_id: str = "",
    allow_empty_response: bool = False,
    expected_request_id: object | None = None,
    protocol_version: str = "",
    method_name: str = "",
    entity_name: str = "",
    generated_transport_headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str], int]:
    started = time.monotonic()
    credential_bearing = (
        bool(headers) or bool(generated_transport_headers) or bool(str(session_id or "").strip())
    )
    safe_url = _connector_rpc_url(
        {
            "transport": "streamable_http",
            "base_url": url,
            "headers": headers if credential_bearing else {},
        },
        generated_transport_headers=generated_transport_headers,
        session_id=session_id,
    )
    request_headers = _with_mcp_platform_headers(
        headers,
        session_id=session_id,
        protocol_version=protocol_version,
        method_name=method_name,
        entity_name=entity_name,
        generated_transport_headers=generated_transport_headers,
    )
    request = urllib.request.Request(  # nosec B310
        safe_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        response_context = url_safety.open_pinned_url(
            request,
            timeout_seconds=timeout_seconds,
            purpose="MCP connector base_url",
            require_private=(
                credential_bearing and urllib.parse.urlsplit(safe_url).scheme == "http"
            ),
        )
        with response_context as response:
            raw = read_bounded_http_body(
                response,
                max_bytes=_MAX_MCP_HTTP_RESPONSE_BYTES,
                purpose="MCP server response",
            ).decode("utf-8")
            response_headers = {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        if 300 <= int(exc.code) < 400:
            location = str(exc.headers.get("Location") or "").strip()
            if location:
                _connector_rpc_url(
                    {
                        "transport": "streamable_http",
                        "base_url": urllib.parse.urljoin(safe_url, location),
                    }
                )
            raise ServiceUnavailableError("MCP connector redirects are not allowed") from exc
        is_modern_probe = (
            protocol_version == MCP_MODERN_VERSION and method_name == _TOKEN_SERVER_DISCOVER
        )
        inspect_modern_error = protocol_version == MCP_MODERN_VERSION and (
            (is_modern_probe and 400 <= int(exc.code) < 500 and int(exc.code) not in {401, 403})
            or (not is_modern_probe and int(exc.code) in {400, 404})
        )
        if inspect_modern_error:
            try:
                encoded_error = read_bounded_http_body(
                    exc,
                    max_bytes=65_536,
                    purpose="MCP server error response",
                )
                decoded_error = json.loads(encoded_error.decode("utf-8"))
            except Exception:
                decoded_error = None
            recognized_envelope = (
                isinstance(decoded_error, dict)
                and decoded_error.get("jsonrpc") == "2.0"
                and decoded_error.get("id") == expected_request_id
                and "result" not in decoded_error
                and isinstance(decoded_error.get("error"), dict)
            )
            if is_modern_probe:
                error_object = (
                    decoded_error.get("error")
                    if recognized_envelope and isinstance(decoded_error, dict)
                    else None
                )
                raise _mcp_discovery_error_from_json_rpc(error_object) from exc
            if recognized_envelope and isinstance(decoded_error, dict):
                try:
                    protocol_error = _mcp_protocol_error_from_json_rpc(decoded_error["error"])
                except McpProtocolError:
                    protocol_error = None
                if protocol_error is not None:
                    raise protocol_error from exc
        raise ServiceUnavailableError(f"MCP request failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ServiceUnavailableError(f"MCP request failed: {exc.reason}") from exc
    except url_safety.PinnedPeerError as exc:
        raise ServiceValidationError(str(exc)) from exc
    except ServiceValidationError:
        raise
    except Exception as exc:
        raise ServiceUnavailableError(f"MCP request failed: {exc}") from exc

    latency_ms = int((time.monotonic() - started) * 1000)
    if allow_empty_response and not raw.strip():
        return None, response_headers, latency_ms
    try:
        parsed = _decode_streamable_http_response(
            raw,
            response_headers.get("content-type", ""),
            expected_request_id=expected_request_id,
        )
    except ServiceValidationError as exc:
        if protocol_version == MCP_MODERN_VERSION and method_name == _TOKEN_SERVER_DISCOVER:
            raise McpProtocolError(
                "MCP server did not return a recognized modern discovery response",
                legacy_signal=True,
            ) from exc
        raise
    if "error" in parsed and parsed["error"] is not None:
        if protocol_version == MCP_MODERN_VERSION and method_name == _TOKEN_SERVER_DISCOVER:
            raise _mcp_discovery_error_from_json_rpc(parsed["error"])
        raise _mcp_protocol_error_from_json_rpc(parsed["error"])
    return parsed, response_headers, latency_ms
