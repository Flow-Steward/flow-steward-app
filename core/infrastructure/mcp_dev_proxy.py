"""Shared contract for the explicit local-development MCP auth bypass."""

from __future__ import annotations

from core.infrastructure.runtime_env import runtime_env

MCP_DEV_AUTH_HEADER = "X-Flow-Steward-MCP-Dev-Auth-Disabled"


def mcp_dev_proxy_enabled() -> bool:
    """Return whether the intentionally unsafe local MCP bypass is enabled."""
    value = str(runtime_env.get("FS_MCP_AUTH_DISABLED") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


__all__ = ["MCP_DEV_AUTH_HEADER", "mcp_dev_proxy_enabled"]
