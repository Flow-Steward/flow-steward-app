"""Task 4: the AI-agent wizard's rules (endpoint, blockers, presets, one token per attempt).

The service runs for real. Only the transaction helper, the project lookup and the
idempotency table are replaced by in-memory doubles; the PostgreSQL suite
(``tests/integration/test_agent_connection_postgres.py``) runs the same flows on the
real tables, including two concurrent requests of one attempt.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.auth import agent_connection_service as service_module
from core.application.auth.agent_access_presets import (
    OPERATIONAL_SCOPES,
    ROLE_PRESET_EXPIRY_DAYS,
    role_preset_scopes,
)
from core.application.auth.agent_connection_catalog import CLAUDE_CODE_LABEL, OPENAI_CLI_ID
from core.application.auth.agent_connection_service import (
    AgentConnectionError,
    AgentConnectionService,
    agent_mcp_endpoint,
)
from core.application.auth.effective_permissions import effective_permissions
from core.application.auth.inbound_oauth_policy import (
    MAX_GRANT_EXPIRY_DAYS,
    OAuthEnvironment,
    inbound_oauth_readiness,
    is_interactive_only_path,
    oauth_principal_denied_route,
)
from core.application.auth.permissions import SYSTEM_ROLE_PERMISSIONS
from core.entrypoints.api.agent_connection_routes import create_agent_connection_router
from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
ACCOUNT = "00000000-0000-4000-8000-0000000000aa"
PROJECT = "00000000-0000-4000-8000-000000000001"
SECRET_PREFIX = "fs_pat_test_"  # pragma: allowlist secret


def _user(role: str = "operator") -> dict[str, Any]:
    user = {
        "id": f"user-{role}",
        "username": f"{role}@example.test",
        "roles": [role],
        "auth_source": "local",
        "account_id": ACCOUNT,
        "scopes": [],
    }
    # The auth middleware puts the person's role permissions on the request.
    granted = effective_permissions(user, role_permissions=SYSTEM_ROLE_PERMISSIONS)
    return {**user, "permissions": sorted(granted.permissions)}


#: What an operator's "as my role" token holds (ADR 0076 §5).
OPERATOR_ROLE_SCOPES = ["read:*", "registry.read", "command:*"]
#: An admin's advanced options: a preset and an expiry.
ADVANCED = {"preset_id": "operational", "expiry_days": 30}


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
    request: dict[str, Any] = {
        "client_id": "claude_code",
        "package_id": "flow-steward-operator",
        "project_ids": [],
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
    # The plugin's server file already carries the address; only the browser adds the token.
    plugin = steps["download_plugin"]
    assert (plugin["kind"], plugin["archive_layout"]) == ("package", "agents_marketplace")
    assert plugin["manifest_dir"] == f".{OPENAI_CLI_ID}-plugin"
    assert plugin["file_name"] == f"flow-steward-operator-{OPENAI_CLI_ID}.zip"
    assert plugin["uses_token"] is True
    assert json.loads(plugin["content"].replace("{token}", "fs_pat_x")) == {
        "mcpServers": {
            "flow-steward": {
                "type": "http",
                "url": "https://fs.example.com/mcp",
                "http_headers": {"Authorization": "Bearer fs_pat_x"},
            }
        }
    }
    assert steps["install_plugin"]["content"].splitlines() == [
        "cd ~",
        f"{OPENAI_CLI_ID} plugin marketplace add ./flow-steward-operator-{OPENAI_CLI_ID}",
        f"{OPENAI_CLI_ID} plugin add flow-steward-operator@flow-steward-operator",
    ]
    assert "FLOW_STEWARD_TOKEN" not in str(view)
    assert "{" + "token}" not in steps["install_plugin"]["content"]
    cursor = next(client for client in view["clients"] if client["id"] == "cursor")
    cursor_plugin = next(s for s in cursor["packages"][0]["steps"] if s["id"] == "download_plugin")
    assert cursor_plugin["archive_layout"] == "plugin_folder"
    assert json.loads(cursor_plugin["content"].replace("{token}", "fs_pat_x")) == {
        "mcpServers": {
            "flow-steward": {
                "url": "https://fs.example.com/mcp",
                "headers": {"Authorization": "Bearer fs_pat_x"},
            }
        }
    }
    developer = {step["id"]: step for step in local["packages"][1]["steps"]}
    assert developer["download_plugin"]["uses_token"] is False
    assert developer["download_plugin"]["content"] == ""
    assert developer["install_plugin"]["content"].splitlines()[-1] == (
        f"{OPENAI_CLI_ID} plugin add flow-steward-developer@flow-steward-developer"
    )


def test_claude_code_download_contains_only_the_address_and_offers_the_token_separately(
    world,
) -> None:
    store, _table = world
    view = _service(store).setup_view(_user())
    claude = next(client for client in view["clients"] if client["id"] == "claude_code")
    steps = {step["id"]: step for step in claude["packages"][0]["steps"]}
    plugin = steps["download_plugin"]
    assert (plugin["kind"], plugin["archive_layout"]) == ("package", "claude_marketplace")
    assert plugin["file_name"] == "flow-steward-operator-claude-code.zip"
    assert plugin["uses_token"] is False
    assert json.loads(plugin["content"]) == {
        "mcpServers": {
            "flow-steward": {
                "type": "http",
                "url": "https://fs.example.com/mcp",
            }
        }
    }
    assert steps["install_plugin"]["content"].splitlines() == [
        "cd ~",
        "claude plugin marketplace add ./flow-steward-operator-claude-code",
        "claude plugin install flow-steward-operator@flow-steward-operator",
    ]
    assert steps["install_plugin"]["uses_token"] is False
    assert steps["install_plugin"]["values"]["package"] == "flow-steward-operator"
    assert steps["configure_plugin_token"]["kind"] == "value"
    assert steps["configure_plugin_token"]["uses_token"] is True
    assert steps["configure_plugin_token"]["content"] == "{token}"
    developer = {step["id"]: step for step in claude["packages"][1]["steps"]}
    assert developer["download_plugin"]["file_name"] == "flow-steward-developer-claude-code.zip"
    assert developer["download_plugin"]["uses_token"] is False
    assert "configure_plugin_token" not in developer


def test_another_client_gets_plain_mcp_json_with_the_token(world) -> None:
    store, _table = world
    view = _service(store).setup_view(_user())
    other = next(client for client in view["clients"] if client["id"] == "other")
    assert other["connection"] == "local"
    operator = other["packages"][0]
    assert operator["auth"] == "personal_token"
    config = next(step for step in operator["steps"] if step["id"] == "mcp_config")
    assert json.loads(config["content"].replace("{token}", "fs_pat_x")) == {
        "mcpServers": {
            "flow-steward": {
                "type": "http",
                "url": "https://fs.example.com/mcp",
                "headers": {"Authorization": "Bearer fs_pat_x"},
            }
        }
    }


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
    assert first["token"]["scopes"] == OPERATOR_ROLE_SCOPES
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
    admin = _user("admin")
    _issue(service, user=admin, **ADVANCED)
    with pytest.raises(AgentConnectionError) as refused:
        _issue(service, user=admin, preset_id="operational", expiry_days=90)
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
    admin = _user("admin")
    with pytest.raises(AgentConnectionError) as refused:
        _issue(service, user=admin, project_ids=["00000000-0000-4000-8000-0000000000ff"])
    assert refused.value.code == "project_not_accessible"
    assert store.tokens == []
    assert table.rows == {}
    with pytest.raises(AgentConnectionError) as refused:
        _issue(service, preset_id="full_authoring")
    assert refused.value.code == "advanced_options_admin_only"
    created = _issue(service, user=admin, key="admin-1", preset_id="full_authoring", expiry_days=30)
    assert created["token"]["scopes"] == ["admin:all"]


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
        _issue(service, user=_user("admin"), **{**ADVANCED, **overrides})
    assert refused.value.code == code
    assert store.tokens == []
    assert table.rows == {}


def test_projects_restrict_the_token(world) -> None:
    store, _table = world
    created = _issue(_service(store), user=_user("admin"), project_ids=[PROJECT, PROJECT])
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


_BODY = {"client_id": "claude_code", "package_id": "flow-steward-operator"}
_ADVANCED_BODY = {**_BODY, "preset": "operational", "expiry_days": 30, "project_ids": []}


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
        "/api/auth/agent-access/tokens", json=_ADVANCED_BODY, headers={"Idempotency-Key": "k-2"}
    )
    assert refused.status_code == 403
    assert refused.json()["error"] == "advanced_options_admin_only"
    setup = client.get("/api/auth/agent-access/setup")
    assert setup.status_code == 200
    assert setup.json()["mcp_url"] == "https://fs.example.com/mcp"


def test_only_a_browser_session_reaches_the_wizard_routes() -> None:
    for path in (
        "/api/auth/agent-access/setup",
        "/api/auth/agent-access/tokens",
        "/api/auth/agent-access/tokens/abc",
        "/api/auth/agent-access/connections",
    ):
        assert is_interactive_only_path(path)
        assert oauth_principal_denied_route(path, "POST")


# --- ADR 0076 §5: "as my role" ----------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "scopes"),
    [
        ("viewer", ["read:*", "registry.read"]),
        ("operator", OPERATOR_ROLE_SCOPES),
        ("admin", ["admin:all"]),
    ],
)
def test_one_button_gives_the_token_the_persons_role(world, role, scopes) -> None:
    store, _table = world
    created = _issue(_service(store), user=_user(role))

    assert created["token"]["scopes"] == scopes
    token = {"roles": [role], "auth_source": "pat", "scopes": scopes}
    held = effective_permissions(token, role_permissions=SYSTEM_ROLE_PERMISSIONS).permissions
    owner = effective_permissions(_user(role), role_permissions=SYSTEM_ROLE_PERMISSIONS)
    # Exactly the owner's permissions, except the secret catalog's exact ones (ADR 0057).
    assert held == owner.permissions - {
        "secrets.value.write",
        "secrets.delete",
        "secrets.bind",
        "secrets.approve",
    }


def test_the_server_chooses_the_expiry_of_a_role_token(world, monkeypatch) -> None:
    store, _table = world
    seen: dict[str, Any] = {}
    original = store.create_pat

    def _create(**values: Any) -> dict[str, Any]:
        seen.update(values)
        return original(**values)

    monkeypatch.setattr(store, "create_pat", _create)
    _issue(_service(store))

    assert seen["expires_in_days"] == ROLE_PRESET_EXPIRY_DAYS == MAX_GRANT_EXPIRY_DAYS


@pytest.mark.parametrize("role", ["operator", "viewer"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"preset_id": "operational"},
        {"preset_id": "full_authoring"},
        {"expiry_days": 30},
        {"project_ids": [PROJECT]},
    ],
)
def test_an_operator_or_viewer_chooses_nothing(world, role, overrides) -> None:
    store, table = world
    with pytest.raises(AgentConnectionError) as refused:
        _issue(_service(store), user=_user(role), **overrides)

    assert (refused.value.code, refused.value.status) == ("advanced_options_admin_only", 403)
    assert store.tokens == []
    assert table.rows == {}


def test_naming_the_role_preset_is_the_same_as_choosing_nothing(world) -> None:
    store, _table = world
    created = _issue(_service(store), preset_id="my_role")

    assert created["token"]["scopes"] == OPERATOR_ROLE_SCOPES


@pytest.mark.parametrize("role", ["admin", "operator", "viewer"])
def test_the_setup_describes_the_role_access(world, role) -> None:
    store, _table = world
    access = _service(store).setup_view(_user(role))["role_access"]

    assert access["preset"] == "my_role"
    assert access["expiry_days"] == ROLE_PRESET_EXPIRY_DAYS
    assert access["advanced_options"] is (role == "admin")
    expected = {"admin": ["admin:all"], "operator": OPERATOR_ROLE_SCOPES}
    assert access["scopes"] == expected.get(role, ["read:*", "registry.read"])


def test_http_admin_keeps_the_advanced_options(world) -> None:
    store, _table = world
    client = _http(_service(store), _user("admin"))
    created = client.post(
        "/api/auth/agent-access/tokens", json=_ADVANCED_BODY, headers={"Idempotency-Key": "k-9"}
    )
    assert created.status_code == 201
    assert created.json()["token"]["scopes"] == list(OPERATIONAL_SCOPES)


def test_a_role_token_passes_the_scope_check_of_every_route_its_role_may_call() -> None:
    """The frozen route snapshot lists each route's token scopes: an "as my role"
    token satisfies them wherever its owner's role is let through, except the secret
    catalog's exact-scope writes no preset grants."""
    from core.application.auth.principal import principal_has_required_scopes
    from core.application.auth.route_permissions import route_permission
    from tests.unit.test_route_permission_parity import _snapshot

    for role, initial in (("operator", "o"), ("viewer", "v")):
        token = {"scopes": list(role_preset_scopes(SYSTEM_ROLE_PERMISSIONS[role]))}
        missing = [
            route
            for route, entry in _snapshot().items()
            if initial in entry["pat"]
            and entry["pat_scopes"]
            and not set(entry["pat_scopes"]) <= {"secrets:write", "secrets:bind"}
            and not principal_has_required_scopes(token, entry["pat_scopes"])
        ]
        if role == "operator":
            # The builder's run route asks a token for admin:all, which no operator
            # token can hold; an agent starts a run through POST /api/workflow-runs.
            assert missing == ["POST /api/workflows/{workflow_id}/run"]
        else:
            # Own-account writes ask a token for command:*, above a viewer token's
            # ceiling (ADR 0066): a viewer changes their account in the browser.
            assert missing, role
            for route in missing:
                method, path = route.split(" ", 1)
                match = route_permission(method, path.replace("{", "").replace("}", ""))
                assert match is not None and match.permission == "account.self", route


