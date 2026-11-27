"""The owner of a company: the person who keeps the ultimate rights there (ADR 0085).

Every account has at most one owner (``accounts.owner_user_id``), a person who is a
member with the admin role there. The first administrator of an account becomes its
owner (a database trigger sets it when an account without an owner gains an active
administrator: the person who creates their own company, the first administrator an
installation administrator adds, the installation's first administrator); the
upgrade gave every existing account its earliest active administrator.

Two rules protect the owner. Every user-management path checks them in the same
transaction as its writes:

- **The owner stays** (:func:`require_owner_kept`). Nobody, the owner included,
  removes the owner from the account, changes the owner's role there, deactivates or
  deletes the owner. The owner transfers ownership first
  (:mod:`core.application.auth.user_admin.account_ownership`); the installation
  administrator may reassign it when the owner lost access.
- **Only the owner touches their own sign-in** (:func:`require_owner_consent`). No one
  but the owner sets or resets the owner's password, requires a password change,
  revokes their sessions, personal tokens or OAuth grants, unlinks their sign-in
  identities or changes their authenticator.

:func:`is_account_owner` is the predicate other code (an owner-only action a platform
module adds, for example) uses to let only the owner act.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.application.management.errors import ServiceForbiddenError

#: The refusal code of both rules, in API answers.
OWNER_PROTECTED_CODE = "account_owner_protected"
#: The owner stays until ownership is transferred.
OWNER_KEPT_MESSAGE = (
    "The company owner cannot be removed, demoted, deactivated or deleted. "
    "The owner transfers ownership to another administrator first."
)
#: Only the owner changes their own sign-in.
OWNER_CONSENT_MESSAGE = (
    "Only the company owner can change their own password, sign-in, sessions or authenticator."
)


class AccountOwnerProtectedError(ServiceForbiddenError):
    """The change would remove, demote or lock out a company owner."""

    def __init__(self, message: str, *, account_ids: Iterable[str] = ()) -> None:
        super().__init__(message, payload={"error": message, "code": OWNER_PROTECTED_CODE})
        self.account_ids = sorted({_text(item) for item in account_ids if _text(item)})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    if isinstance(row, (list, tuple)):
        return row[0] if row else None
    return None


def account_owner_id(cur: Any, account_id: Any) -> str:
    """The owner of the account (``""`` when it has none yet)."""
    account = _text(account_id)
    if not account:
        return ""
    cur.execute(
        "SELECT CAST(owner_user_id AS TEXT) AS owner_user_id FROM accounts"
        " WHERE CAST(id AS TEXT) = %s",
        (account,),
    )
    return _text(_first(cur.fetchone()))


def owned_account_ids(cur: Any, user_id: Any) -> list[str]:
    """The accounts the person owns, sorted (``[]`` for most people)."""
    user = _text(user_id)
    if not user:
        return []
    cur.execute(
        "SELECT CAST(id AS TEXT) AS id FROM accounts WHERE CAST(owner_user_id AS TEXT) = %s"
        " ORDER BY id",
        (user,),
    )
    rows = cur.fetchall()
    if not isinstance(rows, (list, tuple)):
        return []
    return [account for account in (_text(_first(row)) for row in rows) if account]


def is_account_owner(cur: Any, user_id: Any, account_id: Any) -> bool:
    """Whether the person owns the account: the predicate for owner-only actions.

    Anything only the company's owner may do (transferring ownership, and later
    owner-only actions a platform module offers) checks this, in the
    transaction that acts, never a cached answer.
    """
    user = _text(user_id)
    return bool(user) and account_owner_id(cur, account_id) == user


def require_owner_kept(
    cur: Any,
    *,
    user_id: Any,
    account_ids: Iterable[Any] | None = None,
    kept_account_ids: Iterable[Any] | None = None,
) -> None:
    """Refuse a change that removes, demotes, deactivates or deletes an owner.

    ``account_ids`` names the accounts the change takes the person out of (or whose
    role it changes); ``None`` means every account (a deactivation or a deletion).
    ``kept_account_ids`` names the accounts the person stays in when the change sets
    their memberships. The owner is refused too: they transfer ownership first.
    """
    owned = set(owned_account_ids(cur, user_id))
    if account_ids is not None:
        owned &= {_text(item) for item in account_ids}
    if kept_account_ids is not None:
        owned -= {_text(item) for item in kept_account_ids}
    if owned:
        raise AccountOwnerProtectedError(OWNER_KEPT_MESSAGE, account_ids=owned)


def require_owner_consent(cur: Any, *, user_id: Any, actor_user_id: Any) -> None:
    """Refuse a change of an owner's sign-in made by anyone but that owner.

    ``actor_user_id`` is the person making the change; ``""`` (the command line, a
    platform module, a background sync) is never the owner.
    """
    user = _text(user_id)
    if user and _text(actor_user_id).casefold() == user.casefold():
        return
    owned = owned_account_ids(cur, user)
    if owned:
        raise AccountOwnerProtectedError(OWNER_CONSENT_MESSAGE, account_ids=owned)


__all__ = [
    "OWNER_CONSENT_MESSAGE",
    "OWNER_KEPT_MESSAGE",
    "OWNER_PROTECTED_CODE",
    "AccountOwnerProtectedError",
    "account_owner_id",
    "is_account_owner",
    "owned_account_ids",
    "require_owner_consent",
    "require_owner_kept",
]
