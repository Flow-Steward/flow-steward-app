"""Request-local tool discovery for the MCP transport.

``tools/list`` shows a caller the tools they can use, through two presentation filters:

1. **Token scopes** (:data:`MCP_TOOL_SCOPES`): what the access token may reach at all.
2. **Role permissions** (ADR 0076 §4, :data:`MCP_TOOL_PERMISSIONS`): of those, only the
   tools whose catalogue permission the caller's role holds in the company (account)
   the token is bound to. Roles are per account (ADR 0085): a person who is an admin in
   one account and a viewer in another sees the viewer's list through a token for the
   second one. The permission keys come from ``/api/auth/introspect``, called with the
   caller's own bearer; it returns them for a person and a person's token (PAT, agent
   access, OAuth client). They are cached per token for a short time and never shared
   between tokens.

Neither filter is an authority. Every tool call forwards the caller's bearer to the
application API, which enforces the account, the role and any scoped grant itself.
Discovery therefore falls back to the scope-only list when the permission lookup fails:
``tools/list`` still answers, and a call the role does not allow is refused by the API.

Service accounts, extension runtime and system actors are not role based: introspection
returns no permission keys for them, and they keep the scope-only list.

People limited to some projects or workflows (ADR 0076 §8b): their permission keys are
the ceiling of their role, so they see every tool that role allows. Which projects and
workflows a tool reaches for them is still decided by the API on each call.

With authentication disabled (development) every tool is listed, as before.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any

from core.entrypoints.mcp.server_api_client import AppApiClient, AppApiError
from core.entrypoints.mcp.server_tool_annotations import tool_annotations
from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES, tool_allowed_for_permissions

logger = logging.getLogger("platform.mcp.discovery")

#: The authenticated route that returns the caller's effective permission keys.
PERMISSIONS_PATH = "/api/auth/introspect"
#: How long one token's permission keys are reused for ``tools/list``.
PERMISSION_CACHE_TTL_SECONDS = 45.0
#: At most this many tokens are remembered; the oldest entry goes first.
PERMISSION_CACHE_MAX_ENTRIES = 512
_LOOKUP_TIMEOUT_SECONDS = 5.0


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


def role_visible_tool_names(tools: Iterable[str], permissions: Iterable[str]) -> set[str]:
    """Keep the tools whose ADR 0076 permission is among ``permissions``."""
    held = frozenset(str(key) for key in permissions)
    return {tool for tool in tools if tool_allowed_for_permissions(tool, held)}


def _permissions_from_introspection(payload: object) -> frozenset[str] | None:
    """The permission keys of an introspection response; ``None`` when not role based."""
    if not isinstance(payload, dict) or not bool(payload.get("active")):
        raise ValueError("inactive or malformed introspection response")
    principal = payload.get("principal")
    if not isinstance(principal, dict):
        raise ValueError("introspection response names no principal")
    keys = principal.get("permissions")
    if not isinstance(keys, list):
        return None
    return frozenset(str(key).strip() for key in keys if str(key).strip())


class CallerPermissionLookup:
    """The caller's role permissions in the token's account, cached per access token.

    The cache key is a digest of the bearer, so the token itself is never kept as a
    key or written to a log. A failed lookup is not cached: the next ``tools/list``
    asks again.
    """

    def __init__(
        self,
        *,
        client: AppApiClient | None = None,
        ttl_seconds: float = PERMISSION_CACHE_TTL_SECONDS,
        max_entries: int = PERMISSION_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = float(ttl_seconds)
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, frozenset[str] | None]] = OrderedDict()

    def _api_client(self) -> AppApiClient:
        if self._client is None:
            self._client = AppApiClient(timeout_seconds=_LOOKUP_TIMEOUT_SECONDS)
        return self._client

    @staticmethod
    def _key(bearer: str) -> str:
        return hashlib.sha256(bearer.encode("utf-8")).hexdigest()

    def _cached(self, key: str, now: float) -> tuple[bool, frozenset[str] | None]:
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        expires_at, permissions = entry
        if expires_at <= now:
            del self._entries[key]
            return False, None
        return True, permissions

    def _store(self, key: str, permissions: frozenset[str] | None, now: float) -> None:
        self._entries[key] = (now + self._ttl, permissions)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    async def permissions(self, bearer: str) -> frozenset[str] | None:
        """The caller's permission keys, or ``None`` to keep the scope-only list.

        ``None`` means the caller is not role based, or the lookup failed (logged at
        warning, without the token).
        """
        normalized = str(bearer or "").strip()
        if not normalized:
            return None
        key = self._key(normalized)
        hit, cached = self._cached(key, self._clock())
        if hit:
            return cached
        try:
            payload = await self._api_client().request_async(
                "GET", PERMISSIONS_PATH, bearer=normalized
            )
            permissions = _permissions_from_introspection(payload)
        except AppApiError as exc:
            logger.warning(
                "MCP tool discovery lists by token scopes only: role permissions "
                "unavailable (HTTP %s)",
                exc.status_code,
            )
            return None
        except Exception as exc:
            logger.warning(
                "MCP tool discovery lists by token scopes only: role permissions unavailable (%s)",
                type(exc).__name__,
            )
            return None
        self._store(key, permissions, self._clock())
        return permissions


def _annotated(tool: Any) -> Any:
    """Attach the tool's MCP annotations, keeping any title it already declares."""
    name = str(tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", ""))
    hints = tool_annotations(name)
    if isinstance(tool, dict):
        existing = tool.get("annotations") or {}
        return {**tool, "annotations": {**dict(existing), **hints}}
    model_copy = getattr(tool, "model_copy", None)
    if not callable(model_copy):
        return tool
    from mcp.types import ToolAnnotations

    existing = getattr(tool, "annotations", None)
    title = getattr(existing, "title", None) if existing is not None else None
    return model_copy(update={"annotations": ToolAnnotations(title=title, **hints)})


class McpToolDiscoveryMiddleware:
    """Filter only the current ``tools/list`` response."""

    def __init__(
        self,
        *,
        auth_enabled: bool,
        permission_lookup: CallerPermissionLookup | None = None,
    ) -> None:
        self._auth_enabled = bool(auth_enabled)
        self._permission_lookup = (
            permission_lookup if permission_lookup is not None else CallerPermissionLookup()
        )

    async def _visible_tools(self) -> set[str]:
        if not self._auth_enabled:
            return visible_mcp_tool_names(None, auth_enabled=False)
        from mcp.server.auth.middleware.auth_context import get_access_token

        access_token = get_access_token()
        scopes = list(access_token.scopes) if access_token is not None else []
        visible = visible_mcp_tool_names(scopes, auth_enabled=True)
        bearer = str(getattr(access_token, "token", "") or "").strip()
        if not visible or not bearer:
            return visible
        permissions = await self._permission_lookup.permissions(bearer)
        if permissions is None:
            return visible
        return role_visible_tool_names(visible, permissions)

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        if getattr(ctx, "method", "") != "tools/list":
            return result

        raw_tools = (
            result.get("tools") if isinstance(result, dict) else getattr(result, "tools", None)
        )
        if not isinstance(raw_tools, list):
            return result
        visible = await self._visible_tools()
        filtered_tools = [
            _annotated(tool)
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


__all__ = [
    "PERMISSIONS_PATH",
    "CallerPermissionLookup",
    "McpToolDiscoveryMiddleware",
    "role_visible_tool_names",
    "visible_mcp_tool_names",
]
