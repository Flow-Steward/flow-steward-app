"""ADR 0047 decisions 3, 6, 7 at the HTTP adapters (task5b D1, Z1-Z5, T8, C1 audit).

The service runs for real; only its five-table repository and the transaction helper
are replaced by an in-memory double, so the request validation order is exercised
exactly as in production. The PostgreSQL suite covers the same flows end to end.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.auth import inbound_oauth_service as service_module
from core.application.auth.inbound_oauth_clients import FetchedDocument
from core.application.auth.inbound_oauth_policy import OAuthEnvironment, pkce_s256_challenge
from core.application.auth.inbound_oauth_service import InboundOAuthService
from core.entrypoints.api.oauth_authorization_server_routes import (
    create_oauth_authorization_server_router,
)

ORIGIN = "https://fs.example.com"
CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
REDIRECT = "http://127.0.0.1:53111/callback"
CHALLENGE = pkce_s256_challenge("v" * 64)


class _Store:
    def __init__(self) -> None:
        self.clients: dict[str, dict] = {}
        self.authorizations: dict[str, dict] = {}
        self.fetches: list[str] = []
        self.audit: list[dict] = []
        self.enabled = True
        self.storage = True


@pytest.fixture
def store(monkeypatch) -> _Store:
    state = _Store()
    repo = service_module.repo

    @contextlib.contextmanager
    def _transaction(_conn):
        yield object()

    monkeypatch.setattr(service_module, "transaction_connection", _transaction)
    monkeypatch.setattr(repo, "storage_available", lambda _conn: state.storage)
    monkeypatch.setattr(repo, "any_account_enabled", lambda _conn: state.enabled)
    monkeypatch.setattr(repo, "consume_rate_limit", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(repo, "get_client", lambda _conn, client_id: state.clients.get(client_id))

    def _upsert(_conn, **row):
        state.clients[row["client_id"]] = {**row, "redirect_uris_jsonb": list(row["redirect_uris"])}

    def _insert(_conn, **row):
        authorization_id = f"00000000-0000-4000-8000-{len(state.authorizations):012d}"
        state.authorizations[authorization_id] = row
        return authorization_id

    monkeypatch.setattr(repo, "upsert_client", _upsert)
    monkeypatch.setattr(repo, "insert_authorization", _insert)
    return state


def _document() -> bytes:
    return json.dumps(
        {
            "client_id": CLIENT_ID,
            "client_name": "Claude Code",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "token_endpoint_auth_method": "none",
        }
    ).encode()


def _client(store: _Store, *, base_url: str = ORIGIN) -> TestClient:
    bl = SimpleNamespace(conn=object(), log_action=lambda **kwargs: store.audit.append(kwargs))

    def _fetch(client_id: str) -> FetchedDocument:
        store.fetches.append(client_id)
        return FetchedDocument(body=_document(), cache_control="max-age=600")

    service = InboundOAuthService(
        bl,
        environment=OAuthEnvironment(public_base_url=base_url, forward_secret="x" * 32),
        metadata_fetcher=_fetch,
    )
    app = FastAPI()
    app.include_router(
        create_oauth_authorization_server_router(
            service_factory=lambda: service, request_client_ip=lambda _request: "198.51.100.7"
        )
    )
    return TestClient(app, base_url=ORIGIN, follow_redirects=False)


def _authorize(client: TestClient, **overrides):
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "resource": f"{ORIGIN}/mcp",
        "scope": "jobs.read",
        "state": "opaque-state",
    }
    params.update(overrides)
    params = {key: value for key, value in params.items() if value is not None}
    return client.get("/oauth/authorize", params=params)


def _query(response) -> dict[str, list[str]]:
    return parse_qs(urlsplit(response.headers["location"]).query)


def test_authorization_server_metadata_is_exactly_the_contract(store) -> None:
    """D1."""
    response = _client(store).get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.json() == {
        "issuer": ORIGIN,
        "authorization_endpoint": f"{ORIGIN}/oauth/authorize",
        "token_endpoint": f"{ORIGIN}/oauth/token",
        "revocation_endpoint": f"{ORIGIN}/oauth/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
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
    }


def test_not_ready_metadata_and_authorize_explain_the_blocker(store) -> None:
    """D5 at the adapter: 404 oauth_unavailable and the React error page."""
    client = _client(store, base_url="http://localhost:8099")
    metadata = client.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code == 404
    assert metadata.json() == {"error": "oauth_unavailable"}
    response = _authorize(client)
    assert response.status_code == 302
    assert response.headers["location"] == "/oauth/consent?error=public_https_required"

    store.enabled = False
    assert _authorize(_client(store)).headers["location"] == (
        "/oauth/consent?error=disabled_by_admin"
    )


def test_a_valid_request_is_stored_and_bound_to_this_browser(store) -> None:
    response = _authorize(_client(store))
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert location.path == "/oauth/consent"
    request_id = parse_qs(location.query)["request"][0]
    stored = store.authorizations[request_id]
    assert stored["redirect_uri"] == REDIRECT
    assert stored["resource"] == f"{ORIGIN}/mcp"
    assert stored["requested_scopes"] == ["jobs.read"]
    assert stored["state"] == "opaque-state"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("fs_oauth_ar=")
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert flag.lower() in cookie.lower()
    raw_binding = cookie.split(";", 1)[0].split("=", 1)[1]
    assert raw_binding not in json.dumps(store.authorizations, default=str)
    # Nothing from the request travels in the consent URL but its id.
    assert "opaque-state" not in response.headers["location"]
    assert response.headers["cache-control"] == "no-store"
    assert store.fetches == [CLIENT_ID]


@pytest.mark.parametrize(
    "overrides",
    [
        {"code_challenge": None},
        {"code_challenge_method": "plain"},
        {"code_challenge_method": None},
        {"resource": None},
        {"state": "s" * 1025},
    ],
)
def test_invalid_requests_redirect_with_state_and_iss(store, overrides) -> None:
    """Z1, Z2 (missing resource), Z5."""
    response = _authorize(_client(store), **overrides)
    query = _query(response)
    assert response.headers["location"].startswith(REDIRECT)
    assert query["error"] == ["invalid_request"]
    assert query["iss"] == [ORIGIN]
    if "state" not in overrides:
        assert query["state"] == ["opaque-state"]


@pytest.mark.parametrize(
    ("resource", "error"),
    [
        ("https://other.example/mcp", "invalid_target"),
        (f"{ORIGIN}/mcp/", None),
        ("HTTPS://FS.EXAMPLE.COM:443/mcp", None),
    ],
)
def test_resource_must_be_this_installation(store, resource: str, error) -> None:
    """Z2."""
    response = _authorize(_client(store), resource=resource)
    if error is None:
        assert urlsplit(response.headers["location"]).path == "/oauth/consent"
    else:
        assert _query(response)["error"] == [error]
        assert _query(response)["state"] == ["opaque-state"]


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("openid", "invalid_scope"),
        ("offline_access jobs.read", ["jobs.read"]),
        ("", None),
    ],
)
def test_scope_handling(store, scope: str, expected) -> None:
    """Z3."""
    response = _authorize(_client(store), scope=scope)
    if expected == "invalid_scope":
        assert _query(response)["error"] == ["invalid_scope"]
        return
    request_id = _query(response)["request"][0]
    stored = store.authorizations[request_id]["requested_scopes"]
    if expected is None:
        assert len(stored) == 7 and "admin:all" not in stored
    else:
        assert stored == expected


def test_unsupported_response_type(store) -> None:
    assert _query(_authorize(_client(store), response_type="token"))["error"] == [
        "unsupported_response_type"
    ]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"redirect_uri": "https://evil.example/cb"}, "invalid_redirect_uri"),
        ({"redirect_uri": None}, "invalid_redirect_uri"),
        ({"client_id": "https://evil.example/client.json"}, "client_not_allowed"),
        ({"client_id": None}, "client_not_allowed"),
    ],
)
def test_untrusted_redirects_go_to_the_error_page_only(store, overrides, code: str) -> None:
    """Z4: no redirect to a supplied URI before it is validated."""
    response = _authorize(_client(store), **overrides)
    assert response.headers["location"] == f"/oauth/consent?error={code}"
    if code == "client_not_allowed":
        assert store.fetches == []
        assert store.audit[-1]["action_type"] == "auth_oauth_client_rejected"


def test_cached_client_document_is_reused_within_its_lifetime(store) -> None:
    client = _client(store)
    _authorize(client)
    _authorize(client)
    assert store.fetches == [CLIENT_ID]


def test_token_endpoint_requires_a_form_body_and_client_id(store) -> None:
    """T8."""
    client = _client(store)
    as_json = client.post("/oauth/token", json={"grant_type": "authorization_code"})
    assert as_json.status_code == 400
    assert as_json.json()["error"] == "invalid_request"
    assert as_json.headers["cache-control"] == "no-store"
    assert as_json.headers["pragma"] == "no-cache"
    missing = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": "x"})
    assert missing.status_code == 400
    assert missing.json()["error"] == "invalid_request"
    repeated = client.post(
        "/oauth/token",
        content="client_id=a&client_id=b&grant_type=refresh_token",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert repeated.json()["error"] == "invalid_request"
    unknown = client.post("/oauth/token", data={"grant_type": "password", "client_id": CLIENT_ID})
    assert unknown.json()["error"] == "unsupported_grant_type"


def test_revocation_requires_token_and_client(store) -> None:
    response = _client(store).post("/oauth/revoke", data={"token": "x"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
