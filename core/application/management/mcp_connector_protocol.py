"""MCP wire versions, request contracts and classified protocol errors."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from .errors import (
    ServiceValidationError,
)

_TOKEN_SERVER_DISCOVER = "server/discover"


_MESSAGE_MCP_SERVER_DID_NOT_RETURN_A_RECOGNIZED_MODERN_DISCOVERY_ERROR = (
    "MCP server did not return a recognized modern discovery error"
)


_MESSAGE_MCP_REQUEST_PARAMS_MUST_BE_AN_OBJECT = "MCP request params must be an object"


_SUPPORTED_MCP_TRANSPORTS = {"streamable_http"}


_MCP_TRANSPORT_ALIASES = {
    "http": "streamable_http",
    "streamable-http": "streamable_http",
}


def _normalize_transport(value: Any) -> str:
    normalized = str(value or "streamable_http").strip().lower() or "streamable_http"
    normalized = _MCP_TRANSPORT_ALIASES.get(normalized, normalized)
    if normalized not in _SUPPORTED_MCP_TRANSPORTS:
        raise ServiceValidationError(
            "transport must be streamable_http; sse and stdio are not supported by this runtime"
        )
    return normalized


def _json_rpc_error_code(error: Any) -> str:
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        return code[:64]
    return ""


MCP_MODERN_VERSION = "2026-07-28"


MCP_LEGACY_PREFERRED_VERSION = "2025-11-25"


MCP_LEGACY_VERSIONS = frozenset({"2025-03-26", "2025-06-18", "2025-11-25"})


MCP_DISCOVERY_PAGE_LIMIT = 100


_MCP_PROTOCOL_VERSION = MCP_LEGACY_PREFERRED_VERSION


_MCP_MODERN_METHODS = frozenset({_TOKEN_SERVER_DISCOVER, "tools/list", "tools/call"})


class McpProtocolEra(StrEnum):
    MODERN = "modern"
    LEGACY = "legacy"


class McpProtocolError(ServiceValidationError):
    """Controlled MCP wire-contract failure with safe classification metadata."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        legacy_signal: bool = False,
        supported_versions: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = str(code or "").strip()[:64]
        self.legacy_signal = bool(legacy_signal)
        self.supported_versions = tuple(supported_versions)


class UnsupportedMcpResultTypeError(McpProtocolError):
    """Raised when a remote result requires an unsupported MCP interaction mode."""


class McpToolInvocationError(McpProtocolError):
    """Raised when a remote MCP tool reports a controlled invocation failure."""


class McpSessionExpiredError(McpProtocolError):
    """Raised when a mutating request may have run before its session expired."""


class McpExecutionScopeInactiveError(ServiceValidationError):
    """Raised when retained MCP ownership is requested outside an execution attempt."""


def _next_json_rpc_id() -> str:
    return str(uuid.uuid4())


def _mcp_protocol_error_from_json_rpc(error: Any) -> McpProtocolError:
    if not isinstance(error, dict):
        raise McpProtocolError("MCP server returned a malformed JSON-RPC error object")
    raw_code = error.get("code")
    message = error.get("message")
    if (
        isinstance(raw_code, bool)
        or not isinstance(raw_code, (int, str))
        or not str(raw_code).strip()
        or not isinstance(message, str)
    ):
        raise McpProtocolError("MCP server returned a malformed JSON-RPC error object")
    code = str(raw_code).strip()[:64]
    normalized_code = code.lower().replace(" ", "_")
    normalized_message = message.strip().lower()
    explicit_code = normalized_code in {
        "-32601",
        "-32022",
        "unsupported_protocol_version",
        "unsupported-version",
        "unsupported_version",
        "initialization_required",
        "initialize_required",
    }
    explicit_message = any(
        marker in normalized_message
        for marker in (
            "unsupported protocol version",
            "unsupported_protocol_version",
            "unsupported-version",
            "initialization required",
            "initialization_required",
            "not initialized",
            "initialize first",
        )
    )
    return McpProtocolError(
        f"MCP server returned a JSON-RPC error{f' (code {code})' if code else ''}",
        code=code,
        legacy_signal=explicit_code or explicit_message,
    )


def _mcp_discovery_error_from_json_rpc(error: Any) -> McpProtocolError:
    if not isinstance(error, dict):
        return McpProtocolError(
            _MESSAGE_MCP_SERVER_DID_NOT_RETURN_A_RECOGNIZED_MODERN_DISCOVERY_ERROR,
            legacy_signal=True,
        )
    raw_code = error.get("code")
    message = error.get("message")
    if type(raw_code) is not int or not isinstance(message, str):
        return McpProtocolError(
            _MESSAGE_MCP_SERVER_DID_NOT_RETURN_A_RECOGNIZED_MODERN_DISCOVERY_ERROR,
            legacy_signal=True,
        )
    code = str(raw_code).strip()[:64]
    if code in {"-32020", "-32021"}:
        return McpProtocolError(
            f"MCP server returned a JSON-RPC error (code {code})",
            code=code,
        )
    if code != "-32022":
        return McpProtocolError(
            _MESSAGE_MCP_SERVER_DID_NOT_RETURN_A_RECOGNIZED_MODERN_DISCOVERY_ERROR,
            code=code,
            legacy_signal=True,
        )
    data = error.get("data")
    raw_supported = data.get("supported") if isinstance(data, dict) else None
    if (
        not isinstance(raw_supported, list)
        or not raw_supported
        or any(
            not isinstance(version, str) or not version.strip() or version != version.strip()
            for version in raw_supported
        )
    ):
        return McpProtocolError(
            "MCP server did not return actionable supported protocol versions",
            code=code,
            legacy_signal=True,
        )
    supported_versions = tuple(raw_supported)
    has_mutual_legacy = any(version in MCP_LEGACY_VERSIONS for version in supported_versions)
    return McpProtocolError(
        f"MCP server returned a JSON-RPC error (code {code})",
        code=code,
        legacy_signal=has_mutual_legacy,
        supported_versions=supported_versions,
    )


def _normalized_mcp_request_params(
    method: str,
    params: dict[str, Any] | None,
    name: str,
) -> tuple[dict[str, Any], str]:
    normalized = dict(params or {})
    supplied_name = str(name or "")
    if "name" not in normalized:
        if supplied_name:
            raise McpProtocolError("MCP name header does not match JSON-RPC params.name")
        if method == "tools/call":
            raise McpProtocolError("MCP tools/call requires a non-empty params.name")
        return normalized, ""
    raw_name = normalized["name"]
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise McpProtocolError("MCP JSON-RPC params.name must be a non-empty string")
    if supplied_name and supplied_name != raw_name:
        raise McpProtocolError("MCP name header does not match JSON-RPC params.name")
    normalized["name"] = raw_name
    return normalized, raw_name
