"""Password reuse validation for transactions holding the user's credential lock."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.auth.password import verify_password
from core.application.management.errors import ServiceValidationError

_PASSWORD_REUSE_MESSAGE = (
    "Choose a password different from your current password and the last 10 passwords."
)


class PasswordReuseError(ServiceValidationError):
    """The password matches an active or recently retired credential."""


def require_unused_password(
    cur: Any,
    *,
    user_id: str,
    password: str,
    current_hash: str,
    verify: Callable[[str, str], bool] = verify_password,
) -> None:
    if current_hash and verify(password, current_hash):
        raise PasswordReuseError(_PASSWORD_REUSE_MESSAGE)
    cur.execute(
        """
        SELECT password_hash
        FROM auth_password_history
        WHERE user_id = %s
        ORDER BY id DESC
        LIMIT 10
        """,
        (user_id,),
    )
    for row in cur.fetchall():
        previous_hash = row["password_hash"] if isinstance(row, dict) else row[0]
        if verify(password, previous_hash):
            raise PasswordReuseError(_PASSWORD_REUSE_MESSAGE)
