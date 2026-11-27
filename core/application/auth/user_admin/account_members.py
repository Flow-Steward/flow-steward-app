"""The people of one account: find, add and remove a membership (ADR 0085).

Roles are held on the account membership, so an administrator manages the people of
the account they act in: adds a person who already exists in the installation with a
role there, or removes the person's membership of that account. The person, their
password and their other memberships stay as they are.

Who may be added directly: people who already belong to an account the administrator
manages (searched by name or email). Anyone else is only invited by their exact email
address (:mod:`core.application.auth.user_admin.account_invitations`) and becomes a
member when they accept. An administrator never browses, nor learns whether an email
address exists among, the people of accounts they do not manage.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.application.auth.approver_release import release_approver
from core.application.auth.audit import log_auth_audit_event
from core.application.auth.external_session_revocation import revoke_external_sign_in_grants
from core.application.auth.membership_roles import (
    LastAccountAdminError,
    cancel_invitations_of_former_admins,
    require_other_admins,
    revoke_account_access,
)
from core.application.auth.permissions import ADMIN_ROLE, SYSTEM_ROLE_LEVELS
from core.application.auth.scoped_role_ports import ACCOUNT_SCOPED_ROW_PREDICATE
from core.application.auth.user_admin.member_lookup_throttle import (
    Throttle,
    candidate_limits,
)
from core.application.auth.user_admin.member_lookup_throttle import (
    consume as consume_lookup,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.infrastructure.db.db_pool import transaction_connection

#: A search shorter than this lists nobody (a single letter would list everyone).
CANDIDATE_QUERY_MIN_LENGTH = 2
#: Most people one search returns.
CANDIDATE_LIMIT = 10

_LIFECYCLE_LOCK = "flow-steward:user-lifecycle"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _value(row: Any, key: str, index: int) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def _like(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _managed(account_ids: list[str] | None) -> list[str]:
    return [_text(item) for item in account_ids or () if _text(item)]


def search_candidates(
    cur: Any,
    *,
    query: str,
    account_id: str,
    managed_account_ids: list[str],
    limit: int = CANDIDATE_LIMIT,
) -> list[dict[str, Any]]:
    """Active people who are not members of ``account_id`` and whom the
    administrator may add: a name or email match among people of the managed
    accounts. A person outside them is never listed, not even by their exact email
    address (they are invited instead, ADR 0085)."""
    text = _text(query)
    managed = _managed(managed_account_ids)
    if len(text) < CANDIDATE_QUERY_MIN_LENGTH or not _text(account_id) or not managed:
        return []
    cur.execute(
        """
        SELECT u.id::text AS id, u.username AS email,
               COALESCE(NULLIF(u.display_name, ''), u.username) AS display_name
          FROM users AS u
         WHERE u.deleted_at IS NULL
           AND u.is_active
           AND NOT EXISTS (
               SELECT 1 FROM user_account_memberships AS here
                WHERE here.user_id = u.id AND here.account_id::text = %s)
           AND (u.account_id::text = ANY(%s::text[])
                OR EXISTS (
                    SELECT 1 FROM user_account_memberships AS seen
                     WHERE seen.user_id = u.id
                       AND seen.account_id::text = ANY(%s::text[])))
           AND (u.username ILIKE %s ESCAPE '\\'
                OR COALESCE(u.display_name, '') ILIKE %s ESCAPE '\\')
         ORDER BY lower(COALESCE(NULLIF(u.display_name, ''), u.username)), u.username
         LIMIT %s
        """,
        (
            _text(account_id),
            managed,
            managed,
            _like(text),
            _like(text),
            max(1, min(int(limit), CANDIDATE_LIMIT)),
        ),
    )
    return [
        {
            "id": _text(_value(row, "id", 0)),
            "email": _text(_value(row, "email", 1)),
            "display_name": _text(_value(row, "display_name", 2)),
        }
        for row in cur.fetchall() or ()
    ]


def _person(cur: Any, user_id: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT u.id::text AS id, u.username, u.account_id::text AS account_id
          FROM users AS u
         WHERE u.id::text = %s AND u.deleted_at IS NULL
         FOR UPDATE OF u
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": _text(_value(row, "id", 0)),
        "username": _text(_value(row, "username", 1)),
        "account_id": _text(_value(row, "account_id", 2)),
    }


def _member_accounts(cur: Any, user_id: str) -> list[str]:
    cur.execute(
        """
        SELECT m.account_id::text AS account_id
          FROM user_account_memberships AS m
         WHERE m.user_id::text = %s
         ORDER BY m.is_default DESC, m.created_at, m.account_id
        """,
        (user_id,),
    )
    return [_text(_value(row, "account_id", 0)) for row in cur.fetchall() or ()]


def add_member(
    cur: Any,
    *,
    user_id: str,
    account_id: str,
    role: str,
    managed_account_ids: list[str] | None,
) -> dict[str, Any]:
    """Make the person a member of ``account_id`` with ``role``; returns the person.

    The caller holds the transaction. With ``managed_account_ids`` the person must
    already belong to one of those accounts (an administrator adds directly only
    people of the accounts they manage, ADR 0085); ``None`` skips that check for an
    invitation the person accepted themselves.
    """
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_LIFECYCLE_LOCK,))
    person = _person(cur, user_id)
    if person is None:
        raise ServiceNotFoundError("The person was not found")
    members_of = set(_member_accounts(cur, user_id))
    if managed_account_ids is not None:
        managed = set(_managed(managed_account_ids))
        if not managed & (members_of | {person["account_id"]}):
            raise ServiceNotFoundError("The person was not found")
    if account_id in members_of:
        raise ServiceConflictError("The person is already a member of this company")
    cur.execute(
        """
        INSERT INTO user_account_memberships (user_id, account_id, is_default, role_id)
        VALUES (%s, %s, false, (SELECT r.id FROM roles AS r WHERE r.key = %s))
        ON CONFLICT (user_id, account_id) DO NOTHING
        RETURNING user_id
        """,
        (user_id, account_id, role),
    )
    if cur.fetchone() is None:
        raise ServiceConflictError("The person is already a member of this company")
    if role == ADMIN_ROLE:
        # An administrator never signs in externally (ADR 0083); what external sign-in
        # issued in this account ends. Other accounts keep theirs (ADR 0085, B).
        revoke_external_sign_in_grants(cur, [user_id], account_id=account_id)
    return person


def _next_home(cur: Any, user_id: str, leaving_account_id: str) -> str:
    """The membership that becomes the home of a person leaving their home account.

    An active account where the person has a role comes first, so the home never
    points at a company they cannot work in while they have one they can; then the
    default membership and the oldest. ``""`` when no other membership exists.
    """
    cur.execute(
        """
        SELECT m.account_id::text AS account_id
          FROM user_account_memberships AS m
          LEFT JOIN accounts AS a ON a.id = m.account_id
         WHERE m.user_id::text = %s AND m.account_id::text <> %s
         ORDER BY (m.role_id IS NOT NULL AND COALESCE(a.is_active, false)) DESC,
                  m.is_default DESC, m.created_at, m.account_id
         LIMIT 1
        """,
        (user_id, leaving_account_id),
    )
    return _text(_value(cur.fetchone(), "account_id", 0))


def _release_approval_steps(cur: Any, user_id: str, account_id: str) -> list[dict[str, Any]]:
    """Take the person off the approval steps of the account they leave.

    A person who is no member any more can never decide, and a later membership must
    not bring an old assignment back. A step they were the last approver of goes to the
    account's administrators, never to everyone (:mod:`core.application.auth.approver_release`).
    Returns the steps, for the audit.
    """
    return release_approver(cur, user_id=user_id, account_id=account_id)


def remove_member(
    cur: Any,
    *,
    user_id: str,
    account_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    """Remove the person's membership of ``account_id``; returns the person.

    Refused for the administrator themselves, for the account's last active
    administrator and for the person's only membership (the person is deleted
    instead). The
    person's scope limit there, their sessions, personal tokens and OAuth grants of
    that account and their assignments as an approver there go with the membership; a
    home account moves to another membership where the person has a role, if any.
    What the person made in the account (workflows, runs, schedules, history) stays the
    account's.
    """
    if _text(actor_user_id).casefold() == _text(user_id).casefold():
        raise ServiceForbiddenError("You cannot remove yourself from this company")
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_LIFECYCLE_LOCK,))
    person = _person(cur, user_id)
    if person is None:
        raise ServiceNotFoundError("The person was not found")
    accounts = _member_accounts(cur, user_id)
    if account_id not in accounts:
        raise ServiceNotFoundError("The person is not a member of this company")
    try:
        require_other_admins(cur, user_id=user_id, account_ids=[account_id])
    except LastAccountAdminError as exc:
        raise ServiceConflictError(
            "The last active administrator of this company cannot be removed: "
            "give another person the admin role here first"
        ) from exc
    remaining = [item for item in accounts if item != account_id]
    if not remaining:
        raise ServiceConflictError("This is the person's only company: delete the person instead")
    if person["account_id"] == account_id:
        home = _next_home(cur, user_id, account_id) or remaining[0]
        cur.execute(
            "UPDATE users SET account_id = %s, updated_at = now() WHERE id::text = %s",
            (home, user_id),
        )
        cur.execute(
            """UPDATE user_account_memberships SET is_default = (account_id::text = %s)
                WHERE user_id::text = %s""",
            (home, user_id),
        )
    cur.execute(
        "DELETE FROM user_account_memberships WHERE user_id::text = %s AND account_id::text = %s",
        (user_id, account_id),
    )
    cur.execute(
        "DELETE FROM scoped_role_users WHERE user_id::text = %s AND account_id::text = %s",
        (user_id, account_id),
    )
    cur.execute(
        f"DELETE FROM user_roles AS ur WHERE ur.user_id::text = %s AND {ACCOUNT_SCOPED_ROW_PREDICATE}",  # nosec B608 - a constant predicate.
        (user_id, account_id, account_id),
    )
    revoke_account_access(
        cur, user_id=user_id, account_id=account_id, reason="account_membership_removed"
    )
    cur.execute(
        "DELETE FROM external_identities WHERE user_id::text = %s AND account_id::text = %s",
        (user_id, account_id),
    )
    person["released_approval_steps"] = _release_approval_steps(cur, user_id, account_id)
    # An administrator removed from the account no longer invites to it (ADR 0085, C4).
    person["cancelled_invitations"] = cancel_invitations_of_former_admins(cur, user_id)
    return person


class AccountMemberService:
    """Add and remove people of the account an administrator acts in."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        audit: Callable[..., None] = log_auth_audit_event,
        throttle: Throttle = consume_lookup,
    ) -> None:
        self._get_logger = get_logger
        self._audit = audit
        self._throttle = throttle

    def _logger(self) -> Any:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError("Database not configured")
        return bl

    @staticmethod
    def _require_managed(account_id: str, managed_account_ids: list[str]) -> str:
        account = _text(account_id)
        if not account or account not in set(_managed(managed_account_ids)):
            raise ServiceForbiddenError("account access denied")
        return account

    def candidates(
        self,
        *,
        query: str,
        account_id: str,
        managed_account_ids: list[str],
        admin_user_id: str = "",
    ) -> list[dict[str, Any]]:
        account = self._require_managed(account_id, managed_account_ids)
        if len(_text(query)) < CANDIDATE_QUERY_MIN_LENGTH:
            return []
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            # Counted in the shared store (every process), per administrator and account.
            self._throttle(cur, candidate_limits(admin_user_id=admin_user_id, account_id=account))
            return search_candidates(
                cur,
                query=query,
                account_id=account,
                managed_account_ids=managed_account_ids,
            )

    def add(
        self,
        *,
        user_id: str,
        role: str,
        account_id: str,
        managed_account_ids: list[str],
        actor: str,
    ) -> dict[str, Any]:
        account = self._require_managed(account_id, managed_account_ids)
        key = _text(role).lower()
        if key not in SYSTEM_ROLE_LEVELS:
            raise ServiceValidationError("role must be one of admin, operator or viewer")
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            person = add_member(
                cur,
                user_id=_text(user_id),
                account_id=account,
                role=key,
                managed_account_ids=managed_account_ids,
            )
        self._audit(
            bl,
            action="account_member_add",
            username=person["username"],
            actor=actor,
            status="ok",
            detail={"user_id": person["id"], "account_id": account, "role": key},
        )
        return {"user_id": person["id"], "account_id": account, "role": key}

    def remove(
        self,
        *,
        user_id: str,
        account_id: str,
        managed_account_ids: list[str],
        actor_user_id: str,
        actor: str,
    ) -> dict[str, Any]:
        account = self._require_managed(account_id, managed_account_ids)
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            person = remove_member(
                cur,
                user_id=_text(user_id),
                account_id=account,
                actor_user_id=actor_user_id,
            )
        self._audit(
            bl,
            action="account_member_remove",
            username=person["username"],
            actor=actor,
            status="ok",
            detail={
                "user_id": person["id"],
                "account_id": account,
                **(
                    {"released_approval_steps": person["released_approval_steps"]}
                    if person.get("released_approval_steps")
                    else {}
                ),
            },
        )
        for item in person.get("cancelled_invitations") or ():
            self._audit(
                bl,
                action="account_invitation_cancel",
                username="",
                actor=actor,
                status="ok",
                detail={
                    "account_id": item.get("account_id", ""),
                    "invitation_id": item.get("id", ""),
                    "invited_email": item.get("invited_email", ""),
                    "role": item.get("role", ""),
                    "reason": "inviter_no_longer_admin",
                },
            )
        return {"user_id": person["id"], "account_id": account}


__all__ = [
    "CANDIDATE_LIMIT",
    "CANDIDATE_QUERY_MIN_LENGTH",
    "AccountMemberService",
    "add_member",
    "remove_member",
    "search_candidates",
]
