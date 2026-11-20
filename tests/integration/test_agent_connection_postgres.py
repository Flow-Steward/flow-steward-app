"""Task 4 wizard token issuance on real PostgreSQL, through the real ``fs-app`` stack.

One attempt creates at most one personal token: a double click, two concurrent
requests and a retry after a lost response all end with one row in
``personal_access_tokens`` and the secret shown once. An operator's one-button token
("as my role", ADR 0076 §5) then finds a project and its workflows without any id
known in advance.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from core.application.auth.agent_connection_service import (
    AgentConnectionError,
    AgentConnectionService,
)
from tests._db_env import resolve_test_db_dsn
from tests.integration._inbound_oauth_world import CSRF, ORIGIN, build_world, oauth_environment

PATH = "/api/auth/agent-access/tokens"
#: An operator or a viewer chooses nothing: the token gets their role (ADR 0076 §5).
BODY = {"client_id": "claude_code", "package_id": "flow-steward-operator"}
#: Only an admin chooses a preset, an expiry and projects.
ADVANCED_BODY = {**BODY, "preset": "operational", "expiry_days": 30, "project_ids": []}


@pytest.fixture(autouse=True)
def require_audit_database(pg_conn):
    assert pg_conn.info.host == "pg-audit"
    assert pg_conn.info.dbname == "flow_steward_audit"


@pytest.fixture
def world(pg_conn, default_account, monkeypatch):
    oauth_environment(monkeypatch)
    state = build_world(pg_conn, resolve_test_db_dsn())
    try:
        yield state
    finally:
        state.sql(
            "DELETE FROM request_idempotency WHERE replay_response_jsonb->>'token_id' = ANY("
            "SELECT id::text FROM personal_access_tokens WHERE user_id = ANY(%s::uuid[]))",
            ([user["id"] for user in state.users.values()],),
        )
        state.cleanup()


@pytest.fixture
def http(world, monkeypatch):
    from fastapi.testclient import TestClient

    from core.entrypoints.api import route_registration
    from core.entrypoints.api.app_factory import create_app
    from core.infrastructure.db.platform_stores import PlatformProjectStore

    project_store = PlatformProjectStore(world.dsn)
    app = create_app()
    app.state.local_auth_enabled_override = lambda: True
    app.state.external_auth_enabled_override = lambda: False
    factories = app.state.runtime_container.service_factories
    factories["identity_store"] = lambda: world.store
    factories["project_store"] = lambda: project_store
    monkeypatch.setattr(route_registration, "_inbound_oauth_service", lambda _app: world.service())
    clients: list = []

    def make(who: str | None = None, *, bearer: str = ""):
        client = TestClient(app, base_url=ORIGIN, follow_redirects=False)
        if who:
            client.cookies.set("fs_session", world.users[who]["session"])
            client.cookies.set("fs_csrf", CSRF)
        if bearer:
            client.headers["Authorization"] = f"Bearer {bearer}"
        clients.append(client)
        return client

    try:
        yield make
    finally:
        for client in clients:
            client.close()
        if project_store._direct_conn is not None:
            project_store._direct_conn.close()


def _post(client, key: str, body: dict | None = None):
    return client.post(
        PATH, json=body or BODY, headers={"Idempotency-Key": key, "x-csrf-token": CSRF}
    )


def _wizard_tokens(world, who: str) -> list[tuple]:
    return world.sql(
        "SELECT id::text, scopes_jsonb, revoked FROM personal_access_tokens "
        "WHERE user_id = %s AND label LIKE 'AI agent%%'",
        (world.users[who]["id"],),
    )


def test_double_click_and_retry_create_one_token_and_show_the_secret_once(world, http) -> None:
    browser = http("operator")
    key = f"attempt-{uuid.uuid4()}"
    first = _post(browser, key)
    assert first.status_code == 201, first.text
    secret = first.json()["secret"]
    assert secret.startswith("fs_pat_")
    for _ in range(2):
        repeat = _post(browser, key)
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()["secret_already_shown"] is True
        assert repeat.json()["token"]["id"] == first.json()["token"]["id"]
        assert secret not in repeat.text
    assert len(_wizard_tokens(world, "operator")) == 1
    stored = world.sql(
        "SELECT replay_response_jsonb::text FROM request_idempotency "
        "WHERE replay_response_jsonb->>'token_id' = %s",
        (first.json()["token"]["id"],),
    )
    assert len(stored) == 1
    assert secret not in stored[0][0]


def test_two_concurrent_requests_of_one_attempt_create_one_token(world, http) -> None:
    key = f"attempt-{uuid.uuid4()}"
    results: list = []
    barrier = threading.Barrier(2)

    def send() -> None:
        client = http("operator")
        barrier.wait()
        results.append(_post(client, key))

    threads = [threading.Thread(target=send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    statuses = sorted(response.status_code for response in results)
    assert statuses in ([200, 201], [201, 409]), [r.text for r in results]
    assert len(_wizard_tokens(world, "operator")) == 1
    assert sum(1 for response in results if response.json().get("secret")) == 1


def test_a_lost_response_is_answered_by_the_same_attempt(world) -> None:
    service = AgentConnectionService(world.store, oauth_readiness=world.service().readiness)
    admin = world.principal("admin")
    request = {
        "client_id": "cursor",
        "package_id": "flow-steward-operator",
        "preset_id": "operational",
        "project_ids": [world.projects[0]],
        "expiry_days": 7,
    }
    key = f"attempt-{uuid.uuid4()}"
    created = service.issue_token(admin, attempt_key=key, **request)  # response "lost"
    replay = service.issue_token(admin, attempt_key=key, **request)
    assert replay["status"] == "replayed"
    assert replay["secret"] == ""
    assert replay["token"]["id"] == created["token"]["id"]
    assert replay["token"]["allowed_project_ids"] == [world.projects[0]]
    with pytest.raises(AgentConnectionError) as mismatch:
        service.issue_token(admin, attempt_key=key, **{**request, "expiry_days": 30})
    assert mismatch.value.code == "attempt_mismatch"
    assert len(_wizard_tokens(world, "admin")) == 1


def test_a_refusal_leaves_neither_a_token_nor_a_claim(world, http) -> None:
    key = f"attempt-{uuid.uuid4()}"
    viewer = _post(http("viewer"), key, ADVANCED_BODY)
    assert viewer.status_code == 403, viewer.text
    assert viewer.json()["error"] == "advanced_options_admin_only"
    assert _wizard_tokens(world, "viewer") == []
    operator_full = _post(http("operator"), key, {**BODY, "preset": "full_authoring"})
    assert operator_full.status_code == 403
    # The refused attempt left no claim: the same key works once the request is valid.
    assert _post(http("operator"), key).status_code == 201


def test_only_a_browser_session_can_mint(world, http) -> None:
    pat = world.store.create_pat(
        user_id=world.users["admin"]["id"],
        account_id=world.principal("admin")["account_id"],
        label="bearer",
        scopes=["admin:all"],
    )["raw_token"]
    response = http(bearer=pat).post(PATH, json=BODY, headers={"Idempotency-Key": "k"})
    assert response.status_code == 403
    assert http(bearer=pat).get("/api/auth/agent-access/setup").status_code == 403


def test_the_fresh_operational_token_discovers_projects_and_workflows(world, http) -> None:
    created = _post(http("operator"), f"attempt-{uuid.uuid4()}").json()
    agent = http(bearer=created["secret"])
    projects = agent.get("/api/projects")
    assert projects.status_code == 200, projects.text
    project_id = world.projects[0]
    assert project_id in projects.text
    catalog = agent.get(f"/api/projects/{project_id}/workflow-catalog")
    assert catalog.status_code == 200, catalog.text
    status = http("operator").get(f"/api/auth/agent-access/tokens/{created['token']['id']}")
    assert status.status_code == 200
    assert status.json()["token"]["last_used_at"] is not None
    setup = http("operator").get("/api/auth/agent-access/setup").json()
    assert setup["mcp_url"] == f"{ORIGIN}/mcp"
    assert setup["cloud_readiness"]["ready"] is True


# --- ADR 0076 §5: a reminder before an agent connection stops working --------------------


def _oauth_grant(world, who: str, *, created_days_ago: int, ends_in_days: int) -> str:
    world.sql(
        "INSERT INTO oauth_clients (client_id, client_name, redirect_uris_jsonb, "
        "metadata_sha256, fetched_at, cache_expires_at, first_seen_at) "
        "VALUES ('https://claude.ai/reminder-client', 'Claude', '[]', 'x', now(), now(), now()) "
        "ON CONFLICT DO NOTHING"
    )
    grant_id = str(uuid.uuid4())
    world.sql(
        "INSERT INTO oauth_grants (id, user_id, account_id, client_id, client_name_snapshot, "
        "resource, preset, scopes_jsonb, allowed_project_ids_jsonb, created_at, expires_at) "
        "VALUES (%s, %s, %s, 'https://claude.ai/reminder-client', 'Claude', %s, 'my_role', "
        "'[\"jobs.read\"]', '[]', now() - %s * interval '1 day', "
        "now() + %s * interval '1 day')",
        (
            grant_id,
            world.users[who]["id"],
            world.principal(who)["account_id"],
            f"{ORIGIN}/mcp",
            created_days_ago,
            ends_in_days,
        ),
    )
    return grant_id


def _agent_token(world, who: str, *, label: str, created_days_ago: int, ends_in_days: int) -> str:
    token_id = world.store.create_pat(
        user_id=world.users[who]["id"],
        account_id=world.principal(who)["account_id"],
        label=label,
        scopes=["read:*"],
        expires_in_days=90,
    )["id"]
    world.sql(
        "UPDATE personal_access_tokens SET created_at = now() - %s * interval '1 day', "
        "expires_at = now() + %s * interval '1 day' WHERE id = %s",
        (created_days_ago, ends_in_days, token_id),
    )
    return token_id


def _reminded(world, table: str, row_id: str) -> bool:
    [(sent_at,)] = world.sql(
        f"SELECT expiry_reminder_sent_at FROM {table} WHERE id = %s",  # nosec B608 - test
        (row_id,),
    )
    return sent_at is not None


def test_each_agent_connection_ending_within_seven_days_is_reminded_once(world) -> None:
    from core.application.auth.agent_connection_reminders import AgentExpiryReminders

    due_token = _agent_token(
        world, "viewer", label="AI agent · Claude Code", created_days_ago=85, ends_in_days=5
    )
    later_token = _agent_token(
        world, "viewer", label="AI agent · Cursor", created_days_ago=10, ends_in_days=80
    )
    short_token = _agent_token(
        world, "viewer", label="AI agent · Cursor", created_days_ago=1, ends_in_days=6
    )
    other_token = _agent_token(
        world, "viewer", label="deploy script", created_days_ago=85, ends_in_days=5
    )
    due_grant = _oauth_grant(world, "operator", created_days_ago=84, ends_in_days=6)
    sent: list[dict] = []

    def send(**values):
        sent.append(values)
        return {"ok": True, "reason": "sent"}

    def reminder_pass(available: bool = True) -> dict:
        return AgentExpiryReminders(
            world.store, send_email=send, email_available=lambda: available
        ).run_once()

    # No email connection: the in-app reminder is enough and nothing is recorded.
    assert reminder_pass(available=False)["no_email"] == 1
    assert not _reminded(world, "personal_access_tokens", due_token)

    reminder_pass()
    ours = [
        item
        for item in sent
        if item["recipient"]
        in {world.users["viewer"]["username"], world.users["operator"]["username"]}
    ]
    assert sorted((item["client"], item["oauth_app"]) for item in ours) == [
        ("Claude", True),
        ("Claude Code", False),
    ]
    assert _reminded(world, "personal_access_tokens", due_token)
    assert _reminded(world, "oauth_grants", due_grant)
    for untouched in (later_token, short_token, other_token):
        assert not _reminded(world, "personal_access_tokens", untouched)

    # A restart runs the pass again: nothing more is sent.
    count = len(sent)
    reminder_pass()
    assert len(sent) == count


def test_the_ai_agents_page_lists_connections_with_their_end(world, http) -> None:
    due_token = _agent_token(
        world, "viewer", label="AI agent · Claude Code", created_days_ago=85, ends_in_days=5
    )
    _agent_token(world, "viewer", label="deploy script", created_days_ago=85, ends_in_days=5)
    grant = _oauth_grant(world, "viewer", created_days_ago=1, ends_in_days=60)
    response = http("viewer").get("/api/auth/agent-access/connections")
    assert response.status_code == 200, response.text
    connections = response.json()["connections"]
    assert [item["id"] for item in connections] == [due_token, grant]
    token, app = connections
    assert token["kind"] == "personal_token"
    assert token["client_id"] == "claude_code"
    assert token["ending_soon"] is True
    assert token["renew"] == "connect_again"
    assert app["kind"] == "oauth_grant"
    assert app["ending_soon"] is False
    assert app["renew"] == "reconnect_from_client"
