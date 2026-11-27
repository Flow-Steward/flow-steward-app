"""Invitations to join an account: membership only with the person's consent (ADR 0085).

An administrator adds directly only people who already belong to an account the
administrator manages. Anyone else is invited by email. The invitation carries a
single-use, unguessable token (only its SHA-256 is stored): Core's system email sends
the link ``/invitations/<token>`` to the address, or, when no system email is set up,
the administrator gets the link once to deliver it themselves. Whoever opens the link
while signed in as a person (a password or an external sign-in session; never a
token) sees the company, the role and who invited them, and accepts or declines. The
token, not the typed email address, binds the acceptance: the address is kept only
for the administrators' list.

The administrator never learns whether an address belongs to someone: an invitation
is stored and answered the same whether or not the person exists. A new invitation to
the same address replaces the token (the old link stops working). An invitation stays
valid only while its inviter is an active administrator of the account; one whose
inviter lost that role is cancelled. Invitations are rate-limited per administrator,
per account and per recipient in the shared store. Expired invitations cannot be
answered and are removed when the account sends its next invitation.
"""

from __future__ import annotations

import contextlib
import secrets
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any

from core.application.auth.audit import log_auth_audit_event
from core.application.auth.email import normalize_email_address
from core.application.auth.membership_roles import (
    INVITER_IS_ADMIN_PREDICATE,
    cancel_invitations_of_former_admins,
)
from core.application.auth.password import password_reset_token_hash
from core.application.auth.permissions import SYSTEM_ROLE_LEVELS
from core.application.auth.user_admin.account_members import add_member
from core.application.auth.user_admin.member_lookup_throttle import (
    Throttle,
    invitation_limits,
)
from core.application.auth.user_admin.member_lookup_throttle import (
    consume as consume_lookup,
)
from core.application.extensions.system_email import send_account_invitation_email
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.infrastructure.db.db_pool import transaction_connection

#: Days an invitation stays open.
INVITATION_LIFETIME_DAYS = 14
#: The one answer to an invitation, whether or not the person exists.
INVITATION_SENT_MESSAGE = "If this person exists, they will get an invitation"
#: The answer to a link that is not (or no longer) an open invitation, whatever the cause.
INVITATION_INVALID_MESSAGE = "This invitation is no longer valid"
#: The role shown to the person is not the invitation's role any more.
INVITATION_CHANGED_MESSAGE = "This invitation has changed. Open the link again to see it."
#: Refusal of an address that is not plain ASCII (it could look like another one).
INVITATION_ADDRESS_MESSAGE = "Type the person's full email address"
_EMAIL_MAX_LENGTH = 320
#: Bytes of randomness in a link token (256 bits).
_TOKEN_BYTES = 32


def _text(value: Any) -> str:
    return str(value or "").strip()


def _value(row: Any, key: str, index: int) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else _text(value)


def invitation_token_hash(token: str) -> str:
    """The stored form of a link token: its SHA-256, hex."""
    return password_reset_token_hash(_text(token))


def new_invitation_token() -> str:
    """A fresh, unguessable link token (URL-safe)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def invited_address(raw: Any) -> tuple[str, str]:
    """``(as typed, normalized)`` for an invited address, or :class:`ServiceValidationError`.

    Only plain ASCII addresses are invited: a character that NFKC folding or case
    folding turns into another one (a Kelvin sign for ``k``, a full-width ``@``) would
    let two addresses that look alike name different people.
    """
    typed = _text(raw)
    if not typed or len(typed) > _EMAIL_MAX_LENGTH or not typed.isascii():
        raise ServiceValidationError(INVITATION_ADDRESS_MESSAGE)
    if unicodedata.normalize("NFKC", typed).casefold() != typed.lower():
        raise ServiceValidationError(INVITATION_ADDRESS_MESSAGE)
    try:
        normalized = normalize_email_address(typed, field_name="email")
    except ValueError as exc:
        raise ServiceValidationError(INVITATION_ADDRESS_MESSAGE) from exc
    return typed, normalized


def _rows(cur: Any) -> list[Any]:
    rows = cur.fetchall()
    return list(rows) if isinstance(rows, (list, tuple)) else []


def delete_expired(cur: Any, account_id: str) -> list[dict[str, str]]:
    """Remove the account's expired pending invitations; returns what was removed."""
    cur.execute(
        """
        DELETE FROM account_invitations
         WHERE account_id::text = %s
           AND status = 'pending'
           AND expires_at <= now()
        RETURNING id::text AS id, invited_email AS invited_email, role AS role
        """,
        (_text(account_id),),
    )
    return [
        {
            "id": _text(_value(row, "id", 0)),
            "invited_email": _text(_value(row, "invited_email", 1)),
            "role": _text(_value(row, "role", 2)),
        }
        for row in _rows(cur)
    ]


