"""Transferring and reassigning a company's ownership (ADR 0085, "Company owner").

The owner offers ownership to another active administrator of the company; the
offer waits until that person accepts or declines it (or it expires, or the owner
cancels it), so nobody becomes an owner without consenting. One offer per company is
open at a time; a new offer replaces the open one. The installation administrator
reassigns the owner of any company when the owner has lost access (always with a
reason): the new owner must be an active member there and gets the admin role if
they had another one. Every change is recorded in the auth audit.

Who may do what is decided here, in the transaction that writes, under the
user-lifecycle lock every membership and role change takes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.application.auth.account_owner import account_owner_id, is_account_owner
from core.application.auth.audit import log_auth_audit_event
from core.application.auth.membership_roles import set_membership_role
from core.application.auth.permissions import ADMIN_ROLE
from core.application.auth.scoped_role_ports import ACCOUNT_SCOPED_ROW_PREDICATE
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.infrastructure.db.db_pool import transaction_connection

#: Days an ownership offer stays open.
TRANSFER_LIFETIME_DAYS = 14
#: Longest recorded reason of a reassignment.
REASON_MAX_LENGTH = 500
#: The answer to an offer that is not (or no longer) open for the person, whatever the cause.
TRANSFER_INVALID_MESSAGE = "This ownership transfer is no longer open"
#: Only the owner offers or cancels a transfer.
OWNER_ONLY_MESSAGE = "Only the company owner can transfer ownership"
#: The recipient of an offer must be an active administrator of the company.
RECIPIENT_MESSAGE = "Ownership can only go to another active administrator of this company"
#: A reassignment names a person who is an active member of the company.
REASSIGN_MEMBER_MESSAGE = "The new owner must be an active member of this company"

_LIFECYCLE_LOCK = "flow-steward:user-lifecycle"

#: SQL true when person ``%s`` is active, undeleted and admin of account ``%s``.
_ACTIVE_ADMIN_SQL = """SELECT 1
      FROM users AS u
      JOIN user_account_memberships AS m ON m.user_id = u.id
      JOIN roles AS r ON r.id = m.role_id
     WHERE CAST(u.id AS TEXT) = %s
       AND CAST(m.account_id AS TEXT) = %s
       AND u.is_active AND u.deleted_at IS NULL
       AND r.key = 'admin'"""

_TRANSFER_COLUMNS = """CAST(t.id AS TEXT) AS id, CAST(t.account_id AS TEXT) AS account_id,
       CAST(t.from_user_id AS TEXT) AS from_user_id,
       CAST(t.to_user_id AS TEXT) AS to_user_id,
       COALESCE(NULLIF(tu.display_name, ''), tu.username) AS to_name,
       COALESCE(NULLIF(fu.display_name, ''), fu.username) AS from_name,
       t.created_at, t.expires_at"""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _iso(value: Any) -> str | None:
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else (_text(value) or None)


def _row(row: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return {key: row.get(key) for key in keys}
    return dict(zip(keys, row, strict=False))


def _lock(cur: Any) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_LIFECYCLE_LOCK,))


def _is_active_admin(cur: Any, user_id: str, account_id: str) -> bool:
    cur.execute(_ACTIVE_ADMIN_SQL, (user_id, account_id))
    return cur.fetchone() is not None


def _person_name(cur: Any, user_id: str) -> str:
    if not user_id:
        return ""
    cur.execute(
        "SELECT COALESCE(NULLIF(display_name, ''), username) AS name FROM users"
        " WHERE CAST(id AS TEXT) = %s",
        (user_id,),
    )
    return _text(_row(cur.fetchone(), ("name",)).get("name"))


_TRANSFER_KEYS = (
    "id",
    "account_id",
    "from_user_id",
    "to_user_id",
    "to_name",
    "from_name",
    "created_at",
    "expires_at",
)


def _transfer_view(row: Any) -> dict[str, Any] | None:
    values = _row(row, _TRANSFER_KEYS)
    if not values or not _text(values.get("id")):
        return None
    return {
        "id": _text(values["id"]),
        "account_id": _text(values["account_id"]),
        "from_user_id": _text(values["from_user_id"]),
        "from_name": _text(values["from_name"]),
        "to_user_id": _text(values["to_user_id"]),
        "to_name": _text(values["to_name"]),
        "created_at": _iso(values["created_at"]),
        "expires_at": _iso(values["expires_at"]),
    }


def open_transfer(cur: Any, account_id: str) -> dict[str, Any] | None:
    """The account's open offer: pending, unexpired and made by the current owner."""
    cur.execute(
        f"""SELECT {_TRANSFER_COLUMNS}
              FROM account_owner_transfers AS t
              JOIN accounts AS a ON a.id = t.account_id
              JOIN users AS tu ON tu.id = t.to_user_id
              JOIN users AS fu ON fu.id = t.from_user_id
             WHERE CAST(t.account_id AS TEXT) = %s
               AND t.status = 'pending'
               AND t.expires_at > now()
               AND a.owner_user_id = t.from_user_id
             LIMIT 1""",  # nosec B608 - constant columns.
        (account_id,),
    )
    return _transfer_view(cur.fetchone())


