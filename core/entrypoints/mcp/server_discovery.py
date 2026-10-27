"""Request-local tool discovery for the MCP transport."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES


def _presentation_scope_granted(scopes: set[str], required_scope: str) -> bool:
    """Match discovery metadata only; the application API still authorizes calls."""
    required = str(required_scope or "").strip().lower()
    if not required:
        return True
    if required in scopes or "admin:all" in scopes:
        return True
    separator = ":" if ":" in required else ""
    if not separator and "." in required:
        separator = "."
    if not separator:
        return required in scopes
    domain, _, action = required.partition(separator)
    return f"{domain}:*" in scopes or f"*:{action}" in scopes or "*:*" in scopes


def visible_mcp_tool_names(
    scopes: Iterable[str] | None,
    *,
    auth_enabled: bool,
) -> set[str]:
    """Return presentation-visible tools; the app API remains authoritative."""
    if not auth_enabled:
        return set(MCP_TOOL_SCOPES)
    granted = {
        str(scope or "").strip().lower() for scope in (scopes or ()) if str(scope or "").strip()
    }
    if not granted:
        return set()
    return {
        tool
        for tool, required in MCP_TOOL_SCOPES.items()
        if any(_presentation_scope_granted(granted, candidate) for candidate in required)
    }


class McpToolDiscoveryMiddleware:
    """Filter only the current ``tools/list`` response."""

    def __init__(self, *, auth_enabled: bool) -> None:
        self._auth_enabled = bool(auth_enabled)

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        if getattr(ctx, "method", "") != "tools/list":
            return result

        scopes: list[str] | None = None
        if self._auth_enabled:
            from mcp.server.auth.middleware.auth_context import get_access_token

            access_token = get_access_token()
            scopes = list(access_token.scopes) if access_token is not None else []
        visible = visible_mcp_tool_names(scopes, auth_enabled=self._auth_enabled)
        raw_tools = (
            result.get("tools") if isinstance(result, dict) else getattr(result, "tools", None)
        )
        if not isinstance(raw_tools, list):
            return result
        filtered_tools = [
            tool
            for tool in raw_tools
            if str(tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", ""))
            in visible
        ]
        if isinstance(result, dict):
            filtered = dict(result)
            filtered["tools"] = filtered_tools
            return filtered
        model_copy = getattr(result, "model_copy", None)
        if callable(model_copy):
            return model_copy(update={"tools": filtered_tools})
        return result


__all__ = ["McpToolDiscoveryMiddleware", "visible_mcp_tool_names"]
