"""People of one account and switching on the disposable audit database (ADR 0085).

Alice is admin in A and viewer in B, Bob admin in A, Carol admin in B (its only one),
Dave operator in A and B. Erin lives in A and has a membership of B without a role.
Every test runs in a transaction that is rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from core.application.auth import resolve
from core.application.auth.membership_roles import (
    account_role_keys,
    cancel_invitations_of_former_admins,
)
from core.application.auth.password import session_token_hash
from core.application.auth.user_admin.account_invitations import (
    cancel,
    decide,
    delete_expired,
    invitation_token_hash,
    invite,
    preview,
    sent_invitations,
)
from core.application.auth.user_admin.account_members import (
    add_member,
    remove_member,
    search_candidates,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
)
from core.entrypoints.api.auth_sessions import create_auth_sessions_router
from tests.db.test_roles_per_account_postgres import (
    World,
    _account,
    _bl,
    _person,
    _Store,
)


@pytest.fixture
def world(disposable_db_conn) -> Iterator[World]:
    """The people of ``test_roles_per_account_postgres``, in a rolled-back transaction."""
    conn = disposable_db_conn
    with conn.transaction(force_rollback=True):
        a, project_a = _account(conn, "a")
        b, project_b = _account(conn, "b")
        yield World(
            conn=conn,
            a=a,
            b=b,
            project_a=project_a,
            project_b=project_b,
            alice=_person(conn, "alice", a, {a: "admin", b: "viewer"}),
            bob=_person(conn, "bob", a, {a: "admin"}),
            carol=_person(conn, "carol", b, {b: "admin"}),
            dave=_person(conn, "dave", a, {a: "operator", b: "operator"}),
        )


class _NoCommit:
    """The rolled-back test transaction: a route's commit must not end it."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        # Like production: a dict cursor request returns mappings (psycopg dict_row),
        # whichever RealDictCursor class the caller imported.
        if kwargs.pop("cursor_factory", None) is not None:
            kwargs.setdefault("row_factory", dict_row)
        return self._conn.cursor(*args, **kwargs)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _session(world: World, user_id: str, account_id: str) -> str:
    token = f"session-{uuid.uuid4().hex}"
    world.conn.execute(
        """INSERT INTO auth_sessions (user_id, account_id, session_token_hash, expires_at)
           VALUES (%s, %s, %s, now() + interval '1 hour')""",
        (user_id, account_id, session_token_hash(token)),
    )
    return token


def _switch_client(world: World, token: str, audit: list[dict[str, Any]]) -> TestClient:
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
    )
    app = FastAPI()

    @app.middleware("http")
    async def _principal(request: Request, call_next):
        request.state.auth_user = resolve.get_user_by_session_token(store, token)
        return await call_next(request)

    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("fs_session", token)
    return client


def _session_account(world: World, token: str) -> str:
    row = world.conn.execute(
        "SELECT account_id::text FROM auth_sessions WHERE session_token_hash = %s",
        (session_token_hash(token),),
    ).fetchone()
    return str(row[0])


@pytest.fixture
def erin(world: World) -> str:
    user_id = _person(world.conn, "erin", world.a, {world.a: "viewer"})
    world.conn.execute(
        """INSERT INTO user_account_memberships (user_id, account_id, is_default, role_id)
           VALUES (%s, %s, false, NULL)""",
        (user_id, world.b),
    )
    return user_id


def test_switching_into_an_account_without_a_role_is_refused(world: World, erin: str) -> None:
    token = _session(world, erin, world.a)
    audit: list[dict[str, Any]] = []

    response = _switch_client(world, token, audit).post(
        "/api/auth/switch-account", json={"account_id": world.b}
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": "You have no role in this company. Ask its administrator.",
        "code": "no_role_in_account",
    }
    # The session stays where it was, with the role it had there.
    assert _session_account(world, token) == world.a
    assert resolve.get_user_by_session_token(_Store(world.conn), token)["roles"] == ["viewer"]
    assert [(e["action"], e["status"], e["detail"]["reason"]) for e in audit] == [
        ("switch_account", "rejected", "no_role_in_account")
    ]


def test_switching_into_an_account_with_a_role_moves_the_session(world: World) -> None:
    token = _session(world, world.alice, world.a)

    response = _switch_client(world, token, []).post(
        "/api/auth/switch-account", json={"account_id": world.b}
    )

    assert response.status_code == 200
    memberships = {
        item["account_id"]: item["role"] for item in response.json()["account_memberships"]
    }
    assert memberships == {world.a: "admin", world.b: "viewer"}
    assert _session_account(world, token) == world.b
    assert resolve.get_user_by_session_token(_Store(world.conn), token)["roles"] == ["viewer"]


def _username(world: World, user_id: str) -> str:
    return str(
        world.conn.execute("SELECT username FROM users WHERE id = %s", (user_id,)).fetchone()[0]
    )


