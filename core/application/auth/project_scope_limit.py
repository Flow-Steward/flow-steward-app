"""Apply a person's project scope limit to one request (ADR 0076 section 8, 2026-11-26).

A person limited to chosen projects (:mod:`core.application.auth.scoped_assignments`)
reaches only those projects, and in each of them holds the lower of the global role
and the scoped role: the global role stays the ceiling. A personal token of a limited
person never reaches past its owner: its own project list is intersected with the
owner's.

The auth middleware reads the limit once per request and puts on the principal:

* ``allowed_project_ids``: the projects the person may reach. A limit that names no
  project yields :data:`NO_PROJECT_ID`, an id no project has, because an empty list
  means "unrestricted" everywhere downstream.
* ``project_permissions``: ``{project id: permission keys}``, the person's (or the
  token's) permissions cut to the scoped role of each project.

Routes that act on one project are checked here or in their service:

* a route with the project id in its path (``/api/projects/<id>/...``) is checked by
  the middleware with :func:`scope_limit_route_refusal`;
* a write that names its project in the body or through a job, an approval or a
  report is listed in :data:`SERVICE_CHECKED_PROJECT_WRITES`; its service calls
  :func:`core.application.management.guards.require_project_permission`;
* every other write whose permission acts on a project's things
  (:data:`PROJECT_ACTING_PERMISSIONS`) is refused to a limited person: a route that
  cannot be checked per project fails closed.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.application.auth.effective_permissions import role_permission_keys
from core.application.auth.permissions import ADMIN_ROLE, highest_system_role
from core.application.auth.principal import (
    principal_allowed_project_ids,
    principal_allowed_workspace_ids,
    principal_user_id,
)
from core.application.auth.scoped_assignments import CONTAINER_ROLE, ScopeLimit, load_scope_limit

#: The allowed project id of a limited person who has no project: it matches nothing.
NO_PROJECT_ID = "00000000-0000-0000-0000-000000000000"

#: The 503 answer when the limit cannot be read: the request is not let through.
LIMIT_UNAVAILABLE_MESSAGE = "Project access limits are unavailable"
#: The project is not one of the person's projects.
PROJECT_OUT_OF_REACH_MESSAGE = "Access denied"
#: The person's role in this project does not hold the route's permission.
PROJECT_ROLE_REFUSAL_MESSAGE = "Your role in this project does not allow this action"
#: A write that acts on projects but cannot be checked for one project.
UNSCOPED_WRITE_REFUSAL_MESSAGE = (
    "This action is not available to a person whose access is limited to chosen projects"
)

#: Permissions whose writes act on one project's things. A limited person's write
#: with one of them must be checked for its project, or it is refused.
PROJECT_ACTING_PERMISSIONS: frozenset[str] = frozenset(
    {
        "workflows.run",
        "approvals.decide",
        "jobs.write",
        "reports.write",
        "notify.dispatch",
        "extensions.use",
        "projects.write",
        "workflows.manage",
        "jobs.manage",
        "secrets.bind",
        "secrets.approve",
        "platform.manage",
    }
)

_HTTP_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SEG = r"[^/]+"

#: Writes that name their project in the body, a job, an approval thread or a report;
#: each one's service checks the project's permission (``require_project_permission``).
SERVICE_CHECKED_PROJECT_WRITES: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (method, re.compile(rf"^{pattern}$"), permission)
    for method, pattern, permission in (
        ("POST", rf"/api/approvals/{_SEG}", "approvals.decide"),
        ("POST", rf"/api/jobs/{_SEG}/decision-overrides", "approvals.decide"),
        (
            "POST",
            rf"/api/jobs/{_SEG}/(?:acknowledge|unacknowledge|cancel|restart|retry-step)",
            "jobs.write",
        ),
        ("POST", r"/api/workflow-runs", "workflows.run"),
        ("POST", rf"/api/workflows/{_SEG}/run", "workflows.run"),
        ("POST", r"/api/reports/generate", "reports.write"),
        ("POST", rf"/api/extensions/{_SEG}/(?:action|query)/{_SEG}", "extensions.use"),
    )
)

_PROJECT_PATH = re.compile(r"^/api/(?:v2/)?projects/([^/]+)(?:/|$)")


def project_id_from_path(path: str) -> str | None:
    """The project id a ``/api/projects/<id>/...`` path names; ``None`` otherwise.

    Only a UUID counts: ``/api/v2/projects/sections`` names no project.
    """
    match = _PROJECT_PATH.match(str(path or ""))
    if not match:
        return None
    try:
        return str(uuid.UUID(match.group(1)))
    except ValueError:
        return None


def is_service_checked_project_write(method: str, path: str) -> bool:
    """Whether the route's service checks the permission for the project it acts on."""
    upper = str(method or "").upper()
    return any(
        upper == route_method and pattern.match(str(path or ""))
        for route_method, pattern, _permission in SERVICE_CHECKED_PROJECT_WRITES
    )