def invite(
    cur: Any,
    *,
    account_id: str,
    email: str,
    role: str,
    invited_by: str,
    token_hash: str,
) -> dict[str, str]:
    """Store a pending invitation to ``email`` as typed; returns its id and expiry.

    The row is stored whether or not anyone has that address. A pending invitation of
    the same address (case-insensitive) to the same account is renewed: the new role,
    inviter and lifetime, and always the new token, so a link sent earlier stops
    working (ADR 0085, P1).
    """
    cur.execute(
        """
        INSERT INTO account_invitations
            (account_id, invited_email, role, invited_by, token_hash, expires_at)
        VALUES (%s, %s, %s, NULLIF(%s, '')::uuid, %s, now() + make_interval(days => %s))
        ON CONFLICT (account_id, lower(invited_email)) WHERE status = 'pending'
        DO UPDATE SET invited_email = EXCLUDED.invited_email,
                      role = EXCLUDED.role,
                      invited_by = EXCLUDED.invited_by,
                      token_hash = EXCLUDED.token_hash,
                      created_at = now(),
                      expires_at = EXCLUDED.expires_at
        RETURNING id::text AS id, expires_at AS expires_at
        """,
        (
            _text(account_id),
            _text(email),
            role,
            _text(invited_by),
            _text(token_hash),
            INVITATION_LIFETIME_DAYS,
        ),
    )
    row = cur.fetchone()
    return {"id": _text(_value(row, "id", 0)), "expires_at": _iso(_value(row, "expires_at", 1))}


def invitation_names(cur: Any, *, account_id: str, inviter_id: str) -> tuple[str, str]:
    """``(company, inviter)`` as the invitation names them to the person."""
    cur.execute(
        """
        SELECT COALESCE(NULLIF(a.display_name, ''), a.account_key, '') AS company,
               COALESCE((SELECT COALESCE(NULLIF(u.display_name, ''), u.username)
                           FROM users AS u WHERE u.id::text = %s), '') AS inviter
          FROM accounts AS a
         WHERE a.id::text = %s
        """,
        (_text(inviter_id), _text(account_id)),
    )
    row = cur.fetchone()
    return _text(_value(row, "company", 0)), _text(_value(row, "inviter", 1))


def sent_invitations(cur: Any, account_id: str) -> list[dict[str, Any]]:
    """The account's open invitations, each with the address as the administrator typed it.

    Every invitation the administrators sent is listed, whether or not anyone has the
    address, so the list never says whether a person exists. Expired ones are hidden.
    The link token is never listed: it is shown once, when the invitation is made.
    """
    cur.execute(
        """
        SELECT i.id::text AS id, i.invited_email AS email, i.role AS role,
               COALESCE(NULLIF(b.display_name, ''), b.username, '') AS invited_by,
               i.created_at AS created_at, i.expires_at AS expires_at
          FROM account_invitations AS i
          LEFT JOIN users AS b ON b.id = i.invited_by
         WHERE i.account_id::text = %s
           AND i.status = 'pending'
           AND i.expires_at > now()
         ORDER BY i.created_at DESC, i.id
        """,
        (_text(account_id),),
    )
    return [
        {
            "id": _text(_value(row, "id", 0)),
            "email": _text(_value(row, "email", 1)),
            "role": _text(_value(row, "role", 2)),
            "invited_by": _text(_value(row, "invited_by", 3)),
            "created_at": _iso(_value(row, "created_at", 4)),
            "expires_at": _iso(_value(row, "expires_at", 5)),
        }
        for row in cur.fetchall() or ()
    ]


def cancel(cur: Any, *, account_id: str, invitation_id: str) -> dict[str, str]:
    """Cancel an open invitation of the account; returns its id, address and role.

    Refused with :class:`ServiceNotFoundError` for an invitation of another account
    or one that is no longer open.
    """
    cur.execute(
        """
        UPDATE account_invitations
           SET status = 'cancelled'
         WHERE id::text = %s
           AND account_id::text = %s
           AND status = 'pending'
        RETURNING id::text AS id, invited_email AS invited_email, role AS role
        """,
        (_text(invitation_id), _text(account_id)),
    )
    row = cur.fetchone()
    found = _text(_value(row, "id", 0))
    if not found:
        raise ServiceNotFoundError("The invitation was not found or is no longer open")
    return {
        "id": found,
        "invited_email": _text(_value(row, "invited_email", 1)),
        "role": _text(_value(row, "role", 2)),
    }


