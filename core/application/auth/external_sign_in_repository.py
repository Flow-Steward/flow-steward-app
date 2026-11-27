"""SQL for external sign-in (ADR 0083): sign-in states, identities, sessions.

Every function takes a cursor of a connection the caller holds in one transaction.
Rows come back as dictionaries whatever the cursor factory.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

#: Seconds a sign-in attempt may take between start and callback.
STATE_TTL_SECONDS = 600
#: The password hash of a user who has no local password. It never verifies.
UNUSABLE_PASSWORD_MARKER = "!external-sign-in"

#: ``auth_sessions.auth_method`` of a session issued by external sign-in.
EXTERNAL_AUTH_METHOD = "external"


#: The same, for a person created only to answer an invitation who has not accepted
#: yet (no company, no role). Accepting turns it into :data:`UNUSABLE_PASSWORD_MARKER`;
#: a password the person sets with Forgot password replaces it like any other.
AWAITING_INVITATION_MARKER = "!external-sign-in:awaiting-invitation"
#: Every password hash that never verifies.
UNUSABLE_PASSWORD_MARKERS = (UNUSABLE_PASSWORD_MARKER, AWAITING_INVITATION_MARKER)


def is_unusable_password(password_hash: Any) -> bool:
    return str(password_hash or "") in UNUSABLE_PASSWORD_MARKERS


def is_awaiting_invitation(password_hash: Any) -> bool:
    """A person created only to answer an invitation, who has not accepted it yet."""
    return str(password_hash or "") == AWAITING_INVITATION_MARKER


def _rows(cur: Any) -> list[dict[str, Any]]:
    rows = cur.fetchall() or []
    names = [column[0] for column in (cur.description or [])]
    return [
        dict(row) if isinstance(row, Mapping) else dict(zip(names, row, strict=False))
        for row in rows
    ]


def _row(cur: Any) -> dict[str, Any] | None:
    rows = _rows(cur)
    return rows[0] if rows else None


def is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value or ""))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


# --- sign-in states -------------------------------------------------------------------


def insert_state(
    cur: Any,
    *,
    state_hash: str,
    method: str,
    option_key: str,
    browser_binding_hash: str,
    method_data_encrypted: str,
    redirect_uri: str,
    return_path: str,
    link_user_id: str | None,
) -> None:
    # Attempts nobody finished are kept a day for the audit trail, then dropped.
    cur.execute("DELETE FROM external_sign_in_states WHERE expires_at < now() - interval '1 day'")
    cur.execute(
        """
        INSERT INTO external_sign_in_states (
            state_hash, method, option_key, browser_binding_hash, method_data_encrypted,
            redirect_uri, return_path, link_user_id, expires_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
        """,
        (
            state_hash,
            method,
            option_key,
            browser_binding_hash,
            method_data_encrypted,
            redirect_uri,
            return_path,
            link_user_id,
            STATE_TTL_SECONDS,
        ),
    )


def consume_state(cur: Any, state_hash: str) -> dict[str, Any] | None:
    """Mark the attempt used and return it; ``None`` when unknown, used or expired."""
    cur.execute(
        """
        UPDATE external_sign_in_states
        SET consumed_at = now()
        WHERE state_hash = %s AND consumed_at IS NULL AND expires_at > now()
        RETURNING method, option_key, browser_binding_hash, method_data_encrypted,
                  redirect_uri, return_path, link_user_id::text AS link_user_id
        """,
        (state_hash,),
    )
    return _row(cur)


# --- identities -----------------------------------------------------------------------


def find_identity(cur: Any, *, account_id: str, issuer: str, subject: str) -> dict[str, Any] | None:
    """The link of ``(issuer, subject)`` made in ``account_id``.

    An identity is unique within an account, not across the installation (ADR 0083):
    one account can never block, or reach, another account's link.
    """
    if not is_uuid(account_id):
        return None
    cur.execute(
        """
        SELECT id::text AS id, user_id::text AS user_id, account_id::text AS account_id,
               method, option_key
        FROM external_identities
        WHERE account_id = %s AND issuer = %s AND subject = %s
        """,
        (account_id, issuer, subject),
    )
    return _row(cur)


def find_identities(cur: Any, *, issuer: str, subject: str) -> list[dict[str, Any]]:
    """Every link of ``(issuer, subject)``, in any account.

    Only for a method that cannot name the account before sign-in: more than one row
    means the identity is ambiguous and the caller refuses it.
    """
    cur.execute(
        """
        SELECT id::text AS id, user_id::text AS user_id, account_id::text AS account_id,
               method, option_key
        FROM external_identities
        WHERE issuer = %s AND subject = %s
        LIMIT 2
        """,
        (issuer, subject),
    )
    return _rows(cur)


def link_identity(
    cur: Any,
    *,
    account_id: str,
    user_id: str,
    method: str,
    option_key: str,
    issuer: str,
    subject: str,
    created_by: str,
) -> dict[str, Any]:
    """Link ``(issuer, subject)`` to ``user_id`` in ``account_id``.

    Refuses an identity the account already linked to someone else.
    """
    cur.execute(
        """
        INSERT INTO external_identities (
            account_id, method, option_key, issuer, subject, user_id, created_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (account_id, issuer, subject) DO NOTHING
        """,
        (account_id, method, option_key, issuer, subject, user_id, created_by),
    )
    row = find_identity(cur, account_id=account_id, issuer=issuer, subject=subject)
    if row is None or str(row.get("user_id")) != str(user_id):
        raise IdentityLinkedElsewhereError("The external identity is linked to another user")
    return row


def list_identities(cur: Any, *, account_id: str) -> list[dict[str, Any]]:
    """The external identities that belong to ``account_id``.

    An identity belongs to the account it was linked in. A person's membership in an
    account does not show that account's administrator the links another account
    made (ADR 0083).
    """
    if not is_uuid(account_id):
        return []
    cur.execute(
        """
        SELECT ei.id::text AS id, ei.method, ei.option_key, ei.issuer, ei.subject,
               ei.user_id::text AS user_id, u.username, ei.created_at, ei.last_login_at
        FROM external_identities ei
        JOIN users u ON u.id = ei.user_id
        WHERE ei.account_id = %s
        ORDER BY u.username, ei.issuer, ei.subject
        """,
        (account_id,),
    )
    return _rows(cur)


def delete_identity(cur: Any, identity_id: str, *, account_id: str) -> bool:
    if not (is_uuid(identity_id) and is_uuid(account_id)):
        return False
    cur.execute(
        "DELETE FROM external_identities WHERE id = %s AND account_id = %s",
        (identity_id, account_id),
    )
    return int(getattr(cur, "rowcount", 0) or 0) > 0


def delete_identities_of_user(cur: Any, user_id: str) -> int:
    cur.execute("DELETE FROM external_identities WHERE user_id = %s", (user_id,))
    return int(getattr(cur, "rowcount", 0) or 0)


def touch_identity(cur: Any, identity_id: str) -> None:
    cur.execute(
        "UPDATE external_identities SET last_login_at = now() WHERE id = %s", (identity_id,)
    )


class IdentityLinkedElsewhereError(Exception):
    """The external identity already names another user."""


# --- users and sessions ---------------------------------------------------------------


def load_user(cur: Any, user_id: str) -> dict[str, Any] | None:
    if not is_uuid(user_id):
        return None
    cur.execute(
        """
        SELECT id::text AS id, username, display_name, is_active, account_id::text AS account_id
        FROM users
        WHERE id = %s AND deleted_at IS NULL
        """,
        (user_id,),
    )
    return _row(cur)


def user_in_account(cur: Any, *, user_id: str, account_id: str) -> bool:
    """The user is a member of the account, and the account is active."""
    if not (is_uuid(user_id) and is_uuid(account_id)):
        return False
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM users u
            JOIN accounts a ON a.id = %s AND a.is_active
            WHERE u.id = %s AND u.deleted_at IS NULL
              AND (u.account_id = a.id OR EXISTS (
                   SELECT 1 FROM user_account_memberships m
                   WHERE m.user_id = u.id AND m.account_id = a.id))
        )
        """,
        (account_id, user_id),
    )
    return _flag(cur.fetchone())


def _flag(row: Any) -> bool:
    if isinstance(row, Mapping):
        return bool(next(iter(row.values()), False))
    return bool(row[0]) if row else False


#: Roles an external identity never signs in as, and a module never links or grants to.
ADMIN_ROLE_KEYS = ("admin", "system_actor")

#: The refusal code of :func:`external_person_refusal`.
REFUSED_ADMIN = "admin_not_external"


def user_holds_admin_role(cur: Any, user_id: str) -> bool:
    """The person holds an administrator role in any account, or a scoped one.

    Roles are held per account (ADR 0085); this probe deliberately means "anywhere":
    an administrator of any account is never reached through an external identity.
    """
    if not is_uuid(user_id):
        return False
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM user_account_memberships m JOIN roles r ON r.id = m.role_id
            WHERE m.user_id = %s AND r.key = ANY(%s)
        ) OR EXISTS (
            SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_id = %s AND r.key = ANY(%s)
        )
        """,
        (user_id, list(ADMIN_ROLE_KEYS), user_id, list(ADMIN_ROLE_KEYS)),
    )
    return _flag(cur.fetchone())


def user_holds_installation_admin_role(cur: Any, user_id: str) -> bool:
    """The person is an administrator of the installation account, or holds a scoped
    administrator role anywhere."""
    if not is_uuid(user_id):
        return False
    from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID

    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM user_account_memberships m JOIN roles r ON r.id = m.role_id
            WHERE m.user_id = %s AND CAST(m.account_id AS TEXT) = %s AND r.key = ANY(%s)
        ) OR EXISTS (
            SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_id = %s AND r.key = ANY(%s)
        )
        """,
        (
            user_id,
            str(INSTALLATION_ACCOUNT_ID),
            list(ADMIN_ROLE_KEYS),
            user_id,
            list(ADMIN_ROLE_KEYS),
        ),
    )
    return _flag(cur.fetchone())