def test_candidates_are_people_of_managed_accounts_only(world: World) -> None:
    # Carol lives in B; an administrator of A alone neither browses B's people nor
    # finds Carol by her exact email address (ADR 0085 A: she is invited instead).
    with world.conn.cursor() as cur:
        by_name = search_candidates(
            cur, query="carol", account_id=world.a, managed_account_ids=[world.a]
        )
        by_email = search_candidates(
            cur,
            query=_username(world, world.carol),
            account_id=world.a,
            managed_account_ids=[world.a],
        )
        # Managing B too, Bob finds Carol by name; members of A are never offered.
        managing_both = search_candidates(
            cur, query="example.invalid", account_id=world.a, managed_account_ids=[world.a, world.b]
        )

    assert by_name == []
    assert by_email == []
    assert {person["id"] for person in managing_both} == {world.carol}


def test_a_person_of_a_managed_account_is_added_with_a_role_in_this_account_only(
    world: World,
) -> None:
    with world.conn.cursor() as cur:
        add_member(
            cur,
            user_id=world.carol,
            account_id=world.a,
            role="operator",
            managed_account_ids=[world.a, world.b],
        )
        with pytest.raises(ServiceConflictError):
            add_member(
                cur,
                user_id=world.carol,
                account_id=world.a,
                role="viewer",
                managed_account_ids=[world.a, world.b],
            )

    assert account_role_keys(_bl(world), world.carol, world.a) == ["operator"]
    assert account_role_keys(_bl(world), world.carol, world.b) == ["admin"]


def _invite(cur: Any, world: World, email: str, role: str, token: str, **kwargs: Any) -> str:
    return invite(
        cur,
        account_id=kwargs.get("account_id", world.a),
        email=email,
        role=role,
        invited_by=kwargs.get("invited_by", world.bob),
        token_hash=invitation_token_hash(token),
    )["id"]


def test_a_person_out_of_sight_joins_only_by_accepting_an_invitation(world: World) -> None:
    email = _username(world, world.carol)
    with world.conn.cursor() as cur:
        invitation = _invite(cur, world, email.upper(), "viewer", "link-one")
        # Inviting again renews the one pending invitation with a new link (P1).
        assert _invite(cur, world, email, "operator", "link-two") == invitation
        with pytest.raises(ServiceNotFoundError):
            preview(cur, invitation_token_hash("link-one"))
        assert preview(cur, invitation_token_hash("link-two"))["role"] == "operator"
        # An address nobody has is stored too: the answer never says who exists.
        nobody = _invite(cur, world, "Nobody@X.invalid", "viewer", "link-three")
        assert nobody and nobody != invitation
        # The administrators see both, each address as it was typed last.
        sent = {row["id"]: row["email"] for row in sent_invitations(cur, world.a)}
        assert sent == {invitation: email, nobody: "Nobody@X.invalid"}
    assert account_role_keys(_bl(world), world.carol, world.a) == []

    with world.conn.cursor() as cur:
        # The role shown must still be the invitation's.
        with pytest.raises(ServiceConflictError):
            decide(
                cur,
                token_hash=invitation_token_hash("link-two"),
                user_id=world.carol,
                accept=True,
                expected_role="viewer",
            )
        decide(
            cur,
            token_hash=invitation_token_hash("link-two"),
            user_id=world.carol,
            accept=True,
            expected_role="operator",
        )
        # Single use.
        with pytest.raises(ServiceNotFoundError):
            decide(
                cur,
                token_hash=invitation_token_hash("link-two"),
                user_id=world.carol,
                accept=True,
                expected_role="operator",
            )

    assert account_role_keys(_bl(world), world.carol, world.a) == ["operator"]


def test_a_declined_invitation_creates_no_membership(world: World) -> None:
    with world.conn.cursor() as cur:
        _invite(cur, world, _username(world, world.carol), "admin", "link-decline")
        decide(
            cur,
            token_hash=invitation_token_hash("link-decline"),
            user_id=world.carol,
            accept=False,
        )
        with pytest.raises(ServiceNotFoundError):
            preview(cur, invitation_token_hash("link-decline"))
    assert account_role_keys(_bl(world), world.carol, world.a) == []


def test_the_link_not_the_address_decides_who_joins(world: World) -> None:
    """C1: the person who opens the link while signed in joins, whatever address the
    administrator typed (an invitation sent before they had an account included)."""
    with world.conn.cursor() as cur:
        invitation = _invite(cur, world, "New.Person@x.test", "operator", "link-new")
    newcomer = _person(world.conn, "newcomer", world.b, {world.b: "viewer"})

    with world.conn.cursor() as cur:
        decide(
            cur,
            token_hash=invitation_token_hash("link-new"),
            user_id=newcomer,
            accept=True,
            expected_role="operator",
        )
        answered_by = world.conn.execute(
            "SELECT user_id::text FROM account_invitations WHERE id = %s", (invitation,)
        ).fetchone()[0]
    assert answered_by == newcomer
    assert account_role_keys(_bl(world), newcomer, world.a) == ["operator"]
    # The token itself is nowhere in the database.
    stored = world.conn.execute(
        "SELECT token_hash FROM account_invitations WHERE id = %s", (invitation,)
    ).fetchone()[0]
    assert stored == invitation_token_hash("link-new") and "link-new" not in stored