def _close_pending(cur: Any, account_id: str, status: str) -> list[str]:
    cur.execute(
        """UPDATE account_owner_transfers
              SET status = %s, decided_at = now()
            WHERE CAST(account_id AS TEXT) = %s AND status = 'pending'
        RETURNING CAST(id AS TEXT) AS id""",
        (status, account_id),
    )
    rows = cur.fetchall()
    if not isinstance(rows, (list, tuple)):
        return []
    return [_text(_row(row, ("id",)).get("id")) for row in rows if row]


def offer_transfer(cur: Any, *, account_id: str, owner_id: str, to_user_id: str) -> dict[str, Any]:
    """The owner offers ownership to another active administrator of the account."""
    _lock(cur)
    if not is_account_owner(cur, owner_id, account_id):
        raise ServiceForbiddenError(OWNER_ONLY_MESSAGE)
    recipient = _text(to_user_id)
    if not recipient or recipient.casefold() == owner_id.casefold():
        raise ServiceValidationError(RECIPIENT_MESSAGE)
    if not _is_active_admin(cur, recipient, account_id):
        raise ServiceValidationError(RECIPIENT_MESSAGE)
    replaced = _close_pending(cur, account_id, "cancelled")
    cur.execute(
        """INSERT INTO account_owner_transfers (account_id, from_user_id, to_user_id, expires_at)
           VALUES (%s, %s, %s, now() + make_interval(days => %s))""",
        (account_id, owner_id, recipient, TRANSFER_LIFETIME_DAYS),
    )
    transfer = open_transfer(cur, account_id)
    if transfer is None:
        raise ServiceUnavailableError("The ownership transfer could not be stored")
    transfer["replaced"] = replaced
    return transfer


def cancel_transfer(cur: Any, *, account_id: str, owner_id: str) -> list[str]:
    """The owner withdraws the open offer; returns the cancelled offers."""
    _lock(cur)
    if not is_account_owner(cur, owner_id, account_id):
        raise ServiceForbiddenError(OWNER_ONLY_MESSAGE)
    cancelled = _close_pending(cur, account_id, "cancelled")
    if not cancelled:
        raise ServiceNotFoundError(TRANSFER_INVALID_MESSAGE)
    return cancelled


