"""Reusable HTTP auth middleware runtime helpers."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from fastapi.responses import JSONResponse, RedirectResponse

from core.application.auth.principal import ResolvedPrincipal, principal_has_required_scopes
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.mcp_dev_proxy import MCP_DEV_AUTH_HEADER, mcp_dev_proxy_enabled

_TOKEN_COMMAND = "command:*"
_PATH_API_PROJECTS = "/api/projects/"
_TOKEN_READ = "read:*"
_TOKEN_ADMIN_ALL = "admin:all"
_TOKEN_PROJECTS = "projects:*"
_TOKEN_JOBS = "jobs:*"
_TOKEN_WORKFLOWS = "workflows:*"
_TOKEN_WORKFLOWS_READ = "workflows.read"
_TOKEN_REGISTRY_READ = "registry.read"
_TOKEN_REGISTRY = "registry:*"
_TOKEN_PROJECTS_WRITE = "projects.write"
_TOKEN_JOBS_READ = "jobs.read"

_HTTP_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_WORKFLOW_AUTHORING_READ_RE = re.compile(
    r"^/api/projects/[^/]+/workflow-authoring/(?:capabilities|capability|examples)$"
)
_WORKFLOW_AUTHORING_VALIDATE_RE = re.compile(r"^/api/projects/[^/]+/workflow-authoring/validate$")
# Creating workflows in a project: a new draft, or an imported bundle.
_WORKFLOW_AUTHORING_CREATE_RE = re.compile(
    r"^/api/projects/[^/]+/(?:workflow-authoring/drafts|workflow-imports)$"
)
# The one writer of an existing workflow's definition (builder, API and MCP).
_WORKFLOW_CHANGES_RE = re.compile(r"^/api/workflows/[^/]+/changes$")


def _is_workflow_authoring_validate(path: str, method: str) -> bool:
    return method == "POST" and bool(_WORKFLOW_AUTHORING_VALIDATE_RE.fullmatch(path))


def _is_workflow_authoring_create(path: str, method: str) -> bool:
    return method == "POST" and (
        bool(_WORKFLOW_AUTHORING_CREATE_RE.fullmatch(path))
        or bool(_WORKFLOW_CHANGES_RE.fullmatch(path))
    )


_VIEWER_EXTENSION_DETAIL_RE = re.compile(r"^/api/extensions/([^/]+)(?:/compiled-manifest)?$")
_PROJECT_EXTENSION_CALLBACK_WRITE_RE = re.compile(
    r"^/api/extensions/[^/]+/callbacks/[^/]+(?:/(?:rotate|status))?$"
)
_PROJECT_EXTENSION_ACTION_WRITE_RE = re.compile(r"^/api/extensions/[^/]+/action/[^/]+$")
_PROJECT_EFFECT_RESOLUTION_WRITE_RE = re.compile(
    r"^/api/projects/[^/]+/extensions/[^/]+/external-effects/[^/]+/resolution$"
)
# Reads any authenticated principal may make about itself, whatever its scopes: who it
# is, what it may do, which tokens are its own, and the product shell's bootstrap.
_SELF_SERVICE_READ_PATHS = frozenset(
    {"/api/auth/me", "/api/auth/introspect", "/api/auth/tokens", "/api/app/bootstrap"}
)
_SELF_SERVICE_READ_PREFIXES = ("/api/users/me/",)
_JOB_READ_PREFIXES = (
    "/api/v2/jobs",
    "/api/v2/dashboard/",
    "/api/dashboard/",
    "/api/events",
    "/api/messages/",
    "/api/watchdog/",
    "/api/workload",
)
_PROJECT_READ_PREFIXES = ("/api/v2/projects",)
_REGISTRY_READ_PREFIXES = ("/api/changelog", "/api/graph", "/api/registry/", "/api/validate")
# AC17-R155 review, T1: the project's own configuration -- creating it, its settings,
# the workflows linked to it and its run policy. Its connections, secrets, schedules and
# workspace stay behind command:*; deleting it stays an administrator's.
_PROJECT_CONFIGURATION_WRITES = (
    ("POST", re.compile(r"^/api/projects$")),
    ("PUT", re.compile(r"^/api/projects/[^/]+(?:/(?:workflows|policy))?$")),
)
_RESERVED_EXTENSION_ROUTE_SEGMENTS = {
    "bootstrap",
    "descriptors",
    "install",
    "installs",
    "marketplace",
    "releases",
    "runtime",
    "scan",
}
_EXTERNAL_AGENT_ROUTE_SCOPES = {
    "/api/external-agents/register": ["agent:register"],
    "/api/external-agents/heartbeat": ["agent:heartbeat"],
    "/api/external-agents/claim": ["agent:claim"],
    "/api/external-agents/report": ["agent:report"],
}


def _auth_disabled_override_allowed() -> bool:
    raw_override = _runtime_env.get("FS_AUTH_ALLOW_DISABLED")
    if raw_override is not None:
        return str(raw_override).strip().lower() in {"1", "true", "yes", "on"}
    # AC17-R129: one resolver, not a second copy of the same three markers. This read
    # its own and answered "local_development" when none were set, so a deployment that
    # never declared an environment was allowed to switch authentication off.
    from core.infrastructure.security.config import _deployment_environment

    return _deployment_environment() not in {"production", "prod"}


def mcp_dev_system_principal(request: Any) -> ResolvedPrincipal | None:
    """Resolve the explicit local MCP bypass to the documented system actor."""
    if not mcp_dev_proxy_enabled():
        return None
    marker = str(request.headers.get(MCP_DEV_AUTH_HEADER) or "").strip()
    if marker != "1":
        return None
    return ResolvedPrincipal(
        id="mcp-dev-system",
        username="system",
        display_name="Local MCP system actor",
        roles=["admin"],
        scopes=["admin:all"],
        auth_source="mcp_dev",
        principal_type="system_actor",
    )


def _principal_requires_http_scopes(user: dict[str, Any] | None) -> bool:
    if not isinstance(user, dict):
        return False
    auth_source = str(user.get("auth_source") or "").strip().lower()
    principal_type = str(user.get("principal_type") or "").strip().lower()
    return auth_source in {"pat", "machine"} or principal_type == "service_account"


def _is_viewer_extension_detail_read(path: str, method: str) -> bool:
    if method != "GET":
        return False
    match = _VIEWER_EXTENSION_DETAIL_RE.fullmatch(str(path or "").strip())
    return bool(match and match.group(1) not in _RESERVED_EXTENSION_ROUTE_SEGMENTS)


def _is_project_extension_callback_write(path: str, method: str) -> bool:
    return method == "POST" and bool(
        _PROJECT_EXTENSION_CALLBACK_WRITE_RE.fullmatch(str(path or "").strip())
    )


def _is_project_extension_action_write(path: str, method: str) -> bool:
    return method == "POST" and bool(
        _PROJECT_EXTENSION_ACTION_WRITE_RE.fullmatch(str(path or "").strip())
        or _PROJECT_EFFECT_RESOLUTION_WRITE_RE.fullmatch(str(path or "").strip())
    )


def _required_http_scopes(
    path: str,
    method: str,
    *,
    admin_required: bool,
    project_callback_write: bool,
    registry_read: bool,
    registry_write: bool,
    project_action_write: bool = False,
) -> list[str]:
    normalized_path = str(path or "").strip()
    if normalized_path == "/api/changelog" or normalized_path.startswith("/api/changelog/"):
        # Changelog joins the registry role gate, not its token-scope policy.
        # Preserve the existing read family and command fallback below.
        registry_read = False
        registry_write = False
    is_notification_read = (
        normalized_path.startswith(_PATH_API_PROJECTS)
        and method in _HTTP_SAFE_METHODS
        and normalized_path.endswith(
            ("/notification-routes", "/notification-deliveries", "/notification-overview")
        )
    )
    is_project_jobs = normalized_path.startswith(_PATH_API_PROJECTS) and normalized_path.endswith(
        "/jobs"
    )
    is_project_workflow_read = (
        normalized_path.startswith(_PATH_API_PROJECTS)
        and method in _HTTP_SAFE_METHODS
        and (
            normalized_path.endswith(("/workflow-catalog", "/workflow-entrypoints"))
            or "/workflows/" in normalized_path
        )
    )
    is_authoring_read = _is_workflow_authoring_validate(normalized_path, method) or (
        method in _HTTP_SAFE_METHODS
        and (
            normalized_path == "/api/workflow-authoring/schema"
            or bool(_WORKFLOW_AUTHORING_READ_RE.fullmatch(normalized_path))
        )
    )
    if _is_workflow_authoring_create(normalized_path, method):
        return [_TOKEN_ADMIN_ALL]
    if is_authoring_read:
        return [
            _TOKEN_WORKFLOWS_READ,
            _TOKEN_WORKFLOWS,
            _TOKEN_REGISTRY_READ,
            _TOKEN_REGISTRY,
            _TOKEN_READ,
            _TOKEN_ADMIN_ALL,
        ]
    if is_notification_read:
        return ["notify.read", "notify:*", _TOKEN_READ, _TOKEN_ADMIN_ALL]
    if is_project_jobs:
        return [_TOKEN_JOBS_READ, _TOKEN_JOBS, _TOKEN_READ, _TOKEN_ADMIN_ALL]
    if is_project_workflow_read:
        return [
            _TOKEN_WORKFLOWS_READ,
            _TOKEN_WORKFLOWS,
            _TOKEN_REGISTRY_READ,
            _TOKEN_REGISTRY,
            _TOKEN_READ,
            _TOKEN_ADMIN_ALL,
        ]
    if normalized_path.startswith("/api/reports"):
        if method in _HTTP_SAFE_METHODS:
            return ["reports.read", "reports:*", _TOKEN_READ, _TOKEN_ADMIN_ALL]
        return ["reports.write", "reports:*", _TOKEN_COMMAND, _TOKEN_ADMIN_ALL]
    if normalized_path.startswith(("/api/jobs", "/api/approvals")):
        if method in _HTTP_SAFE_METHODS:
            return [_TOKEN_JOBS_READ, _TOKEN_JOBS, _TOKEN_READ, _TOKEN_ADMIN_ALL]
        return ["jobs.write", _TOKEN_JOBS, _TOKEN_COMMAND, _TOKEN_ADMIN_ALL]
    if normalized_path == "/api/workflow-runs" and method == "POST":
        return ["workflows.write", _TOKEN_WORKFLOWS, _TOKEN_COMMAND, _TOKEN_ADMIN_ALL]
    if method in _HTTP_SAFE_METHODS:
        if admin_required:
            return [_TOKEN_ADMIN_ALL]
        if normalized_path.startswith("/api/projects"):
            return ["projects.read", _TOKEN_PROJECTS, _TOKEN_READ, _TOKEN_ADMIN_ALL]
        if normalized_path.startswith("/api/workflows"):
            return [
                _TOKEN_WORKFLOWS_READ,
                _TOKEN_WORKFLOWS,
                _TOKEN_REGISTRY_READ,
                _TOKEN_REGISTRY,
                _TOKEN_ADMIN_ALL,
            ]
        if normalized_path.startswith("/api/mcp"):
            return ["mcp:use", _TOKEN_COMMAND, _TOKEN_ADMIN_ALL]
        if registry_read:
            return [_TOKEN_REGISTRY_READ, _TOKEN_REGISTRY, _TOKEN_ADMIN_ALL]
        return _read_scopes(normalized_path)
    if project_callback_write or project_action_write:
        return [_TOKEN_PROJECTS_WRITE, _TOKEN_PROJECTS, _TOKEN_COMMAND, _TOKEN_ADMIN_ALL]
    if any(
        method == write_method and pattern.fullmatch(normalized_path)
        for write_method, pattern in _PROJECT_CONFIGURATION_WRITES
    ):
        # Before the admin branch: linking workflows is an admin-only route, and that
        # stays the middleware's role gate. The scope decides only what a token of an
        # administrator may be narrowed to (T1 acceptance: it was still admin:all).
        return [_TOKEN_PROJECTS_WRITE, _TOKEN_PROJECTS, _TOKEN_COMMAND, _TOKEN_ADMIN_ALL]
    if admin_required or registry_write or method == "DELETE":
        return [_TOKEN_ADMIN_ALL]
    if normalized_path.startswith(_PATH_API_PROJECTS) and normalized_path.endswith(
        "/notifications/dispatch"
    ):
        return ["notify:dispatch", "notify:*", _TOKEN_COMMAND, _TOKEN_ADMIN_ALL]
    if normalized_path.startswith("/webhook/workflows/"):
        return ["workflows.write", _TOKEN_WORKFLOWS, _TOKEN_COMMAND, _TOKEN_ADMIN_ALL]
    agent_scopes = _EXTERNAL_AGENT_ROUTE_SCOPES.get(normalized_path)
    if agent_scopes is not None:
        return [*agent_scopes, _TOKEN_COMMAND]
    if normalized_path.startswith("/api/mcp"):
        return ["mcp:use", _TOKEN_COMMAND]
    return [_TOKEN_COMMAND]


def _read_scopes(path: str) -> list[str]:
    """The scopes a read needs when no route family above claimed it.

    Review (P1): every other read answered no scope at all, so a token scoped to, say,
    a narrowly scoped token read unrelated job details, security findings, or search
    results. A read now belongs to a resource family or needs read:*. Public paths never
    reach this point (the middleware passes them before any scope check).
    """
    if path in _SELF_SERVICE_READ_PATHS or path.startswith(_SELF_SERVICE_READ_PREFIXES):
        return []
    if path.startswith("/api/v2/") and path.endswith("/jobs"):
        return [_TOKEN_JOBS_READ, _TOKEN_JOBS, _TOKEN_READ, _TOKEN_ADMIN_ALL]
    if path.startswith(_JOB_READ_PREFIXES):
        return [_TOKEN_JOBS_READ, _TOKEN_JOBS, _TOKEN_READ, _TOKEN_ADMIN_ALL]
    if path.startswith(_PROJECT_READ_PREFIXES):
        return ["projects.read", _TOKEN_PROJECTS, _TOKEN_READ, _TOKEN_ADMIN_ALL]
    if path.startswith(_REGISTRY_READ_PREFIXES):
        return [_TOKEN_REGISTRY_READ, _TOKEN_REGISTRY, _TOKEN_READ, _TOKEN_ADMIN_ALL]
    return [_TOKEN_READ, _TOKEN_ADMIN_ALL]


def _login_redirect_target(path_login: str, *, next_path: str) -> str:
    target = str(path_login or "/login").strip() or "/login"
    requested = str(next_path or "").strip()
    if not requested.startswith("/") or requested.startswith("//"):
        return target
    separator = "&" if "?" in target else "?"
    return f"{target}{separator}{urlencode({'next': requested})}"


async def local_auth_middleware(
    request,
    call_next,
    *,
    local_auth_enabled_fn,
    external_auth_enabled,
    is_public_auth_path_fn,
    route_registered_fn,
    request_principal_resolver_fn,
    normalize_live_principal_fn,
    get_logger_only_fn,
    auth_tables_available_fn,
    get_user_by_session_token_fn,
    local_auth_bootstrap_required_fn,
    auth_account_tables_available_fn,
    roles_intersect_fn,
    is_admin_only_route_fn,
    is_registry_read_route_fn,
    is_self_service_auth_write_route_fn,
    csrf_request_valid_fn,
    auth_cookie_name: str,
    auth_unsafe_methods: set[str],
    auth_admin_roles: set[str],
    auth_write_roles: set[str],
    err_database_not_configured: str,
    err_auth_tables_not_available: str,
    err_authentication_required: str,
    err_account_disabled: str,
    err_admin_role_required: str,
    err_operator_role_required: str,
    path_bootstrap: str,
    path_login: str,
) -> Any:
    request.state.auth_user = None
    request.state.auth_principal = None
    request.state.auth_source = None
    local_auth_enabled = local_auth_enabled_fn()
    external_enabled = (
        external_auth_enabled() if callable(external_auth_enabled) else external_auth_enabled
    )
    path = str(request.url.path or "")
    method = str(request.method or "GET").upper()
    dev_principal = mcp_dev_system_principal(request)
    if not (local_auth_enabled or external_enabled):
        if dev_principal is None and _auth_disabled_override_allowed():
            return await call_next(request)
        if dev_principal is None:
            return JSONResponse({"error": "Authentication is not configured"}, status_code=503)

    public_auth_path = bool(is_public_auth_path_fn(path))
    if callable(route_registered_fn):
        try:
            if not route_registered_fn(path, method):
                return await call_next(request)
        except Exception:
            pass

    auth_source = ""
    token_source = ""
    bl = None

    user = None
    principal = dev_principal
    if isinstance(principal, ResolvedPrincipal):
        auth_source = "mcp_dev"
    elif external_enabled:
        bl = get_logger_only_fn()
        user = request_principal_resolver_fn(request, bl)
        principal = normalize_live_principal_fn(user, default_auth_source="external")
        if isinstance(principal, ResolvedPrincipal):
            auth_source = "external"
    if not isinstance(principal, ResolvedPrincipal) and local_auth_enabled:
        bl = bl or get_logger_only_fn()
        auth_header = str(request.headers.get("authorization") or "").strip()
        bearer_token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        session_cookie_token = str(request.cookies.get(auth_cookie_name) or "").strip()
        token = bearer_token or session_cookie_token
        if bearer_token:
            token_source = "bearer"
        elif session_cookie_token:
            token_source = "cookie"
        else:
            token_source = ""

        if not bl:
            if public_auth_path:
                return await call_next(request)
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if session_cookie_token and not auth_tables_available_fn(bl):
            if public_auth_path:
                return await call_next(request)
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        user = get_user_by_session_token_fn(bl, token) if token else None
        if not isinstance(user, dict) and bearer_token:
            from core.application.auth.resolve import (
                resolve_machine_principal,
                resolve_pat_principal,
            )

            user = resolve_pat_principal(bl, bearer_token)
            if not isinstance(user, dict):
                user = resolve_machine_principal(bl, bearer_token)
        principal = normalize_live_principal_fn(user, default_auth_source="local")
        auth_source = "local"

    if not isinstance(principal, ResolvedPrincipal):
        if public_auth_path:
            return await call_next(request)
        bootstrap_required = bool(local_auth_enabled and local_auth_bootstrap_required_fn(bl))
        accept = str(request.headers.get("accept") or "")
        if "text/html" in accept and not path.startswith("/api/"):
            query_suffix = f"?{request.url.query}" if request.url.query else ""
            next_path = f"{path}{query_suffix}"
            return RedirectResponse(
                url=path_bootstrap
                if bootstrap_required
                else _login_redirect_target(
                    path_login,
                    next_path=next_path,
                ),
                status_code=302,
            )
        payload = {"error": err_authentication_required}
        if bootstrap_required:
            payload["bootstrap_required"] = True
        return JSONResponse(payload, status_code=401)

    if not bool(principal.is_active):
        if public_auth_path:
            return await call_next(request)
        return JSONResponse({"error": err_account_disabled}, status_code=403)
    if local_auth_enabled and bl and auth_account_tables_available_fn(bl):
        account_active = getattr(bl, "is_account_active", None)
        if callable(account_active):
            try:
                if not bool(account_active(principal.account_id)):
                    if public_auth_path:
                        return await call_next(request)
                    return JSONResponse({"error": err_account_disabled}, status_code=403)
            except Exception:
                if public_auth_path:
                    return await call_next(request)
                return JSONResponse({"error": "Auth account state is unavailable"}, status_code=503)

    request.state.auth_principal = principal
    request.state.auth_user = principal.model_dump()
    request.state.auth_source = str(principal.auth_source or auth_source).strip() or auth_source
    user = request.state.auth_user

    if (
        auth_source == "local"
        and token_source == "cookie"
        and method in auth_unsafe_methods
        and not csrf_request_valid_fn(request)
    ):
        return JSONResponse({"error": "CSRF validation failed"}, status_code=403)

    authoring_validate = _is_workflow_authoring_validate(path, method)
    admin_required = bool(
        is_admin_only_route_fn(path)
        or method == "DELETE"
        or _is_workflow_authoring_create(path, method)
    )
    registry_read_route = bool(is_registry_read_route_fn(path))
    registry_write = bool(registry_read_route and method not in _HTTP_SAFE_METHODS)
    viewer_extension_detail_read = _is_viewer_extension_detail_read(path, method)
    project_callback_write = _is_project_extension_callback_write(path, method)
    project_action_write = _is_project_extension_action_write(path, method)

    required_token_scopes = (
        []
        if public_auth_path and method in _HTTP_SAFE_METHODS
        else _required_http_scopes(
            path,
            method,
            admin_required=admin_required,
            project_callback_write=project_callback_write,
            project_action_write=project_action_write,
            registry_read=registry_read_route,
            registry_write=registry_write,
        )
    )
    if (
        required_token_scopes
        and _principal_requires_http_scopes(user)
        and not principal_has_required_scopes(user, required_token_scopes)
    ):
        return JSONResponse({"error": "Missing required token scope"}, status_code=403)

    if admin_required:
        if not roles_intersect_fn(user.get("roles") or [], auth_admin_roles):
            return JSONResponse({"error": err_admin_role_required}, status_code=403)
    elif registry_read_route and not viewer_extension_detail_read:
        if method in _HTTP_SAFE_METHODS:
            if not roles_intersect_fn(user.get("roles") or [], auth_write_roles):
                return JSONResponse({"error": err_operator_role_required}, status_code=403)
        else:
            required_roles = (
                auth_write_roles
                if project_callback_write or project_action_write
                else auth_admin_roles
            )
            if not roles_intersect_fn(user.get("roles") or [], required_roles):
                error = (
                    err_operator_role_required
                    if project_callback_write or project_action_write
                    else err_admin_role_required
                )
                return JSONResponse({"error": error}, status_code=403)
    elif method not in _HTTP_SAFE_METHODS and not authoring_validate:
        if auth_source == "local" and is_self_service_auth_write_route_fn(path):
            return await call_next(request)
        if not roles_intersect_fn(user.get("roles") or [], auth_write_roles):
            return JSONResponse({"error": err_operator_role_required}, status_code=403)

    return await call_next(request)


def build_local_auth_middleware_handler(**kwargs):
    async def _handler(request, call_next):
        return await local_auth_middleware(request, call_next, **kwargs)

    return _handler
