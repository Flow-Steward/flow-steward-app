"""Installation administrator versus account administrator.

One installation can hold several accounts (``public.accounts``). Roles are global
(ADR 0066), so the admin role alone says only that a person administers the account
they act in. Some resources belong to the whole installation rather than to one
account: the system email sender, extension installs, platform-wide monitoring and
maintenance, the global provider catalogue, and the list of accounts itself.

The rule, decided once here and used at every installation-wide action:

    An **installation administrator** is an administrator acting in the
    installation account, :data:`INSTALLATION_ACCOUNT_ID` (the default account,
    ADR 0020).

An administrator acting in any other account is an **account administrator**: full
rights inside that account, none over installation-wide resources. The acting
account is the one the request runs in (the session's current account, or the
token's account), not every account the person belongs to, so an installation
administrator who switched into another account switches back before an
installation-wide change.

Fail closed: a principal without an account never acts in the installation account,
so it is never an installation administrator (ADR 0083, security notes). The one
documented default is upstream of this module: the request scope normalisation of a
caller without a session (the CLI, the worker, the single-account bootstrap; ADR 0020
"Deliberately kept defaults") fills the installation account into a ``ScopeContext``
before it reaches here, and Core's own system actor (``principal_type``
``system_actor``, never a person) acts there when it names no account. An
authenticated person's principal is never filled in.

The module is pure: no database, no request object.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.principal import principal_type_has_minimum_role

#: The account that owns the installation's shared resources (ADR 0020).
INSTALLATION_ACCOUNT_ID = DEFAULT_ACCOUNT_ID
#: The machine code a refusal carries when the caller is not an installation administrator.
INSTALLATION_ADMIN_REQUIRED_CODE = "installation_admin_required"
INSTALLATION_ADMIN_REQUIRED_MESSAGE = (
    "Only an installation administrator can do this: an administrator acting in "
    "the installation account"
)
_ADMIN_ROLES = frozenset({"admin", "system_actor"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_installation_account(account_id: Any) -> bool:
    """Whether ``account_id`` is the installation account; empty never is."""
    text = _text(account_id)
    return bool(text) and text == INSTALLATION_ACCOUNT_ID


def acting_account_id(principal: Any) -> str:
    """The account a principal (an auth user mapping or a scope object) acts in.

    Empty when the principal names none; empty is no account, never the installation's.
    """
    if isinstance(principal, Mapping):
        raw = principal.get("account_id")
        principal_type = principal.get("principal_type")
    else:
        raw = getattr(principal, "account_id", "")
        principal_type = getattr(principal, "principal_type", "")
    text = _text(raw)
    if not text and _text(principal_type).lower() == "system_actor":
        return INSTALLATION_ACCOUNT_ID
    return text


def acts_in_installation_account(principal: Any) -> bool:
    """Whether the principal acts in the installation account."""
    return principal is not None and is_installation_account(acting_account_id(principal))


def _mapping_is_admin(principal: Mapping[str, Any]) -> bool:
    roles = principal.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    if {_text(role).lower() for role in roles} & _ADMIN_ROLES:
        return True
    return principal_type_has_minimum_role(_text(principal.get("principal_type")), "admin")


def _scope_is_admin(principal: Any) -> bool:
    if not principal_type_has_minimum_role(
        _text(getattr(principal, "principal_type", "")), "admin"
    ):
        return False
    # A token acts within its scopes: an admin's token needs ``admin:all``.
    token_scopes = frozenset(getattr(principal, "token_scopes", frozenset()) or frozenset())
    return not token_scopes or "admin:all" in token_scopes


def is_installation_admin(principal: Any) -> bool:
    """Whether ``principal`` may change installation-wide resources.

    ``principal`` is either an authenticated user mapping (``request.state.auth_user``:
    admin by role; token scopes are already decided by the route table) or a scope
    object such as ``ScopeContext`` (admin by principal type; a token also needs
    ``admin:all``). Either way it must act in the installation account.
    """
    if principal is None:
        return False
    if isinstance(principal, Mapping):
        is_admin = _mapping_is_admin(principal)
    else:
        is_admin = _scope_is_admin(principal)
    return is_admin and acts_in_installation_account(principal)


def installation_admin_refusal(message: str = "") -> dict[str, str]:
    """The JSON body of a refusal for a caller who is not an installation administrator."""
    return {
        "error": _text(message) or INSTALLATION_ADMIN_REQUIRED_MESSAGE,
        "code": INSTALLATION_ADMIN_REQUIRED_CODE,
    }


__all__ = [
    "INSTALLATION_ACCOUNT_ID",
    "INSTALLATION_ADMIN_REQUIRED_CODE",
    "INSTALLATION_ADMIN_REQUIRED_MESSAGE",
    "acting_account_id",
    "acts_in_installation_account",
    "installation_admin_refusal",
    "is_installation_account",
    "is_installation_admin",
]