def test_a_cancelled_or_expired_invitation_is_gone_for_both_sides(world: World) -> None:
    email = _username(world, world.carol)
    with world.conn.cursor() as cur:
        invitation = _invite(cur, world, email, "viewer", "link-cancel")
        # Another account's administrator cannot cancel it.
        with pytest.raises(ServiceNotFoundError):
            cancel(cur, account_id=world.b, invitation_id=invitation)
        assert cancel(cur, account_id=world.a, invitation_id=invitation)["id"] == invitation
        assert sent_invitations(cur, world.a) == []
        with pytest.raises(ServiceNotFoundError):
            preview(cur, invitation_token_hash("link-cancel"))
        with pytest.raises(ServiceNotFoundError):
            cancel(cur, account_id=world.a, invitation_id=invitation)

        expired = _invite(cur, world, email, "viewer", "link-expired")
        cur.execute(
            "UPDATE account_invitations SET created_at = now() - interval '20 days',"
            " expires_at = now() - interval '1 day' WHERE id = %s",
            (expired,),
        )
        assert sent_invitations(cur, world.a) == []
        with pytest.raises(ServiceNotFoundError):
            decide(
                cur,
                token_hash=invitation_token_hash("link-expired"),
                user_id=world.carol,
                accept=True,
                expected_role="viewer",
            )
        # The account's next invitation removes the expired one, reporting it (P2).
        assert [row["id"] for row in delete_expired(cur, world.a)] == [expired]
        cur.execute("SELECT count(*) FROM account_invitations WHERE id = %s", (expired,))
        assert cur.fetchone()[0] == 0


def test_an_inviter_who_stops_being_admin_ends_their_invitations(world: World) -> None:
    """C4: Bob invites to A, then is demoted there."""
    with world.conn.cursor() as cur:
        invitation = _invite(cur, world, "someone@x.test", "viewer", "link-bob")
        kept = _invite(cur, world, "other@x.test", "viewer", "link-alice", invited_by=world.alice)
        cur.execute(
            "UPDATE user_account_memberships SET role_id = (SELECT id FROM roles WHERE key = "
            "'viewer') WHERE user_id = %s AND account_id = %s",
            (world.bob, world.a),
        )
        # The link no longer opens, even before the cancellation runs.
        with pytest.raises(ServiceNotFoundError):
            preview(cur, invitation_token_hash("link-bob"))
        cancelled = cancel_invitations_of_former_admins(cur, world.bob)
        assert [row["id"] for row in cancelled] == [invitation]
        assert cancel_invitations_of_former_admins(cur, world.alice) == []
        cur.execute("SELECT status FROM account_invitations WHERE id = %s", (kept,))
        assert cur.fetchone()[0] == "pending"


def test_a_person_out_of_sight_is_not_added(world: World) -> None:
    with world.conn.cursor() as cur, pytest.raises(ServiceNotFoundError):
        # Carol is in no account Bob manages: she can only be invited.
        add_member(
            cur,
            user_id=world.carol,
            account_id=world.a,
            role="viewer",
            managed_account_ids=[world.a],
        )
    assert account_role_keys(_bl(world), world.carol, world.a) == []


def test_removing_a_person_from_this_account_keeps_their_other_accounts(world: World) -> None:
    token_in_b = _session(world, world.dave, world.b)
    token_in_a = _session(world, world.dave, world.a)
    with world.conn.cursor() as cur:
        remove_member(cur, user_id=world.dave, account_id=world.a, actor_user_id=world.bob)

    assert account_role_keys(_bl(world), world.dave, world.a) == []
    assert account_role_keys(_bl(world), world.dave, world.b) == ["operator"]
    home, default = world.conn.execute(
        """SELECT u.account_id::text, m.is_default FROM users u
             JOIN user_account_memberships m ON m.user_id = u.id
            WHERE u.id = %s""",
        (world.dave,),
    ).fetchone()
    assert (home, default) == (world.b, True)
    revoked = dict(
        world.conn.execute(
            "SELECT session_token_hash, revoked FROM auth_sessions WHERE user_id = %s",
            (world.dave,),
        ).fetchall()
    )
    assert revoked == {session_token_hash(token_in_a): True, session_token_hash(token_in_b): False}


def test_removal_is_refused_for_the_last_admin_the_only_account_and_yourself(
    world: World,
) -> None:
    with world.conn.cursor() as cur:
        with pytest.raises(ServiceConflictError, match="last active administrator"):
            remove_member(cur, user_id=world.carol, account_id=world.b, actor_user_id=world.alice)
        with pytest.raises(ServiceConflictError, match="only company"):
            remove_member(cur, user_id=world.bob, account_id=world.a, actor_user_id=world.alice)
        with pytest.raises(ServiceForbiddenError):
            remove_member(cur, user_id=world.alice, account_id=world.b, actor_user_id=world.alice)

    assert account_role_keys(_bl(world), world.carol, world.b) == ["admin"]
    assert account_role_keys(_bl(world), world.bob, world.a) == ["admin"]
