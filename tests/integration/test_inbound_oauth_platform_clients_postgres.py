"""Admin-registered platform OAuth clients on real PostgreSQL (ADR 0063 decision 2).

A Microsoft 365 Copilot registration signs in with an opaque client id and the
preset's fixed redirect URI, may omit ``resource``, and stops working, with its
grants, when an admin revokes it. Everything after the client check is ADR 0047.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pytest

from core.application.auth import inbound_oauth_platform_clients as platform_clients
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.inbound_oauth_platform_admin import (
    create_platform_client,
    platform_clients_view,
    revoke_platform_client,
)
from core.application.auth.inbound_oauth_platform_clients import PLATFORM_CLIENTS_SETTING_KEY
from core.application.auth.inbound_oauth_service import (
    ConsentError,
    InboundOAuthService,
    OAuthProtocolError,
)
from tests._db_env import resolve_test_db_dsn
from tests.integration._inbound_oauth_world import (
    CSRF,
    ORIGIN,
    RESOURCE,
    authorize_params,
    build_world,
    code_from,
    exchange,
    oauth_environment,
    pkce_pair,
    query_of,
)

TEAMS_REDIRECT = "https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect"


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
        state.conn.rollback()
        state.sql("DELETE FROM oauth_authorizations WHERE client_id LIKE 'fs-platform-%%'")
        state.sql("DELETE FROM oauth_grants WHERE client_id LIKE 'fs-platform-%%'")
        state.sql("DELETE FROM oauth_clients WHERE client_id LIKE 'fs-platform-%%'")
        state.sql(
            "DELETE FROM system_settings WHERE account_id = %s AND setting_key = %s",
            (DEFAULT_ACCOUNT_ID, PLATFORM_CLIENTS_SETTING_KEY),
        )
        state.cleanup()


@pytest.fixture
def platform_client(world) -> str:
    view = create_platform_client(
        world.service(), DEFAULT_ACCOUNT_ID, "microsoft_365", actor="admin"
    )
    return view["clients"][-1]["client_id"]


def _platform_params(registered_client_id: str, **overrides: str | None) -> dict[str, str | None]:
    # Microsoft may send no resource; the platform client is bound to this installation.
    return {
        "client_id": registered_client_id,
        "redirect_uri": TEAMS_REDIRECT,
        "resource": None,
        **overrides,
    }


def test_an_admin_registration_lists_the_values_for_the_teams_developer_portal(world) -> None:
    service = world.service()
    view = create_platform_client(service, DEFAULT_ACCOUNT_ID, "microsoft_365", actor="admin")
    client = view["clients"][-1]
    assert client["client_id"].startswith("fs-platform-m365-")
    assert client["name"] == "Microsoft 365 Copilot"
    assert client["revoked_at"] is None
    assert view["presets"][0]["redirect_uris"] == [TEAMS_REDIRECT]
    assert view["portal"] == {
        "base_url": RESOURCE,
        "authorization_endpoint": f"{ORIGIN}/oauth/authorize",
        "token_endpoint": f"{ORIGIN}/oauth/token",
        "refresh_endpoint": f"{ORIGIN}/oauth/token",
        "scope": "offline_access",
        "pkce": True,
        "client_secret": "",
    }
    with pytest.raises(ConsentError) as caught:
        create_platform_client(service, DEFAULT_ACCOUNT_ID, "unknown", actor="admin")
    assert caught.value.code == "unknown_preset"


def test_a_platform_client_signs_in_without_resource_and_gets_tokens(
    world, platform_client
) -> None:
    service = world.service()
    code, verifier, _request = code_from(world, service, params=_platform_params(platform_client))
    tokens = exchange(
        service, code, verifier, client_id=platform_client, redirect_uri=TEAMS_REDIRECT
    )
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    rows = world.sql("SELECT resource FROM oauth_grants WHERE client_id = %s", (platform_client,))
    assert rows == [(RESOURCE,)]
    refreshed = service.token(
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": platform_client,
        }
    )
    assert refreshed["access_token"]


def test_the_installation_binding_holds_through_exchange_and_refresh(
    world, platform_client
) -> None:
    # ADR 0063 decision 3: a missing resource binds the grant; the token endpoint
    # refuses a different one and an omitted one keeps the stored binding.
    service = world.service()
    other = "https://other.example/mcp"
    code, verifier, _request = code_from(world, service, params=_platform_params(platform_client))
    with pytest.raises(OAuthProtocolError) as caught:
        exchange(
            service,
            code,
            verifier,
            client_id=platform_client,
            redirect_uri=TEAMS_REDIRECT,
            resource=other,
        )
    assert caught.value.error == "invalid_target"

    code, verifier, _request = code_from(world, service, params=_platform_params(platform_client))
    tokens = exchange(
        service,
        code,
        verifier,
        client_id=platform_client,
        redirect_uri=TEAMS_REDIRECT,
        resource=RESOURCE,
    )
    refreshed = service.token(
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": platform_client,
        }
    )
    with pytest.raises(OAuthProtocolError) as caught:
        service.token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refreshed["refresh_token"],
                "client_id": platform_client,
                "resource": other,
            }
        )
    assert caught.value.error == "invalid_target"
    resources = world.sql(
        "SELECT DISTINCT resource FROM oauth_grants WHERE client_id = %s", (platform_client,)
    )
    assert resources == [(RESOURCE,)]


def test_the_consent_page_names_the_platform_client(world, platform_client) -> None:
    service = world.service()
    _verifier, challenge = pkce_pair()
    outcome = service.start_authorization(
        authorize_params(challenge, **_platform_params(platform_client))
    )
    view = service.consent_view(
        query_of(outcome.location)["request"],
        user=world.principal("operator"),
        browser_binding=outcome.browser_binding,
    )
    assert view["client"]["client_name"] == "Microsoft 365 Copilot"
    assert view["client"]["client_kind"] == "platform"
    assert view["client"]["redirect_uri_host"] == "teams.microsoft.com"


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"redirect_uri": "https://evil.example/callback"}, "invalid_redirect_uri"),
        ({"client_id": "fs-platform-m365-" + "0" * 32}, "client_not_allowed"),
    ],
)
def test_only_the_preset_redirect_and_a_registered_id_are_accepted(
    world, platform_client, overrides, error
) -> None:
    _verifier, challenge = pkce_pair()
    outcome = world.service().start_authorization(
        authorize_params(challenge, **_platform_params(platform_client, **overrides))
    )
    location = urlsplit(outcome.location)
    assert location.path == "/oauth/consent"
    assert query_of(outcome.location) == {"error": error}


def test_a_resource_the_platform_sends_must_still_match(world, platform_client) -> None:
    _verifier, challenge = pkce_pair()
    outcome = world.service().start_authorization(
        authorize_params(
            challenge, **_platform_params(platform_client, resource="https://other.example/mcp")
        )
    )
    assert outcome.location.startswith(TEAMS_REDIRECT)
    assert query_of(outcome.location)["error"] == "invalid_target"


def test_a_registration_is_usable_only_by_its_own_account(world, platform_client) -> None:
    # Consent, decision, code exchange and refresh all go through this one check.
    usable = InboundOAuthService._platform_client_usable
    assert usable(world.conn, platform_client, DEFAULT_ACCOUNT_ID) is True
    assert usable(world.conn, platform_client, "another-account") is False
    assert usable(world.conn, "fs-platform-m365-" + "1" * 32, DEFAULT_ACCOUNT_ID) is False
    assert usable(world.conn, "https://claude.ai/oauth/client", "another-account") is True
    world.conn.rollback()


def test_revoking_the_registration_revokes_its_grants_and_new_sign_ins(
    world, platform_client
) -> None:
    service = world.service()
    code, verifier, _request = code_from(world, service, params=_platform_params(platform_client))
    tokens = exchange(
        service, code, verifier, client_id=platform_client, redirect_uri=TEAMS_REDIRECT
    )

    view = revoke_platform_client(service, DEFAULT_ACCOUNT_ID, platform_client, actor="admin")

    assert view["clients"][-1]["revoked_at"]
    reasons = world.sql(
        "SELECT revoked_reason FROM oauth_grants WHERE client_id = %s", (platform_client,)
    )
    assert reasons == [("platform_client_revoked",)]
    with pytest.raises(OAuthProtocolError) as caught:
        service.token(
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": platform_client,
            }
        )
    assert caught.value.error == "invalid_grant"
    _verifier, challenge = pkce_pair()
    outcome = service.start_authorization(
        authorize_params(challenge, **_platform_params(platform_client))
    )
    assert query_of(outcome.location) == {"error": "client_not_allowed"}
    assert platform_clients_view(service, DEFAULT_ACCOUNT_ID)["clients"][-1]["revoked_at"]


@pytest.fixture
def http(world, monkeypatch):
    from fastapi.testclient import TestClient

    from core.entrypoints.api import route_registration
    from core.entrypoints.api.app_factory import create_app

    app = create_app()
    app.state.local_auth_enabled_override = lambda: True
    app.state.external_auth_enabled_override = lambda: False
    app.state.runtime_container.service_factories["identity_store"] = lambda: world.store
    monkeypatch.setattr(route_registration, "_inbound_oauth_service", lambda _app: world.service())
    clients: list = []

    def browser(who: str) -> TestClient:
        client = TestClient(app, base_url=ORIGIN, follow_redirects=False)
        client.cookies.set("fs_session", world.users[who]["session"])
        client.cookies.set("fs_csrf", CSRF)
        clients.append(client)
        return client

    try:
        yield browser
    finally:
        for client in clients:
            client.close()


def test_only_an_admin_manages_platform_clients_over_http(http) -> None:
    path = "/api/settings/mcp-oauth/platform-clients"
    assert http("operator").get(path).status_code == 403
    admin = http("admin")
    created = admin.post(path, json={"preset": "microsoft_365"}, headers={"x-csrf-token": CSRF})
    assert created.status_code == 201, created.text
    client_id = created.json()["clients"][-1]["client_id"]
    listed = admin.get(path)
    assert listed.status_code == 200
    assert [row["client_id"] for row in listed.json()["clients"]] == [client_id]
    revoked = admin.post(f"{path}/{client_id}/revoke", headers={"x-csrf-token": CSRF})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["clients"][0]["revoked_at"]


# ---------------------------------------------------------------------------
# Concurrency (ADR 0063): a second connection holds the account's lock in the
# middle of a change; the call under test runs in a thread with its own store,
# and the test proceeds only once PostgreSQL reports that call blocked behind the
# holder. The interleaving is the one the database observed, not a timed guess.
# ---------------------------------------------------------------------------


@contextmanager
def _holder() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(resolve_test_db_dsn())
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


class _Call:
    """One service call in its own thread, with its own store and connection."""

    def __init__(self, world, run: Callable[[InboundOAuthService], Any]) -> None:
        from core.infrastructure.db.platform_stores import PlatformIdentityStore

        self.store = PlatformIdentityStore(world.dsn)
        self.service = dataclasses.replace(world.service(), bl=self.store)
        self.result: Any = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(run,), daemon=True)

    def _run(self, run: Callable[[InboundOAuthService], Any]) -> None:
        try:
            self.result = run(self.service)
        except BaseException as error:  # reported to the test thread
            self.error = error

    def start(self) -> _Call:
        self.thread.start()
        return self

    def join(self) -> _Call:
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "the call never finished"
        connection = getattr(self.store, "_direct_conn", None)
        if connection is not None:
            connection.close()
        return self


def _wait_until_blocked_by(holder: psycopg.Connection, count: int = 1) -> None:
    """Return once ``count`` backends wait on a lock the holder owns."""
    observer = psycopg.connect(resolve_test_db_dsn(), autocommit=True)
    try:
        deadline = time.monotonic() + 30
        while True:
            blocked = observer.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                (holder.info.backend_pid,),
            ).fetchone()[0]
            if blocked >= count:
                return
            assert time.monotonic() < deadline, "the call did not wait for the lock"
            time.sleep(0.01)  # polling an observed state, not ordering by time
    finally:
        observer.close()


def _registrations(world) -> list[dict[str, Any]]:
    rows = world.sql(
        "SELECT setting_value_jsonb FROM system_settings WHERE account_id = %s AND setting_key = %s",
        (DEFAULT_ACCOUNT_ID, PLATFORM_CLIENTS_SETTING_KEY),
    )
    return list(rows[0][0]["clients"]) if rows else []


def test_concurrent_changes_never_undo_a_revocation_or_lose_a_registration(
    world, platform_client
) -> None:
    def create(service: InboundOAuthService) -> Any:
        return create_platform_client(service, DEFAULT_ACCOUNT_ID, "microsoft_365", actor="b")

    with _holder() as holder:
        # The holder is an admin revoking the first client, mid-transaction.
        platform_clients.revoke(
            holder, DEFAULT_ACCOUNT_ID, platform_client, revoked_by="a", now=world.service().clock()
        )
        first = _Call(world, create).start()
        second = _Call(world, create).start()
        _wait_until_blocked_by(holder, count=2)
        holder.commit()
    first.join()
    second.join()
    assert first.error is None and second.error is None, (first.error, second.error)

    rows = _registrations(world)
    assert len(rows) == 3
    revoked = next(row for row in rows if row["client_id"] == platform_client)
    assert revoked["revoked_at"], "a concurrent create wrote back a stale list"
    assert sum(1 for row in rows if not row["revoked_at"]) == 2


def test_the_first_registration_of_an_account_is_serialized_too(world) -> None:
    # No settings row exists yet; the lock does not depend on one.
    def create(service: InboundOAuthService) -> Any:
        return create_platform_client(service, DEFAULT_ACCOUNT_ID, "microsoft_365", actor="b")

    with _holder() as holder:
        platform_clients.lock_registrations(holder, DEFAULT_ACCOUNT_ID)
        calls = [_Call(world, create).start() for _ in range(2)]
        _wait_until_blocked_by(holder, count=2)
        holder.commit()
    for call in calls:
        call.join()
        assert call.error is None, call.error
    assert len(_registrations(world)) == 2


def test_no_token_is_issued_across_a_revocation(world, platform_client) -> None:
    service = world.service()
    # A grant with a refresh token, and a code approved but not yet exchanged.
    code, verifier, _request = code_from(world, service, params=_platform_params(platform_client))
    tokens = exchange(
        service, code, verifier, client_id=platform_client, redirect_uri=TEAMS_REDIRECT
    )
    pending_code, pending_verifier, _request = code_from(
        world, service, params=_platform_params(platform_client)
    )

    def exchange_pending(call_service: InboundOAuthService) -> Any:
        return exchange(
            call_service,
            pending_code,
            pending_verifier,
            client_id=platform_client,
            redirect_uri=TEAMS_REDIRECT,
        )

    def refresh(call_service: InboundOAuthService) -> Any:
        return call_service.token(
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": platform_client,
            }
        )

    with _holder() as holder:
        # The revocation has updated the registration and the existing grants but
        # not committed; issuance must wait for it, then see it.
        platform_clients.revoke(
            holder, DEFAULT_ACCOUNT_ID, platform_client, revoked_by="a", now=service.clock()
        )
        exchanged = _Call(world, exchange_pending).start()
        refreshed = _Call(world, refresh).start()
        _wait_until_blocked_by(holder, count=2)
        holder.commit()
    exchanged.join()
    refreshed.join()

    for call in (exchanged, refreshed):
        assert isinstance(call.error, OAuthProtocolError), call.result
        assert call.error.error == "invalid_grant"
    live = world.sql(
        "SELECT count(*) FROM oauth_grants WHERE client_id = %s AND revoked_at IS NULL",
        (platform_client,),
    )
    assert live == [(0,)]
