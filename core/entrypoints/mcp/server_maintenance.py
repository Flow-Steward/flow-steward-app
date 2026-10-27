"""API-backed maintenance gate for MCP mutation tools."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from core.entrypoints.mcp.server_api_client import MCP_TOOL_ROUTES, AppApiClient, AppApiError
from core.entrypoints.mcp.server_caller_identity import current_caller_bearer
from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES
from mcp.types import CallToolResult, TextContent


def mcp_tool_is_mutation(tool: str) -> bool:
    """Derive mutation behavior from the public capability taxonomy."""
    required_scopes = MCP_TOOL_SCOPES.get(str(tool or "").strip())
    if required_scopes is None:
        return True
    return not any(scope == "read:*" or scope.endswith(".read") for scope in required_scopes)


def _caller_bearer() -> str:
    return current_caller_bearer()


async def _app_maintenance_active(bearer: str) -> bool | None:
    try:
        payload = await AppApiClient().request_async(
            "GET",
            "/api/system/maintenance",
            bearer=bearer,
        )
    except AppApiError as exc:
        # The privileged status read is not a prerequisite for an ordinary
        # mutation scope. A 403 says nothing about maintenance; the mutation's
        # canonical API ingress still enforces both its own scope and maintenance.
        if exc.status_code == 403:
            return None
        return True
    except ValueError:
        return True
    if not isinstance(payload, Mapping):
        return True
    return str(payload.get("status") or "").strip().lower() != "disabled"


class McpMaintenanceMiddleware:
    """Reject mutations while the canonical app reports Maintenance Mode."""

    def __init__(
        self,
        *,
        maintenance_active: Callable[[str], Awaitable[bool | None]] = _app_maintenance_active,
        caller_bearer: Callable[[], str] = _caller_bearer,
    ) -> None:
        self._maintenance_active = maintenance_active
        self._caller_bearer = caller_bearer

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if getattr(ctx, "method", "") != "tools/call":
            return await call_next(ctx)
        params = getattr(ctx, "params", None)
        tool = str(params.get("name") or "") if isinstance(params, Mapping) else ""
        if not mcp_tool_is_mutation(tool):
            return await call_next(ctx)
        active = await self._maintenance_active(self._caller_bearer())
        if active is False:
            return await call_next(ctx)
        if active is None:
            route = MCP_TOOL_ROUTES.get(tool)
            if route is not None and route.method in {"POST", "PUT", "PATCH", "DELETE"}:
                return await call_next(ctx)
        payload = {
            "ok": False,
            "tool": tool,
            "error": {
                "code": "maintenance_mode_enabled",
                "message": "Maintenance Mode is enabled; retry later.",
            },
        }
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, sort_keys=True))],
            is_error=True,
        )


__all__ = ["McpMaintenanceMiddleware", "mcp_tool_is_mutation"]
