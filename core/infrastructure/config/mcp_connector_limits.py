"""Operator-owned bounds for remote MCP connector responses."""

from core.infrastructure.config.env_registry import registry_default_value
from core.infrastructure.config.runtime_env import runtime_env
from core.infrastructure.config.size_limits import clamp_size_limit_bytes


def mcp_http_response_max_bytes() -> int:
    return clamp_size_limit_bytes(
        runtime_env.get("FS_MCP_HTTP_RESPONSE_MAX_BYTES"),
        default=int(registry_default_value("FS_MCP_HTTP_RESPONSE_MAX_BYTES")),
        minimum=1024,
        maximum=1024 * 1024 * 1024,
    )
