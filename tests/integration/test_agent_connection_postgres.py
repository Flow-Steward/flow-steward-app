"""Task 4 wizard token issuance on real PostgreSQL, through the real ``fs-app`` stack.

One attempt creates at most one personal token: a double click, two concurrent
requests and a retry after a lost response all end with one row in
``personal_access_tokens`` and the secret shown once. The fresh operational token
then finds a project and its workflows without any id known in advance.
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
BODY = {
    "client_id": "claude_code",
    "package_id": "flow-steward-operator",
    "preset": "operational",
    "expiry_days": 30,
    "project_ids": [],
}


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
    assert len(stored) == 1 and secret not in stored[0][0]


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
    operator = world.principal("operator")
    request = {
        "client_id": "cursor",
        "package_id": "flow-steward-operator",
        "preset_id": "operational",
        "project_ids": [world.projects[0]],
        "expiry_days": 7,
    }
    key = f"attempt-{uuid.uuid4()}"
    created = service.issue_token(operator, attempt_key=key, **request)  # response "lost"
    replay = service.issue_token(operator, attempt_key=key, **request)
    assert replay["status"] == "replayed" and replay["secret"] == ""
    assert replay["token"]["id"] == created["token"]["id"]
    assert replay["token"]["allowed_project_ids"] == [world.projects[0]]
    with pytest.raises(AgentConnectionError) as mismatch:
        service.issue_token(operator, attempt_key=key, **{**request, "expiry_days": 30})
    assert mismatch.value.code == "attempt_mismatch"
    assert len(_wizard_tokens(world, "operator")) == 1


def test_a_refusal_leaves_neither_a_token_nor_a_claim(world, http) -> None:
    key = f"attempt-{uuid.uuid4()}"
    viewer = _post(http("viewer"), key)
    assert viewer.status_code == 403, viewer.text
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
