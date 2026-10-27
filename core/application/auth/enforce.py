"""Authorization enforcement — transport-agnostic (no Request dependency)."""

from __future__ import annotations

import re

from core.application.auth.config import (
    AUTH_ADMIN_ONLY_PATTERNS,
    AUTH_PUBLIC_PATHS,
    PUBLIC_FRONTEND_PATH_PREFIXES,
    PUBLIC_HEALTH_PATHS,
    PUBLIC_WEBHOOK_PATH_PREFIXES,
    env_flag,
)
from core.application.auth.principal import roles_intersect

_MCP_CLIENT_METADATA_RE = re.compile(r"^/api/mcp/oauth/client-metadata/[^/]+\.json$")


def local_auth_enabled() -> bool:
    return env_flag("FS_LOCAL_AUTH_ENABLED", True)


def auth_enforced() -> bool:
    return local_auth_enabled()


def is_public_auth_path(path: str) -> bool:
    path_text = str(path or "").strip()
    if path_text in PUBLIC_HEALTH_PATHS or path_text in AUTH_PUBLIC_PATHS:
        return True
    if _MCP_CLIENT_METADATA_RE.fullmatch(path_text):
        return True
    if any(path_text.startswith(prefix) for prefix in PUBLIC_WEBHOOK_PATH_PREFIXES):
        return True
    if any(
        path_text == prefix or path_text.startswith(f"{prefix}/")
        for prefix in PUBLIC_FRONTEND_PATH_PREFIXES
    ):
        return True
    return path_text.startswith(("/static/", "/static-v/"))


def is_admin_only_route(path: str) -> bool:
    path_text = str(path or "").strip()
    if not path_text:
        return False
    return any(pattern.match(path_text) for pattern in AUTH_ADMIN_ONLY_PATTERNS)


def require_authenticated(user: dict | None) -> dict:
    if not isinstance(user, dict):
        raise PermissionError("Authentication required")
    return user


def require_roles(user: dict | None, required_roles: set[str] | None = None) -> dict:
    user = require_authenticated(user)
    required = {
        str(role).strip().lower() for role in (required_roles or set()) if str(role).strip()
    }
    if required and not roles_intersect(user.get("roles") or [], required):
        raise PermissionError("Insufficient role")
    return user


def normalize_requested_roles(raw_roles: list[str] | None) -> list[str]:
    allowed_roles = {"admin", "operator", "viewer"}
    deduped: list[str] = []
    for role in raw_roles or []:
        normalized = str(role or "").strip().lower()
        if normalized and normalized in allowed_roles and normalized not in deduped:
            deduped.append(normalized)
    return deduped
