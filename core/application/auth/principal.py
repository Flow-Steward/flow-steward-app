"""Principal inspection — pure functions, no DB or Request dependency."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.scope_contracts import scope_granted


class ResolvedPrincipal(BaseModel):
    """Normalized principal that every auth source resolves to.

    Session users, PATs, machine tokens, and optional external resolvers all
    produce this same shape so downstream code never inspects raw dicts.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    username: str = ""
    email: str = ""
    display_name: str = ""
    is_active: bool = True
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    auth_source: str = ""  # "local", "machine", "pat", "external"
    principal_type: str = "user"  # "user", "service_account"
    account_id: str = DEFAULT_ACCOUNT_ID
    account_ids: list[str] = Field(default_factory=list)
    account_memberships: list[dict[str, Any]] = Field(default_factory=list)
    workspace_id: str | None = None
    allowed_workspace_ids: list[str] = Field(default_factory=list)
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_workflow_ids: list[str] = Field(default_factory=list)
    service_account_id: str = ""
    service_account_key: str = ""
    expires_at: datetime | None = None
    must_change_password: bool = False
    timezone: str = ""
    locale: str = ""

    def to_scope_dict(self) -> dict[str, Any]:
        """Return a dict suitable for ``scope_from_principal``."""
        return self.model_dump()


_ROLE_LEVELS = {
    "viewer": 1,
    "operator": 2,
    "admin": 3,
}

_PRINCIPAL_TYPE_ROLE_MAP = {
    "viewer": "viewer",
    "user": "viewer",
    "operator": "operator",
    "service_account": "operator",
    "admin": "admin",
    "system_actor": "admin",
}

_VIEWER_SCOPE_CEILING = frozenset(
    {
        "read:*",
        "jobs.read",
        "projects.read",
        "workflows.read",
        "registry.read",
        "reports.read",
        "notify.read",
    }
)
_OPERATOR_SCOPE_CEILING = frozenset(
    {
        *_VIEWER_SCOPE_CEILING,
        "command:*",
        "jobs.write",
        "projects.write",
        "workflows.write",
        "reports.write",
    }
)


def principal_type_role(principal_type: str | None) -> str:
    normalized = str(principal_type or "").strip().lower()
    return _PRINCIPAL_TYPE_ROLE_MAP.get(normalized, "")


def principal_type_has_minimum_role(principal_type: str | None, minimum_role: str) -> bool:
    required = str(minimum_role or "").strip().lower()
    if required not in _ROLE_LEVELS:
        return False
    actual = principal_type_role(principal_type)
    if not actual:
        return False
    return _ROLE_LEVELS[actual] >= _ROLE_LEVELS[required]


def roles_intersect(actual: list[str], required: set[str]) -> bool:
    if not required:
        return True
    return bool({str(role or "").strip().lower() for role in (actual or [])}.intersection(required))