def limited_allowed_project_ids(limit: ScopeLimit, existing: Iterable[str] = ()) -> list[str]:
    """The projects a limited person (or their token) may reach, never an empty list.

    ``existing`` is the token's own project list: a token reaches the projects both it
    and its owner reach. An empty ``existing`` means the token named no projects.
    """
    allowed = set(limit.project_ids)
    token_ids = {str(item).strip() for item in existing or () if str(item or "").strip()}
    if token_ids:
        allowed &= token_ids
    return sorted(allowed) or [NO_PROJECT_ID]


def _role_set(
    roles: Iterable[str], ceiling: frozenset[str], role_permissions: Mapping[str, Iterable[str]]
) -> frozenset[str]:
    scoped = frozenset().union(
        *(role_permission_keys(role, role_permissions) for role in sorted(roles))
    )
    return ceiling & scoped


def scoped_project_permissions(
    granted: Iterable[str],
    limit: ScopeLimit,
    role_permissions: Mapping[str, Iterable[str]],
    *,
    allowed_project_ids: Iterable[str],
) -> dict[str, frozenset[str]]:
    """``{project id: permissions}``: ``granted`` cut to the project's covering grant.

    The project grant wins over the workspace grant. A project reached only through
    workflow grants holds the :data:`CONTAINER_ROLE` there: it is shown, and acting
    on its workflows is checked per workflow. ``granted`` is what the person (or the
    token) holds globally, so the result is never wider than the global role.
    """
    ceiling = frozenset(str(key) for key in granted or ())
    allowed = {str(item) for item in allowed_project_ids or ()}
    result: dict[str, frozenset[str]] = {}
    for project_id in sorted(limit.project_ids & allowed):
        roles = limit.project_level_roles(project_id)
        result[project_id] = _role_set(
            roles if roles is not None else (CONTAINER_ROLE,), ceiling, role_permissions
        )
    return result


@dataclass(frozen=True)
class ScopedPlaces:
    """What a limited person (or their token) reaches in one request, and may do there."""

    allowed_project_ids: list[str]
    allowed_workspace_ids: list[str]
    project_permissions: dict[str, frozenset[str]]
    workspace_permissions: dict[str, frozenset[str]]
    #: ``{project id: {workflow id: permissions}}`` of workflow grants only.
    workflow_permissions: dict[str, dict[str, frozenset[str]]]
    #: ``{project id: workflow ids}``: projects where only these workflows are reachable.
    project_workflow_ids: dict[str, frozenset[str]]


