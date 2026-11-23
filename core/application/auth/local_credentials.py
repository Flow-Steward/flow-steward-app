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


def local_session_credential_filter(bl: Any) -> str:
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