def external_person_refusal(
    cur: Any,
    *,
    user_id: str,
    account_id: str,
    allow_admin: bool = False,
    allow_account_admin: bool = False,
) -> str | None:
    """Why an identity of ``account_id`` may not sign in as, or be linked to, a person.

    The caller has checked that the person is a member of ``account_id``
    (:func:`user_in_account`). Roles are held per account (ADR 0085) and an external
    session is pinned to its account, so the person's memberships of other accounts
    no longer matter (ADR 0083 amendment of 2026-11-27). ``None`` unless the person
    is an administrator of any account, the installation account included, or holds
    a scoped administrator role; ``allow_admin`` (a role grant, which never removes
    or raises an administrator) waives that check. ``allow_account_admin`` (an
    identity of an installation-wide sign-in method: the person's own sign-in, set up
    by the installation and not by one account) waives it for administrators of
    ordinary accounts; an administrator of the installation account, or a scoped
    administrator, is still refused.
    """
    del account_id
    if allow_admin:
        return None
    if allow_account_admin:
        return REFUSED_ADMIN if user_holds_installation_admin_role(cur, user_id) else None
    if user_holds_admin_role(cur, user_id):
        return REFUSED_ADMIN
    return None


def account_id_for_hint(cur: Any, hint: str) -> str | None:
    """The active account a sign-in page hint names: its id or its key (slug)."""
    text = str(hint or "").strip()
    if not text or len(text) > 255:
        return None
    if is_uuid(text):
        cur.execute("SELECT id::text AS id FROM accounts WHERE id = %s AND is_active", (text,))
    else:
        cur.execute(
            "SELECT id::text AS id FROM accounts"
            " WHERE lower(account_key) = lower(%s) AND is_active LIMIT 2",
            (text,),
        )
    rows = _rows(cur)
    if len(rows) != 1:
        return None
    return str(rows[0].get("id") or "") or None


def insert_session(
    cur: Any,
    *,
    user_id: str,
    account_id: str,
    token_hash: str,
    ttl_hours: int,
    external_identity_id: str,
) -> None:
    """Insert a session issued by external sign-in (``auth_method`` 'external')."""
    cur.execute(
        """
        INSERT INTO auth_sessions (
            user_id, account_id, session_token_hash, expires_at, revoked,
            auth_method, external_identity_id
        )
        VALUES (%s, %s, %s, now() + make_interval(hours => %s), false, %s, %s)
        """,
        (
            user_id,
            account_id,
            token_hash,
            int(ttl_hours),
            EXTERNAL_AUTH_METHOD,
            external_identity_id,
        ),
    )


def session_is_recent(cur: Any, token_hash: str, max_age_seconds: int) -> bool:
    """The local session ``token_hash`` names is live and was started (by entering
    the password) at most ``max_age_seconds`` ago."""
    cur.execute(
        """
        SELECT created_at > now() - make_interval(secs => %s)
          FROM auth_sessions
         WHERE session_token_hash = %s AND revoked = false AND expires_at > now()
        """,
        (int(max_age_seconds), str(token_hash or "")),
    )
    return _flag(cur.fetchone())
