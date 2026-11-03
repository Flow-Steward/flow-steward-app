"""Who may change, bind and approve catalog secrets (ADR 0057).

People act through their global role. Machine callers — tokens, service accounts,
MCP clients and LLM-driven authoring on their behalf — act only through the two
exact-only scopes, and never approve.
"""

from __future__ import annotations

from typing import Any

from core.application.auth.principal import is_token_scoped_principal
from core.application.auth.scope_contracts import scope_granted
from core.application.management.errors import ServiceForbiddenError

SECRET_WRITE_SCOPE = "secrets:write"  # pragma: allowlist secret - a scope name
SECRET_BIND_SCOPE = "secrets:bind"  # pragma: allowlist secret - a scope name

#: Trusted interactive authentication mechanisms. `auth_source` is set by the
#: authentication layer, never by request content.
_INTERACTIVE_AUTH_SOURCES = frozenset({"local", "external"})
_ROLE_LEVELS = {"viewer": 1, "operator": 2, "admin": 3}


def _role_level(scope: Any) -> int:
    return _ROLE_LEVELS.get(str(getattr(scope, "principal_type", "") or "").strip().lower(), 0)


def is_interactive_human(scope: Any) -> bool:
    """A person in a trusted interactive session, not a token, service account or system actor."""
    if scope is None or is_token_scoped_principal(scope):
        return False
    source = str(getattr(scope, "auth_source", "") or "").strip().lower()
    return source in _INTERACTIVE_AUTH_SOURCES and _role_level(scope) > 0


def _require_human_operator(scope: Any, action: str) -> None:
    if _role_level(scope) < _ROLE_LEVELS["operator"]:
        raise ServiceForbiddenError(f"Operator role required to {action}")


def _require(scope: Any, *, required_scope: str, action: str) -> None:
    if is_interactive_human(scope):
        _require_human_operator(scope, action)
        return
    if is_token_scoped_principal(scope) and scope_granted(
        set(getattr(scope, "token_scopes", ()) or ()), required_scope
    ):
        return
    raise ServiceForbiddenError(f"'{required_scope}' is required to {action}")


def require_secret_write(scope: Any) -> None:
    """Create a secret, replace its value, edit its metadata or delete it."""
    _require(scope, required_scope=SECRET_WRITE_SCOPE, action="change a stored secret")


def require_secret_bind(scope: Any) -> None:
    """Bind a secret to a consumer, or remove a binding."""
    _require(scope, required_scope=SECRET_BIND_SCOPE, action="choose a stored secret")


def require_secret_read_metadata(scope: Any) -> None:
    if is_interactive_human(scope) or is_token_scoped_principal(scope):
        return
    if _role_level(scope) < _ROLE_LEVELS["viewer"]:
        raise ServiceForbiddenError("Authentication required")


def require_approval_authority(scope: Any) -> None:
    """Approve a configuration's use of catalog secrets: a person, operator or above."""
    if not is_interactive_human(scope):
        raise ServiceForbiddenError(
            "Approving the use of stored secrets requires a person signed in interactively"
        )
    _require_human_operator(scope, "approve the use of stored secrets")


def actor_identity(scope: Any) -> tuple[str, str]:
    """The (kind, id) pair recorded in audit rows."""
    kind = "human" if is_interactive_human(scope) else "machine"
    actor = str(getattr(scope, "actor_id", "") or "").strip() or (
        "operator" if kind == "human" else "service_account"
    )
    return kind, actor


__all__ = [
    "SECRET_BIND_SCOPE",
    "SECRET_WRITE_SCOPE",
    "actor_identity",
    "is_interactive_human",
    "require_approval_authority",
    "require_secret_bind",
    "require_secret_read_metadata",
    "require_secret_write",
]
