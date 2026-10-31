"""Fail-closed MCP bearer verification through the canonical app API."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from core.infrastructure.runtime_env import runtime_env
from mcp.server.auth.provider import AccessToken

logger = logging.getLogger("platform.mcp.auth")

#: ADR 0047 decision 13. Proves to ``fs-app`` that an OAuth access token arrives from
#: this MCP server, its own back end, and not directly from the internet. Must equal
#: ``core.application.auth.inbound_oauth_policy.MCP_FORWARD_HEADER`` (pinned by a test;
#: the MCP image does not ship the application package).
MCP_FORWARD_HEADER = "X-Flow-Steward-Mcp-Forward"
_OAUTH_AUTH_SOURCE = "oauth"


def app_api_base_url() -> str:
    """Return the internal application API origin used by the MCP adapter."""
    return str(runtime_env.get("FS_APP_INTERNAL_URL") or "http://fs-app:8099").rstrip("/")


def mcp_forward_headers() -> dict[str, str]:
    """The forward-secret header for requests to ``FS_APP_INTERNAL_URL`` only."""
    secret = str(runtime_env.get("FS_MCP_FORWARD_SECRET") or "").strip()
    return {MCP_FORWARD_HEADER: secret} if secret else {}


def _expires_at_epoch(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


class AppApiTokenVerifier:
    """Verify one inbound bearer through ``fs-app`` without local DB access."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 5.0,
        resource_server_url: str = "",
        issuer_url: str = "",
    ) -> None:
        self._base_url = str(base_url or app_api_base_url()).rstrip("/")
        self._transport = transport
        self._timeout = httpx.Timeout(timeout_seconds)
        # ADR 0047 decision 14: the resource a PAT, machine token or session is valid
        # for (this server), and the issuer named in an OAuth token's claims.
        self._resource_server_url = str(resource_server_url or "").strip()
        self._issuer_url = str(issuer_url or "").strip()

    async def verify_token(self, token: str) -> AccessToken | None:
        normalized = str(token or "").strip()
        if not normalized:
            return None
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    "/api/auth/introspect",
                    headers={"Authorization": f"Bearer {normalized}", **mcp_forward_headers()},
                )
        except httpx.HTTPError as exc:
            logger.warning("MCP auth introspection unavailable: %s", type(exc).__name__)
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict) or not bool(payload.get("active")):
            return None
        principal = payload.get("principal")
        if not isinstance(principal, dict):
            return None

        scopes = [str(item) for item in principal.get("scopes") or [] if str(item).strip()]
        principal_type = str(principal.get("principal_type") or "user").strip().lower()
        if principal_type == "service_account":
            identity = str(
                principal.get("service_account_key") or principal.get("id") or "unknown"
            ).strip()
            client_id = f"sa:{identity}"
        else:
            identity = str(principal.get("username") or principal.get("id") or "unknown").strip()
            client_id = f"user:{identity}"

        expires_at = _expires_at_epoch(principal.get("expires_at"))
        if str(principal.get("auth_source") or "").strip().lower() == _OAUTH_AUTH_SOURCE:
            resource = str(principal.get("resource") or "").strip()
            if not resource:
                # An OAuth token is bound to a resource; without one it is refused.
                return None
            return AccessToken(
                token=normalized,
                client_id=client_id,
                scopes=scopes,
                expires_at=expires_at,
                resource=resource,
                subject=str(principal.get("id") or "").strip() or None,
                claims={
                    "iss": self._issuer_url,
                    "oauth_client_id": str(principal.get("oauth_client_id") or ""),
                    "grant_id": str(principal.get("grant_id") or ""),
                },
            )
        # A PAT, machine token or session is valid at its own installation's MCP
        # endpoint: the verifier vouches for it, so the SDK resource check passes.
        return AccessToken(
            token=normalized,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            resource=self._resource_server_url or None,
        )


__all__ = ["MCP_FORWARD_HEADER", "AppApiTokenVerifier", "app_api_base_url", "mcp_forward_headers"]
