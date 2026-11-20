"""Local multi-factor authentication use cases shared by settings and sign-in.

Settings operations bind state changes to a live browser session and run atomically.
Sign-in callers pass their transaction cursor to factor verification.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypedDict

from cryptography.exceptions import InvalidTag

from core.application.auth import mfa_codes, mfa_repository
from core.application.auth.local_credentials import installation_requires_mfa, lock_local_user
from core.infrastructure.db.db_pool import transaction_connection
from core.infrastructure.security.secret_codec import (
    decrypt_secret,
    derive_secret_storage_key,
    encrypt_secret,
)

_ENROLLMENT_TTL = timedelta(minutes=10)


class TotpEnrollment(TypedDict):
    secret: str
    uri: str


def _now() -> datetime:
    return datetime.now(UTC)


def _encryption_key(user_id: str) -> bytes:
    return derive_secret_storage_key(
        f"flow-steward.local-authenticator.v1:{user_id.lower()}".encode()
    )


def _pending_encryption_key(user_id: str, session_id: str) -> bytes:
    context = f"flow-steward.pending-authenticator.v1:{user_id.lower()}:{session_id.lower()}"
    return derive_secret_storage_key(context.encode())


def mfa_enabled(cur: Any, user_id: str) -> bool:
    """Check whether the account has a verified authenticator."""
    return mfa_repository.active_factor_exists(cur, user_id)


def verify_mfa(cur: Any, user_id: str, code: str) -> bool:
    """Consume a fresh authenticator step or a single recovery code."""
    code = code.strip()
    if not mfa_codes.is_totp_code(code):
        return mfa_codes.is_recovery_code(code) and mfa_repository.consume_recovery_code(
            cur, user_id, mfa_codes.recovery_code_hash(code)
        )

    factor = mfa_repository.lock_active_factor(cur, user_id)
    if factor is None:
        return False
    encrypted_secret, last_used_step = factor
    secret = decrypt_secret(encrypted_secret, _encryption_key(user_id))
    step = mfa_codes.matching_totp_step(secret, code, now=_now())
    if step is None or (last_used_step is not None and step <= last_used_step):
        return False
    return mfa_repository.advance_totp_step(cur, user_id, step)


def begin_mfa_enrollment(
    cur: Any, *, user_id: str, username: str, session_id: str
) -> TotpEnrollment:
    """Create a short-lived pending authenticator and return its provisioning details."""
    secret = mfa_codes.new_totp_secret()
    secret_enc = encrypt_secret(secret, _pending_encryption_key(user_id, session_id))
    mfa_repository.store_pending_factor(cur, user_id, secret_enc, _now() + _ENROLLMENT_TTL)
    return {"secret": secret, "uri": mfa_codes.provisioning_uri(secret, username)}


def complete_mfa_enrollment(
    cur: Any, *, user_id: str, code: str, session_id: str
) -> list[str] | None:
    """Activate the pending authenticator and issue recovery codes once."""
    code = code.strip()
    if not mfa_codes.is_totp_code(code):
        return None
    now = _now()
    pending = mfa_repository.lock_pending_factor(cur, user_id, now)
    if pending is None:
        return None
    try:
        secret = decrypt_secret(pending, _pending_encryption_key(user_id, session_id))
    except InvalidTag:
        return None
    step = mfa_codes.matching_totp_step(secret, code, now=now)
    if step is None:
        return None

    recovery_codes = mfa_codes.new_recovery_codes()
    recovery_hashes = [mfa_codes.recovery_code_hash(code) for code in recovery_codes]
    active_secret_enc = encrypt_secret(secret, _encryption_key(user_id))
    mfa_repository.activate_pending_factor(cur, user_id, active_secret_enc, step)
    mfa_repository.replace_recovery_codes(cur, user_id, recovery_hashes)
    return recovery_codes


def disable_mfa(cur: Any, user_id: str) -> None:
    """Remove pending and active factors and all associated recovery codes."""
    mfa_repository.delete_factor(cur, user_id)


AuthenticatorOperation = Literal["enroll", "verify", "disable"]
_Outcome = Literal["ok", "inactive_session", "required", "invalid_proof", "throttled"]


@dataclass(frozen=True)
class AuthenticatorChange:
    outcome: _Outcome
    payload: dict[str, Any] | None = None
    retry_after_seconds: int = 0


def authenticator_settings_status(
    bl: Any, *, user_id: str, cookie_hash: str
) -> dict[str, bool] | None:
    """Read policy and factor state only for a live cookie session."""
    with transaction_connection(bl) as conn, conn.cursor() as cur:
        if mfa_repository.active_cookie_session_id(cur, user_id, cookie_hash) is None:
            return None
        return {"enabled": mfa_enabled(cur, user_id), "required": installation_requires_mfa()}


def update_authenticator_settings(
    bl: Any,
    *,
    operation: AuthenticatorOperation,
    user_id: str,
    username: str,
    password_hash: str,
    cookie_hash: str,
    code: str,
    verify_current_password: Callable[[], bool],
    check_attempts: Callable[[], tuple[bool, int]],
    record_failure: Callable[[], tuple[bool, int]],
) -> AuthenticatorChange:
    """Change a verified user's authenticator and preserve their current session."""
    with transaction_connection(bl) as conn, conn.cursor() as cur:
        if not lock_local_user(cur, user_id=user_id, password_hash=password_hash):
            return AuthenticatorChange("inactive_session")
        session_id = mfa_repository.active_cookie_session_id(cur, user_id, cookie_hash, lock=True)
        if session_id is None:
            return AuthenticatorChange("inactive_session")
        if operation == "disable" and installation_requires_mfa():
            return AuthenticatorChange("required")
        locked, retry_after = check_attempts()
        if locked:
            return AuthenticatorChange("throttled", retry_after_seconds=retry_after)

        payload = _apply_authenticator_change(
            cur,
            operation=operation,
            user_id=user_id,
            username=username,
            session_id=session_id,
            code=code,
            verify_current_password=verify_current_password,
        )
        if payload is None:
            locked, retry_after = record_failure()
            return AuthenticatorChange(
                "throttled" if locked else "invalid_proof", retry_after_seconds=retry_after
            )
        if operation != "enroll":
            mfa_repository.revoke_other_sessions(cur, user_id, session_id)
        return AuthenticatorChange("ok", payload=payload)


def _apply_authenticator_change(
    cur: Any,
    *,
    operation: AuthenticatorOperation,
    user_id: str,
    username: str,
    session_id: str,
    code: str,
    verify_current_password: Callable[[], bool],
) -> dict[str, Any] | None:
    if operation == "verify":
        codes = complete_mfa_enrollment(cur, user_id=user_id, code=code, session_id=session_id)
        return {"recovery_codes": codes} if codes is not None else None

    if not verify_current_password():
        return None
    enabled = mfa_enabled(cur, user_id)
    if enabled and not verify_mfa(cur, user_id, code):
        return None
    if operation == "enroll":
        enrollment = begin_mfa_enrollment(
            cur, user_id=user_id, username=username, session_id=session_id
        )
        return dict(enrollment)

    disable_mfa(cur, user_id)
    return {"enabled": False}
