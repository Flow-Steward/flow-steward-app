"""Choosing and switching companies with an installation-wide sign-in (ADR 0083
amendment of 2026-11-27), on the disposable audit database.

Pat holds a role in companies A (operator, default) and B (admin) and signs in with
an installation-wide method (kind ``wide``): the sign-in asks which company to open,
the session can move to B and back to A, never into a company where Pat has no role,
and removing Pat from the company the session acts in ends that session. A session
of one company's own sign-in source (kind ``corp``) stays pinned. Every test runs in
a transaction that is rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth import resolve
from core.application.auth.external_sign_in_ports import (
    BeginResult,
    ExternalIdentityResolution,
    ExternalSignInPort,
    SignInOption,
    VerifiedExternalIdentity,
)
from core.application.auth.external_sign_in_service import ExternalSignInService
from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID
from core.application.auth.password import session_token_hash
from core.application.auth.user_admin.account_members import remove_member
from core.entrypoints.api.auth_sessions import create_auth_sessions_router
from core.infrastructure.config.platform_identity import DEFAULT_TENANT_ID
from tests.db.test_account_members_postgres import _NoCommit
from tests.db.test_roles_per_account_postgres import _account, _person, _Store

WIDE = "wide"
ISSUER = "wide:https://id.example.test"
CALLBACK = "https://flow.example.test/api/auth/external/callback"


@dataclass
class World:
    conn: Any
    a: str
    b: str
    c: str  # Pat is a member without a role
    pat: str
    admin_b: str


@pytest.fixture
def world(disposable_db_conn) -> Iterator[World]:
    conn = disposable_db_conn
    with conn.transaction(force_rollback=True):
        conn.execute(
            """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
               VALUES (%s, %s, 'default', 'Default Account', true)
               ON CONFLICT DO NOTHING""",
            (str(INSTALLATION_ACCOUNT_ID), DEFAULT_TENANT_ID),
        )
        a, _ = _account(conn, "a")
        b, _ = _account(conn, "b")
        c, _ = _account(conn, "c")
        pat = _person(conn, "pat", a, {a: "operator", b: "admin"})
        conn.execute("UPDATE users SET password_hash = '!external-sign-in' WHERE id = %s", (pat,))
        conn.execute(
            """INSERT INTO user_account_memberships (user_id, account_id, is_default)
               VALUES (%s, %s, false)""",
            (pat, c),
        )
        admin_b = _person(conn, "admin-b", b, {b: "admin"})
        yield World(conn=conn, a=a, b=b, c=c, pat=pat, admin_b=admin_b)


def _link(world: World, method: str, account_id: str) -> str:
    row = world.conn.execute(
        """INSERT INTO external_identities
               (account_id, method, option_key, issuer, subject, user_id, created_by)
           VALUES (%s, %s, 'one', %s, %s, %s, 'test')
           RETURNING id::text""",
        (
            account_id,
            method,
            f"{method}:https://id.example.test",
            f"s-{uuid.uuid4().hex}",
            world.pat,
        ),
    ).fetchone()
    return str(row[0])


def _external_session(world: World, method: str, account_id: str) -> str:
    identity = _link(world, method, account_id)
    token = f"session-{uuid.uuid4().hex}"
    world.conn.execute(
        """INSERT INTO auth_sessions
               (user_id, account_id, session_token_hash, expires_at, auth_method,
                external_identity_id)
           VALUES (%s, %s, %s, now() + interval '1 hour', 'external', %s)""",
        (world.pat, account_id, session_token_hash(token), identity),
    )
    return token


def _client(world: World, token: str, audit: list[dict[str, Any]]) -> TestClient:
    store = _Store(world.conn)
    store.conn = _NoCommit(store.conn)
    router = create_auth_sessions_router(
        get_logger_only=lambda: store,
        session_token_hash=session_token_hash,
        auth_cookie_secure=lambda: False,
        log_auth_audit_event=lambda *_args, **kwargs: audit.append(kwargs),
        local_auth_enabled=lambda: True,
        bootstrap_request_allowed=lambda _request: (True, ""),
        auth_tables_available=lambda _bl: True,
        resolve_actor_username=lambda _request, _requested, default="auth": default,
        get_user_by_username=lambda _bl, _username: None,
        request_client_ip=lambda _request: "127.0.0.1",
        auth_login_lock_state=lambda _username, _client_ip: (False, 0),
        auth_login_record_failure=lambda _username, _client_ip: (False, 0),
        verify_password=lambda _plain, _hashed: False,
        password_hash=lambda plain: plain,
        auth_user_column_exists=lambda _bl, _column: False,
        auth_login_clear_failures=lambda _username, _client_ip: None,
        csrf_request_valid=lambda _request: True,
        api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        err_database_not_configured="db-missing",
        err_local_auth_disabled="local-disabled",
        err_auth_tables_not_available="auth-missing",
        err_too_many_login_attempts="rate-limited",
        auth_password_min_length=12,
        auth_session_ttl_hours=24,
        auth_remember_session_ttl_hours=168,
        auth_cookie_name="fs_session",
        auth_csrf_cookie_name="fs_csrf",
        first_admin_service_factory=lambda _bl: None,
        installation_wide_methods=lambda: [WIDE],
    )
    app = FastAPI()

    @app.middleware("http")
    async def _principal(request: Request, call_next):
        user = resolve.get_user_by_session_token(store, token)
        request.state.auth_user = user
        request.state.auth_source = (user or {}).get("auth_source", "")
        return await call_next(request)

    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("fs_session", token)
    return client


def _acting(world: World, token: str) -> tuple[str, list[str]]:
    user = resolve.get_user_by_session_token(_Store(world.conn), token)
    assert user is not None
    return str(user.get("account_id") or ""), sorted(user.get("roles") or [])


def _switch(world: World, token: str, account_id: str, audit: list[dict[str, Any]]):
    return _client(world, token, audit).post(
        "/api/auth/switch-account", json={"account_id": account_id}
    )


# --- signing in: the chooser -------------------------------------------------------------


class _WideMethod:
    kind = WIDE
    installation_wide = True

    def options(self):
        return [SignInOption(key="one", display_name="One")]

    def option(self, key):
        return self.options()[0] if key == "one" else None

    def begin(self, key, *, callback_url, state):
        return BeginResult(redirect_url=f"https://id.example.test/?state={state}")

    def complete(self, key, *, params, headers, data, callback_url):
        return VerifiedExternalIdentity(
            method=WIDE,
            option_key=key,
            account_id="",
            issuer="https://id.example.test",
            subject="pat",
        )


def test_a_person_with_two_companies_is_asked_to_choose(world: World) -> None:
    @contextmanager
    def connection():
        with world.conn.transaction():
            yield world.conn

    class Resolver:
        def resolve(self, identity):
            return ExternalIdentityResolution(user_id=world.pat, account_id=world.a)

    port = ExternalSignInPort(connection)
    port.register_sign_in_method(_WideMethod())
    port.set_identity_resolver(Resolver())
    service = ExternalSignInService(
        connection=connection,
        port=port,
        entitled=lambda *_args: True,
        audit=lambda **_kwargs: None,
        session_ttl_hours=1,
    )
    started = service.start(method=WIDE, key="one", next_path="/jobs", callback_url=CALLBACK)
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    result = service.callback(params={"state": state}, browser_binding=started.browser_binding)

    assert result.choose_account is True
    assert result.account_id == world.a  # the default company until Pat chooses
    # Pat (admin in B) may be signed in by an installation-wide identity.
    assert _acting(world, result.session_token) == (world.a, ["operator"])


# --- switching ------------------------------------------------------------------------------


def test_an_installation_wide_session_switches_between_its_companies(world: World) -> None:
    token = _external_session(world, WIDE, world.a)
    audit: list[dict[str, Any]] = []

    to_b = _switch(world, token, world.b, audit)
    assert to_b.status_code == 200, to_b.text
    assert _acting(world, token) == (world.b, ["admin"])

    back = _switch(world, token, world.a, audit)
    assert back.status_code == 200, back.text
    assert _acting(world, token) == (world.a, ["operator"])


def test_a_session_of_one_companys_sign_in_stays_pinned(world: World) -> None:
    token = _external_session(world, "corp", world.a)
    audit: list[dict[str, Any]] = []

    response = _switch(world, token, world.b, audit)

    assert response.status_code == 403
    assert response.json()["code"] == "external_session_account_pinned"
    assert _acting(world, token)[0] == world.a


def test_switching_into_a_company_without_a_role_is_refused(world: World) -> None:
    token = _external_session(world, WIDE, world.a)
    audit: list[dict[str, Any]] = []

    response = _switch(world, token, world.c, audit)

    assert response.status_code == 403
    assert response.json()["code"] == "no_role_in_account"
    assert _acting(world, token)[0] == world.a


def test_switching_into_the_installation_account_is_refused(world: World) -> None:
    installation = str(INSTALLATION_ACCOUNT_ID)
    world.conn.execute(
        """INSERT INTO user_account_memberships (user_id, account_id, is_default, role_id)
           VALUES (%s, %s, false, (SELECT id FROM roles WHERE key = 'viewer'))""",
        (world.pat, installation),
    )
    token = _external_session(world, WIDE, world.a)
    audit: list[dict[str, Any]] = []

    response = _switch(world, token, installation, audit)

    assert response.status_code == 403
    assert response.json()["code"] == "external_session_installation_account"
    assert _acting(world, token)[0] == world.a


def test_removal_from_the_acting_company_ends_the_session(world: World) -> None:
    token = _external_session(world, WIDE, world.a)
    audit: list[dict[str, Any]] = []
    assert _switch(world, token, world.b, audit).status_code == 200

    with world.conn.cursor() as cur:
        remove_member(cur, user_id=world.pat, account_id=world.b, actor_user_id=world.admin_b)

    revoked = world.conn.execute(
        "SELECT revoked FROM auth_sessions WHERE session_token_hash = %s",
        (session_token_hash(token),),
    ).fetchone()[0]
    assert revoked is True
    assert resolve.get_user_by_session_token(_Store(world.conn), token) is None
