"""Task 4: the AI-agent wizard's rules (endpoint, blockers, presets, one token per attempt).

The service runs for real. Only the transaction helper, the project lookup and the
idempotency table are replaced by in-memory doubles; the PostgreSQL suite
(``tests/integration/test_agent_connection_postgres.py``) runs the same flows on the
real tables, including two concurrent requests of one attempt.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.auth import agent_connection_service as service_module
from core.application.auth.agent_access_presets import OPERATIONAL_SCOPES
from core.application.auth.agent_connection_catalog import CLAUDE_CODE_LABEL, OPENAI_CLI_ID
from core.application.auth.agent_connection_service import (
    AgentConnectionError,
    AgentConnectionService,
    agent_mcp_endpoint,
)
from core.application.auth.inbound_oauth_policy import (
    OAuthEnvironment,
    inbound_oauth_readiness,
    is_interactive_only_path,
    oauth_principal_denied_route,
)
from core.entrypoints.api.agent_connection_routes import create_agent_connection_router
from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
ACCOUNT = "00000000-0000-4000-8000-0000000000aa"
PROJECT = "00000000-0000-4000-8000-000000000001"
SECRET_PREFIX = "fs_pat_test_"  # pragma: allowlist secret


def _user(role: str = "operator") -> dict[str, Any]:
    return {
        "id": f"user-{role}",
        "username": f"{role}@example.test",
        "roles": [role],
        "auth_source": "local",
        "account_id": ACCOUNT,
        "scopes": [],
    }


class _Store:
    """The identity store's PAT methods and the audit log, in memory."""

    def __init__(self) -> None:
        self.conn = object()
        self.tokens: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []

    def create_pat(self, **values: Any) -> dict[str, Any]:
        token_id = f"tok-{len(self.tokens) + 1}"
        row = {
            "id": token_id,
            "label": values["label"],
            "scopes": list(values["scopes"]),
            "allowed_project_ids": list(values["allowed_project_ids"]),
            "allowed_workflow_ids": [],
            "expires_at": "2026-11-30 12:00:00+00:00",
            "created_at": "2026-10-31 12:00:00+00:00",
            "last_used_at": None,
            "revoked": False,
            "user_id": values["user_id"],
        }
        self.tokens.append(row)
        return {**row, "raw_token": f"{SECRET_PREFIX}{token_id}"}

    def list_user_pats(self, user_id: str, *, include_revoked: bool = False):
        return [
            dict(row)
            for row in self.tokens
            if row["user_id"] == user_id and (include_revoked or not row["revoked"])
        ]

    def log_action(self, **values: Any) -> None:
        self.audit.append(values)


