"""The bounded effective principal that ``/api/auth/introspect`` returns to transport adapters."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from core.application.auth.inbound_oauth_policy import oauth_introspection_fields
from core.application.auth.principal import is_token_scoped_principal


def introspection_payload(request: Request, principal: dict[str, Any]) -> dict[str, Any]:
    """Shape the authenticated ``principal`` of ``request`` for the introspection response."""
    expires_at = principal.get("expires_at")
    expires_at_isoformat = getattr(expires_at, "isoformat", None)
    if callable(expires_at_isoformat):
        expires_at = expires_at_isoformat()
    elif expires_at is not None and not isinstance(expires_at, (int, float, str)):
        expires_at = str(expires_at)

    auth_source = str(
        getattr(request.state, "auth_source", "") or principal.get("auth_source") or ""
    ).strip()
    principal_type = str(principal.get("principal_type") or "user").strip()
    scopes = [str(item) for item in principal.get("scopes") or [] if str(item).strip()]
    token_scoped = is_token_scoped_principal(
        {"auth_source": auth_source, "principal_type": principal_type}
    )
    if not scopes and not token_scoped:
        roles = {str(item).strip().lower() for item in principal.get("roles") or []}
        if "admin" in roles:
            scopes = ["admin:all"]
        elif "operator" in roles:
            scopes = ["read:*", "command:*"]
        else:
            scopes = ["read:*"]

    return {
        "active": bool(principal.get("is_active", True)),
        "principal": {
            "id": str(principal.get("id") or "").strip(),
            "username": str(principal.get("username") or "").strip(),
            "principal_type": principal_type,
            "service_account_key": str(principal.get("service_account_key") or "").strip(),
            "auth_source": auth_source,
            "account_id": str(principal.get("account_id") or "").strip(),
            "roles": list(principal.get("roles") or []),
            "scopes": scopes,
            "allowed_project_ids": list(principal.get("allowed_project_ids") or []),
            "allowed_workflow_ids": list(principal.get("allowed_workflow_ids") or []),
            "expires_at": expires_at,
            **oauth_introspection_fields(principal, auth_source),
        },
    }
