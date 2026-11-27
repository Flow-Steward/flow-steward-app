"""Reusable HTTP auth middleware runtime helpers."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from core.application.auth.auth_http_scopes import (
    _HTTP_SAFE_METHODS,
    _is_operator_delete,
    _is_project_extension_action_write,
    _is_project_extension_callback_write,
    _is_viewer_extension_detail_read,
    _is_workflow_authoring_create,
    _is_workflow_authoring_validate,
    _required_http_scopes,
)
from core.application.auth.auth_http_scopes import (
    _SELF_SERVICE_READ_PATHS as _SELF_SERVICE_READ_PATHS,
)
from core.application.auth.effective_permissions import (
    NOT_ROLE_BASED,
    effective_permissions,
    load_role_permissions,
    owner_role_permissions,
    principal_permission_kind,
    role_permission_keys,
)
from core.application.auth.inbound_oauth_policy import (
    MCP_FORWARD_HEADER,
    PROTOCOL_PATHS,
    current_oauth_environment,
    is_interactive_only_path,
    is_oauth_access_token,
    is_oauth_non_bearer_secret,
    oauth_forward_accepted,
    oauth_principal_denied_route,
)
from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID
from core.application.auth.permissions import (
    ADMIN_ROLE,
    OPERATOR_ROLE,
    PERMISSIONS,
    SYSTEM_ROLE_PERMISSIONS,
    highest_system_role,
    token_grants_permission,
)
from core.application.auth.principal import (
    ResolvedPrincipal,
    is_machine_principal,
    is_token_scoped_principal,
    principal_allowed_project_ids,
    principal_allowed_workspace_ids,
    principal_has_required_scopes,
    principal_scopes,
    principal_user_id,
)
from core.application.auth.project_scope_limit import (
    BODY_WORKFLOW_MAX_BYTES,
    LIMIT_UNAVAILABLE_MESSAGE,
    ScopedPlaces,
    principal_places_payload,
    project_id_from_path,
    scope_limit_route_refusal,
    scoped_places,
    stored_scope_limit,
    workflow_id_from_body,
    workflow_id_from_project_path,
)
from core.application.auth.route_permissions import (
    RoutePermission,
    RoutePermissionRule,
    is_module_api_path,
    route_admin_only,
    route_permission,
)
from core.application.registry.workflow_account import bind_workflow_account
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.mcp_dev_proxy import MCP_DEV_AUTH_HEADER, mcp_dev_proxy_enabled

_LOG = logging.getLogger(__name__)


#: OAuth grants and the AI agent wizard (ADR 0076 ``account.self``).
OWN_ACCOUNT_PERMISSION = "account.self"
#: Open owner question (ADR 0076 part 2): a person signed in through an external
#: provider writes to their own account only with what the profile writes need, the
#: operator's today. ``account.self`` would let an external viewer do it as well.
_EXTERNAL_OWN_ACCOUNT_WRITE_PERMISSION = "account.profile.write"
#: The writes a person's session may make where they have no role (ADR 0085, P5),
#: besides answering an invitation link (a route any signed-in person may call): sign
#: out, and switch to an account where they have a role (the route checks that).
ROLELESS_SESSION_WRITES = frozenset(
    {("POST", "/api/auth/logout"), ("POST", "/api/auth/switch-account")}
)
NO_ROLE_IN_ACCOUNT_CODE = "no_role_in_account"
NO_ROLE_IN_ACCOUNT_MESSAGE = "You have no role in this company. Ask its administrator."
#: No rule of the route table maps the request: a write nobody may make.
_UNMAPPED = object()
_WORKFLOW_RUN_RE = re.compile(r"^/api/workflows/[^/]+/run$")


def _route_needs(
    method: str,
    path: str,
    permission: str,
    contributed_rules: Sequence[RoutePermissionRule] = (),
) -> bool:
    match = route_permission(method, path, contributed_rules)
    return match is not None and match.permission == permission


def _is_workflow_run_write(path: str, method: str) -> bool:
    """Starting a workflow from the registry route: a service account's operator work."""
    return method == "POST" and bool(_WORKFLOW_RUN_RE.fullmatch(path))


