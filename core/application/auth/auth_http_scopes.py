"""Token scopes an HTTP request needs, by route family.

The auth middleware asks :func:`_required_http_scopes` which token scopes a
token-scoped principal must hold for a method and path. This module also holds
the route patterns and predicates that classify requests (workflow authoring,
secret catalog writes, extension callbacks and actions, self-service reads).
"""

from __future__ import annotations

import re

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


#: Unbinding a catalog secret from one field of a workflow or connection (ADR 0057).
#: It removes the binding, never the secret, and is the operator's like binding is;
#: the secret catalog service checks role, interactive session and scopes.
_SECRET_BINDING_RE = re.compile(
    r"^/api/projects/[^/]+/(?:workflows|connections)/[^/]+/secret-bindings/[^/]+$"
)


def _is_secret_unbind(path: str, method: str) -> bool:
    return method == "DELETE" and bool(_SECRET_BINDING_RE.fullmatch(path))


#: Deleting one catalog secret (ADR 0057): the operator's, like creating and editing
#: one; the catalog refuses a secret that is still bound.
_CATALOG_SECRET_RE = re.compile(r"^/api/secrets/[^/]+$")
_SECRET_WRITE_SCOPE = "secrets:write"  # pragma: allowlist secret - a scope name
_SECRET_BIND_SCOPE = "secrets:bind"  # pragma: allowlist secret - a scope name
#: The secret catalog's writes and the one exact token scope each needs (ADR 0057).
#: These scopes are exact-only: no wildcard and no admin:all grants them, and none
#: of these requests needs any other scope beside it.
_CATALOG_WRITE_SCOPES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"^/api/secrets$"), _SECRET_WRITE_SCOPE),
    ("PATCH", _CATALOG_SECRET_RE, _SECRET_WRITE_SCOPE),
    ("DELETE", _CATALOG_SECRET_RE, _SECRET_WRITE_SCOPE),
    ("PUT", re.compile(r"^/api/secrets/[^/]+/value$"), _SECRET_WRITE_SCOPE),
    ("PUT", _SECRET_BINDING_RE, _SECRET_BIND_SCOPE),
    # A value typed into a workflow secret input becomes a catalog secret bound to it:
    # the service also needs bind, as saving a connection's typed values does.
    (
        "PUT",
        re.compile(r"^/api/projects/[^/]+/workflows/[^/]+/secret-bindings/[^/]+/value$"),
        _SECRET_WRITE_SCOPE,
    ),
    ("DELETE", _SECRET_BINDING_RE, _SECRET_BIND_SCOPE),
)


def _catalog_write_scope(path: str, method: str) -> str | None:
    for write_method, pattern, scope in _CATALOG_WRITE_SCOPES:
        if method == write_method and pattern.fullmatch(path):
            return scope
    return None


def _is_catalog_secret_delete(path: str, method: str) -> bool:
    return method == "DELETE" and bool(_CATALOG_SECRET_RE.fullmatch(path))


def _is_operator_delete(path: str, method: str) -> bool:
    """The only DELETE requests below admin: unbinding and deleting catalog secrets."""
    return _is_secret_unbind(path, method) or _is_catalog_secret_delete(path, method)


#: A person's writes to their own account: signing out, password, email, own tokens,
_VIEWER_EXTENSION_DETAIL_RE = re.compile(r"^/api/extensions/([^/]+)(?:/compiled-manifest)?$")
_PROJECT_EXTENSION_CALLBACK_WRITE_RE = re.compile(
    r"^/api/extensions/[^/]+/callbacks/[^/]+(?:/(?:rotate|status))?$"
)
_PROJECT_EXTENSION_ACTION_WRITE_RE = re.compile(r"^/api/extensions/[^/]+/action/[^/]+$")
# Running an extension query spends the same authority as an action (ADR 0067): a
# query may resolve a project connection's credentials, so it follows the action
# rule -- operator or admin, or the token scopes of an action write.
_PROJECT_EXTENSION_QUERY_WRITE_RE = re.compile(r"^/api/extensions/[^/]+/query/[^/]+$")
_PROJECT_EFFECT_RESOLUTION_WRITE_RE = re.compile(
    r"^/api/projects/[^/]+/extensions/[^/]+/external-effects/[^/]+/resolution$"
)
# Reads any authenticated principal may make about itself, whatever its scopes: who it
# is, what it may do, which tokens are its own, and the product shell's bootstrap.
_SELF_SERVICE_READ_PATHS = frozenset(
    {
        "/api/auth/me",
        "/api/auth/introspect",
        "/api/auth/tokens",
        "/api/auth/multi-factor/status",
        "/api/app/bootstrap",
    }
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
        or _PROJECT_EXTENSION_QUERY_WRITE_RE.fullmatch(str(path or "").strip())
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
    catalog_scope = None if admin_required else _catalog_write_scope(normalized_path, method)
    if catalog_scope is not None:
        return [catalog_scope]
    if (
        admin_required
        or registry_write
        or (method == "DELETE" and not _is_operator_delete(normalized_path, method))
    ):
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
