from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from core.entrypoints.mcp.server_api_auth import AppApiTokenVerifier


def test_app_api_token_verifier_uses_only_inbound_bearer() -> None:
    observed: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("authorization", "")
        observed["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "active": True,
                "principal": {
                    "id": "principal-1",
                    "username": "mcp-agent",
                    "principal_type": "service_account",
                    "service_account_key": "mcp-functional",
                    "account_id": "account-1",
                    "roles": ["operator"],
                    "scopes": ["code.read", "projects.read"],
                    "allowed_project_ids": ["project-1"],
                    "allowed_workflow_ids": ["workflow-1"],
                    "expires_at": 1_900_000_000,
                },
            },
        )

    verifier = AppApiTokenVerifier(
        base_url="http://fs-app:8099",
        transport=httpx.MockTransport(_handler),
    )

    access_token = asyncio.run(verifier.verify_token("caller-secret"))

    assert access_token is not None
    assert observed == {
        "authorization": "Bearer caller-secret",
        "path": "/api/auth/introspect",
    }
    assert access_token.client_id == "sa:mcp-functional"
    assert access_token.scopes == ["code.read", "projects.read"]
    assert access_token.expires_at == 1_900_000_000


@pytest.mark.parametrize("status", [401, 403, 500])
def test_app_api_token_verifier_fails_closed(status: int) -> None:
    verifier = AppApiTokenVerifier(
        base_url="http://fs-app:8099",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, json={"error": "denied"})
        ),
    )

    assert asyncio.run(verifier.verify_token("caller-secret")) is None


def test_app_api_token_verifier_rejects_inactive_or_malformed_payload() -> None:
    responses = iter(
        (
            httpx.Response(200, json={"active": False, "principal": {"id": "p"}}),
            httpx.Response(200, content=json.dumps({"active": True, "principal": []})),
        )
    )
    verifier = AppApiTokenVerifier(
        base_url="http://fs-app:8099",
        transport=httpx.MockTransport(lambda _request: next(responses)),
    )

    assert asyncio.run(verifier.verify_token("caller-secret")) is None
    assert asyncio.run(verifier.verify_token("caller-secret")) is None
