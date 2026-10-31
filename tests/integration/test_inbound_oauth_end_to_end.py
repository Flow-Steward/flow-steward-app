"""Local end-to-end OAuth with a scripted public client (ADR 0047; task5b M5, L1-L4 offline).

The real ``fs-app`` (in-process uvicorn) and the real ``fs-mcp`` process
(``python -m core.entrypoints.mcp.server``) on loopback ports, the audit PostgreSQL,
and a scripted client that presents Claude Code's CIMD ``client_id`` with a loopback
redirect. The CIMD document is served by an injected fetcher (the only stand-in: the
real host is claude.ai). Discovery -> authorize -> sign-in session -> consent -> code
exchange -> MCP tools/list and tools/call -> API -> application service -> refresh ->
revoke -> MCP refuses.

The live check against real ChatGPT / Claude.ai needs the owner's accounts and a
public tunnel; it is not part of this suite.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from tests._db_env import resolve_test_db_dsn
from tests.integration._inbound_oauth_world import (
    CLIENT_ID,
    CSRF,
    FORWARD_SECRET,
    ORIGIN,
    REDIRECT,
    RESOURCE,
    authorize_params,
    build_world,
    oauth_environment,
    pkce_pair,
    query_of,
)

ROOT = Path(__file__).resolve().parents[2]
_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2026-07-28",
}
_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "scripted-oauth-client", "version": "1.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture(autouse=True)
def require_audit_database(pg_conn):
    assert pg_conn.info.host == "pg-audit"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait(url: str, *, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return
        except httpx.HTTPError as exc:
            last = exc
        time.sleep(0.25)
    raise AssertionError(f"{url} did not become ready: {last}")


@pytest.fixture
def stack(pg_conn, default_account, monkeypatch):
    import uvicorn
    from fastapi.testclient import TestClient

    from core.entrypoints.api import route_registration
    from core.entrypoints.api.app_factory import create_app
    from core.infrastructure.db.platform_stores import PlatformProjectStore

    oauth_environment(monkeypatch)
    world = build_world(pg_conn, resolve_test_db_dsn())
    project_store = PlatformProjectStore(world.dsn)
    app = create_app()
    app.state.local_auth_enabled_override = lambda: True
    app.state.external_auth_enabled_override = lambda: False
    factories = app.state.runtime_container.service_factories
    factories["identity_store"] = lambda: world.store
    factories["project_store"] = lambda: project_store
    monkeypatch.setattr(route_registration, "_inbound_oauth_service", lambda _app: world.service())

    app_port, mcp_port = _free_port(), _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=app_port, lifespan="off", log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait(f"http://127.0.0.1:{app_port}/health/live")

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"FS_MCP_AUTH_DISABLED", "FS_DEPLOYMENT_REVISION", "FS_MCP_ALLOWED_HOSTS"}
    }
    env.update(
        FS_MCP_AUTH_ENABLED="1",
        FS_APP_INTERNAL_URL=f"http://127.0.0.1:{app_port}",
        FS_PUBLIC_BASE_URL=ORIGIN,
        FS_MCP_FORWARD_SECRET=FORWARD_SECRET,
        PYTHONUNBUFFERED="1",
    )
    env.pop("FS_MCP_ISSUER_URL", None)
    env.pop("FS_MCP_RESOURCE_URL", None)
    mcp = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "core.entrypoints.mcp.server",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(mcp_port),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    browser = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    browser.cookies.set("fs_session", world.users["operator"]["session"])
    browser.cookies.set("fs_csrf", CSRF)
    try:
        _wait(f"http://127.0.0.1:{mcp_port}/.well-known/oauth-protected-resource/mcp")
        yield {
            "world": world,
            "app": f"http://127.0.0.1:{app_port}",
            "mcp": f"http://127.0.0.1:{mcp_port}",
            "browser": browser,
            "mcp_process": mcp,
        }
    finally:
        browser.close()
        mcp.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            mcp.wait(timeout=10)
        mcp_log = mcp.stdout.read() if mcp.stdout else ""
        server.should_exit = True
        thread.join(timeout=10)
        if project_store._direct_conn is not None:
            project_store._direct_conn.close()
        world.cleanup()
        stack_log = mcp_log
        for secret in (FORWARD_SECRET,):
            assert secret not in stack_log


def _mcp(base: str, method: str, token: str | None, params: dict | None = None):
    headers = dict(_MCP_HEADERS, **{"Mcp-Method": method})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if method == "tools/call" and params:
        headers["Mcp-Name"] = params["name"]
    return httpx.post(
        f"{base}/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {**(params or {}), "_meta": _META},
        },
        timeout=30.0,
    )


def _tool_text(response: httpx.Response) -> dict:
    result = response.json()["result"]
    text = next(item["text"] for item in result["content"] if item["type"] == "text")
    return json.loads(text)


def test_scripted_public_client_full_chain(stack) -> None:
    world, app, mcp, browser = stack["world"], stack["app"], stack["mcp"], stack["browser"]
    allowed, hidden = world.projects

    # L1: 401 -> protected resource metadata -> authorization server metadata.
    challenge_response = _mcp(mcp, "tools/list", None)
    assert challenge_response.status_code == 401
    challenge = challenge_response.headers["www-authenticate"]
    assert f'resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/mcp"' in challenge
    assert 'scope="projects.read' in challenge
    prm = httpx.get(f"{mcp}/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == RESOURCE
    assert prm["authorization_servers"] == [ORIGIN]
    metadata = httpx.get(f"{app}/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"] == ORIGIN
    assert metadata["client_id_metadata_document_supported"] is True

    # Authorization request in the browser; the user is signed in; consent.
    verifier, code_challenge = pkce_pair()
    response = browser.get(
        "/oauth/authorize", params=authorize_params(code_challenge, state="e2e-state")
    )
    request_id = query_of(response.headers["location"])["request"]
    view = browser.get(f"/api/oauth/authorizations/{request_id}").json()
    assert view["client"]["client_name"] == "Claude Code (test)"
    decision = browser.post(
        f"/api/oauth/authorizations/{request_id}/decision",
        json={
            "decision": "approve",
            "preset": "operational",
            "expiry_days": 30,
            "project_ids": [allowed],
        },
        headers={"x-csrf-token": CSRF},
    ).json()["redirect_to"]
    assert urlsplit(decision).netloc == urlsplit(REDIRECT).netloc
    callback = query_of(decision)
    assert callback["state"] == "e2e-state"
    assert callback["iss"] == ORIGIN

    # L3: a wrong verifier burns the code; a fresh flow is needed afterwards.
    tokens = httpx.post(
        f"{app}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": callback["code"],
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
    ).json()
    assert tokens["token_type"] == "Bearer"
    reuse = httpx.post(
        f"{app}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": callback["code"],
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert reuse.json()["error"] == "invalid_grant"
    # The reuse revoked the grant (T2): start again for the rest of the chain.
    assert _mcp(mcp, "tools/list", tokens["access_token"]).status_code == 401

    verifier, code_challenge = pkce_pair()
    response = browser.get("/oauth/authorize", params=authorize_params(code_challenge))
    request_id = query_of(response.headers["location"])["request"]
    code = query_of(
        browser.post(
            f"/api/oauth/authorizations/{request_id}/decision",
            json={
                "decision": "approve",
                "preset": "operational",
                "expiry_days": 30,
                "project_ids": [allowed],
            },
            headers={"x-csrf-token": CSRF},
        ).json()["redirect_to"]
    )["code"]
    tokens = httpx.post(
        f"{app}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    ).json()
    access = tokens["access_token"]

    # M5 / L2: tools/list filtered by the grant's scopes; tools/call reaches the API.
    listed = _mcp(mcp, "tools/list", access)
    assert listed.status_code == 200, listed.text
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert {"project_list", "job_list", "workflow_trigger"} <= names
    assert "agent_create" not in names
    projects = _mcp(mcp, "tools/call", access, {"name": "project_list", "arguments": {}})
    assert projects.status_code == 200, projects.text
    payload = _tool_text(projects)
    serialized = json.dumps(payload)
    assert allowed in serialized
    assert hidden not in serialized

    # The same token straight at the public API is refused (not the MCP back end).
    assert (
        httpx.get(f"{app}/api/projects", headers={"Authorization": f"Bearer {access}"}).status_code
        == 401
    )

    # Refresh rotates; L4: revocation stops MCP calls and refresh.
    refreshed = httpx.post(
        f"{app}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": CLIENT_ID,
        },
    ).json()
    assert refreshed["refresh_token"] != tokens["refresh_token"]
    assert _mcp(mcp, "tools/list", refreshed["access_token"]).status_code == 200
    revoked = httpx.post(
        f"{app}/oauth/revoke",
        data={"token": refreshed["refresh_token"], "client_id": CLIENT_ID},
    )
    assert revoked.status_code == 200
    assert _mcp(mcp, "tools/list", refreshed["access_token"]).status_code == 401
    after = httpx.post(
        f"{app}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refreshed["refresh_token"],
            "client_id": CLIENT_ID,
        },
    )
    assert after.json()["error"] == "invalid_grant"


def test_token_for_another_resource_is_refused_by_the_mcp_server(stack) -> None:
    """A4: a token issued for another resource never passes fs-mcp's SDK check."""
    world = stack["world"]
    service = world.service()
    verifier, code_challenge = pkce_pair()
    outcome = service.start_authorization(authorize_params(code_challenge))
    request_id = query_of(outcome.location)["request"]
    code = query_of(
        service.decide(
            request_id,
            user=world.principal("operator"),
            browser_binding=outcome.browser_binding,
            decision="approve",
            preset_id="operational",
            expiry_days=7,
            project_ids=[],
        )
    )["code"]
    access = service.token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        }
    )["access_token"]
    world.sql("UPDATE oauth_grants SET resource = 'https://other.example/mcp'")
    # fs-app no longer resolves it (resource moved) and fs-mcp would refuse it anyway.
    assert _mcp(stack["mcp"], "tools/list", access).status_code == 401


def test_pat_still_works_through_the_mcp_server(stack) -> None:
    """A9 / M2 live: resource validation on, a PAT keeps working."""
    world = stack["world"]
    pat = world.store.create_pat(
        user_id=world.users["operator"]["id"],
        account_id="00000000-0000-0000-0000-000000000002",
        scopes=["projects.read"],
    )["raw_token"]
    listed = _mcp(stack["mcp"], "tools/list", pat)
    assert listed.status_code == 200, listed.text
    projects = _mcp(stack["mcp"], "tools/call", pat, {"name": "project_list", "arguments": {}})
    assert projects.status_code == 200