def _stored_role_permissions(bl: Any) -> Mapping[str, frozenset[str]]:
    """The ``role_permissions`` rows, read once per request.

    A table without a single row was never seeded (the migration writes the system
    roles' lists): the system roles then act on the catalogue matrix those rows hold,
    instead of every operator and viewer losing every permission.
    """
    stored = load_role_permissions(bl.conn)
    if stored:
        return stored
    _LOG.warning("role_permissions holds no rows; using the system roles' catalogue lists")
    return SYSTEM_ROLE_PERMISSIONS


def _contributed_route_rules(
    contributed_route_rules_fn: Callable[[], Sequence[RoutePermissionRule]] | None,
) -> tuple[RoutePermissionRule, ...]:
    """Module route rules for this request; a failing reader contributes none (closed)."""
    if not callable(contributed_route_rules_fn):
        return ()
    try:
        return tuple(contributed_route_rules_fn() or ())
    except Exception:
        _LOG.warning("module route permission rules are unavailable; using Core rules only")
        return ()


def _person_route_permission(
    method: str,
    path: str,
    *,
    interactive_source: str,
    contributed_own_account_write: bool,
    contributed_rules: Sequence[RoutePermissionRule] = (),
) -> str | None | object:
    """The permission a person's request needs, ``None`` for any signed-in person, or
    :data:`_UNMAPPED` when no rule maps it.

    A write an edition contributes as a person's own-account route (the
    ``self_service_auth_write_paths`` registry) needs ``account.self`` like Core's own.
    ``contributed_rules`` are the route rules platform modules registered (ADR 0082).
    """
    match = route_permission(method, path, contributed_rules)
    if match is not None:
        permission = match.permission
    elif contributed_own_account_write:
        permission = OWN_ACCOUNT_PERMISSION
    else:
        return _UNMAPPED
    if (
        permission == OWN_ACCOUNT_PERMISSION
        and method not in _HTTP_SAFE_METHODS
        and interactive_source == "external"
    ):
        return _EXTERNAL_OWN_ACCOUNT_WRITE_PERMISSION
    return permission


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
        # Core's system actor acts in the installation account; it names it rather
        # than relying on a default (ADR 0083: an empty account is no account).
        account_id=INSTALLATION_ACCOUNT_ID,
    )


def _principal_requires_http_scopes(user: dict[str, Any] | None) -> bool:
    return isinstance(user, dict) and is_token_scoped_principal(user)


def _module_token_refusal(user: dict[str, Any], match: RoutePermission | None) -> Any:
    """A token on a module route also needs the catalogue grant of the route's permission.

    The coarse read/command scope list above is what Core asks of every route; for a
    module route the token must in addition be granted the declared permission through
    :func:`core.application.auth.permissions.token_grants_permission`, the catalogue's
    own permission-to-scope table (``secrets:write`` for ``secrets.value.write``,
    ``admin:all`` for an admin permission, never for ``secrets.approve``).
    """
    if match is None or not _principal_requires_http_scopes(user):
        return None
    definition = PERMISSIONS.get(str(match.permission or ""))
    if definition is not None and token_grants_permission(definition, principal_scopes(user)):
        return None
    return JSONResponse(
        {"error": f"Missing required token scope (the route needs {match.permission})"},
        status_code=403,
    )


#: The machine code a module route refuses with when the caller is not an
#: installation administrator; the same code Core's installation-wide routes use.
MODULE_INSTALLATION_ADMIN_REQUIRED_CODE = "installation_admin_required"


def _module_installation_admin_refusal(
    user: dict[str, Any],
    match: RoutePermission | None,
    *,
    installation_admin_fn: Callable[[Any], bool] | None,
    roles_intersect_fn: Any,
    auth_admin_roles: set[str],
) -> Any:
    """Refuse a module route that requires an installation administrator (ADR 0082).

    Only a rule registered with ``requires_installation_admin`` asks. Its permission
    is admin-only, so the route table already demands the admin check; this adds
    ``installation_admin_fn``, the installation-administrator predicate. Without one
    the predicate falls back to that same admin check, the admin role. The app
    composition passes ``core.application.auth.installation_admin.is_installation_admin``,
    so an administrator acting in another account is refused here too.
    """
    if match is None or not match.rule.requires_installation_admin:
        return None
    if callable(installation_admin_fn):
        allowed = bool(installation_admin_fn(user))
    else:
        allowed = bool(roles_intersect_fn(user.get("roles") or [], auth_admin_roles))
    if allowed:
        return None
    return JSONResponse(
        {
            "error": "Only an installation administrator can use this route",
            "code": MODULE_INSTALLATION_ADMIN_REQUIRED_CODE,
        },
        status_code=403,
    )


