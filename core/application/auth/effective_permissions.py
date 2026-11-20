"""What a principal may do: the ADR 0076 permissions and the client read/mutate projection."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.application.auth.permissions import (
    ADMIN_ROLE,
    ALL_PERMISSION_KEYS,
    PERMISSIONS,
    role_level,
    token_grants_permission,
)
from core.application.auth.principal import is_token_scoped_principal
from core.application.auth.role_assignments import RoleAssignment, global_assignments


def resolve_effective_permissions(
    *,
    principal_type: str = "",
    effective_permissions: dict[str, Any] | None = None,
) -> dict[str, bool]:
    """Resolve active permissions without allowing viewers to gain mutation access."""
    payload = dict(effective_permissions or {})
    normalized_principal = str(principal_type or "").strip().lower()
    raw_roles = payload.get("roles")
    if isinstance(raw_roles, str):
        role_hints = {raw_roles.strip().lower()} if raw_roles.strip() else set()
    elif isinstance(raw_roles, (list, tuple, set)):
        role_hints = {
            str(role or "").strip().lower() for role in raw_roles if str(role or "").strip()
        }
    else:
        role_hints = set()
    is_read_only = normalized_principal in {"viewer", ""} or "viewer" in role_hints
    can_read = bool(payload.get("can_read")) if "can_read" in payload else True
    requested_can_mutate = (
        bool(payload.get("can_mutate")) if "can_mutate" in payload else not is_read_only
    )
    return {"can_read": can_read, "can_mutate": bool(requested_can_mutate and not is_read_only)}


# ---------------------------------------------------------------------------
# ADR 0076: what a principal may do, from the permission catalogue and the roles
# ---------------------------------------------------------------------------

#: A person signed in interactively (browser session or external sign-in).
PERSON = "person"
#: A person's own token (PAT, agent access, OAuth client): the owner's role ∩ its scopes.
PERSONAL_TOKEN = "personal_token"
#: Service accounts, extension runtime and system actors keep their own rules.
NOT_ROLE_BASED = "not_role_based"

_LOG = logging.getLogger(__name__)
#: Auth sources of system work done on nobody's behalf: a project connection running an
#: extension and the OAuth callback finishing a connector's sign-in.
_SYSTEM_AUTH_SOURCES = frozenset({"project_connection", "oauth_callback"})


@dataclass(frozen=True, slots=True)
class PermissionResource:
    """The workspace, project and workflow a request acts on, as far as it names them."""

    workspace_id: str | None = None
    project_id: str | None = None
    workflow_id: str | None = None


@dataclass(frozen=True, slots=True)
class EffectivePermissions:
    """The permissions a principal holds for one request.

    ``kind`` is :data:`NOT_ROLE_BASED` for service accounts, extension runtime and
    system actors: their permission set is empty and callers keep their current
    rules (machine grants and token scopes) for them.
    """

    kind: str
    permissions: frozenset[str]

    @property
    def role_based(self) -> bool:
        return self.kind != NOT_ROLE_BASED

    def allows(self, permission: str) -> bool:
        return permission in self.permissions


def load_role_permissions(conn: Any) -> dict[str, frozenset[str]]:
    """Every role's stored permission keys, in one query (call it once per request).

    Keys are as stored; :func:`effective_permissions` drops the ones the catalogue
    does not know. ``admin`` has no rows: it holds the whole catalogue implicitly.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.key, rp.permission_key
            FROM role_permissions rp
            JOIN roles r ON r.id = rp.role_id
            ORDER BY r.key, rp.permission_key
            """
        )
        rows = cur.fetchall() or []
    grouped: dict[str, set[str]] = {}
    for row in rows:
        role, permission = (
            (row.get("key"), row.get("permission_key")) if isinstance(row, Mapping) else row
        )
        role_text = str(role or "").strip().lower()
        if role_text:
            grouped.setdefault(role_text, set()).add(str(permission or "").strip())
    return {role: frozenset(keys) for role, keys in grouped.items()}


def role_permission_keys(
    role: str, role_permissions: Mapping[str, Iterable[str]]
) -> frozenset[str]:
    """The catalogue permissions ``role`` holds; unknown stored keys are logged and dropped."""
    role_text = str(role or "").strip().lower()
    if role_text == ADMIN_ROLE:
        return ALL_PERMISSION_KEYS
    stored = {str(key or "").strip() for key in role_permissions.get(role_text, ()) or ()}
    stored.discard("")
    unknown = sorted(stored - ALL_PERMISSION_KEYS)
    if unknown:
        _LOG.warning(
            "Ignoring permission keys stored for role %r that the catalogue does not define: %s",
            role_text,
            ", ".join(unknown),
        )
    return frozenset(stored & ALL_PERMISSION_KEYS)


def _field(principal: Any, name: str) -> Any:
    if isinstance(principal, Mapping):
        return principal.get(name)
    return getattr(principal, name, None)


def principal_permission_kind(principal: Any) -> str:
    """:data:`PERSON`, :data:`PERSONAL_TOKEN` or :data:`NOT_ROLE_BASED`."""
    source = str(_field(principal, "auth_source") or "").strip().lower()
    principal_type = str(_field(principal, "principal_type") or "").strip().lower()
    if principal is None or source == "machine" or principal_type == "service_account":
        return NOT_ROLE_BASED
    if principal_type == "system_actor" or source in _SYSTEM_AUTH_SOURCES:
        return NOT_ROLE_BASED
    if is_token_scoped_principal(principal):
        return PERSONAL_TOKEN
    return PERSON


def _principal_roles(principal: Any) -> list[str]:
    roles = _field(principal, "roles")
    if isinstance(roles, str):
        roles = [roles]
    if roles:
        return [str(role or "").strip().lower() for role in roles if str(role or "").strip()]
    # A ScopeContext names the person's highest role as its principal type.
    principal_type = str(_field(principal, "principal_type") or "").strip().lower()
    return [principal_type] if role_level(principal_type) else []


def _token_scopes(principal: Any) -> set[str]:
    raw = _field(principal, "scopes")
    if raw is None:
        raw = _field(principal, "token_scopes")
    return {str(scope or "").strip().lower() for scope in raw or () if str(scope or "").strip()}


def _assignment_applies(assignment: RoleAssignment, resource: PermissionResource | None) -> bool:
    if assignment.is_global:
        return True
    if resource is None:
        return False
    return all(
        required is None or required == actual
        for required, actual in (
            (assignment.workspace_id, resource.workspace_id),
            (assignment.project_id, resource.project_id),
            (assignment.workflow_id, resource.workflow_id),
        )
    )


def effective_permissions(
    principal: Any,
    resource: PermissionResource | None = None,
    *,
    role_permissions: Mapping[str, Iterable[str]],
    assignments: Iterable[RoleAssignment] | None = None,
) -> EffectivePermissions:
    """What ``principal`` may do, for ``resource`` when the request names one (ADR 0076 §3).

    * A person: the permissions of their role. Of the system roles that apply (every
      global assignment and every scoped one that matches ``resource``) the highest
      decides; other roles add theirs. ``admin`` holds the whole catalogue.
    * A person's own token: the owner's permissions, each kept only when the token's
      scopes grant it (:func:`core.application.auth.permissions.token_grants_permission`).
      A token is never wider than its owner.
    * Service accounts, extension runtime and system actors: :data:`NOT_ROLE_BASED`,
      no permissions; their callers keep the current rules.

    ``role_permissions`` is :func:`load_role_permissions`, read once per request.
    ``assignments`` defaults to the principal's roles as global assignments, which is
    all this edition reads.
    """
    kind = principal_permission_kind(principal)
    if kind == NOT_ROLE_BASED:
        return EffectivePermissions(kind, frozenset())
    if assignments is None:
        assignments = global_assignments(_principal_roles(principal))
    applicable = [item for item in assignments if _assignment_applies(item, resource)]
    system_roles = [item.role for item in applicable if role_level(item.role)]
    roles = {item.role for item in applicable if not role_level(item.role)}
    if system_roles:
        roles.add(max(system_roles, key=role_level))
    granted: frozenset[str] = frozenset().union(
        *(role_permission_keys(role, role_permissions) for role in sorted(roles))
    )
    if kind == PERSONAL_TOKEN:
        scopes = _token_scopes(principal)
        granted = frozenset(
            key for key in granted if token_grants_permission(PERMISSIONS[key], scopes)
        )
    return EffectivePermissions(kind, granted)