def decide_transfer(
    cur: Any, *, account_id: str, user_id: str, transfer_id: str, accept: bool
) -> dict[str, Any]:
    """The recipient accepts (becomes the owner) or declines the open offer.

    An offer that is not open for this person in this account (another recipient,
    answered, cancelled, expired, made by someone who is no longer the owner, or a
    recipient who is no active administrator any more) is answered "no longer open".
    """
    _lock(cur)
    transfer = open_transfer(cur, account_id)
    if (
        transfer is None
        or transfer["id"] != _text(transfer_id)
        or transfer["to_user_id"].casefold() != _text(user_id).casefold()
        or not _is_active_admin(cur, transfer["to_user_id"], account_id)
    ):
        raise ServiceNotFoundError(TRANSFER_INVALID_MESSAGE)
    if accept:
        cur.execute(
            """UPDATE accounts SET owner_user_id = %s
                WHERE CAST(id AS TEXT) = %s AND CAST(owner_user_id AS TEXT) = %s""",
            (transfer["to_user_id"], account_id, transfer["from_user_id"]),
        )
        if not int(getattr(cur, "rowcount", 1) or 0):
            raise ServiceNotFoundError(TRANSFER_INVALID_MESSAGE)
    cur.execute(
        """UPDATE account_owner_transfers SET status = %s, decided_at = now()
            WHERE CAST(id AS TEXT) = %s AND status = 'pending'""",
        ("accepted" if accept else "declined", transfer["id"]),
    )
    return transfer


def reassign_owner(cur: Any, *, account_id: str, to_user_id: str) -> dict[str, Any]:
    """Make ``to_user_id`` the account's owner (installation administrator recovery).

    The person must be an active member of the account; a member without the admin
    role there gets it (and loses any scope limit there: an administrator is never
    limited). An open offer is cancelled. Returns the previous owner, whether the
    person was promoted and the cancelled offers.
    """
    _lock(cur)
    cur.execute(
        "SELECT CAST(owner_user_id AS TEXT) AS owner FROM accounts WHERE CAST(id AS TEXT) = %s FOR UPDATE",
        (account_id,),
    )
    found = cur.fetchone()
    if found is None:
        raise ServiceNotFoundError("Account not found")
    previous = _text(_row(found, ("owner",)).get("owner"))
    recipient = _text(to_user_id)
    cur.execute(
        """SELECT COALESCE(r.key, '') AS role
             FROM users AS u
             JOIN user_account_memberships AS m ON m.user_id = u.id
             LEFT JOIN roles AS r ON r.id = m.role_id
            WHERE CAST(u.id AS TEXT) = %s AND CAST(m.account_id AS TEXT) = %s
              AND u.is_active AND u.deleted_at IS NULL""",
        (recipient, account_id),
    )
    member = cur.fetchone()
    if member is None:
        raise ServiceValidationError(REASSIGN_MEMBER_MESSAGE)
    role = _text(_row(member, ("role",)).get("role"))
    promoted = role != ADMIN_ROLE
    if promoted:
        set_membership_role(cur, user_id=recipient, account_id=account_id, role=ADMIN_ROLE)
        cur.execute(
            "DELETE FROM scoped_role_users WHERE CAST(user_id AS TEXT) = %s"
            " AND CAST(account_id AS TEXT) = %s",
            (recipient, account_id),
        )
        cur.execute(
            f"DELETE FROM user_roles AS ur WHERE CAST(ur.user_id AS TEXT) = %s"
            f" AND {ACCOUNT_SCOPED_ROW_PREDICATE}",  # nosec B608 - a constant predicate.
            (recipient, account_id, account_id),
        )
    cur.execute(
        "UPDATE accounts SET owner_user_id = %s WHERE CAST(id AS TEXT) = %s",
        (recipient, account_id),
    )
    cancelled = _close_pending(cur, account_id, "cancelled")
    return {
        "previous_owner_id": previous,
        "promoted_to_admin": promoted,
        "cancelled_transfers": cancelled,
    }


