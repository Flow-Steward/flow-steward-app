"""Who may change, bind and approve catalog secrets (ADR 0057, ADR 0076).

People act through the permissions of their role: the four secret permissions of the
catalogue (:mod:`core.application.auth.permissions`). Machine callers — tokens,
service accounts, MCP clients and LLM-driven authoring on their behalf — act only
through the two exact-only scopes those permissions list, and never approve.
"""

from __future__ import annotations

from typing import Any

from core.application.auth.permissions import (
    ADMIN_ROLE,
    OPERATOR_ROLE,
    PERMISSIONS,
    SYSTEM_ROLE_PERMISSIONS,
    VIEWER_ROLE,
    role_level,
    token_grants_permission,
)
from core.application.auth.principal import is_token_scoped_principal
from core.application.management.errors import ServiceForbiddenError

SECRET_WRITE_SCOPE = "secrets:write"  # pragma: allowlist secret - a scope name
SECRET_BIND_SCOPE = "secrets:bind"  # pragma: allowlist secret - a scope name

#: The catalogue permissions this module checks.
SECRET_VALUE_WRITE_PERMISSION = "secrets.value.write"  # pragma: allowlist secret - a key
SECRET_DELETE_PERMISSION = "secrets.delete"  # pragma: allowlist secret - a key
SECRET_BIND_PERMISSION = "secrets.bind"  # pragma: allowlist secret - a key
SECRET_APPROVE_PERMISSION = "secrets.approve"  # pragma: allowlist secret - a key

#: Trusted interactive authentication mechanisms. `auth_source` is set by the
#: authentication layer, never by request content.
_INTERACTIVE_AUTH_SOURCES = frozenset({"local", "external"})


def _role(scope: Any) -> str:
    return str(getattr(scope, "principal_type", "") or "").strip().lower()


def is_interactive_human(scope: Any) -> bool:
    """A person in a trusted interactive session, not a token, service account or system actor."""
    if scope is None or is_token_scoped_principal(scope):
        return False
    source = str(getattr(scope, "auth_source", "") or "").strip().lower()
    return source in _INTERACTIVE_AUTH_SOURCES and role_level(_role(scope)) > 0


def _person_permissions(scope: Any) -> frozenset[str]:
    """What the person may do: the request's effective permissions, read once by the
    middleware from ``role_permissions``; for a scope built outside a request, the
    system role it names."""
    granted = getattr(scope, "permissions", None)
    if granted is not None:
        return frozenset(granted)
    role = _role(scope)
    if role == ADMIN_ROLE:
        return frozenset(PERMISSIONS)
    return SYSTEM_ROLE_PERMISSIONS.get(role, frozenset())


def _lowest_role_holding(permission: str) -> str:
    for role in (VIEWER_ROLE, OPERATOR_ROLE):
        if permission in SYSTEM_ROLE_PERMISSIONS.get(role, frozenset()):
            return role
    return ADMIN_ROLE


def _refuse_person(permission: str, action: str) -> ServiceForbiddenError:
    role = _lowest_role_holding(permission)
    return ServiceForbiddenError(f"{role.capitalize()} role required to {action}")


def _require(scope: Any, *, permission: str, action: str) -> None:
    entry = PERMISSIONS[permission]
    if is_interactive_human(scope):
        if permission not in _person_permissions(scope):
            raise _refuse_person(permission, action)
        return
    if is_token_scoped_principal(scope) and token_grants_permission(
        entry, getattr(scope, "token_scopes", ()) or ()
    ):
        return
    raise ServiceForbiddenError(f"'{entry.token_scopes[0]}' is required to {action}")


def require_secret_write(scope: Any) -> None:
    """Create a secret, replace its value or edit its metadata (``secrets.value.write``)."""
    _require(scope, permission=SECRET_VALUE_WRITE_PERMISSION, action="change a stored secret")


def require_secret_delete(scope: Any) -> None:
    """Delete a secret (``secrets.delete``): the admin's, who sees every consumer it
    unbinds (ADR 0066 amendment). A machine caller keeps the exact `secrets:write`."""
    _require(scope, permission=SECRET_DELETE_PERMISSION, action="delete a stored secret")


def require_secret_bind(scope: Any) -> None:
    """Bind a secret to a consumer, or remove a binding (``secrets.bind``): configuration,
    the admin's (ADR 0066 amendment); a machine keeps `secrets:bind`."""
    _require(scope, permission=SECRET_BIND_PERMISSION, action="choose a stored secret")


def require_secret_read_metadata(scope: Any) -> None:
    if is_interactive_human(scope) or is_token_scoped_principal(scope):
        return
    if role_level(_role(scope)) < role_level(VIEWER_ROLE):
        raise ServiceForbiddenError("Authentication required")


def require_approval_authority(scope: Any) -> None:
    """Approve a configuration's use of catalog secrets: a person with the admin role.

    This is configuration approval, not a workflow run's approval step, which an
    operator decides (ADR 0066 amendment).
    """
    if not is_interactive_human(scope):
        raise ServiceForbiddenError(
            "Approving the use of stored secrets requires a person signed in interactively"
        )
    if SECRET_APPROVE_PERMISSION not in _person_permissions(scope):
        raise _refuse_person(SECRET_APPROVE_PERMISSION, "approve the use of stored secrets")


def actor_identity(scope: Any) -> tuple[str, str]:
    """The (kind, id) pair recorded in audit rows."""
    kind = "human" if is_interactive_human(scope) else "machine"
    actor = str(getattr(scope, "actor_id", "") or "").strip() or (
        "operator" if kind == "human" else "service_account"
    )
    return kind, actor


__all__ = [
    "SECRET_APPROVE_PERMISSION",
    "SECRET_BIND_PERMISSION",
    "SECRET_BIND_SCOPE",
    "SECRET_DELETE_PERMISSION",
    "SECRET_VALUE_WRITE_PERMISSION",
    "SECRET_WRITE_SCOPE",
    "actor_identity",
    "is_interactive_human",
    "require_approval_authority",
    "require_secret_bind",
    "require_secret_delete",
    "require_secret_read_metadata",
    "require_secret_write",
]
