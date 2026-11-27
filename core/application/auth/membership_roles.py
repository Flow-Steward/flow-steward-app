"""Roles per account: a person's role is held on the account membership (ADR 0085).

``user_account_memberships.role_id`` is the person's one system role in that
account; ``NULL`` grants nothing. The acting account's role applies to a request:
the session's current account, a personal access token's account, an inbound
OAuth grant's account. ``user_roles`` holds scoped rows only (a check constraint
refuses a row without a workspace, project or workflow).

Every function that writes takes a cursor of the caller's transaction; the caller
holds the user-lifecycle lock where a "last administrator" rule applies.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.application.auth.permissions import ADMIN_ROLE, highest_system_role
from core.infrastructure.db.membership_role_reader import (
    MEMBERSHIP_ROLE_SQL,
    membership_role_keys,
)

#: A membership added without a requested role reads only (least privilege).
DEFAULT_MEMBERSHIP_ROLE = "viewer"

#: SQL (alias ``u`` for ``users``) true when the person is an active administrator of
#: the account bound to the one ``%s`` placeholder.
ACCOUNT_ADMIN_PREDICATE = """EXISTS (
    SELECT 1
      FROM user_account_memberships AS am
      JOIN roles AS ar ON ar.id = am.role_id
     WHERE am.user_id = u.id
       AND CAST(am.account_id AS TEXT) = %s
       AND ar.key = 'admin')"""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    return row[0]


def account_role_keys(bl: Any, user_id: Any, account_id: Any) -> list[str]:
    """The person's role keys in the account (``[]``: no role there)."""
    if not bl or not _text(user_id) or not _text(account_id):
        return []
    with bl.conn.cursor() as cur:
        return membership_role_keys(cur, user_id, account_id)


def set_membership_role(cur: Any, *, user_id: str, account_id: str, role: str | None) -> list[str]:
    """Set the person's role in the account; returns the stored roles (``[]`` for none).

    The membership must exist; :class:`LookupError` when it does not. ``role`` is one
    system role key; ``None`` removes the role (the person keeps the membership and
    has no access there). The account's owner keeps the admin role: any other role
    for them is refused with
    :class:`~core.application.auth.account_owner.AccountOwnerProtectedError`.
    """
    key = _text(role).lower() or None
    if key != ADMIN_ROLE:
        # The owner keeps the admin role until ownership is transferred (ADR 0085,
        # "Company owner"): every writer of a membership role passes here.
        from core.application.auth.account_owner import require_owner_kept

        require_owner_kept(cur, user_id=user_id, account_ids=[account_id])
    cur.execute(
        """UPDATE user_account_memberships
              SET role_id = (SELECT r.id FROM roles AS r WHERE r.key = %s),
                  updated_at = now()
            WHERE CAST(user_id AS TEXT) = %s
              AND CAST(account_id AS TEXT) = %s
        RETURNING id""",
        (key, _text(user_id), _text(account_id)),
    )
    if cur.fetchone() is None:
        raise LookupError("The person is not a member of the account")
    # A role that no longer decides approvals leaves the account's approval steps, as
    # leaving the account does (ADR 0085, "Leaving a company"). Imported here: the
    # permission reader imports this module.
    from core.application.auth.approver_release import release_if_no_longer_deciding

    release_if_no_longer_deciding(
        cur, user_id=_text(user_id), account_id=_text(account_id), role=key
    )
    return [key] if key else []


def roles_by_account(cur: Any, user_id: str) -> dict[str, str]:
    """``{account id: role key}`` of the person's memberships that carry a role."""
    cur.execute(
        """SELECT CAST(m.account_id AS TEXT) AS account_id, r.key AS role
             FROM user_account_memberships AS m
             JOIN roles AS r ON r.id = m.role_id
            WHERE CAST(m.user_id AS TEXT) = %s""",
        (_text(user_id),),
    )
    result: dict[str, str] = {}
    for row in cur.fetchall() or ():
        if isinstance(row, Mapping):
            account, role = row.get("account_id"), row.get("role")
        else:
            account, role = row[0], row[1]
        if _text(account) and _text(role):
            result[_text(account)] = _text(role)
    return result