def scoped_places(
    granted: Iterable[str],
    limit: ScopeLimit,
    role_permissions: Mapping[str, Iterable[str]],
    *,
    token_project_ids: Iterable[str] = (),
    token_workspace_ids: Iterable[str] = (),
) -> ScopedPlaces:
    """Every place a limited person reaches, with the permissions in each.

    The effective role in a place is the most specific grant covering it (workflow,
    then project, then workspace), capped by ``granted``. A token reaches only the
    places both it and its owner reach.
    """
    ceiling = frozenset(str(key) for key in granted or ())
    allowed = limited_allowed_project_ids(limit, token_project_ids)
    reachable = set(allowed) - {NO_PROJECT_ID}
    reachable_workspaces = {
        limit.project_workspaces[project_id]
        for project_id in reachable
        if limit.project_workspaces.get(project_id)
    }
    token_projects = {
        str(item).strip() for item in token_project_ids or () if str(item or "").strip()
    }
    # A token limited to projects reaches only those projects' workspaces: a workspace
    # grant of its owner elsewhere is not the token's (ADR 0076 section 8b).
    workspaces = (
        reachable_workspaces
        if token_projects
        else set(limit.workspace_roles) | reachable_workspaces
    )
    token_workspaces = {
        str(item).strip() for item in token_workspace_ids or () if str(item or "").strip()
    }
    if token_workspaces:
        workspaces &= token_workspaces
        reachable = {
            project_id
            for project_id in reachable
            if not limit.project_workspaces.get(project_id)
            or limit.project_workspaces[project_id] in workspaces
        }
        allowed = sorted(reachable) or [NO_PROJECT_ID]
    workspace_permissions = {
        workspace_id: _role_set(
            limit.workspace_roles.get(workspace_id) or (CONTAINER_ROLE,),
            ceiling,
            role_permissions,
        )
        for workspace_id in sorted(workspaces)
    }
    workflow_permissions = {
        project_id: {
            workflow_id: _role_set(roles, ceiling, role_permissions)
            for workflow_id, roles in sorted(flows.items())
        }
        for project_id, flows in sorted(limit.workflow_roles.items())
        if project_id in reachable and flows
    }
    project_workflow_ids = {
        project_id: flows
        for project_id, flows in sorted(limit.workflow_only_projects().items())
        if project_id in reachable
    }
    return ScopedPlaces(
        allowed_project_ids=allowed,
        allowed_workspace_ids=sorted(workspaces) or [NO_PROJECT_ID],
        project_permissions=scoped_project_permissions(
            granted, limit, role_permissions, allowed_project_ids=reachable
        ),
        workspace_permissions=workspace_permissions,
        workflow_permissions=workflow_permissions,
        project_workflow_ids=project_workflow_ids,
    )


_PROJECT_WORKFLOW_PATH = re.compile(r"^/api/(?:v2/)?projects/[^/]+/workflows/([^/]+)(?:/|$)")


def workflow_id_from_project_path(path: str) -> str | None:
    """The workflow a ``/api/projects/<id>/workflows/<workflow>/...`` path names."""
    match = _PROJECT_WORKFLOW_PATH.match(str(path or ""))
    return match.group(1) if match else None


#: The body keys a project-path write names its workflow with.
BODY_WORKFLOW_KEYS: tuple[str, ...] = ("workflow_id", "selected_workflow_id")
#: Bodies larger than this are not read by the middleware; the service checks them.
BODY_WORKFLOW_MAX_BYTES = 1_000_000


def workflow_id_from_body(method: str, path: str, body: bytes | None) -> str | None:
    """The workflow a project-path write's JSON body names at its top level, if any.

    Only a write under ``/api/projects/<id>/`` whose path names no workflow is read;
    anything that is not a JSON object yields ``None``.
    """
    if str(method or "").upper() in _HTTP_SAFE_METHODS or not body:
        return None
    if project_id_from_path(path) is None or workflow_id_from_project_path(path):
        return None
    if len(body) > BODY_WORKFLOW_MAX_BYTES:
        return None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in BODY_WORKFLOW_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def place_permissions(
    project_id: str,
    workflow_id: str | None,
    project_permissions: Mapping[str, Iterable[str]],
    workflow_permissions: Mapping[str, Mapping[str, Iterable[str]]] | None,
    project_workflow_ids: Mapping[str, Iterable[str]] | None,
) -> frozenset[str] | None:
    """The permissions in a project, or in one workflow of it; ``None`` when out of reach.

    Workflow grant, then the project's covering grant. In a project reached only
    through workflow grants, any other workflow is out of reach.
    """
    held = project_permissions.get(project_id)
    if held is None:
        return None
    if not workflow_id:
        return frozenset(held)
    granted = (workflow_permissions or {}).get(project_id, {}).get(workflow_id)
    if granted is not None:
        return frozenset(granted)
    restricted = (project_workflow_ids or {}).get(project_id)
    if restricted is not None and workflow_id not in set(restricted):
        return None
    return frozenset(held)


