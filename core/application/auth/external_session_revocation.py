"""Revoke what external sign-in issued when the person rules may no longer hold (ADR 0083).

External sign-in admits only a member of the issuing account who is an administrator
of no account. Those rules are checked when the session is issued; a later change of
the person's roles or account memberships, or disabling the person or their account,
could leave a session that would no longer be issued. Such a change
therefore revokes the person's sessions from external sign-in and the personal tokens
of a person who signs in externally. Core does not record which session minted a token,
so every personal token of such a person is revoked: they sign in again and mint new
ones under the rules that hold now.

The caller owns the transaction (``cur``); the revocation commits or rolls back with
the change that caused it. Storage without external sign-in has nothing to revoke.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: ``auth_sessions.auth_method`` of a session issued by external sign-in.
_EXTERNAL_AUTH_METHOD = "external"

_STORAGE_SQL = """
SELECT to_regclass('public.external_identities') IS NOT NULL
   AND EXISTS (
       SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'auth_sessions'
          AND column_name = 'auth_method'
   ) AS available
"""

#: Personal tokens of people who sign in externally: a linked identity or a session
#: that external sign-in issued (revoked or not, it may have minted a token).
_REVOKE_TOKENS_SQL = """
UPDATE personal_access_tokens AS t
   SET revoked = true, updated_at = now()
 WHERE t.user_id::text = ANY(%s::text[])
   AND t.revoked = false
   AND (
       EXISTS (SELECT 1 FROM external_identities ei WHERE ei.user_id = t.user_id)
       OR EXISTS (
           SELECT 1 FROM auth_sessions s
            WHERE s.user_id = t.user_id AND s.auth_method = %s
       )
   )
"""

_REVOKE_SESSIONS_SQL = """
UPDATE auth_sessions
   SET revoked = true, updated_at = now()
 WHERE user_id::text = ANY(%s::text[])
   AND auth_method = %s
   AND revoked = false
"""

#: The same two statements limited to one account (ADR 0085, owner decision B): a role
#: change in one account ends only what external sign-in issued in that account.
_REVOKE_ACCOUNT_TOKENS_SQL = """
UPDATE personal_access_tokens AS t
   SET revoked = true, updated_at = now()
 WHERE t.user_id::text = ANY(%s::text[])
   AND t.account_id::text = %s
   AND t.revoked = false
   AND (
       EXISTS (
           SELECT 1 FROM external_identities ei
            WHERE ei.user_id = t.user_id AND ei.account_id = t.account_id
       )
       OR EXISTS (
           SELECT 1 FROM auth_sessions s
            WHERE s.user_id = t.user_id AND s.account_id = t.account_id
              AND s.auth_method = %s
       )
   )
"""

_REVOKE_ACCOUNT_SESSIONS_SQL = """
UPDATE auth_sessions
   SET revoked = true, updated_at = now()
 WHERE user_id::text = ANY(%s::text[])
   AND account_id::text = %s
   AND auth_method = %s
   AND revoked = false