class AccountOwnershipService:
    """Read, offer, answer and reassign a company's ownership, audited."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        audit: Callable[..., None] = log_auth_audit_event,
    ) -> None:
        self._get_logger = get_logger
        self._audit = audit

    def _logger(self) -> Any:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError("Database not configured")
        return bl

    def state(self, *, account_id: str, viewer_id: str) -> dict[str, Any]:
        """Who owns the account and the open offer, as an administrator there sees it."""
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            owner = account_owner_id(cur, account_id)
            transfer = open_transfer(cur, account_id)
            owner_name = _person_name(cur, owner)
        viewer = _text(viewer_id).casefold()
        return {
            "account_id": account_id,
            "owner_user_id": owner,
            "owner_name": owner_name,
            "is_owner": bool(owner) and owner.casefold() == viewer,
            "pending_transfer": transfer,
            "transfer_is_for_me": bool(transfer)
            and _text((transfer or {}).get("to_user_id")).casefold() == viewer,
        }

    def offer(
        self, *, account_id: str, owner_id: str, to_user_id: str, actor: str
    ) -> dict[str, Any]:
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            transfer = offer_transfer(
                cur, account_id=account_id, owner_id=_text(owner_id), to_user_id=to_user_id
            )
        replaced = transfer.pop("replaced", [])
        self._record(
            bl,
            "account_owner_transfer_offer",
            actor,
            account_id=account_id,
            transfer_id=transfer["id"],
            from_user_id=transfer["from_user_id"],
            to_user_id=transfer["to_user_id"],
            expires_at=transfer["expires_at"],
            replaced_transfers=replaced,
        )
        return {"status": "pending", "transfer": transfer}

    def cancel(self, *, account_id: str, owner_id: str, actor: str) -> dict[str, Any]:
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            cancelled = cancel_transfer(cur, account_id=account_id, owner_id=_text(owner_id))
        self._record(
            bl,
            "account_owner_transfer_cancel",
            actor,
            account_id=account_id,
            transfer_ids=cancelled,
        )
        return {"status": "cancelled"}

    def decide(
        self, *, account_id: str, user_id: str, transfer_id: str, accept: bool, actor: str
    ) -> dict[str, Any]:
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            transfer = decide_transfer(
                cur,
                account_id=account_id,
                user_id=_text(user_id),
                transfer_id=transfer_id,
                accept=accept,
            )
        self._record(
            bl,
            "account_owner_transfer_accept" if accept else "account_owner_transfer_decline",
            actor,
            account_id=account_id,
            transfer_id=transfer["id"],
            from_user_id=transfer["from_user_id"],
            to_user_id=transfer["to_user_id"],
        )
        return {
            "status": "accepted" if accept else "declined",
            "owner_user_id": (transfer["to_user_id"] if accept else transfer["from_user_id"]),
        }

    def reassign(
        self, *, account_id: str, to_user_id: str, reason: str, actor: str
    ) -> dict[str, Any]:
        """Installation administrator recovery; the caller checked who may do it."""
        why = _text(reason)
        if not why:
            raise ServiceValidationError("A reason is required to reassign the owner")
        if len(why) > REASON_MAX_LENGTH:
            raise ServiceValidationError(
                f"The reason must be at most {REASON_MAX_LENGTH} characters"
            )
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            result = reassign_owner(cur, account_id=_text(account_id), to_user_id=to_user_id)
        self._record(
            bl,
            "account_owner_reassign",
            actor,
            account_id=_text(account_id),
            previous_owner_id=result["previous_owner_id"],
            owner_user_id=_text(to_user_id),
            promoted_to_admin=result["promoted_to_admin"],
            cancelled_transfers=result["cancelled_transfers"],
            reason=why,
        )
        return {
            "status": "reassigned",
            "account_id": _text(account_id),
            "owner_user_id": _text(to_user_id),
            "previous_owner_id": result["previous_owner_id"],
            "promoted_to_admin": result["promoted_to_admin"],
        }

    def _record(self, bl: Any, action: str, actor: str, **detail: Any) -> None:
        self._audit(
            bl,
            action=action,
            username=_text(detail.get("account_id")),
            actor=_text(actor) or "admin",
            status="ok",
            detail=detail,
        )


__all__ = [
    "REASSIGN_MEMBER_MESSAGE",
    "RECIPIENT_MESSAGE",
    "TRANSFER_INVALID_MESSAGE",
    "TRANSFER_LIFETIME_DAYS",
    "AccountOwnershipService",
    "cancel_transfer",
    "decide_transfer",
    "offer_transfer",
    "open_transfer",
    "reassign_owner",
]