class _Idempotency:
    """``request_idempotency`` rows with the claim semantics the service relies on."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def insert_pending(self, **values: Any):
        key = (values["scope_key"], values["key_digest"])
        if key in self.rows:
            return None
        self.rows[key] = {**values, "status": "pending", "replay_response_jsonb": None}
        return dict(self.rows[key])

    def load_for_update(self, *, scope_key: str, key_digest: str):
        row = self.rows.get((scope_key, key_digest))
        return dict(row) if row else None

    def accept(self, **values: Any) -> None:
        row = self.rows[(values["scope_key"], values["key_digest"])]
        row.update(
            status="accepted",
            replay_response_jsonb=values["response"],
            result_expires_at=values["result_expires_at"],
            tombstone_expires_at=values["tombstone_expires_at"],
        )


@pytest.fixture
def world(monkeypatch):
    store = _Store()
    table = _Idempotency()
    snapshots: list[dict] = []

    @contextlib.contextmanager
    def _transaction(_conn):
        # A failure rolls back the claim, like the real transaction.
        before = {key: dict(row) for key, row in table.rows.items()}
        snapshots.append(before)
        try:
            yield object()
        except Exception:
            table.rows = before
            raise

    monkeypatch.setattr(service_module, "transaction_connection", _transaction)
    monkeypatch.setattr(service_module, "RequestIdempotencyRepository", lambda _conn: table)
    monkeypatch.setattr(
        service_module.oauth_repo,
        "accessible_project_ids",
        lambda _conn, _account, ids: {item for item in ids if item == PROJECT},
    )
    return store, table


def _service(
    store: _Store, base_url: str = "https://fs.example.com", **env
) -> AgentConnectionService:
    environment = OAuthEnvironment(public_base_url=base_url, forward_secret="x" * 32, **env)
    return AgentConnectionService(
        store,
        environment=environment,
        oauth_readiness=lambda _account: inbound_oauth_readiness(
            environment, storage_available=True, enabled=True
        ),
        core_version="0.9.6",
        clock=lambda: NOW,
    )


def _issue(service: AgentConnectionService, user=None, key: str = "attempt-1", **overrides):
    request = {
        "client_id": "claude_code",
        "package_id": "flow-steward-operator",
        "preset_id": "operational",
        "project_ids": [],
        "expiry_days": 30,
    }
    request.update(overrides)
    return service.issue_token(user or _user(), attempt_key=key, **request)


# --- endpoint -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "resource", "expected"),
    [
        ("https://fs.example.com", "", ("https://fs.example.com/mcp", "")),
        ("https://fs.example.com:443/", "", ("https://fs.example.com/mcp", "")),
        ("https://localhost:8443", "", ("https://localhost:8443/mcp", "")),
        ("", "", ("", "public_address_missing")),
        ("https://fs.example.com", "http://fs-mcp:8100/mcp", ("", "internal_address")),
        (
            "https://fs.example.com",
            "https://mcp.example.com/mcp",
            ("https://mcp.example.com/mcp", ""),
        ),
    ],
)
def test_the_mcp_address_comes_from_the_public_configuration(base_url, resource, expected) -> None:
    endpoint = agent_mcp_endpoint(
        OAuthEnvironment(public_base_url=base_url, resource_override=resource)
    )
    expected_url, expected_blocker = expected
    assert endpoint.url == expected_url
    assert endpoint.blocker == expected_blocker


def test_no_development_fallback_address_is_ever_handed_out(world) -> None:
    store, _table = world
    view = _service(store, base_url="").setup_view(_user())
    assert view["mcp_url"] == ""
    assert view["mcp_blocker"] == "public_address_missing"
    assert "localhost:8100" not in str(view)
    assert "fs-app" not in str(view)
    service = _service(store, base_url="")
    with pytest.raises(AgentConnectionError) as refused:
        _issue(service)
    assert refused.value.code == "public_address_missing"
    assert store.tokens == []


# --- setup view -------------------------------------------------------------------------


def test_cloud_clients_get_the_oauth_blocker_on_localhost(world) -> None:
    store, _table = world
    view = _service(store, base_url="https://localhost:8443").setup_view(_user())
    clients = {client["id"]: client for client in view["clients"]}
    assert clients["claude_code"]["mcp_blockers"] == []
    assert "public_https_required" in clients["chatgpt"]["mcp_blockers"]
    assert "public_https_required" in clients["claude_ai"]["mcp_blockers"]
    assert view["cloud_readiness"]["ready"] is False


def test_a_public_https_install_has_no_cloud_blocker(world) -> None:
    store, _table = world
    view = _service(store).setup_view(_user())
    assert view["cloud_readiness"] == {"ready": True, "blockers": []}
    claude_ai = next(client for client in view["clients"] if client["id"] == "claude_ai")
    operator = claude_ai["packages"][0]
    assert operator["auth"] == "oauth"
    connector = next(step for step in operator["steps"] if step["id"] == "custom_connector")
    assert connector["content"] == "https://fs.example.com/mcp"
    assert connector["uses_token"] is False


def test_steps_are_filled_except_the_token(world) -> None:
    store, _table = world
    view = _service(store).setup_view(_user())
    local = next(client for client in view["clients"] if client["id"] == OPENAI_CLI_ID)
    steps = {step["id"]: step for step in local["packages"][0]["steps"]}
    assert 'url = "https://fs.example.com/mcp"' in steps["mcp_config"]["content"]
    assert steps["mcp_config"]["uses_token"] is False
    assert steps["token_environment"]["content"] == 'export FLOW_STEWARD_TOKEN="{token}"'
    assert steps["download_package"]["values"]["archive"] == "flow-steward-operator-1.0.1.zip"
    assert steps["download_package"]["url"].endswith("/releases/tag/v0.9.6")
    assert steps["open_readme"]["url"].endswith(
        f"/blob/v0.9.6/docs/agent-skills/flow-steward-operator/README.md#{OPENAI_CLI_ID}"
    )
    cursor = next(client for client in view["clients"] if client["id"] == "cursor")
    cursor_config = next(s for s in cursor["packages"][0]["steps"] if s["id"] == "mcp_config")
    assert "${env:FLOW_STEWARD_TOKEN}" in cursor_config["content"]


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("viewer", {"operational": False, "full_authoring": False}),
        ("operator", {"operational": True, "full_authoring": False}),
        ("admin", {"operational": True, "full_authoring": True}),
    ],
)
def test_presets_are_the_shared_contract_capped_by_the_caller(world, role, expected) -> None:
    store, _table = world
    presets = _service(store).setup_view(_user(role))["presets"]
    assert {preset["id"]: preset["available"] for preset in presets} == expected
    for preset in presets:
        assert preset["reason"] == ("" if preset["available"] else "exceeds_authority")
    assert next(p for p in presets if p["id"] == "operational")["scopes"] == list(
        OPERATIONAL_SCOPES
    )


def test_operational_scopes_find_a_project_and_workflow_and_start_it() -> None:
    for tool in ("project_list", "workflow_list", "workflow_trigger", "job_status"):
        assert set(MCP_TOOL_SCOPES[tool]) & set(OPERATIONAL_SCOPES), tool


# --- issuance ---------------------------------------------------------------------------


def test_one_attempt_creates_one_token_and_a_repeat_never_shows_the_secret(world) -> None:
    store, _table = world
    service = _service(store)
    first = _issue(service)
    assert first["status"] == "created"
    assert first["secret"] == f"{SECRET_PREFIX}tok-1"
    assert first["token"]["scopes"] == list(OPERATIONAL_SCOPES)
    assert first["token"]["label"] == f"AI agent · {CLAUDE_CODE_LABEL}"
    for _ in range(3):  # double click, retry after a lost response, a third try
        repeat = _issue(service)
        assert repeat["status"] == "replayed"
        assert repeat["secret"] == ""
        assert repeat["secret_already_shown"] is True
        assert repeat["token"]["id"] == "tok-1"
        assert SECRET_PREFIX not in str(repeat)
    assert len(store.tokens) == 1
    assert [entry["action_type"] for entry in store.audit] == ["auth_token_create"]
    assert SECRET_PREFIX not in str(store.audit)


def test_a_replay_shows_the_token_as_it_is_now(world) -> None:
    store, _table = world
    service = _service(store)
    _issue(service)
    store.tokens[0]["revoked"] = True
    store.tokens[0]["last_used_at"] = "2026-10-31 12:05:00+00:00"
    repeat = _issue(service)
    assert repeat["token"]["revoked"] is True
    assert repeat["token"]["last_used_at"] == "2026-10-31 12:05:00+00:00"


def test_the_same_attempt_with_other_settings_is_refused(world) -> None:
    store, _table = world
    service = _service(store)
    _issue(service)
    with pytest.raises(AgentConnectionError) as refused:
        _issue(service, expiry_days=90)
    assert (refused.value.code, refused.value.status) == ("attempt_mismatch", 409)
    assert len(store.tokens) == 1


def test_a_new_attempt_is_a_new_token(world) -> None:
    store, _table = world
    service = _service(store)
    _issue(service, key="attempt-1")
    _issue(service, key="attempt-2")
    assert len(store.tokens) == 2


def test_a_refused_attempt_leaves_no_claim_behind(world) -> None:
    store, table = world
    service = _service(store)
    viewer = _user("viewer")
    with pytest.raises(AgentConnectionError) as refused:
        _issue(service, user=viewer)
    assert refused.value.code == "exceeds_authority"
    assert store.tokens == []
    assert table.rows == {}
    with pytest.raises(AgentConnectionError) as refused:
        _issue(service, preset_id="full_authoring")
    assert refused.value.code == "exceeds_authority"
    admin = _issue(service, user=_user("admin"), key="admin-1", preset_id="full_authoring")
    assert admin["token"]["scopes"] == ["admin:all"]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"client_id": "chatgpt"}, "token_not_used"),
        ({"client_id": "claude_ai"}, "token_not_used"),
        ({"package_id": "flow-steward-developer"}, "token_not_used"),
        ({"client_id": "nope"}, "unknown_client"),
        ({"preset_id": "everything"}, "invalid_preset"),
        ({"expiry_days": 365}, "invalid_expiry"),
        ({"expiry_days": None}, "invalid_expiry"),
        ({"project_ids": ["00000000-0000-4000-8000-0000000000ff"]}, "project_not_accessible"),
    ],
)
def test_unsupported_requests_are_refused_without_a_token(world, overrides, code) -> None:
    store, table = world
    service = _service(store)
    with pytest.raises(AgentConnectionError) as refused:
        _issue(service, **overrides)
    assert refused.value.code == code
    assert store.tokens == []
    assert table.rows == {}


def test_projects_restrict_the_token(world) -> None:
    store, _table = world
    created = _issue(_service(store), project_ids=[PROJECT, PROJECT])
    assert created["token"]["allowed_project_ids"] == [PROJECT]


def test_token_status_reports_only_the_token_facts(world) -> None:
    store, _table = world
    service = _service(store)
    created = _issue(service)
    status = service.token_status(_user(), created["token"]["id"])["token"]
    assert status["last_used_at"] is None
    assert set(status) == {
        "id",
        "label",
        "scopes",
        "allowed_project_ids",
        "expires_at",
        "created_at",
        "last_used_at",
        "revoked",
        "expired",
    }
    admin = _user("admin")
    with pytest.raises(AgentConnectionError) as missing:
        service.token_status(admin, created["token"]["id"])
    assert missing.value.status == 404


# --- HTTP adapter -----------------------------------------------------------------------


def _http(service: AgentConnectionService, user: dict | None = None) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _session(request, call_next):
        request.state.auth_user = user or _user()
        return await call_next(request)

    app.include_router(create_agent_connection_router(service_factory=lambda: service))
    return TestClient(app)


_BODY = {
    "client_id": "claude_code",
    "package_id": "flow-steward-operator",
    "preset": "operational",
    "expiry_days": 30,
    "project_ids": [],
}


def test_http_creation_then_replay_without_the_secret(world) -> None:
    store, _table = world
    client = _http(_service(store))
    created = client.post(
        "/api/auth/agent-access/tokens", json=_BODY, headers={"Idempotency-Key": "k-1"}
    )
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    assert created.json()["secret"].startswith(SECRET_PREFIX)
    repeat = client.post(
        "/api/auth/agent-access/tokens", json=_BODY, headers={"Idempotency-Key": "k-1"}
    )
    assert repeat.status_code == 200
    assert repeat.json()["secret_already_shown"] is True
    assert SECRET_PREFIX not in repeat.text
    status = client.get(f"/api/auth/agent-access/tokens/{created.json()['token']['id']}")
    assert status.status_code == 200
    assert SECRET_PREFIX not in status.text
    assert len(store.tokens) == 1


def test_http_requires_an_attempt_key(world) -> None:
    store, _table = world
    client = _http(_service(store))
    missing = client.post("/api/auth/agent-access/tokens", json=_BODY)
    assert missing.status_code == 428
    bad = client.post(
        "/api/auth/agent-access/tokens", json=_BODY, headers={"Idempotency-Key": "x" * 300}
    )
    assert bad.status_code == 422
    assert store.tokens == []


def test_http_maps_refusals_and_serves_the_setup(world) -> None:
    store, _table = world
    client = _http(_service(store), _user("viewer"))
    refused = client.post(
        "/api/auth/agent-access/tokens", json=_BODY, headers={"Idempotency-Key": "k-2"}
    )
    assert refused.status_code == 403
    assert refused.json()["error"] == "exceeds_authority"
    setup = client.get("/api/auth/agent-access/setup")
    assert setup.status_code == 200
    assert setup.json()["mcp_url"] == "https://fs.example.com/mcp"


def test_only_a_browser_session_reaches_the_wizard_routes() -> None:
    for path in (
        "/api/auth/agent-access/setup",
        "/api/auth/agent-access/tokens",
        "/api/auth/agent-access/tokens/abc",
    ):
        assert is_interactive_only_path(path)
        assert oauth_principal_denied_route(path, "POST")
