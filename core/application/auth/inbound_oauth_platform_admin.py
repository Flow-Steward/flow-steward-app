"""Admin operations on platform OAuth clients (ADR 0063 decision 2).

An administrator lists, creates and revokes the account's platform clients and
reads the values the platform's own registration asks for (for Microsoft 365:
the Teams Developer Portal OAuth client registration). Routes are admin-only and
interactive-only; this module only orchestrates storage and audit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.application.auth import inbound_oauth_platform_clients as platform_clients

if TYPE_CHECKING:
    from core.application.auth.inbound_oauth_service import InboundOAuthService

#: The scope value the platform registration sends. ``offline_access`` asks for a
#: refresh token and is dropped by the scope parser, so the grant gets the
#: operational preset the consent page offers (ADR 0047 decision 6).
PORTAL_SCOPE = "offline_access"


def _consent_error(error: platform_clients.PlatformClientError) -> Exception:
    from core.application.auth.inbound_oauth_service import ConsentError

    return ConsentError(error.code, error.status)


def _client_view(row: dict[str, Any]) -> dict[str, Any]:
    preset = platform_clients.PLATFORM_PRESETS.get(str(row.get("preset") or ""))
    return {
        "client_id": str(row.get("client_id") or ""),
        "preset": str(row.get("preset") or ""),
        "name": preset.name if preset else str(row.get("preset") or ""),
        "created_at": row.get("created_at"),
        "created_by": row.get("created_by") or "",
        "revoked_at": row.get("revoked_at"),
    }


def platform_clients_view(service: InboundOAuthService, account_id: str) -> dict[str, Any]:
    """The account's platform clients and what to enter on the platform's side."""
    readiness = service.readiness(account_id)
    with service._transaction() as conn:
        rows = platform_clients.load_registrations(conn, account_id)
    issuer = readiness.issuer
    portal = None
    if issuer and readiness.resource:
        token_endpoint = f"{issuer}/oauth/token"
        portal = {
            # Microsoft requires the Base URL to equal the MCP server URL.
            "base_url": readiness.resource,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": token_endpoint,
            "refresh_endpoint": token_endpoint,
            "scope": PORTAL_SCOPE,
            "pkce": True,
            "client_secret": "",
        }
    return {
        "presets": [
            {"id": preset.id, "name": preset.name, "redirect_uris": list(preset.redirect_uris)}
            for preset in platform_clients.PLATFORM_PRESETS.values()
        ],
        "clients": [_client_view(row) for row in rows],
        "portal": portal,
        "readiness": readiness.to_payload(),
    }


def create_platform_client(
    service: InboundOAuthService, account_id: str, preset_id: str, *, actor: str
) -> dict[str, Any]:
    try:
        with service._transaction() as conn:
            registration = platform_clients.register(
                conn, account_id, preset_id, created_by=actor or "admin", now=service.clock()
            )
    except platform_clients.PlatformClientError as error:
        raise _consent_error(error) from error
    service._audit(
        "oauth_platform_client_create",
        username=actor,
        account_id=account_id,
        preset=preset_id,
        client_id=registration["client_id"],
    )
    return platform_clients_view(service, account_id)


def revoke_platform_client(
    service: InboundOAuthService, account_id: str, client_id: str, *, actor: str
) -> dict[str, Any]:
    try:
        with service._transaction() as conn:
            revoked_grants = platform_clients.revoke(
                conn, account_id, client_id, revoked_by=actor or "admin", now=service.clock()
            )
    except platform_clients.PlatformClientError as error:
        raise _consent_error(error) from error
    service._audit(
        "oauth_platform_client_revoke",
        username=actor,
        account_id=account_id,
        client_id=client_id,
        revoked_grants=revoked_grants,
    )
    return platform_clients_view(service, account_id)


__all__ = [
    "PORTAL_SCOPE",
    "create_platform_client",
    "platform_clients_view",
    "revoke_platform_client",
]