def is_account_admin(cur: Any, user_id: str, account_id: str) -> bool:
    """Whether the person's role in the account is admin."""
    return ADMIN_ROLE in membership_role_keys(cur, user_id, account_id)


def holds_admin_anywhere(cur: Any, user_id: str) -> bool:
    """Whether the person is an administrator of any account."""
    return ADMIN_ROLE in roles_by_account(cur, user_id).values()


def other_active_admins(cur: Any, *, account_id: str, user_id: str) -> int:
    """Active, live administrators of the account other than ``user_id``."""
    cur.execute(
        f"""SELECT COUNT(*) AS total
              FROM users AS u
             WHERE u.deleted_at IS NULL
               AND u.is_active
               AND CAST(u.id AS TEXT) <> %s
               AND {ACCOUNT_ADMIN_PREDICATE}""",  # nosec B608 - a constant predicate.
        (_text(user_id), _text(account_id)),
    )
    return int(_first(cur.fetchone()) or 0)


class LastAccountAdminError(ValueError):
    """The change would leave an account without an active administrator."""

    def __init__(self, account_id: str) -> None:
        super().__init__(
            "The last active administrator of an account cannot lose that role: "
            "give another person the admin role there first"
        )
        self.account_id = account_id


def require_other_admins(cur: Any, *, user_id: str, account_ids: Iterable[str]) -> None:
    """Refuse when one of ``account_ids`` would have no active administrator left.

    Only accounts where ``user_id`` is an administrator today are checked; the caller
    passes the accounts whose administrator the change removes.
    """
    current = roles_by_account(cur, user_id)
    for account_id in sorted({_text(item) for item in account_ids if _text(item)}):
        if current.get(account_id) != ADMIN_ROLE:
            continue
        if other_active_admins(cur, account_id=account_id, user_id=user_id) < 1:
            raise LastAccountAdminError(account_id)


#: Whether the inbound OAuth grants table exists (optional storage, ADR 0047).
_OAUTH_GRANTS_STORAGE_SQL = "SELECT to_regclass('public.oauth_grants') IS NOT NULL AS available"


def revoke_account_access(cur: Any, *, user_id: str, account_id: str, reason: str) -> int:
    """End what the person holds in one account; returns the revoked sessions.

    Revokes the person's sessions, personal access tokens and inbound OAuth grants
    bound to ``account_id`` (ADR 0085 §4: a role is read at use, but a role change or
    a removed membership also ends what was issued under the old one there). Their
    sessions, tokens and grants of other accounts stay. The caller owns the
    transaction; ``reason`` is stored on the revoked OAuth grants.
    """
    user, account = _text(user_id), _text(account_id)
    if not user or not account:
        return 0
    # Nobody ends the owner's access to the company they own (ADR 0085, "Company
    # owner"); a role change or a removal of the owner is refused before this.
    from core.application.auth.account_owner import require_owner_kept

    require_owner_kept(cur, user_id=user, account_ids=[account])
    cur.execute(
        """UPDATE auth_sessions SET revoked = true, updated_at = now()
            WHERE CAST(user_id AS TEXT) = %s AND CAST(account_id AS TEXT) = %s
              AND revoked = false""",
        (user, account),
    )
    revoked_sessions = int(getattr(cur, "rowcount", 0) or 0)
    cur.execute(
        """UPDATE personal_access_tokens SET revoked = true, updated_at = now()
            WHERE CAST(user_id AS TEXT) = %s AND CAST(account_id AS TEXT) = %s
              AND revoked = false""",
        (user, account),
    )
    cur.execute(_OAUTH_GRANTS_STORAGE_SQL)
    if _first(cur.fetchone()) is True:
        cur.execute(
            """UPDATE oauth_grants SET revoked_at = now(), revoked_reason = %s
                WHERE CAST(user_id AS TEXT) = %s AND CAST(account_id AS TEXT) = %s
                  AND revoked_at IS NULL""",
            (_text(reason) or "account_access_changed", user, account),
        )
    return revoked_sessions


