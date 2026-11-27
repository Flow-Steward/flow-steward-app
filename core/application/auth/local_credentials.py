"""Storage checks and session conditions for local credentials."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.application.auth.config import env_flag
from core.application.auth.password_policy import PASSWORD_MAX_AGE_DAYS


def installation_requires_mfa() -> bool:
    return env_flag("FS_AUTH_MFA_REQUIRED", False)


def authenticator_storage_available(bl: Any) -> bool:
    try:
        return (
            bl._table_exists("auth_totp_factors") is True
            and bl._table_exists("auth_mfa_recovery_codes") is True
        )
    except AttributeError:
        return False


def authenticator_policy_available(bl: Any) -> bool:
    try:
        return (
            bl._column_exists("users", "authenticator_required") is True
            and bl._column_exists("users", "authenticator_reset_pending") is True
        )
    except AttributeError:
        return False


@dataclass(frozen=True)
class UserAuthenticatorPolicy:
    required: bool = False
    reset_pending: bool = False

    @property
    def enforced(self) -> bool:
        return self.required or self.reset_pending


def read_authenticator_policy(cur: Any, user_id: str) -> UserAuthenticatorPolicy:
    cur.execute(
        "SELECT authenticator_required, authenticator_reset_pending FROM users WHERE id = %s",
        (user_id,),
    )
    row = cur.fetchone()
    if row is None:
        return UserAuthenticatorPolicy()
    if isinstance(row, dict):
        return UserAuthenticatorPolicy(
            row["authenticator_required"], row["authenticator_reset_pending"]
        )
    return UserAuthenticatorPolicy(*row)


def password_age_available(bl: Any) -> bool:
    try:
        return bl._column_exists("users", "password_changed_at") is True
    except AttributeError:
        return False


def session_auth_method_available(bl: Any) -> bool:
    try:
        return bl._column_exists("auth_sessions", "auth_method") is True
    except AttributeError:
        return False


#: The value of ``auth_sessions.auth_method`` for a session issued by a local password.
LOCAL_AUTH_METHOD = "local"


def local_session_credential_filter(bl: Any, *, session_alias: str = "s") -> str:
    """SQL conditions a session must meet for its user's local credentials.

    Password age and the local authenticator rules belong to the local password:
    a session issued by external sign-in (ADR 0083) is not held to them. The
    sign-in method authenticated that person; the user and account checks of
    the session lookup still apply.
    """
    if not session_alias.isidentifier():
        raise ValueError("session_alias must be an SQL identifier")
    local = _local_credential_conditions(bl)
    if not local or not session_auth_method_available(bl):
        return local
    return f" AND ({session_alias}.auth_method <> '{LOCAL_AUTH_METHOD}' OR (TRUE{local}))"


#: ``auth_sessions.auth_method`` of a session issued by external sign-in (ADR 0083).
EXTERNAL_AUTH_METHOD = "external"


def session_auth_method_select(bl: Any, *, session_alias: str = "s") -> str:
    """SELECT fragment naming how a session was issued, ``auth_method``.

    Before the column exists every session is local.
    """
    if not session_alias.isidentifier():
        raise ValueError("session_alias must be an SQL identifier")
    if session_auth_method_available(bl):
        return f"{session_alias}.auth_method AS auth_method"
    return f"'{LOCAL_AUTH_METHOD}'::text AS auth_method"


def apply_session_auth_source(payload: dict[str, Any]) -> dict[str, Any]:
    """Mark a session issued by external sign-in as ``auth_source`` 'external'.

    A session lookup selects ``auth_method``; this replaces it with the principal's
    ``auth_source``. A local session keeps the caller's default ('local'), so the
    local-only checks (linking a sign-in, changing the email address, managing the
    authenticator, the local password rules) never apply to an external session as
    if it were local (ADR 0083).
    """
    method = str(payload.pop("auth_method", "") or "").strip().lower()
    if method == EXTERNAL_AUTH_METHOD:
        payload["auth_source"] = "external"
    return payload


def _local_credential_conditions(bl: Any) -> str:
    conditions = []
    if password_age_available(bl):
        conditions.append(
            f" AND u.password_changed_at > now() - make_interval(hours => {PASSWORD_MAX_AGE_DAYS * 24})"
        )
    individual_policy = authenticator_policy_available(bl)
    if individual_policy:
        conditions.append(" AND NOT u.authenticator_reset_pending")
    installation_policy = installation_requires_mfa()
    if installation_policy or individual_policy:
        verified_factor = (
            "EXISTS (SELECT 1 FROM auth_totp_factors f"
            " WHERE f.user_id = u.id AND f.secret_enc IS NOT NULL)"
            if authenticator_storage_available(bl)
            else "false"
        )
        conditions.append(
            f" AND {verified_factor}"
            if installation_policy
            else f" AND (NOT u.authenticator_required OR {verified_factor})"
        )
    return "".join(conditions)


def lock_local_user(
    cur: Any, *, user_id: str, password_hash: str, must_change_password: bool | None = None
) -> bool:
    """Serialize session issue, factor changes, and password changes for this user."""
    query = (
        "SELECT id FROM users WHERE id = %s AND password_hash = %s"
        " AND is_active AND deleted_at IS NULL"
    )
    params: tuple = (user_id, password_hash)
    if must_change_password is not None:
        query += " AND must_change_password = %s"
        params += (must_change_password,)
    cur.execute(query + " FOR UPDATE", params)
    return cur.fetchone() is not None