# --- ADR 0076 §5: every connection says when it ends ------------------------------------


def test_the_connections_say_when_they_end_and_which_end_soon(world, monkeypatch) -> None:
    from datetime import timedelta

    store, _table = world
    seen: list[tuple[str, datetime]] = []

    def connections(_conn, user_id, *, now):
        seen.append((user_id, now))
        return [
            {
                "kind": "personal_token",
                "id": "tok-1",
                "label": f"AI agent · {CLAUDE_CODE_LABEL}",
                "created_at": now - timedelta(days=85),
                "expires_at": now + timedelta(days=5),
                "last_used_at": None,
            },
            {
                "kind": "oauth_grant",
                "id": "grant-1",
                "label": "ChatGPT",
                "client_id": "https://chatgpt.com/client",
                "created_at": now - timedelta(days=1),
                "expires_at": now + timedelta(days=89),
                "last_used_at": now,
            },
        ]

    monkeypatch.setattr(service_module.connection_repo, "list_user_connections", connections)
    response = _http(_service(store), _user("viewer")).get("/api/auth/agent-access/connections")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert seen == [("user-viewer", NOW)]
    assert payload["reminder_days"] == 7
    token, grant = payload["connections"]
    assert token["client_id"] == "claude_code"
    assert token["ends_at"] == (NOW + timedelta(days=5)).isoformat()
    assert token["ending_soon"] is True
    assert token["days_left"] == 5
    assert token["renew"] == "connect_again"
    assert grant["client_label"] == "ChatGPT"
    assert grant["ending_soon"] is False
    assert grant["renew"] == "reconnect_from_client"


def test_the_wizard_labels_its_tokens_for_the_connection_list(world) -> None:
    store, _table = world
    _issue(_service(store), user=_user("operator"))
    assert store.tokens[0]["label"] == f"AI agent · {CLAUDE_CODE_LABEL}"
