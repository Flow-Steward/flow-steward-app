"""Account administration of external identity links (ADR 0083).

Links belong to one account. Only an administrator of that account (the admin role,
and ``admin:all`` for a token) lists or removes them, always within the account the
administrator acts in: no account reaches the links another account made. Every
change is audited. Making links and configuring sign-in methods belong to the
platform modules that provide those methods.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any

from core.application.auth import external_sign_in_repository as repository
from core.application.auth.account_owner import require_owner_consent
from core.application.management.errors import ServiceForbiddenError, ServiceNotFoundError

ConnectionFactory = Callable[[], AbstractContextManager[Any]]
AuditFn = Callable[..., None]


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_platform_admin(scope: Any) -> bool:
    from core.application.auth.principal import principal_type_has_minimum_role

    if scope is None or not principal_type_has_minimum_role(
        _text(getattr(scope, "principal_type", "")), "admin"
    ):
        return False
    token_scopes = frozenset(getattr(scope, "token_scopes", frozenset()) or frozenset())
    return not token_scopes or "admin:all" in token_scopes


def _require_admin(scope: Any) -> tuple[str, str]:
    if not is_platform_admin(scope):
        raise ServiceForbiddenError(
            "Only a platform administrator can manage external sign-in",
            payload={"error": "platform_admin_required", "code": "platform_admin_required"},
        )
    account_id = _text(getattr(scope, "account_id", ""))
    if not repository.is_uuid(account_id):
        raise ServiceForbiddenError("The caller has no account")
    return account_id, _text(getattr(scope, "actor_name", "")) or "admin"


def _iso(value: Any) -> str | None:
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else (str(value) if value else None)


class ExternalSignInAdmin:
    def __init__(self, *, connection: ConnectionFactory, audit: AuditFn) -> None:
        self._connection = connection
        self._audit = audit

    def _log(self, actor: str, action: str, detail: Mapping[str, Any]) -> None:
        self._audit(action=action, username=actor, actor=actor, status="ok", detail=dict(detail))

    def list_identities(self, scope: Any) -> dict[str, Any]:
        account_id, _actor = _require_admin(scope)
        with self._connection() as conn, conn.cursor() as cur:
            rows = repository.list_identities(cur, account_id=account_id)
        return {
            "identities": [
                {
                    "id": row["id"],
                    "method": row.get("method") or "",
                    "option_key": row.get("option_key") or "",
                    "issuer": row.get("issuer") or "",
                    "subject": row.get("subject") or "",
                    "user_id": row.get("user_id") or "",
                    "username": row.get("username") or "",
                    "created_at": _iso(row.get("created_at")),
                    "last_login_at": _iso(row.get("last_login_at")),
                }
                for row in rows
            ]
        }

    def unlink_identity(self, scope: Any, identity_id: str) -> dict[str, Any]:
        account_id, actor = _require_admin(scope)
        with self._connection() as conn, conn.cursor() as cur:
            # Only a company owner unlinks their own sign-in (ADR 0085, "Company owner").
            linked = repository.identity_user_id(cur, identity_id, account_id=account_id)
            if linked:
                require_owner_consent(
                    cur, user_id=linked, actor_user_id=_text(getattr(scope, "user_id", ""))
                )
            if not repository.delete_identity(cur, identity_id, account_id=account_id):
                raise ServiceNotFoundError("External identity not found")
        self._log(actor, "external_identity_unlink", {"identity_id": identity_id})
        return {"deleted": True, "id": identity_id}


__all__ = ["ExternalSignInAdmin", "is_platform_admin"]
