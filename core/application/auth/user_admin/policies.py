"""Validation, selection, and authorization policies for user administration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.auth.config import AUTH_PASSWORD_MIN_LENGTH
from core.application.auth.enforce import normalize_requested_roles
from core.application.auth.resolve import get_user_by_id, get_user_by_username
from core.application.auth.user_admin.contracts import UserAuditContext
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceValidationError,
)


def resolve_user(
    bl: Any,
    *,
    user_id: Any = None,
    email: str = "",
    get_user_by_id_fn: Callable[..., dict[str, Any] | None] = get_user_by_id,
    get_user_by_username_fn: Callable[..., dict[str, Any] | None] = get_user_by_username,
) -> dict[str, Any]:
    normalized_user_id = str(user_id or "").strip()
    normalized_email = str(email or "").strip()
    if bool(normalized_user_id) == bool(normalized_email):
        raise ServiceValidationError("Exactly one of user_id or email is required")
    if normalized_user_id:
        payload = get_user_by_id_fn(bl, normalized_user_id)
        if not payload:
            raise ServiceNotFoundError(f"User id={normalized_user_id!s} not found")
        return dict(payload)
    payload = get_user_by_username_fn(bl, normalized_email)
    if not payload:
        raise ServiceNotFoundError(f"User '{normalized_email}' not found")
    return dict(payload)


def require_password(password: str) -> str:
    normalized = str(password or "")
    if not normalized:
        raise ServiceValidationError("Password is required")
    if len(normalized) < AUTH_PASSWORD_MIN_LENGTH:
        raise ServiceValidationError(
            f"password must be at least {AUTH_PASSWORD_MIN_LENGTH} characters"
        )
    return normalized


def normalized_roles(
    raw_roles: list[str] | None,
    *,
    normalize_roles_fn: Callable[[list[str] | None], list[str]] = normalize_requested_roles,
) -> list[str]:
    roles = normalize_roles_fn(raw_roles or [])
    if not roles:
        raise ServiceValidationError("At least one valid role is required: admin,operator,viewer")
    return roles


def resolve_audit_context(
    command_actor: str,
    audit_context: UserAuditContext | None,
) -> UserAuditContext:
    if audit_context is None:
        return UserAuditContext(actor=str(command_actor or "cli"), source="cli")
    return UserAuditContext(
        actor=str(getattr(audit_context, "actor", "") or command_actor or "cli"),
        source=str(getattr(audit_context, "source", "") or "cli"),
    )


def require_account_access(
    account_ids: list[str],
    allowed_account_ids: list[str] | None,
) -> None:
    if allowed_account_ids is None:
        return
    allowed = {str(value or "").strip() for value in allowed_account_ids}
    requested = {str(value or "").strip() for value in account_ids}
    if not requested or not requested.issubset(allowed):
        raise ServiceForbiddenError("account access denied")


def require_user_access(
    user: dict[str, Any],
    allowed_account_ids: list[str] | None,
) -> None:
    values = [user.get("account_id"), *(user.get("account_ids") or [])]
    values.extend(
        membership.get("account_id")
        for membership in (user.get("account_memberships") or [])
        if isinstance(membership, dict)
    )
    account_ids = list(
        dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip())
    )
    require_account_access(account_ids, allowed_account_ids)
