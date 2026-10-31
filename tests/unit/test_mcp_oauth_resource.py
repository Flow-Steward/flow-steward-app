"""ADR 0047 decisions 3, 13, 14 in fs-mcp (task5b D2, D3, M1, M2, M3, F9, S8)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from core.entrypoints.mcp import server_api_auth, server_oauth_discovery
from core.entrypoints.mcp.server_api_auth import MCP_FORWARD_HEADER, AppApiTokenVerifier
from core.entrypoints.mcp.server_api_client import AppApiClient

ROOT = Path(__file__).resolve().parents[2]
ISSUER = "https://fs.example.com"
RESOURCE = "https://fs.example.com/mcp"
SECRET = "m" * 40


def _introspection(principal: dict) -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        _handler.seen = dict(request.headers)  # type: ignore[attr-defined]
        return httpx.Response(200, json={"active": True, "principal": principal})

    return httpx.MockTransport(_handler)


_OAUTH = {
    "id": "user-1",
    "username": "operator",
    "principal_type": "operator",
    "auth_source": "oauth",
    "scopes": ["projects.read", "jobs.read"],
    "expires_at": 4_000_000_000,
    "resource": RESOURCE,
    "oauth_client_id": "https://claude.ai/oauth/claude-code-client-metadata",
    "grant_id": "grant-1",
}


def _verify(principal: dict, **kwargs):
    verifier = AppApiTokenVerifier(
        base_url="http://fs-app:8099",
        transport=_introspection(principal),
        resource_server_url=RESOURCE,
        issuer_url=ISSUER,
        **kwargs,
    )
    return asyncio.run(verifier.verify_token("fs_oat_x"))


def test_shared_constants_match_the_application_contract() -> None:
    from core.application.auth.inbound_oauth_policy import (
        MCP_FORWARD_HEADER as APP_HEADER,
    )
    from core.application.auth.inbound_oauth_policy import SUPPORTED_SCOPES

    assert MCP_FORWARD_HEADER == APP_HEADER
    assert server_oauth_discovery.MCP_OAUTH_SCOPES == SUPPORTED_SCOPES


def test_oauth_introspection_carries_resource_subject_and_claims() -> None:
    """M1 (accepted)."""
    token = _verify(_OAUTH)
    assert token is not None
    assert token.resource == RESOURCE
    assert token.subject == "user-1"
    assert token.claims == {
        "iss": ISSUER,
        "oauth_client_id": _OAUTH["oauth_client_id"],
        "grant_id": "grant-1",
    }
    assert token.client_id == "user:operator"


def test_oauth_introspection_without_resource_is_refused() -> None:
    """M1 (verifier rejects)."""
    assert _verify({**_OAUTH, "resource": ""}) is None


@pytest.mark.parametrize("source", ["pat", "machine", "local"])
def test_pat_machine_and_session_are_vouched_for_this_resource(source: str) -> None:
    """M2."""
    principal = {key: value for key, value in _OAUTH.items() if key not in {"resource", "grant_id"}}
    principal["auth_source"] = source
    token = _verify(principal)
    assert token is not None
    assert token.resource == RESOURCE
    assert token.subject is None


def test_verifier_sends_the_forward_secret_to_the_app(monkeypatch) -> None:
    monkeypatch.setenv("FS_MCP_FORWARD_SECRET", SECRET)
    transport = _introspection(_OAUTH)
    verifier = AppApiTokenVerifier(
        base_url="http://fs-app:8099",
        transport=transport,
        resource_server_url=RESOURCE,
        issuer_url=ISSUER,
    )
    asyncio.run(verifier.verify_token("fs_oat_x"))
    seen = transport.handler.seen  # type: ignore[attr-defined]
    assert seen[MCP_FORWARD_HEADER.lower()] == SECRET
    assert seen["authorization"] == "Bearer fs_oat_x"


def test_api_client_sets_the_forward_header_last_and_refuses_overrides(monkeypatch) -> None:
    """F9."""
    monkeypatch.setenv("FS_MCP_FORWARD_SECRET", SECRET)
    observed: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        observed.update(request.headers)
        observed["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    client = AppApiClient(base_url="http://fs-app:8099", transport=httpx.MockTransport(_handler))
    client.request(
        "GET",
        "/api/projects",
        bearer="fs_oat_x",
        additional_headers={MCP_FORWARD_HEADER: "attacker", "X-Other": "kept"},
    )
    assert observed[MCP_FORWARD_HEADER.lower()] == SECRET
    assert observed["x-other"] == "kept"
    assert observed["url"] == "http://fs-app:8099/api/projects"
    with pytest.raises(ValueError):
        client.request("GET", "https://evil.example/api", bearer="fs_oat_x")
    with pytest.raises(ValueError):
        client.request("GET", "//evil.example/api", bearer="fs_oat_x")


def test_no_forward_header_without_a_configured_secret(monkeypatch) -> None:
    monkeypatch.delenv("FS_MCP_FORWARD_SECRET", raising=False)
    assert server_api_auth.mcp_forward_headers() == {}


class _StaticVerifier:
    def __init__(self, resource: str | None) -> None:
        self._resource = resource

    async def verify_token(self, token: str):
        from mcp.server.auth.provider import AccessToken

        return AccessToken(
            token=token, client_id="user:x", scopes=["projects.read"], resource=self._resource
        )


def _wrapped_app(verifier) -> server_oauth_discovery.OAuthDiscoveryAdapter:
    server = MCPServer(
        name="oauth-discovery",
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=ISSUER,
            resource_server_url=RESOURCE,
            validate_token_resource=True,
        ),
    )

    @server.tool(name="ping")
    def ping() -> str:
        return "pong"

    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=["fs.example.com"]),
    )
    return server_oauth_discovery.OAuthDiscoveryAdapter(
        app, resource_url=RESOURCE, issuer_url=ISSUER
    )


def _initialize(client: TestClient, bearer: str | None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        },
    )


def test_protected_resource_metadata_is_ours_not_the_sdks() -> None:
    """D2."""
    with TestClient(
        _wrapped_app(_StaticVerifier(RESOURCE)), base_url="https://fs.example.com"
    ) as c:
        response = c.get("/.well-known/oauth-protected-resource/mcp")
        preflight = c.options("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert preflight.status_code == 204
    assert response.json() == {
        "resource": RESOURCE,
        "authorization_servers": [ISSUER],
        "scopes_supported": [
            "projects.read",
            "jobs.read",
            "jobs.write",
            "reports.read",
            "reports.write",
            "workflows.read",
            "workflows.write",
            "admin:all",
        ],
        "bearer_methods_supported": ["header"],
        "resource_name": "Flow Steward",
    }


def test_unauthenticated_mcp_call_is_challenged_with_metadata_and_scopes() -> None:
    """D3."""
    with TestClient(
        _wrapped_app(_StaticVerifier(RESOURCE)), base_url="https://fs.example.com"
    ) as c:
        response = _initialize(c, None)
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert 'error="invalid_token"' in challenge
    assert (
        'resource_metadata="https://fs.example.com/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )
    assert (
        'scope="projects.read jobs.read jobs.write reports.read reports.write '
        'workflows.read workflows.write admin:all"' in challenge
    )


@pytest.mark.parametrize(
    ("resource", "status"),
    [
        (RESOURCE, 200),
        ("https://fs.example.com/mcp/", 200),
        ("https://other.example.com/mcp", 401),
        (None, 401),
    ],
)
def test_the_sdk_refuses_a_token_for_another_resource(resource, status) -> None:
    """M1 (SDK rejects another resource) and M2 (a vouched token passes)."""
    with TestClient(
        _wrapped_app(_StaticVerifier(resource)), base_url="https://fs.example.com"
    ) as c:
        response = _initialize(c, "token")
    assert response.status_code == status, response.text
    if status == 200:
        assert json.loads(response.text)["result"]["tools"][0]["name"] == "ping"


def test_runtime_builds_the_wrapped_app_with_resource_validation(monkeypatch) -> None:
    """M3 at the build step: server_main wraps the SDK app when auth is configured."""
    from core.entrypoints.mcp import server_main, server_runtime

    monkeypatch.setattr(server_runtime, "auth_settings", object())
    monkeypatch.setattr(server_runtime, "MCP_RESOURCE_URL", RESOURCE)
    monkeypatch.setattr(server_runtime, "MCP_ISSUER_URL", ISSUER)
    app = server_main.build_streamable_http_app(host="0.0.0.0", port=8100)
    assert isinstance(app, server_oauth_discovery.OAuthDiscoveryAdapter)
    monkeypatch.setattr(server_runtime, "auth_settings", None)
    assert not isinstance(
        server_main.build_streamable_http_app(host="0.0.0.0", port=8100),
        server_oauth_discovery.OAuthDiscoveryAdapter,
    )


def test_mcp_image_copies_the_shared_public_identity() -> None:
    """S8: the MCP image ships core/infrastructure/public_identity.py and nothing of core.application."""
    dockerfile = (ROOT / "infra/mcp/Dockerfile").read_text(encoding="utf-8")
    assert (
        "core/infrastructure/public_identity.py core/infrastructure/public_identity.py"
        in dockerfile
    )
    assert "core/application" not in dockerfile
    import ast

    for path in sorted((ROOT / "core/entrypoints/mcp").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not str(node.module or "").startswith("core.application"), path
            elif isinstance(node, ast.Import):
                assert not any(a.name.startswith("core.application") for a in node.names), path
