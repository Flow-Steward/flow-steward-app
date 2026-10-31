"""Inbound OAuth on real PostgreSQL (ADR 0047; task5b sections 1-6, 8, P level).

Service-level cases drive the canonical use cases with an injected clock; HTTP cases
run the real ``fs-app`` ASGI application with the real middleware. Everything runs on
the gate-owned audit database.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest

from core.application.auth.inbound_oauth_policy import MCP_FORWARD_HEADER
from core.application.auth.inbound_oauth_service import ConsentError, OAuthProtocolError
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
    code_from,
    exchange,
    oauth_environment,
    pkce_pair,
    query_of,
)


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

    def browser(who: str | None = None) -> TestClient:
        client = TestClient(app, base_url=ORIGIN, follow_redirects=False)
        if who:
            client.cookies.set("fs_session", world.users[who]["session"])
            client.cookies.set("fs_csrf", CSRF)
        return client

    clients: list = []

    def make(who: str | None = None):
        client = browser(who)
        clients.append(client)
        return client

    try:
        yield make
    finally:
        for client in clients:
            client.close()
        if project_store._direct_conn is not None:
            project_store._direct_conn.close()


def _browser_flow(http, who: str = "operator", *, scope: str | None = None, state="st-1"):
    """authorize in a browser, sign in, and return (browser, request_id, verifier)."""
    browser = http(who)
    verifier, challenge = pkce_pair()
    overrides = {"state": state}
    if scope is not None:
        overrides["scope"] = scope
    response = browser.get("/oauth/authorize", params=authorize_params(challenge, **overrides))
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert location.path == "/oauth/consent"
    return browser, query_of(response.headers["location"])["request"], verifier


def _decide(browser, request_id: str, body: dict, *, csrf: bool = True):
    headers = {"x-csrf-token": CSRF} if csrf else {}
    return browser.post(
        f"/api/oauth/authorizations/{request_id}/decision", json=body, headers=headers
    )


def _approve_over_http(http, who="operator", **body):
    browser, request_id, verifier = _browser_flow(http, who)
    response = _decide(
        browser,
        request_id,
        {"decision": "approve", "preset": "operational", "expiry_days": 30, **body},
    )
    assert response.status_code == 200, response.text
    return query_of(response.json()["redirect_to"])["code"], verifier


def _token(http, **form):
    return http().post("/oauth/token", data={"client_id": CLIENT_ID, **form})


def _api(http, token: str, path: str, *, method="GET", forward=True, **kwargs):
    headers = {"Authorization": f"Bearer {token}"}
    if forward:
        headers[MCP_FORWARD_HEADER] = FORWARD_SECRET
    return http().request(method, path, headers=headers, **kwargs)


# ---------------------------------------------------------------------------
# Discovery and readiness (D5, D7)
# ---------------------------------------------------------------------------


def test_ready_installation_publishes_metadata(http) -> None:
    response = http().get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    assert response.json()["issuer"] == ORIGIN


def test_not_ready_without_public_https(http, monkeypatch) -> None:
    """D5."""
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://localhost:8099")
    client = http()
    assert client.get("/.well-known/oauth-authorization-server").status_code == 404
    _verifier, challenge = pkce_pair()
    response = client.get("/oauth/authorize", params=authorize_params(challenge))
    assert response.headers["location"] == "/oauth/consent?error=public_https_required"
    token = client.post("/oauth/token", data={"client_id": CLIENT_ID, "grant_type": "x"})
    assert token.status_code == 503
    assert token.json()["error"] == "temporarily_unavailable"


def test_account_switch_off_rejects_existing_tokens_but_keeps_grants(world, http) -> None:
    """D7."""
    code, verifier = _approve_over_http(http)
    tokens = _token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
    ).json()
    assert _api(http, tokens["access_token"], "/api/auth/introspect").status_code == 200

    world.set_enabled(False)
    _verifier, challenge = pkce_pair()
    response = http().get("/oauth/authorize", params=authorize_params(challenge))
    assert response.headers["location"] == "/oauth/consent?error=disabled_by_admin"
    assert _api(http, tokens["access_token"], "/api/auth/introspect").status_code == 401
    refresh = _token(http, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert refresh.json()["error"] == "temporarily_unavailable"
    grants = http("operator").get("/api/auth/oauth-grants").json()
    assert len(grants["grants"]) == 1
    assert grants["readiness"]["blockers"] == ["disabled_by_admin"]


def test_cimd_document_is_cached_and_refetched_after_expiry(world) -> None:
    """C5."""
    now = datetime.now(UTC)
    service = world.service(clock=lambda: now)
    _verifier, challenge = pkce_pair()
    service.start_authorization(authorize_params(challenge))
    service.start_authorization(authorize_params(challenge))
    assert world.fetches == [CLIENT_ID]
    later = world.service(clock=lambda: now + timedelta(minutes=11))
    later.start_authorization(authorize_params(challenge))
    assert world.fetches == [CLIENT_ID, CLIENT_ID]

    from core.application.auth.inbound_oauth_clients import ClientMetadataUnavailableError

    def _down(_client_id):
        raise ClientMetadataUnavailableError("down")

    failing = world.service(clock=lambda: now + timedelta(hours=2))
    failing.metadata_fetcher = _down
    outcome = failing.start_authorization(authorize_params(challenge))
    assert outcome.location == "/oauth/consent?error=client_metadata_unavailable"


# ---------------------------------------------------------------------------
# Consent (Z6-Z10, Z12-Z15)
# ---------------------------------------------------------------------------


def test_consent_view_lists_presets_and_client(http) -> None:
    browser, request_id, _verifier = _browser_flow(
        http, "admin", scope="admin:all projects.read jobs.read"
    )
    view = browser.get(f"/api/oauth/authorizations/{request_id}")
    assert view.status_code == 200, view.text
    payload = view.json()
    assert payload["client"]["client_host"] == "claude.ai"
    assert payload["client"]["redirect_uri_loopback"] is True
    assert [preset["id"] for preset in payload["presets"]] == ["operational", "full_authoring"]
    assert payload["presets"][0]["scopes"] == [
        "projects.read",
        "jobs.read",
        "jobs.write",
        "reports.read",
        "reports.write",
        "workflows.read",
        "workflows.write",
    ]
    assert payload["default_expiry_days"] == 30


def test_decision_requires_this_browser(http, world) -> None:
    """Z6."""
    _browser, request_id, _verifier = _browser_flow(http)
    other = http("operator")  # same user, another browser: no fs_oauth_ar cookie
    body = {"decision": "approve", "preset": "operational", "expiry_days": 30}
    assert _decide(other, request_id, body).status_code == 403
    other.cookies.set("fs_oauth_ar", "another-browser-binding")
    assert _decide(other, request_id, body).status_code == 403
    assert world.sql(
        "SELECT decided_at FROM oauth_authorizations WHERE id = %s", (request_id,)
    ) == [(None,)]


def test_decision_refuses_bearers_and_missing_csrf(http, world) -> None:
    """Z7."""
    browser, request_id, _verifier = _browser_flow(http)
    body = {"decision": "approve", "preset": "operational", "expiry_days": 30}
    assert _decide(browser, request_id, body, csrf=False).status_code == 403
    pat = world.store.create_pat(
        user_id=world.users["operator"]["id"],
        account_id="00000000-0000-0000-0000-000000000002",
        scopes=["admin:all"],
    )["raw_token"]
    with_pat = http().post(
        f"/api/oauth/authorizations/{request_id}/decision",
        json=body,
        headers={"Authorization": f"Bearer {pat}"},
    )
    assert with_pat.status_code == 403
    code, verifier = _approve_over_http(http)
    access = _token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
    ).json()["access_token"]
    with_oauth = _api(
        http, access, f"/api/oauth/authorizations/{request_id}/decision", method="POST", json=body
    )
    assert with_oauth.status_code == 403
    assert world.sql(
        "SELECT decided_at FROM oauth_authorizations WHERE id = %s", (request_id,)
    ) == [(None,)]


def test_decision_once_and_before_expiry(world) -> None:
    """Z8."""
    now = datetime.now(UTC)
    service = world.service(clock=lambda: now)
    _verifier, challenge = pkce_pair()
    outcome = service.start_authorization(authorize_params(challenge))
    request_id = query_of(outcome.location)["request"]
    late = world.service(clock=lambda: now + timedelta(minutes=10, seconds=1))
    with pytest.raises(ConsentError) as expired:
        late.decide(
            request_id,
            user=world.principal("operator"),
            browser_binding=outcome.browser_binding,
            decision="deny",
        )
    assert expired.value.status == 410
    service.decide(
        request_id,
        user=world.principal("operator"),
        browser_binding=outcome.browser_binding,
        decision="deny",
    )
    with pytest.raises(ConsentError) as twice:
        service.decide(
            request_id,
            user=world.principal("operator"),
            browser_binding=outcome.browser_binding,
            decision="deny",
        )
    assert twice.value.status == 409


def test_abandoned_flow_grants_nothing_and_is_cleaned_up(world) -> None:
    """Z9, R13 (pending rows)."""
    from core.application.tooling.ops.retention_repository import _delete_or_count_inbound_oauth

    now = datetime.now(UTC) - timedelta(minutes=30)
    service = world.service(clock=lambda: now)
    _verifier, challenge = pkce_pair()
    outcome = service.start_authorization(authorize_params(challenge))
    request_id = query_of(outcome.location)["request"]
    rows = world.sql(
        "SELECT code_hash, grant_id, decided_at FROM oauth_authorizations WHERE id = %s",
        (request_id,),
    )
    assert rows == [(None, None, None)]
    assert world.grant_rows() == []
    with world.conn.cursor() as cur:
        deleted = _delete_or_count_inbound_oauth(cur, cache={}, dry_run=False)
    world.conn.commit()
    assert deleted >= 1
    assert world.sql("SELECT 1 FROM oauth_authorizations WHERE id = %s", (request_id,)) == []


def test_deny_redirects_with_access_denied(http, world) -> None:
    """Z10."""
    browser, request_id, _verifier = _browser_flow(http, state="deny-state")
    response = _decide(browser, request_id, {"decision": "deny"})
    query = query_of(response.json()["redirect_to"])
    assert response.json()["redirect_to"].startswith(REDIRECT)
    assert query == {"error": "access_denied", "state": "deny-state", "iss": ORIGIN}
    assert world.grant_rows() == []


def test_consent_never_exceeds_the_users_authority(http, world) -> None:
    """Z12, Z13, Z15."""
    browser, request_id, _verifier = _browser_flow(http, "viewer")
    view = browser.get(f"/api/oauth/authorizations/{request_id}").json()
    operational = view["presets"][0]
    assert operational["available"] is False
    assert operational["reason"] == "exceeds_authority"
    forced = _decide(
        browser, request_id, {"decision": "approve", "preset": "operational", "expiry_days": 30}
    )
    assert forced.status_code == 403
    assert forced.json()["error"] == "exceeds_authority"

    browser, request_id, _verifier = _browser_flow(
        http, "operator", scope="admin:all projects.read"
    )
    authoring = _decide(
        browser, request_id, {"decision": "approve", "preset": "full_authoring", "expiry_days": 30}
    )
    assert authoring.status_code == 403

    browser, request_id, _verifier = _browser_flow(http, "operator")
    foreign = _decide(
        browser,
        request_id,
        {
            "decision": "approve",
            "preset": "operational",
            "expiry_days": 30,
            "project_ids": [str(uuid.uuid4())],
        },
    )
    assert foreign.status_code == 403
    assert world.grant_rows() == []


def test_grant_lifetime_is_a_choice_of_at_most_90_days(http, world) -> None:
    """Z14."""
    for expiry in (91, None):
        browser, request_id, _verifier = _browser_flow(http)
        body = {"decision": "approve", "preset": "operational"}
        if expiry is not None:
            body["expiry_days"] = expiry
        assert _decide(browser, request_id, body).status_code == 400
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        world.sql(
            "INSERT INTO oauth_clients (client_id, redirect_uris_jsonb, metadata_sha256, "
            "fetched_at, cache_expires_at, first_seen_at) VALUES (%s, '[]', 'x', now(), now(), "
            "now()) ON CONFLICT DO NOTHING",
            (CLIENT_ID,),
        )
        world.sql(
            "INSERT INTO oauth_grants (id, user_id, account_id, client_id, client_name_snapshot, "
            "resource, preset, scopes_jsonb, allowed_project_ids_jsonb, created_at, expires_at) "
            "VALUES (%s, %s, '00000000-0000-0000-0000-000000000002', %s, 'x', %s, "
            "'operational', '[]', '[]', now(), now() + interval '91 days')",
            (str(uuid.uuid4()), world.users["operator"]["id"], CLIENT_ID, RESOURCE),
        )
    world.conn.rollback()


# ---------------------------------------------------------------------------
# Code exchange (T1-T7) and secrets in logs/DB (S1, S2)
# ---------------------------------------------------------------------------


def test_code_exchange_happy_path_without_secrets_at_rest_or_in_logs(http, world, caplog) -> None:
    """T1, S1, S2."""
    caplog.set_level(logging.DEBUG)
    browser, request_id, verifier = _browser_flow(http, state="secret-state-value")
    binding = browser.cookies.get("fs_oauth_ar")
    decision = _decide(
        browser, request_id, {"decision": "approve", "preset": "operational", "expiry_days": 30}
    )
    code = query_of(decision.json()["redirect_to"])["code"]
    response = _token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    payload = response.json()
    assert payload["token_type"] == "Bearer"
    assert 0 < payload["expires_in"] <= 3600
    assert payload["scope"].split(" ") == [
        "projects.read",
        "jobs.read",
        "jobs.write",
        "reports.read",
        "reports.write",
        "workflows.read",
        "workflows.write",
    ]
    assert payload["access_token"].startswith("fs_oat_")
    assert payload["refresh_token"].startswith("fs_ort_")
    grants = world.grant_rows()
    assert len(grants) == 1
    counts = world.sql(
        "SELECT (SELECT count(*) FROM oauth_access_tokens WHERE grant_id = %s), "
        "(SELECT count(*) FROM oauth_refresh_tokens WHERE grant_id = %s)",
        (grants[0][0], grants[0][0]),
    )
    assert counts == [(1, 1)]
    secrets_ = [
        code,
        payload["access_token"],
        payload["refresh_token"],
        verifier,
        binding,
        FORWARD_SECRET,
        "secret-state-value",
    ]
    dump = " ".join(
        str(row)
        for table in ("oauth_access_tokens", "oauth_refresh_tokens", "oauth_grants")
        for row in world.sql(f"SELECT row_to_json(t)::text FROM {table} t")
    ) + str(
        world.sql(
            "SELECT row_to_json(t)::text FROM (SELECT id, client_id, redirect_uri, resource, "
            "code_challenge, requested_scopes_jsonb, browser_binding_hash, code_hash FROM "
            "oauth_authorizations) t"
        )
    )
    audit = str(
        world.sql(
            "SELECT action_type, result::text, payload::text FROM platform_action_log "
            "WHERE action_type LIKE 'auth_oauth_%%' ORDER BY id DESC LIMIT 20"
        )
    )
    assert "auth_oauth_consent_approve" in audit
    _api(http, payload["access_token"], "/api/projects", forward=False)
    for secret in secrets_:
        assert secret not in dump, "secret persisted"
        assert secret not in caplog.text, "secret logged"
        assert secret not in audit, "secret audited"


def test_code_reuse_revokes_the_grant(http, world) -> None:
    """T2."""
    code, verifier = _approve_over_http(http)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    }
    first = _token(http, **form).json()
    second = _token(http, **form)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"
    [(_grant_id, revoked_at, reason, _expires, _scopes)] = world.grant_rows()
    assert revoked_at is not None and reason == "code_reuse"
    assert _api(http, first["access_token"], "/api/auth/introspect").status_code == 401
    audit = world.sql(
        "SELECT count(*) FROM platform_action_log WHERE action_type = 'auth_oauth_code_reuse'"
    )
    assert audit[0][0] >= 1


def test_concurrent_exchange_creates_at_most_one_grant_and_it_ends_revoked(world) -> None:
    """T3."""
    service = world.service()
    code, verifier, _request_id = code_from(world, service)
    barrier = threading.Barrier(2)
    results: list = []

    def _exchange() -> None:
        barrier.wait()
        try:
            results.append(exchange(world.service(), code, verifier))
        except OAuthProtocolError as error:
            results.append(error.error)

    threads = [threading.Thread(target=_exchange) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(type(item).__name__ for item in results) == ["dict", "str"]
    assert "invalid_grant" in results
    grants = world.grant_rows()
    assert len(grants) == 1
    assert grants[0][1] is not None


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"code_verifier": "w" * 64}, "invalid_grant"),
        ({"client_id": "https://claude.ai/other-client"}, "invalid_grant"),
        ({"redirect_uri": REDIRECT + "/"}, "invalid_grant"),
        ({"resource": "https://other.example/mcp"}, "invalid_target"),
    ],
)
def test_wrong_exchange_burns_the_code(world, override, error) -> None:
    """T4, T5."""
    service = world.service()
    code, verifier, _request_id = code_from(world, service)
    with pytest.raises(OAuthProtocolError) as refused:
        exchange(service, code, verifier, **override)
    assert refused.value.error == error
    with pytest.raises(OAuthProtocolError) as burned:
        exchange(service, code, verifier)
    assert burned.value.error == "invalid_grant"


def test_code_expires_after_60_seconds(world) -> None:
    """T6."""
    now = datetime.now(UTC)
    code, verifier, _request_id = code_from(world, world.service(clock=lambda: now))
    late = world.service(clock=lambda: now + timedelta(seconds=61))
    with pytest.raises(OAuthProtocolError) as refused:
        exchange(late, code, verifier)
    assert refused.value.error == "invalid_grant"
    assert world.grant_rows() == []


def test_user_deactivated_before_exchange_gets_no_grant(world) -> None:
    """T7."""
    service = world.service()
    code, verifier, _request_id = code_from(world, service)
    world.sql("UPDATE users SET is_active = false WHERE id = %s", (world.users["operator"]["id"],))
    with pytest.raises(OAuthProtocolError) as refused:
        exchange(service, code, verifier)
    assert refused.value.error == "invalid_grant"
    assert world.grant_rows() == []


# ---------------------------------------------------------------------------
# Refresh, revocation, expiry (R1-R13)
# ---------------------------------------------------------------------------


def _tokens(world, service, **decision) -> dict:
    code, verifier, _request_id = code_from(world, service, **decision)
    return exchange(service, code, verifier)


def _refresh(service, refresh_token: str, **extra) -> dict:
    return service.token(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            **extra,
        }
    )


def test_refresh_rotates_within_the_same_grant(world) -> None:
    """R1."""
    service = world.service()
    first = _tokens(world, service)
    second = _refresh(service, first["refresh_token"])
    assert second["refresh_token"] != first["refresh_token"]
    assert len(world.grant_rows()) == 1
    rows = world.sql(
        "SELECT consumed_at IS NOT NULL, parent_id IS NOT NULL FROM oauth_refresh_tokens "
        "ORDER BY created_at"
    )
    assert (True, False) in rows and (False, True) in rows


def test_refresh_reuse_revokes_everything(world) -> None:
    """R2."""
    service = world.service()
    first = _tokens(world, service)
    second = _refresh(service, first["refresh_token"])
    with pytest.raises(OAuthProtocolError) as reuse:
        _refresh(service, first["refresh_token"])
    assert reuse.value.error == "invalid_grant"
    [(_id, revoked_at, reason, _expires, _scopes)] = world.grant_rows()
    assert revoked_at is not None and reason == "refresh_reuse"
    assert service.resolve_access_principal(second["access_token"]) is None
    with pytest.raises(OAuthProtocolError):
        _refresh(service, second["refresh_token"])


def test_concurrent_refresh_is_strict(world) -> None:
    """R3: one wins, the other is a reuse and revokes the grant."""
    first = _tokens(world, world.service())
    barrier = threading.Barrier(2)
    results: list = []

    def _go() -> None:
        barrier.wait()
        try:
            results.append(_refresh(world.service(), first["refresh_token"]))
        except OAuthProtocolError as error:
            results.append(error.error)

    threads = [threading.Thread(target=_go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert "invalid_grant" in results and any(isinstance(item, dict) for item in results)
    assert world.grant_rows()[0][1] is not None


def test_refresh_can_narrow_but_never_widen(world) -> None:
    """R4, R5."""
    service = world.service()
    first = _tokens(world, service)
    narrow = _refresh(service, first["refresh_token"], scope="jobs.read")
    assert narrow["scope"] == "jobs.read"
    principal = service.resolve_access_principal(narrow["access_token"])
    assert principal is not None and principal["scopes"] == ["jobs.read"]
    full = _refresh(service, narrow["refresh_token"])
    assert len(full["scope"].split(" ")) == 7
    with pytest.raises(OAuthProtocolError) as wider:
        _refresh(service, full["refresh_token"], scope="jobs.read admin:all")
    assert wider.value.error == "invalid_scope"
    assert len(world.grant_rows()) == 1


def test_refresh_never_extends_the_grant(world) -> None:
    """R6, R7."""
    start = datetime.now(UTC)
    service = world.service(clock=lambda: start)
    first = _tokens(world, service, expiry_days=7)
    [(_id, _revoked, _reason, grant_expires_at, _scopes)] = world.grant_rows()
    near = world.service(clock=lambda: grant_expires_at - timedelta(minutes=10))
    second = _refresh(near, first["refresh_token"])
    assert second["expires_in"] <= 600
    assert world.sql("SELECT max(expires_at) FROM oauth_refresh_tokens")[0][0] == grant_expires_at
    after = world.service(clock=lambda: grant_expires_at + timedelta(seconds=1))
    with pytest.raises(OAuthProtocolError) as expired:
        _refresh(after, second["refresh_token"])
    assert expired.value.error == "invalid_grant"
    assert after.resolve_access_principal(second["access_token"]) is None


def test_settings_revocation_stops_access_and_refresh(http, world) -> None:
    """R8, A2."""
    code, verifier = _approve_over_http(http)
    tokens = _token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
    ).json()
    assert _api(http, tokens["access_token"], "/api/projects").status_code == 200
    settings = http("operator")
    [grant] = settings.get("/api/auth/oauth-grants").json()["grants"]
    assert grant["status"] == "active"
    assert grant["client_host"] == "claude.ai"
    revoked = settings.post(
        f"/api/auth/oauth-grants/{grant['id']}/revoke", headers={"x-csrf-token": CSRF}
    )
    assert revoked.status_code == 200
    assert revoked.json()["grant"]["status"] == "revoked"
    assert _api(http, tokens["access_token"], "/api/projects").status_code == 401
    refresh = _token(http, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert refresh.json()["error"] == "invalid_grant"
    # Another user's grant is invisible and cannot be revoked.
    other = http("admin")
    assert other.get("/api/auth/oauth-grants").json()["grants"] == []
    assert (
        other.post(
            f"/api/auth/oauth-grants/{grant['id']}/revoke", headers={"x-csrf-token": CSRF}
        ).status_code
        == 404
    )


def test_client_revocation_endpoint(http, world) -> None:
    """R9."""
    service = world.service()
    by_access = _tokens(world, service)
    response = http().post(
        "/oauth/revoke", data={"token": by_access["access_token"], "client_id": CLIENT_ID}
    )
    assert response.status_code == 200 and response.content == b""
    assert world.grant_rows()[-1][2] == "client_revoked"

    by_refresh = _tokens(world, service)
    http().post(
        "/oauth/revoke", data={"token": by_refresh["refresh_token"], "client_id": CLIENT_ID}
    )
    assert world.grant_rows()[-1][2] == "client_revoked"

    unknown = http().post("/oauth/revoke", data={"token": "fs_oat_nope", "client_id": CLIENT_ID})
    assert unknown.status_code == 200
    intact = _tokens(world, service)
    other = http().post(
        "/oauth/revoke",
        data={"token": intact["access_token"], "client_id": "https://claude.ai/other"},
    )
    assert other.status_code == 200
    assert world.grant_rows()[-1][1] is None
    assert service.resolve_access_principal(intact["access_token"]) is not None


def test_revocation_racing_a_refresh_leaves_nothing_usable(world) -> None:
    """R10."""
    service = world.service()
    first = _tokens(world, service)
    [(grant_id, *_rest)] = world.grant_rows()
    barrier = threading.Barrier(2)
    issued: list = []

    def _refresh_once() -> None:
        barrier.wait()
        with contextlib.suppress(OAuthProtocolError):
            issued.append(_refresh(world.service(), first["refresh_token"]))

    def _revoke() -> None:
        barrier.wait()
        world.service().revoke_user_grant(world.principal("operator"), str(grant_id))

    threads = [threading.Thread(target=_refresh_once), threading.Thread(target=_revoke)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    for tokens in issued:
        assert service.resolve_access_principal(tokens["access_token"]) is None
        with pytest.raises(OAuthProtocolError):
            _refresh(service, tokens["refresh_token"])
    assert service.resolve_access_principal(first["access_token"]) is None


def test_authority_changes_follow_the_pat_policy(http, world) -> None:
    """R11, R12: live roles and membership on every request; the grant is not re-capped."""
    service = world.service()
    code, verifier, _request_id = code_from(
        world, service, who="admin", preset="full_authoring", params={"scope": "admin:all"}
    )
    tokens = exchange(service, code, verifier)
    assert tokens["scope"] == "admin:all"
    admin_id = world.users["admin"]["id"]
    assert _api(http, tokens["access_token"], "/api/auth/users").status_code == 200
    world.sql(
        "UPDATE user_roles SET role_id = (SELECT id FROM roles WHERE key = 'operator') "
        "WHERE user_id = %s",
        (admin_id,),
    )
    # Demoted: the admin-only route fails on the role gate at once; operator routes work.
    assert _api(http, tokens["access_token"], "/api/auth/users").status_code == 403
    assert _api(http, tokens["access_token"], "/api/projects").status_code == 200
    world.sql("DELETE FROM user_account_memberships WHERE user_id = %s", (admin_id,))
    assert _api(http, tokens["access_token"], "/api/projects").status_code == 401
    refresh = _token(http, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert refresh.json()["error"] == "invalid_grant"
    assert len(world.grant_rows()) == 1


def test_deactivated_user_loses_access_and_refresh(world) -> None:
    """R12."""
    service = world.service()
    tokens = _tokens(world, service)
    world.sql("UPDATE users SET is_active = false WHERE id = %s", (world.users["operator"]["id"],))
    assert service.resolve_access_principal(tokens["access_token"]) is None
    with pytest.raises(OAuthProtocolError) as refused:
        _refresh(service, tokens["refresh_token"])
    assert refused.value.error == "invalid_grant"
    assert len(world.grant_rows()) == 1


def test_retention_keeps_ended_grants_for_30_days(world) -> None:
    """R13."""
    from core.application.tooling.ops.retention_repository import _delete_or_count_inbound_oauth

    service = world.service()
    _tokens(world, service)
    [(grant_id, *_rest)] = world.grant_rows()
    world.sql(
        "UPDATE oauth_grants SET revoked_at = now() - interval '29 days', "
        "revoked_reason = 'user_revoked' WHERE id = %s",
        (grant_id,),
    )
    world.sql("UPDATE oauth_access_tokens SET expires_at = now() - interval '1 minute'")
    with world.conn.cursor() as cur:
        _delete_or_count_inbound_oauth(cur, cache={}, dry_run=False)
    world.conn.commit()
    assert world.sql(
        "SELECT count(*) FROM oauth_access_tokens WHERE grant_id = %s", (grant_id,)
    ) == [(0,)]
    assert len(world.grant_rows()) == 1
    world.sql(
        "UPDATE oauth_grants SET revoked_at = now() - interval '31 days' WHERE id = %s",
        (grant_id,),
    )
    with world.conn.cursor() as cur:
        _delete_or_count_inbound_oauth(cur, cache={}, dry_run=False)
    world.conn.commit()
    assert world.grant_rows() == []
    assert world.sql(
        "SELECT count(*) FROM oauth_refresh_tokens WHERE grant_id = %s", (grant_id,)
    ) == [(0,)]


# ---------------------------------------------------------------------------
# Principal and the API (F3-F8)
# ---------------------------------------------------------------------------


def test_introspection_of_an_admins_operational_grant(http, world) -> None:
    """F3, F7."""
    code, verifier = _approve_over_http(http, "admin")
    access = _token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
    ).json()["access_token"]
    principal = _api(http, access, "/api/auth/introspect", forward=False).json()["principal"]
    assert principal["auth_source"] == "oauth"
    assert "admin:all" not in principal["scopes"]
    assert len(principal["scopes"]) == 7
    assert principal["resource"] == RESOURCE
    assert principal["oauth_client_id"] == CLIENT_ID
    assert principal["grant_id"]


def test_direct_and_forwarded_api_use(http, world) -> None:
    """F5, F6."""
    code, verifier = _approve_over_http(http)
    access = _token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
    ).json()["access_token"]
    assert _api(http, access, "/api/projects", forward=False).status_code == 401
    wrong = http().get(
        "/api/projects",
        headers={"Authorization": f"Bearer {access}", MCP_FORWARD_HEADER: "w" * 40},
    )
    assert wrong.status_code == 401
    assert _api(http, access, "/api/projects").status_code == 200
    for method, path in (
        ("POST", "/api/auth/tokens"),
        ("GET", "/api/auth/tokens"),
        ("POST", "/api/auth/change-password"),
        ("GET", "/api/auth/oauth-grants"),
    ):
        assert _api(http, access, path, method=method, json={}).status_code == 403, path
    assert world.sql(
        "SELECT count(*) FROM personal_access_tokens WHERE user_id = %s",
        (world.users["operator"]["id"],),
    ) == [(0,)]


def test_project_restricted_grant_sees_only_its_project(http, world) -> None:
    """F8."""
    allowed, hidden = world.projects
    code, verifier = _approve_over_http(http, project_ids=[allowed])
    access = _token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
    ).json()["access_token"]
    listing = _api(http, access, "/api/projects")
    assert listing.status_code == 200
    visible = {str(item.get("id")) for item in listing.json().get("projects", [])}
    assert allowed in visible and hidden not in visible
    assert _api(http, access, f"/api/projects/{hidden}").status_code in {403, 404}


def test_pats_are_unchanged(http, world) -> None:
    """A9: a PAT on the same API needs no forward header and is not an OAuth principal."""
    pat = world.store.create_pat(
        user_id=world.users["operator"]["id"],
        account_id="00000000-0000-0000-0000-000000000002",
        scopes=["projects.read"],
    )["raw_token"]
    response = _api(http, pat, "/api/projects", forward=False)
    assert response.status_code == 200
    principal = _api(http, pat, "/api/auth/introspect", forward=False).json()["principal"]
    assert principal["auth_source"] == "pat"
    assert "resource" not in principal


def test_database_without_the_tables_reports_storage_missing(world) -> None:
    """D6 at the readiness function (the startup contract does not list the tables)."""
    from core.infrastructure.db.schema_contract_catalog import REQUIRED_RUNTIME_TABLES

    assert not {"oauth_grants", "oauth_clients"} & set(REQUIRED_RUNTIME_TABLES)
    service = world.service()
    from core.application.auth import inbound_oauth_repository

    original = inbound_oauth_repository.storage_available
    try:
        inbound_oauth_repository.storage_available = lambda _conn: False
        readiness = service.readiness()
    finally:
        inbound_oauth_repository.storage_available = original
    assert "storage_missing" in readiness.blockers
    assert service.authorization_server_metadata() is not None  # real tables present here
