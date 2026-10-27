"""Auth audit logging helpers."""

from __future__ import annotations

import logging
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.principal import is_machine_principal, principal_account_id
from core.application.auth.resolve import interactive_principal_type
from core.application.management.errors import safe_operator_exception

logger = logging.getLogger(__name__)


def log_auth_audit_event(
    bl,
    *,
    action: str,
    username: str = "",
    actor: str = "",
    status: str = "ok",
    detail: dict[str, Any] | None = None,
) -> None:
    if not bl:
        return
    normalized_action = str(action or "event").strip().lower()
    action_type = f"auth_{normalized_action}"
    try:
        bl.log_action(
            project_id=None,
            job_id=None,
            task_id="auth",
            task_summary=f"Auth {action}",
            agent=str(actor or "auth").strip() or "auth",
            action_type=action_type,
            status=str(status or "ok").strip() or "ok",
            payload={"username": str(username or "").strip()},
            result=detail or {},
            meta={"audit_category": "auth"},
        )
    except Exception as exc:
        logger.warning(
            "Auth audit write failed for %s: %s",
            action_type,
            safe_operator_exception(exc),
        )


def request_actor_context(user: dict | None) -> tuple[str, str, str]:
    if isinstance(user, dict):
        if is_machine_principal(user):
            return (
                "service_account",
                str(
                    user.get("service_account_key") or user.get("username") or "service_account"
                ).strip()
                or "service_account",
                principal_account_id(user),
            )
        return (
            interactive_principal_type(user),
            str(user.get("username") or "operator").strip() or "operator",
            principal_account_id(user),
        )
    return ("system_actor", "system", DEFAULT_ACCOUNT_ID)