async def _module_machine_refusal(
    principal: ResolvedPrincipal,
    user: dict[str, Any],
    *,
    match: RoutePermission | None,
    admin_required: bool,
    bl: Any,
    load_role_permissions_fn: Any,
    roles_intersect_fn: Any,
    auth_admin_roles: set[str],
    err_route_not_mapped: str,
    err_admin_role_required: str,
    err_operator_role_required: str,
) -> Any:
    """Service accounts, extension runtime and system actors in a module's API tree.

    No blanket rule of the machine branch applies there: the declared permission must
    be held through the principal's roles and the stored role permissions, the mapping
    a person's request uses. A route no module rule maps is a read under the generic
    ``platform.read`` rule or an unmapped write, refused to everybody.
    """
    if match is None or match.permission is None:
        return JSONResponse({"error": err_route_not_mapped}, status_code=403)
    if admin_required and not roles_intersect_fn(user.get("roles") or [], auth_admin_roles):
        return JSONResponse({"error": err_admin_role_required}, status_code=403)
    try:
        stored = (
            {}
            if highest_system_role(principal.roles) == ADMIN_ROLE
            else await run_in_threadpool(load_role_permissions_fn or _stored_role_permissions, bl)
        )
    except Exception:
        return JSONResponse({"error": "Role permissions are unavailable"}, status_code=503)
    if match.permission in owner_role_permissions(principal, role_permissions=stored):
        return None
    operator_holds = match.permission in role_permission_keys(OPERATOR_ROLE, stored)
    error = err_operator_role_required if operator_holds else err_admin_role_required
    return JSONResponse({"error": error}, status_code=403)


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
    load_role_permissions_fn: Any = None,
    err_route_not_mapped: str = "No permission is defined for this route",
    contributed_route_rules_fn: Callable[[], Sequence[RoutePermissionRule]] | None = None,
    load_scope_limit_fn: Callable[[Any, str, str], Any] | None = None,
    installation_admin_fn: Callable[[Any], bool] | None = None,
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

    if path in PROTOCOL_PATHS:
        # ADR 0047 decision 4: the protocol endpoints authenticate the client by the
        # protocol itself and never derive identity from a cookie or a bearer.
        return await call_next(request)

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
        bl = await run_in_threadpool(get_logger_only_fn)
        user = await run_in_threadpool(request_principal_resolver_fn, request, bl)
        principal = normalize_live_principal_fn(user, default_auth_source="external")
        if isinstance(principal, ResolvedPrincipal):
            auth_source = "external"
    if not isinstance(principal, ResolvedPrincipal) and local_auth_enabled:
        bl = bl or await run_in_threadpool(get_logger_only_fn)
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
        if session_cookie_token and not await run_in_threadpool(auth_tables_available_fn, bl):
            if public_auth_path:
                return await call_next(request)
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        user = await run_in_threadpool(
            _resolve_local_token,
            bl,
            token=token,
            bearer_token=bearer_token,
            get_user_by_session_token_fn=get_user_by_session_token_fn,
        )
        principal = normalize_live_principal_fn(user, default_auth_source="local")
        auth_source = "local"

    if not isinstance(principal, ResolvedPrincipal):
        if public_auth_path:
            return await call_next(request)
        bootstrap_required = bool(
            local_auth_enabled and await run_in_threadpool(local_auth_bootstrap_required_fn, bl)
        )
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
    if local_auth_enabled and bl and await run_in_threadpool(auth_account_tables_available_fn, bl):
        account_active = getattr(bl, "is_account_active", None)
        if callable(account_active):
            try:
                if not bool(await run_in_threadpool(account_active, principal.account_id)):
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
    # The principal's own source decides its rules: a cookie session that an external
    # sign-in issued resolves through the local token path but stays ``external``.
    # ``auth_source`` (how the credential arrived) only decides the CSRF check below.
    principal_source = request.state.auth_source
    user = request.state.auth_user
    # Workflow identity is account-qualified (ADR 0020, C4b): every workflow read
    # and write of this request that names no account works on the caller's own.
    bind_workflow_account(principal.account_id)

    oauth_refusal = _oauth_request_refusal(
        request, principal=principal, path=path, method=method, token_source=token_source
    )
    if oauth_refusal is not None:
        return oauth_refusal

    if (
        auth_source == "local"
        and token_source == "cookie"
        and method in auth_unsafe_methods
        and not csrf_request_valid_fn(request)
    ):
        return JSONResponse({"error": "CSRF validation failed"}, status_code=403)

    authoring_validate = _is_workflow_authoring_validate(path, method)
    machine = is_machine_principal(user)
    contributed_rules = _contributed_route_rules(contributed_route_rules_fn)
    # ADR 0082: a module's API tree is decided by its own rules, else the generic read
    # rule; no own-account or machine-write default reaches into it.
    module_tree = is_module_api_path(path)
    module_match = route_permission(method, path, contributed_rules) if module_tree else None
    # ADR 0076 §3: token scopes and service accounts keep their rules. A former
    # admin-only route, a delete and creating workflows need admin:all from a token and
    # the admin role from a service account (its operator deletes excepted).
    admin_required = bool(
        route_admin_only(method, path, contributed_rules)
        or (method == "DELETE" and not (machine and _is_operator_delete(path, method)))
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
        # Scope names are public; naming them tells the caller which token to use.
        accepted = ", ".join(sorted({str(scope) for scope in required_token_scopes}))
        return JSONResponse(
            {"error": f"Missing required token scope (needs one of: {accepted})"},
            status_code=403,
        )
    if module_tree:
        token_refusal = _module_token_refusal(user, module_match)
        if token_refusal is not None:
            return token_refusal
        installation_admin_refusal = _module_installation_admin_refusal(
            user,
            module_match,
            installation_admin_fn=installation_admin_fn,
            roles_intersect_fn=roles_intersect_fn,
            auth_admin_roles=auth_admin_roles,
        )
        if installation_admin_refusal is not None:
            return installation_admin_refusal

    own_account_write = (
        method not in _HTTP_SAFE_METHODS
        and not module_tree
        and (
            _route_needs(method, path, OWN_ACCOUNT_PERMISSION, contributed_rules)
            or bool(is_self_service_auth_write_route_fn(path))
        )
    )
    if principal_permission_kind(principal) != NOT_ROLE_BASED:
        # A person, or a person's own token: the route table and the permissions of
        # the person's role (ADR 0076 §4). A token's scopes were checked above, so it
        # acts within both its scopes and its owner's role.
        try:
            # An admin holds the whole catalogue implicitly and has no rows to read.
            stored = (
                {}
                if highest_system_role(principal.roles) == ADMIN_ROLE
                else await run_in_threadpool(
                    load_role_permissions_fn or _stored_role_permissions, bl
                )
            )
        except Exception:
            return JSONResponse({"error": "Role permissions are unavailable"}, status_code=503)
        granted = effective_permissions(principal, role_permissions=stored)
        request.state.effective_permissions = granted
        user["permissions"] = sorted(granted.permissions)
        try:
            places = await _apply_scope_limit(
                request,
                principal,
                user,
                bl=bl,
                granted=granted.permissions,
                stored=stored,
                load_scope_limit_fn=load_scope_limit_fn,
            )
        except Exception:
            # Fail closed: a limited person must never act as an unlimited one.
            return JSONResponse({"error": LIMIT_UNAVAILABLE_MESSAGE}, status_code=503)
        permission = _person_route_permission(
            method,
            path,
            interactive_source=principal_source,
            contributed_own_account_write=own_account_write,
            contributed_rules=contributed_rules,
        )
        if permission is _UNMAPPED:
            return JSONResponse({"error": err_route_not_mapped}, status_code=403)
        if highest_system_role(principal.roles) is None:
            # ADR 0085, P5: no role in the acting account grants nothing (the permission
            # catalogue would read it as a viewer). Such a principal reaches only routes
            # any signed-in principal may call; a person's session (signed in to answer
            # an invitation link) may also sign out and move to an account where it has
            # a role. Everything else is refused.
            if permission is None or (
                (method, path) in ROLELESS_SESSION_WRITES
                and not is_token_scoped_principal(principal)
            ):
                return await call_next(request)
            return JSONResponse(
                {"error": NO_ROLE_IN_ACCOUNT_MESSAGE, "code": NO_ROLE_IN_ACCOUNT_CODE},
                status_code=403,
            )
        owner = owner_role_permissions(principal, role_permissions=stored)
        if permission is not None and permission not in owner:
            operator_holds = permission in role_permission_keys(OPERATOR_ROLE, stored)
            error = err_operator_role_required if operator_holds else err_admin_role_required
            return JSONResponse({"error": error}, status_code=403)
        if places is not None:
            refusal = scope_limit_route_refusal(
                method,
                path,
                permission,
                places.project_permissions,
                places.workflow_permissions,
                places.project_workflow_ids,
                body_workflow_id=await _request_body_workflow_id(request, method, path),
            )
            if refusal is not None:
                return JSONResponse({"error": refusal}, status_code=403)
        return await call_next(request)

    if module_tree:
        machine_refusal = await _module_machine_refusal(
            principal,
            user,
            match=module_match,
            admin_required=admin_required,
            bl=bl,
            load_role_permissions_fn=load_role_permissions_fn,
            roles_intersect_fn=roles_intersect_fn,
            auth_admin_roles=auth_admin_roles,
            err_route_not_mapped=err_route_not_mapped,
            err_admin_role_required=err_admin_role_required,
            err_operator_role_required=err_operator_role_required,
        )
        if machine_refusal is not None:
            return machine_refusal
        return await call_next(request)

    # Service accounts, extension runtime and system actors: their rules are unchanged.
    if admin_required:
        if not roles_intersect_fn(user.get("roles") or [], auth_admin_roles):
            return JSONResponse({"error": err_admin_role_required}, status_code=403)
    elif registry_read_route and not viewer_extension_detail_read:
        if method in _HTTP_SAFE_METHODS:
            if not roles_intersect_fn(user.get("roles") or [], auth_write_roles):
                return JSONResponse({"error": err_operator_role_required}, status_code=403)
        else:
            operator_write = (
                (project_callback_write and machine)
                or project_action_write
                or _is_workflow_run_write(path, method)
            )
            required_roles = auth_write_roles if operator_write else auth_admin_roles
            if not roles_intersect_fn(user.get("roles") or [], required_roles):
                error = err_operator_role_required if operator_write else err_admin_role_required
                return JSONResponse({"error": error}, status_code=403)
    elif method not in _HTTP_SAFE_METHODS and not authoring_validate:
        if principal_source == "local" and own_account_write:
            return await call_next(request)
        operator_write = machine
        required_roles = auth_write_roles if operator_write else auth_admin_roles
        if not roles_intersect_fn(user.get("roles") or [], required_roles):
            error = err_operator_role_required if operator_write else err_admin_role_required
            return JSONResponse({"error": error}, status_code=403)

    return await call_next(request)


async def _request_body_workflow_id(request: Any, method: str, path: str) -> str | None:
    """The workflow a limited person's project-path write names in its JSON body.

    The body is read once (Starlette replays it to the route) and only for a JSON
    write under ``/api/projects/<id>/`` whose path names no workflow.
    """
    if str(method or "").upper() in {"GET", "HEAD", "OPTIONS"}:
        return None
    if project_id_from_path(path) is None or workflow_id_from_project_path(path):
        return None
    headers = getattr(request, "headers", None) or {}
    content_type = str(headers.get("content-type") or "").lower()
    if "json" not in content_type:
        return None
    try:
        declared = int(headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > BODY_WORKFLOW_MAX_BYTES:
        return None
    body_reader = getattr(request, "body", None)
    if not callable(body_reader):
        return None
    try:
        body = await body_reader()
    except Exception:
        return None
    return workflow_id_from_body(method, path, body)


async def _apply_scope_limit(
    request: Any,
    principal: ResolvedPrincipal,
    user: dict[str, Any],
    *,
    bl: Any,
    granted: frozenset[str],
    stored: Mapping[str, frozenset[str]],
    load_scope_limit_fn: Callable[[Any, str, str], Any] | None,
) -> ScopedPlaces | None:
    """Put a limited person's places and per-place permissions on the request.

    ADR 0076 section 8 (2026-11-26): a person, or a personal token of a person, with a
    project scope limit reaches only the limit's projects (a token: those it and its
    owner both reach) and holds in each the lower of the global and the scoped role.
    Workspace, project and workflow grants apply, the most specific one in each place
    (ADR 0076 section 8a). Returns the places, or ``None`` for a person without a limit,
    who keeps exactly the global role's access. An admin is never limited and has
    nothing read. A read error propagates: the caller answers 503.
    """
    if load_scope_limit_fn is None or highest_system_role(principal.roles) == ADMIN_ROLE:
        return None
    user_id = principal_user_id(user)
    if not user_id:
        return None
    limit = await run_in_threadpool(
        load_scope_limit_fn, bl, str(principal.account_id or ""), user_id
    )
    if limit is None:
        return None
    places = scoped_places(
        granted,
        limit,
        stored,
        token_project_ids=principal_allowed_project_ids(user),
        token_workspace_ids=principal_allowed_workspace_ids(user),
    )
    user.update(principal_places_payload(places))
    request.state.auth_principal = principal.model_copy(
        update={
            "allowed_project_ids": list(places.allowed_project_ids),
            "allowed_workspace_ids": list(places.allowed_workspace_ids),
        }
    )
    request.state.project_permissions = places.project_permissions
    request.state.scoped_places = places
    return places


def _resolve_local_token(
    bl: Any,
    *,
    token: str,
    bearer_token: str,
    get_user_by_session_token_fn: Any,
) -> dict[str, Any] | None:
    """Session, then PAT, then machine token -- or the OAuth resolver alone.

    ADR 0047 decision 10: an ``fs_oat_`` bearer is resolved only as an OAuth access
    token, never as a session, PAT or machine token; ``fs_oac_`` codes and
    ``fs_ort_`` refresh tokens are never accepted as bearers.
    """
    if bearer_token and is_oauth_non_bearer_secret(bearer_token):
        return None
    if bearer_token and is_oauth_access_token(bearer_token):
        from core.application.auth.inbound_oauth_service import resolve_oauth_access_principal

        return resolve_oauth_access_principal(bl, bearer_token)
    user = get_user_by_session_token_fn(bl, token) if token else None
    if not isinstance(user, dict) and bearer_token:
        from core.application.auth.resolve import (
            resolve_machine_principal,
            resolve_pat_principal,
        )

        user = resolve_pat_principal(bl, bearer_token)
        if not isinstance(user, dict):
            user = resolve_machine_principal(bl, bearer_token)
    return user


def _oauth_request_refusal(
    request: Any,
    *,
    principal: ResolvedPrincipal,
    path: str,
    method: str,
    token_source: str,
) -> JSONResponse | None:
    """The inbound OAuth rules of ADR 0047 decisions 4, 12 and 13, or ``None``."""
    source = str(principal.auth_source or "").strip().lower()
    if source == "oauth":
        forwarded = oauth_forward_accepted(
            path,
            request.headers.get(MCP_FORWARD_HEADER),
            current_oauth_environment().forward_secret,
        )
        if not forwarded:
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        if oauth_principal_denied_route(path, method):
            return JSONResponse(
                {"error": "This route is not available to an OAuth access token"},
                status_code=403,
            )
        return None
    if is_interactive_only_path(path):
        # A header-resolved external principal has no token; a session from external
        # sign-in (ADR 0083) is interactive only from its cookie, as a local one is.
        interactive = (source == "external" and token_source in {"", "cookie"}) or (
            source == "local" and token_source == "cookie"
        )
        if not interactive:
            return JSONResponse(
                {"error": "This route requires an interactive browser session"},
                status_code=403,
            )
    return None


#: The production reader of a person's project scope limit (one indexed query).
read_stored_scope_limit = stored_scope_limit


def build_local_auth_middleware_handler(**kwargs):
    async def _handler(request, call_next):
        return await local_auth_middleware(request, call_next, **kwargs)

    return _handler