def scope_limit_route_refusal(
    method: str,
    path: str,
    permission: str | None,
    project_permissions: Mapping[str, Iterable[str]],
    workflow_permissions: Mapping[str, Mapping[str, Iterable[str]]] | None = None,
    project_workflow_ids: Mapping[str, Iterable[str]] | None = None,
    *,
    body_workflow_id: str | None = None,
) -> str | None:
    """Why a limited person may not make this request; ``None`` when they may.

    A project in the path is checked here, in the workflow the path names or else the
    one the write's body names (``body_workflow_id``, ADR 0076 section 8b); a write
    listed in :data:`SERVICE_CHECKED_PROJECT_WRITES` is checked by its service; any
    other write whose permission acts on projects is refused.
    """
    project_id = project_id_from_path(path)
    if project_id is not None:
        held = place_permissions(
            project_id,
            workflow_id_from_project_path(path) or (str(body_workflow_id or "").strip() or None),
            project_permissions,
            workflow_permissions,
            project_workflow_ids,
        )
        if held is None:
            return PROJECT_OUT_OF_REACH_MESSAGE
        if permission is not None and permission not in set(held):
            return PROJECT_ROLE_REFUSAL_MESSAGE
        return None
    if str(method or "").upper() in _HTTP_SAFE_METHODS:
        return None
    if permission in PROJECT_ACTING_PERMISSIONS and not is_service_checked_project_write(
        method, path
    ):
        return UNSCOPED_WRITE_REFUSAL_MESSAGE
    return None


def principal_places_payload(places: ScopedPlaces) -> dict[str, Any]:
    """The keys a limited principal carries in ``request.state.auth_user`` (JSON-safe)."""
    return {
        "allowed_project_ids": list(places.allowed_project_ids),
        "allowed_workspace_ids": list(places.allowed_workspace_ids),
        "project_permissions": {
            pid: sorted(keys) for pid, keys in sorted(places.project_permissions.items())
        },
        "workspace_permissions": {
            wid: sorted(keys) for wid, keys in sorted(places.workspace_permissions.items())
        },
        "workflow_permissions": {
            pid: {wid: sorted(keys) for wid, keys in sorted(flows.items())}
            for pid, flows in sorted(places.workflow_permissions.items())
        },
        "project_workflow_ids": {
            pid: sorted(flows) for pid, flows in sorted(places.project_workflow_ids.items())
        },
    }


def limit_principal_projects(
    user: dict[str, Any], bl: Any, load_scope_limit_fn: Any
) -> ScopeLimit | None:
    """Narrow a limited person's reachable places in ``user``, in place.

    For a caller that authenticates outside the HTTP middleware (the jobs WebSocket),
    which only reads: projects, workspaces and the workflows of projects reached only
    through workflow grants are limited; permissions are not computed. An admin, a
    service account and a machine token have nothing read. A read error propagates:
    the caller refuses.
    """
    roles = [str(role) for role in user.get("roles") or []]
    if highest_system_role(roles) == ADMIN_ROLE:
        return None
    user_id = principal_user_id(user)
    if not user_id:
        return None
    limit = load_scope_limit_fn(bl, str(user.get("account_id") or ""), user_id)
    if limit is None:
        return None
    places = scoped_places(
        (),
        limit,
        {},
        token_project_ids=principal_allowed_project_ids(user),
        token_workspace_ids=principal_allowed_workspace_ids(user),
    )
    user["allowed_project_ids"] = list(places.allowed_project_ids)
    user["allowed_workspace_ids"] = list(places.allowed_workspace_ids)
    user["project_workflow_ids"] = {
        pid: sorted(flows) for pid, flows in sorted(places.project_workflow_ids.items())
    }
    return limit


def stored_scope_limit(bl: Any, account_id: str, user_id: str) -> ScopeLimit | None:
    """The person's stored limit, read on the request's connection; raises when unreadable."""
    conn = getattr(bl, "conn", None)
    if conn is None:
        raise RuntimeError("No database connection to read project access limits")
    return load_scope_limit(conn, account_id=account_id, user_id=user_id)


__all__ = [
    "BODY_WORKFLOW_KEYS",
    "BODY_WORKFLOW_MAX_BYTES",
    "LIMIT_UNAVAILABLE_MESSAGE",
    "NO_PROJECT_ID",
    "PROJECT_ACTING_PERMISSIONS",
    "PROJECT_OUT_OF_REACH_MESSAGE",
    "PROJECT_ROLE_REFUSAL_MESSAGE",
    "SERVICE_CHECKED_PROJECT_WRITES",
    "UNSCOPED_WRITE_REFUSAL_MESSAGE",
    "ScopedPlaces",
    "is_service_checked_project_write",
    "limit_principal_projects",
    "limited_allowed_project_ids",
    "place_permissions",
    "principal_places_payload",
    "project_id_from_path",
    "scope_limit_route_refusal",
    "scoped_places",
    "scoped_project_permissions",
    "stored_scope_limit",
    "workflow_id_from_body",
    "workflow_id_from_project_path",
]