def principal_account_id(user: dict | None) -> str:
    if not isinstance(user, dict):
        return DEFAULT_ACCOUNT_ID
    return str(user.get("account_id") or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID


def coerce_optional_uuid(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized


def coerce_optional_workspace_id(value: Any) -> str | None:
    return coerce_optional_uuid(value)


def coerce_optional_project_id(value: Any) -> str | None:
    return coerce_optional_uuid(value)


def coerce_optional_workflow_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def principal_allowed_workspace_ids(user: dict | None) -> set[str]:
    if not isinstance(user, dict):
        return set()
    return {
        workspace_id
        for workspace_id in (
            coerce_optional_workspace_id(item) for item in (user.get("allowed_workspace_ids") or [])
        )
        if workspace_id
    }


def principal_allowed_project_ids(user: dict | None) -> set[str]:
    if not isinstance(user, dict):
        return set()
    allowed: set[str] = set()
    for item in user.get("allowed_project_ids") or []:
        normalized = coerce_optional_project_id(item)
        if normalized is not None:
            allowed.add(normalized)
    return allowed


def principal_allowed_workflow_ids(user: dict | None) -> set[str]:
    if not isinstance(user, dict):
        return set()
    return {
        workflow_id
        for workflow_id in (
            coerce_optional_workflow_id(item) for item in (user.get("allowed_workflow_ids") or [])
        )
        if workflow_id
    }


def principal_scopes(user: dict | None) -> set[str]:
    if not isinstance(user, dict):
        return set()
    return {
        str(scope or "").strip().lower()
        for scope in (user.get("scopes") or [])
        if str(scope or "").strip()
    }


def principal_has_required_scopes(
    user: dict | None,
    required_scopes: list[str] | set[str] | tuple[str, ...],
    *,
    require_all: bool = False,
) -> bool:
    needed = [
        str(item or "").strip().lower()
        for item in (required_scopes or [])
        if str(item or "").strip()
    ]
    if not needed:
        return True
    scopes = principal_scopes(user)
    if not scopes:
        return False
    if require_all:
        return all(scope_granted(scopes, scope) for scope in needed)
    return any(scope_granted(scopes, scope) for scope in needed)


class TokenAuthorityError(ValueError):
    """Raised when a requested token grant exceeds the caller's own authority.

    Carries an HTTP status (403) so route handlers can translate it into a
    Forbidden response without leaking which specific value was rejected beyond
    the safe category message.
    """

    status_code: int = 403


def _normalized_requested_scopes(scopes: list[str] | None) -> list[str]:
    return [
        str(scope or "").strip().lower() for scope in (scopes or []) if str(scope or "").strip()
    ]


def _caller_is_admin(caller: dict | None) -> bool:
    if not isinstance(caller, dict):
        return False
    roles = {str(role or "").strip().lower() for role in (caller.get("roles") or [])}
    return bool(roles.intersection({"admin", "system_actor"})) or "admin:all" in principal_scopes(
        caller
    )


def _caller_is_scope_restricted(caller: dict | None) -> bool:
    """Whether the caller's persisted scopes are the trust basis downstream.

    PAT, machine-token, and service-account principals are authorized by their
    stored scope set (see ``auth_http_runtime._principal_requires_http_scopes``).
    Interactive session / external users are authorized by *role* and carry no
    meaningful scope list, so scope-subset enforcement does not apply to them
    beyond the admin carve-out handled separately.
    """
    if not isinstance(caller, dict):
        return False
    auth_source = str(caller.get("auth_source") or "").strip().lower()
    principal_type = str(caller.get("principal_type") or "").strip().lower()
    return auth_source in {"pat", "machine"} or principal_type == "service_account"


def _caller_role_scope_ceiling(caller: dict | None) -> set[str]:
    if not isinstance(caller, dict):
        return set()
    roles = {str(role or "").strip().lower() for role in (caller.get("roles") or [])}
    principal_role = principal_type_role(str(caller.get("principal_type") or ""))
    if principal_role:
        roles.add(principal_role)
    if "operator" in roles:
        return set(_OPERATOR_SCOPE_CEILING)
    return set(_VIEWER_SCOPE_CEILING)


def enforce_token_scope_within_caller_authority(
    caller: dict | None,
    *,
    requested_scopes: list[str] | None,
    requested_project_ids: list[str] | None,
    requested_workflow_ids: list[str] | None,
) -> None:
    """Reject token issuance that would exceed the caller principal's authority.

    A token (PAT, service account, or machine token) must never be broader than
    the principal minting it. This enforces subset/intersection semantics:

    * **Scopes** — an ``admin`` (or ``admin:all``-bearing) caller may grant any
      scope. A *scope-restricted* caller (PAT / machine token / service account,
      whose persisted scopes are the downstream trust basis) may only grant
      scopes it effectively holds: every requested scope must pass
      :func:`scope_granted` against the caller's own scope set (so wildcard
      holders grant within their reach, a narrow caller cannot widen).
      Interactive session/external users are bounded by a role-derived scope
      ceiling: viewers can mint read-only PATs, operators can mint read-only
      and generic ``command:*`` PATs, and admins can grant freely. Special
      machine/admin scopes such as ``admin:all``, ``mcp:use``, ``agent:*``, and
      ``notify:dispatch`` require an admin or a scope-restricted caller that
      already holds them.
    * **allowed_project_ids / allowed_workflow_ids** — an *empty* caller
      allowlist means the caller is unrestricted on that axis and may set any
      value (this is the privileged/admin carve-out, since admins carry empty
      allowlists). A *restricted* caller may only narrow, never widen: every
      requested id must be in the caller's allowlist, and the caller may not
      mint an unrestricted token (empty requested allowlist) on an axis where it
      is itself restricted.

    Raises :class:`TokenAuthorityError` (HTTP 403) on any violation. A caller of
    ``None`` is treated as having no authority and may grant nothing.
    """
    is_admin = _caller_is_admin(caller)

    if not is_admin and _caller_is_scope_restricted(caller):
        caller_scopes = principal_scopes(caller)
        for scope in _normalized_requested_scopes(requested_scopes):
            if not scope_granted(caller_scopes, scope):
                raise TokenAuthorityError("Requested scopes exceed the caller's authority")
    elif not is_admin:
        caller_scopes = _caller_role_scope_ceiling(caller)
        for scope in _normalized_requested_scopes(requested_scopes):
            if not scope_granted(caller_scopes, scope):
                raise TokenAuthorityError("Requested scopes exceed the caller's role authority")

    caller_projects = principal_allowed_project_ids(caller)
    if caller_projects:
        requested_projects = {
            normalized
            for normalized in (
                coerce_optional_project_id(item) for item in (requested_project_ids or [])
            )
            if normalized is not None
        }
        if not requested_projects or not requested_projects.issubset(caller_projects):
            raise TokenAuthorityError("Requested project allowlist exceeds the caller's authority")

    caller_workflows = principal_allowed_workflow_ids(caller)
    if caller_workflows:
        requested_workflows = {
            normalized
            for normalized in (
                coerce_optional_workflow_id(item) for item in (requested_workflow_ids or [])
            )
            if normalized is not None
        }
        if not requested_workflows or not requested_workflows.issubset(caller_workflows):
            raise TokenAuthorityError("Requested workflow allowlist exceeds the caller's authority")


def is_machine_principal(user: dict | None) -> bool:
    if not isinstance(user, dict):
        return False
    source = str(user.get("auth_source") or "").strip().lower()
    principal_type = str(user.get("principal_type") or "").strip().lower()
    return source == "machine" or principal_type == "service_account"