#: Whether the inviter of invitation ``i`` is still an active administrator of its
#: account (ADR 0085, C4): an invitation speaks for one.
INVITER_IS_ADMIN_PREDICATE = """EXISTS (
    SELECT 1
      FROM user_account_memberships AS im
      JOIN roles AS ir ON ir.id = im.role_id
      JOIN users AS iu ON iu.id = im.user_id
     WHERE im.user_id = i.invited_by
       AND im.account_id = i.account_id
       AND ir.key = 'admin'
       AND iu.is_active
       AND iu.deleted_at IS NULL)"""


def cancel_invitations_of_former_admins(cur: Any, inviter_id: str) -> list[dict[str, str]]:
    """Cancel the open invitations ``inviter_id`` sent where they are no admin any more.

    Called in the transaction that demotes, deactivates, deletes or removes the person
    (ADR 0085, C4). Returns the cancelled invitations (id, account, address, role) for
    the auth audit.
    """
    inviter = _text(inviter_id)
    if not inviter:
        return []
    cur.execute(
        f"""UPDATE account_invitations AS i
               SET status = 'cancelled'
             WHERE CAST(i.invited_by AS TEXT) = %s
               AND i.status = 'pending'
               AND NOT {INVITER_IS_ADMIN_PREDICATE}
         RETURNING CAST(i.id AS TEXT) AS id, CAST(i.account_id AS TEXT) AS account_id,
                   i.invited_email AS invited_email, i.role AS role""",  # nosec B608 - a constant predicate.
        (inviter,),
    )
    rows = cur.fetchall()
    cancelled: list[dict[str, str]] = []
    for row in rows if isinstance(rows, (list, tuple)) else ():
        if isinstance(row, dict):
            values = [row.get(key) for key in ("id", "account_id", "invited_email", "role")]
        elif isinstance(row, (list, tuple)) and len(row) >= 4:
            values = list(row[:4])
        else:
            continue
        cancelled.append(
            dict(
                zip(("id", "account_id", "invited_email", "role"), map(_text, values), strict=True)
            )
        )
    return cancelled


def account_with_role(cur: Any, user_id: str, preferred_account_id: str = "") -> str:
    """The account a sign-in lands in: ``preferred_account_id`` when the person has a
    role there, else their default membership with a role, else any membership with a
    role; ``""`` when they have a role nowhere (ADR 0085: no role, no access)."""
    preferred = _text(preferred_account_id)
    cur.execute(
        """SELECT CAST(m.account_id AS TEXT) AS account_id
             FROM user_account_memberships AS m
            WHERE CAST(m.user_id AS TEXT) = %s AND m.role_id IS NOT NULL
            ORDER BY (CAST(m.account_id AS TEXT) = %s) DESC, m.is_default DESC,
                     m.created_at, m.account_id
            LIMIT 1""",
        (_text(user_id), preferred),
    )
    return _text(_first(cur.fetchone()))


def keep_one_role(roles: Iterable[str]) -> str | None:
    """The one system role to store from ``roles``: the highest, or ``None``."""
    return highest_system_role([_text(role).lower() for role in roles if _text(role)])


__all__ = [
    "ACCOUNT_ADMIN_PREDICATE",
    "DEFAULT_MEMBERSHIP_ROLE",
    "INVITER_IS_ADMIN_PREDICATE",
    "MEMBERSHIP_ROLE_SQL",
    "LastAccountAdminError",
    "account_role_keys",
    "account_with_role",
    "cancel_invitations_of_former_admins",
    "holds_admin_anywhere",
    "is_account_admin",
    "keep_one_role",
    "membership_role_keys",
    "other_active_admins",
    "require_other_admins",
    "revoke_account_access",
    "roles_by_account",
    "set_membership_role",
]