"""

_ACCOUNT_USERS_SQL = """
SELECT u.id::text AS user_id FROM users u WHERE u.account_id::text = %s
UNION
SELECT m.user_id::text AS user_id FROM user_account_memberships m WHERE m.account_id::text = %s
"""


def _first(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    try:
        return row[0]
    except (IndexError, KeyError, TypeError):
        return None


def _rows(cur: Any) -> list[Any]:
    try:
        return list(cur.fetchall() or ())
    except Exception:
        return []


def external_sign_in_storage_available(cur: Any) -> bool:
    cur.execute(_STORAGE_SQL)
    return _first(cur.fetchone()) is True


def revoke_external_sign_in_grants(
    cur: Any, user_ids: Iterable[Any], *, account_id: Any = None
) -> bool:
    """Revoke the people's external sessions and, for people who sign in externally,
    their personal tokens. Returns whether storage had external sign-in to revoke.

    With ``account_id`` only the sessions and tokens of that account go: a change of
    the person's role in one account leaves what they hold in another (ADR 0085).
    """
    ids = sorted({str(user_id or "").strip() for user_id in user_ids} - {""})
    if not ids or not external_sign_in_storage_available(cur):
        return False
    account = str(account_id or "").strip()
    if account:
        cur.execute(_REVOKE_ACCOUNT_TOKENS_SQL, (ids, account, _EXTERNAL_AUTH_METHOD))
        cur.execute(_REVOKE_ACCOUNT_SESSIONS_SQL, (ids, account, _EXTERNAL_AUTH_METHOD))
        return True
    cur.execute(_REVOKE_TOKENS_SQL, (ids, _EXTERNAL_AUTH_METHOD))
    cur.execute(_REVOKE_SESSIONS_SQL, (ids, _EXTERNAL_AUTH_METHOD))
    return True


def revoke_external_sign_in_grants_of_account(cur: Any, account_id: Any) -> bool:
    """The same for every person who lives in or belongs to ``account_id``."""
    account = str(account_id or "").strip()
    if not account or not external_sign_in_storage_available(cur):
        return False
    cur.execute(_ACCOUNT_USERS_SQL, (account, account))
    user_ids = [_first(row) for row in _rows(cur)]
    return revoke_external_sign_in_grants(cur, user_ids)


def _texts(values: Iterable[Any] | None) -> frozenset[str]:
    return frozenset(str(value or "").strip().lower() for value in values or ()) - {""}


def membership_account_ids(user: Mapping[str, Any]) -> frozenset[str] | None:
    """The accounts a loaded user payload names, or ``None`` when it names none."""
    if "account_ids" not in user and "account_memberships" not in user:
        return None
    names: list[Any] = []
    if "account_ids" in user:
        names.extend(user.get("account_ids") or [])
    for membership in user.get("account_memberships") or []:
        if isinstance(membership, Mapping):
            names.append(membership.get("account_id"))
    if user.get("account_id"):
        names.append(user.get("account_id"))
    return _texts(names)


def roles_changed(before: Iterable[Any] | None, after: Iterable[Any]) -> bool:
    """Whether the global roles differ; unknown roles before count as a change."""
    if before is None:
        return True
    return _texts(before) != _texts(after)


def accounts_changed(before: frozenset[str] | None, after: Iterable[Any]) -> bool:
    """Whether the account memberships differ; unknown ones before count as a change."""
    if before is None:
        return True
    return before != _texts(after)


class ExternalSignInRevocationPort:
    """The revocation helpers handed to a platform module's control hook (ADR 0082).

    A module that changes who may sign in externally (a background sync of people,
    roles or accounts) revokes what external sign-in issued in the same transaction
    as the change, through the cursor it owns.
    """

    __slots__ = ()

    @staticmethod
    def storage_available(cur: Any) -> bool:
        return external_sign_in_storage_available(cur)

    @staticmethod
    def revoke_grants(cur: Any, user_ids: Iterable[Any]) -> bool:
        return revoke_external_sign_in_grants(cur, user_ids)

    @staticmethod
    def revoke_grants_of_account(cur: Any, account_id: Any) -> bool:
        return revoke_external_sign_in_grants_of_account(cur, account_id)

    @staticmethod
    def revoke_access_in_account(cur: Any, *, user_id: Any, account_id: Any) -> int:
        """End the person's sessions, personal tokens and OAuth grants of one account
        (a directory sync that removed or lowered their role there, ADR 0085). Refused
        for the account's owner (``AccountOwnerProtectedError``), whose role no sync
        changes."""
        from core.application.auth.membership_roles import revoke_account_access

        return revoke_account_access(
            cur,
            user_id=str(user_id or ""),
            account_id=str(account_id or ""),
            reason="account_role_changed",
        )


__all__ = [
    "ExternalSignInRevocationPort",
    "accounts_changed",
    "external_sign_in_storage_available",
    "membership_account_ids",
    "revoke_external_sign_in_grants",
    "revoke_external_sign_in_grants_of_account",
    "roles_changed",
]