def _open_invitation(cur: Any, token_hash: str, *, lock: bool) -> dict[str, str] | None:
    """The open invitation the token names, or ``None``.

    Open: pending, not expired, of an active account, and its inviter still an active
    administrator there (ADR 0085, C4).
    """
    cur.execute(
        f"""
        SELECT i.id::text AS id, i.account_id::text AS account_id, i.role AS role,
               COALESCE(NULLIF(a.display_name, ''), a.account_key, '') AS account_name,
               COALESCE(NULLIF(b.display_name, ''), b.username, '') AS invited_by,
               i.expires_at AS expires_at
          FROM account_invitations AS i
          JOIN accounts AS a ON a.id = i.account_id AND a.is_active
          LEFT JOIN users AS b ON b.id = i.invited_by
         WHERE i.token_hash = %s
           AND i.status = 'pending'
           AND i.expires_at > now()
           AND {INVITER_IS_ADMIN_PREDICATE}
        {"FOR UPDATE OF i" if lock else ""}
        """,  # nosec B608 - constant predicates.
        (_text(token_hash),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": _text(_value(row, "id", 0)),
        "account_id": _text(_value(row, "account_id", 1)),
        "role": _text(_value(row, "role", 2)),
        "account_name": _text(_value(row, "account_name", 3)),
        "invited_by": _text(_value(row, "invited_by", 4)),
        "expires_at": _iso(_value(row, "expires_at", 5)),
    }


def preview(cur: Any, token_hash: str) -> dict[str, str]:
    """What the person is invited to: company, role, inviter and expiry."""
    found = _open_invitation(cur, token_hash, lock=False)
    if found is None:
        raise ServiceNotFoundError(INVITATION_INVALID_MESSAGE)
    return {
        "account_name": found["account_name"],
        "role": found["role"],
        "invited_by": found["invited_by"],
        "expires_at": found["expires_at"],
    }


def decide(
    cur: Any,
    *,
    token_hash: str,
    user_id: str,
    accept: bool,
    expected_role: str = "",
) -> dict[str, Any]:
    """Accept or decline the invitation the token names, as ``user_id``.

    The token is the authority: whoever holds it and is signed in answers it, and the
    invitation is closed for good (single use). Accepting creates the membership with
    the invited role in the caller's transaction; it is refused with
    :class:`ServiceConflictError` when ``expected_role`` (the role the person was
    shown) is not the invitation's role any more. Refused with
    :class:`ServiceNotFoundError` for a token that names no open invitation.
    """
    found = _open_invitation(cur, token_hash, lock=True)
    if found is None:
        raise ServiceNotFoundError(INVITATION_INVALID_MESSAGE)
    role = found["role"]
    if accept and _text(expected_role).lower() != role:
        raise ServiceConflictError(INVITATION_CHANGED_MESSAGE)
    if accept:
        # Already a member (added meanwhile): the invitation is simply done.
        with contextlib.suppress(ServiceConflictError):
            add_member(
                cur,
                user_id=_text(user_id),
                account_id=found["account_id"],
                role=role,
                managed_account_ids=None,
            )
    # The person who answered is recorded on the invitation.
    cur.execute(
        "UPDATE account_invitations SET status = %s, user_id = %s::uuid WHERE id::text = %s",
        ("accepted" if accept else "declined", _text(user_id), found["id"]),
    )
    return {
        "id": found["id"],
        "status": "accepted" if accept else "declined",
        "account_id": found["account_id"],
        "account_name": found["account_name"],
        "role": role,
    }


class AccountInvitationService:
    """Invite to the acting account, list and cancel its invitations; answer a link."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        audit: Callable[..., None] = log_auth_audit_event,
        send_invitation: Callable[..., dict[str, Any]] = send_account_invitation_email,
        throttle: Throttle = consume_lookup,
        new_token: Callable[[], str] = new_invitation_token,
    ) -> None:
        self._get_logger = get_logger
        self._audit = audit
        self._send_invitation = send_invitation
        self._throttle = throttle
        self._new_token = new_token

    def _logger(self) -> Any:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError("Database not configured")
        return bl

    @staticmethod
    def _managed(account_id: str, managed_account_ids: list[str]) -> str:
        account = _text(account_id)
        if not account or account not in {_text(item) for item in managed_account_ids}:
            raise ServiceForbiddenError("account access denied")
        return account

    def invite(
        self,
        *,
        email: str,
        role: str,
        account_id: str,
        managed_account_ids: list[str],
        admin_user_id: str,
        actor: str,
        link_base: str = "",
        locale: str = "",
    ) -> dict[str, str]:
        """Invite ``email`` to the account; the answer never says whether they exist.

        The link goes out by system email. When it cannot (no system email, or the
        send failed), the answer carries ``invitation_link`` once, for the
        administrator to deliver; it is never listed again.
        """
        account = self._managed(account_id, managed_account_ids)
        key = _text(role).lower()
        if key not in SYSTEM_ROLE_LEVELS:
            raise ServiceValidationError("role must be one of admin, operator or viewer")
        typed, address = invited_address(email)
        token = self._new_token()
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            self._throttle(
                cur,
                invitation_limits(
                    admin_user_id=admin_user_id, account_id=account, recipient=address
                ),
            )
            expired = delete_expired(cur, account)
            stored = invite(
                cur,
                account_id=account,
                email=typed,
                role=key,
                invited_by=admin_user_id,
                token_hash=invitation_token_hash(token),
            )
            company, inviter = invitation_names(cur, account_id=account, inviter_id=admin_user_id)
        if expired:
            self._audit(
                bl,
                action="account_invitation_expire",
                username="",
                actor=actor,
                status="ok",
                detail={"account_id": account, "invitations": expired},
            )
        link = f"{_text(link_base).rstrip('/')}/invitations/{token}"
        try:
            delivery = self._send_invitation(
                recipient=address,
                inviter=inviter or actor,
                company=company or account,
                role=key,
                invitation_url=link,
                expires_in_days=INVITATION_LIFETIME_DAYS,
                locale=locale,
            )
        except Exception:
            delivery = {"ok": False, "reason": "send_failed"}
        emailed = bool((delivery or {}).get("ok"))
        self._audit(
            bl,
            action="account_member_invite",
            username="",
            actor=actor,
            status="ok",
            detail={
                "account_id": account,
                "invitation_id": stored["id"],
                "invited_email": typed,
                "role": key,
                "invited_by": _text(admin_user_id),
                "expires_at": stored["expires_at"],
                "delivery": "email" if emailed else "link",
                "email_reason": _text((delivery or {}).get("reason")),
            },
        )
        answer = {"status": "ok", "message": INVITATION_SENT_MESSAGE}
        if emailed:
            answer["delivery"] = "email"
        else:
            answer["delivery"] = "link"
            answer["invitation_link"] = link
        return answer

    def sent(self, *, account_id: str, managed_account_ids: list[str]) -> list[dict[str, Any]]:
        """The acting account's open invitations, for its administrators."""
        account = self._managed(account_id, managed_account_ids)
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            return sent_invitations(cur, account)

    def cancel(
        self,
        *,
        invitation_id: str,
        account_id: str,
        managed_account_ids: list[str],
        actor: str,
    ) -> dict[str, str]:
        """Cancel an open invitation of the acting account."""
        account = self._managed(account_id, managed_account_ids)
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            cancelled = cancel(cur, account_id=account, invitation_id=invitation_id)
        self._audit(
            bl,
            action="account_invitation_cancel",
            username="",
            actor=actor,
            status="ok",
            detail={
                "account_id": account,
                "invitation_id": cancelled["id"],
                "invited_email": cancelled["invited_email"],
                "role": cancelled["role"],
                "reason": "cancelled_by_administrator",
            },
        )
        return {"id": cancelled["id"], "status": "cancelled"}

    def token_is_open(self, *, token: str) -> bool:
        """Whether ``token`` names an open invitation (sign-in without a role, P5)."""
        if not _text(token):
            return False
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            return _open_invitation(cur, invitation_token_hash(token), lock=False) is not None

    def preview(self, *, token: str) -> dict[str, str]:
        """What the link invites to, for the signed-in person who opened it."""
        if not _text(token):
            raise ServiceNotFoundError(INVITATION_INVALID_MESSAGE)
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            return preview(cur, invitation_token_hash(token))

    def decide(
        self,
        *,
        token: str,
        user_id: str,
        username: str,
        accept: bool,
        role: str = "",
    ) -> dict[str, Any]:
        """Accept (with the role shown) or decline the invitation the link names."""
        if not _text(token) or not _text(user_id):
            raise ServiceNotFoundError(INVITATION_INVALID_MESSAGE)
        bl = self._logger()
        with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
            result = decide(
                cur,
                token_hash=invitation_token_hash(token),
                user_id=user_id,
                accept=accept,
                expected_role=role,
            )
        self._audit(
            bl,
            action="account_invitation_accept" if accept else "account_invitation_decline",
            username=username,
            actor=username or "auth",
            status="ok",
            detail={
                "invitation_id": result["id"],
                "account_id": result["account_id"],
                "role": result["role"],
                "user_id": _text(user_id),
            },
        )
        return result


__all__ = [
    "INVITATION_CHANGED_MESSAGE",
    "INVITATION_INVALID_MESSAGE",
    "INVITATION_LIFETIME_DAYS",
    "INVITATION_SENT_MESSAGE",
    "AccountInvitationService",
    "cancel",
    "cancel_invitations_of_former_admins",
    "decide",
    "delete_expired",
    "invitation_names",
    "invitation_token_hash",
    "invite",
    "invited_address",
    "new_invitation_token",
    "preview",
    "sent_invitations",
]
