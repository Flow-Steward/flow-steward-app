from __future__ import annotations

from http.cookies import SimpleCookie
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.entrypoints.api.auth_sessions import create_auth_sessions_router


class _Cursor:
    def __init__(self, *, membership_rows: list[dict[str, Any]] | None = None) -> None:
        self.rowcount = 0
        self.executed_queries: list[str] = []
        self._membership_rows = [dict(item) for item in (membership_rows or [])]
        self._next_fetchall: list[Any] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, query: str, _params: Any = None) -> None:
        normalized = " ".join(str(query).split())
        self.executed_queries.append(normalized)
        self.rowcount = 0
        self._next_fetchall = []
        if (
            "FROM user_account_memberships uam JOIN accounts a ON a.account_id = uam.account_id"
            in normalized
        ):
            self._next_fetchall = list(self._membership_rows)
            return
        if "UPDATE auth_sessions" in normalized:
            self.rowcount = 1

    def fetchall(self) -> list[Any]:
        value = list(self._next_fetchall)
        self._next_fetchall = []
        return value


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self, **_kwargs: Any) -> _Cursor:
        return self._cursor


def _api_error_response(exc: Exception) -> JSONResponse:
    return JSONResponse({"api_error": str(exc)}, status_code=500)


def _client_with_state(
    *,
    auth_user: dict[str, Any] | None = None,
    membership_rows: list[dict[str, Any]] | None = None,
    login_user: dict[str, Any] | None = None,
    verify_password_result: bool = True,
    secure_cookies: bool = False,
    verify_password_calls: list[tuple[str, str]] | None = None,
    session_columns: frozenset[str] = frozenset({"account_id"}),
    audit_events: list[dict[str, Any]] | None = None,
    open_invitation_tokens: frozenset[str] = frozenset(),
) -> tuple[TestClient, _Cursor]:
    cursor = _Cursor(membership_rows=membership_rows)
    bl = SimpleNamespace(
        conn=_Conn(cursor),
        _column_exists=lambda table, column: table == "auth_sessions" and column in session_columns,
        _table_exists=lambda table: table in {"user_account_memberships", "accounts"},
    )
    router = create_auth_sessions_router(
        get_logger_only=lambda: bl,
        session_token_hash=lambda token: f"hash:{token}",
        auth_cookie_secure=lambda: secure_cookies,
        log_auth_audit_event=lambda *_args, **kwargs: (
            audit_events.append(dict(kwargs)) if audit_events is not None else None
        ),
        local_auth_enabled=lambda: True,
        bootstrap_request_allowed=lambda _request: (True, ""),
        auth_tables_available=lambda _bl: True,
        resolve_actor_username=lambda _request, _requested, default="auth": default,
        get_user_by_username=lambda _bl, _username: (
            dict(login_user) if login_user is not None else None
        ),
        request_client_ip=lambda _request: "127.0.0.1",
        auth_login_lock_state=lambda _username, _client_ip: (False, 0),
        auth_login_record_failure=lambda _username, _client_ip: (False, 0),
        verify_password=lambda plain, hashed: (
            (
                verify_password_calls.append((str(plain), str(hashed)))
                if verify_password_calls is not None
                else None
            )
            or verify_password_result
        ),
        password_hash=lambda plain: f"hashed:{plain}",
        auth_user_column_exists=lambda _bl, column: column == "must_change_password",
        auth_login_clear_failures=lambda _username, _client_ip: None,
        csrf_request_valid=lambda _request: True,
        api_error_response=_api_error_response,
        err_database_not_configured="db-missing",
        err_local_auth_disabled="local-disabled",
        err_auth_tables_not_available="auth-missing",
        err_too_many_login_attempts="rate-limited",
        auth_password_min_length=10,
        auth_session_ttl_hours=24,
        auth_remember_session_ttl_hours=168,
        auth_cookie_name="fs_session",
        auth_csrf_cookie_name="fs_csrf",
        first_admin_service_factory=lambda _bl: None,
        invitation_token_open=lambda _bl, token: token in open_invitation_tokens,
    )
    app = FastAPI()

    @app.middleware("http")
    async def _inject_state(request: Request, call_next):
        if auth_user is not None:
            request.state.auth_user = dict(auth_user)
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False), cursor


def test_auth_route_inventory_has_no_retired_executor_endpoints() -> None:
    client, _cursor = _client_with_state()
    retired_path_fragment = "/api/" + "co" + "dex-cli/"
    paths = {str(route.path) for route in client.app.routes}

    assert not any(retired_path_fragment in path for path in paths)
    assert "/api/auth/login" in paths
    assert "/api/auth/logout" in paths


def test_switch_account_updates_current_session() -> None:
    client, cursor = _client_with_state(
        auth_user={
            "id": 7,
            "username": "dev@example.com",
            "account_id": "acc_alpha",
            "account_memberships": [
                {
                    "account_id": "acc_alpha",
                    "display_name": "Alpha",
                    "is_active": True,
                    "is_default": True,
                },
                {
                    "account_id": "acc_beta",
                    "display_name": "Beta",
                    "is_active": True,
                    "is_default": False,
                },
            ],
        },
        membership_rows=[
            {
                "account_id": "acc_alpha",
                "display_name": "Alpha",
                "is_active": True,
                "is_default": True,
            },
            {
                "account_id": "acc_beta",
                "display_name": "Beta",
                "is_active": True,
                "is_default": False,
                "role": "viewer",
            },
        ],
    )

    client.cookies.set("fs_session", "session-token")
    response = client.post(
        "/api/auth/switch-account",
        json={"account_id": "acc_beta"},
    )

    assert response.status_code == 200
    assert response.json()["account_id"] == "acc_beta"
    assert any("UPDATE auth_sessions" in query for query in cursor.executed_queries)


def test_switch_account_rejects_unavailable_account() -> None:
    client, cursor = _client_with_state(
        auth_user={
            "id": 7,
            "username": "dev@example.com",
            "account_id": "acc_alpha",
            "account_memberships": [
                {
                    "account_id": "acc_alpha",
                    "display_name": "Alpha",
                    "is_active": True,
                    "is_default": True,
                }
            ],
        },
        membership_rows=[
            {
                "account_id": "acc_alpha",
                "display_name": "Alpha",
                "is_active": True,
                "is_default": True,
            }
        ],
    )

    client.cookies.set("fs_session", "session-token")
    response = client.post(
        "/api/auth/switch-account",
        json={"account_id": "acc_beta"},
    )

    assert response.status_code == 403
    assert "not available" in response.json()["error"].lower()
    assert not any("UPDATE auth_sessions" in query for query in cursor.executed_queries)


_TWO_ACCOUNTS = [
    {
        "account_id": "acc_alpha",
        "display_name": "Alpha",
        "is_active": True,
        "is_default": True,
        "role": "admin",
    },
    {
        "account_id": "acc_beta",
        "display_name": "Beta",
        "is_active": True,
        "is_default": False,
        "role": "viewer",
    },
]


def test_switch_account_refuses_a_session_from_external_sign_in() -> None:
    """ADR 0083: an external session stays in the account that issued it."""
    audit_events: list[dict[str, Any]] = []
    client, cursor = _client_with_state(
        audit_events=audit_events,
        auth_user={
            "id": 7,
            "username": "dev@example.com",
            "account_id": "acc_alpha",
            "auth_source": "external",
            "account_memberships": _TWO_ACCOUNTS,
        },
        membership_rows=_TWO_ACCOUNTS,
    )

    client.cookies.set("fs_session", "session-token")
    response = client.post("/api/auth/switch-account", json={"account_id": "acc_beta"})

    assert response.status_code == 403
    assert response.json()["code"] == "external_session_account_pinned"
    assert not any("UPDATE auth_sessions" in query for query in cursor.executed_queries)
    # The refusal is in the auth audit, not only in the response.
    assert [
        (event["action"], event["status"], event["detail"]["reason"]) for event in audit_events
    ] == [("switch_account", "rejected", "external_session_account_pinned")]


def test_switch_account_refuses_an_account_where_the_person_has_no_role() -> None:
    """ADR 0085: a membership without a role grants nothing; the switch is refused
    with a clear 403 instead of moving the session into an empty account."""
    audit_events: list[dict[str, Any]] = []
    rows = [dict(_TWO_ACCOUNTS[0]), {**_TWO_ACCOUNTS[1], "role": None}]
    client, cursor = _client_with_state(
        audit_events=audit_events,
        auth_user={
            "id": 7,
            "username": "dev@example.com",
            "account_id": "acc_alpha",
            "account_memberships": rows,
        },
        membership_rows=rows,
    )

    client.cookies.set("fs_session", "session-token")
    response = client.post("/api/auth/switch-account", json={"account_id": "acc_beta"})

    assert response.status_code == 403
    assert response.json() == {
        "error": "You have no role in this company. Ask its administrator.",
        "code": "no_role_in_account",
    }
    assert not any("UPDATE auth_sessions" in query for query in cursor.executed_queries)
    assert [
        (event["action"], event["status"], event["detail"]["reason"]) for event in audit_events
    ] == [("switch_account", "rejected", "no_role_in_account")]


def test_switch_account_moves_only_a_local_session_where_sessions_record_their_method() -> None:
    client, cursor = _client_with_state(
        auth_user={
            "id": 7,
            "username": "dev@example.com",
            "account_id": "acc_alpha",
            "account_memberships": _TWO_ACCOUNTS,
        },
        membership_rows=_TWO_ACCOUNTS,
        session_columns=frozenset({"account_id", "auth_method"}),
    )

    client.cookies.set("fs_session", "session-token")
    response = client.post("/api/auth/switch-account", json={"account_id": "acc_beta"})

    assert response.status_code == 200
    update = next(query for query in cursor.executed_queries if "UPDATE auth_sessions" in query)
    assert "AND auth_method = 'local'" in update


def test_login_rejects_username_alias_payload() -> None:
    client, _cursor = _client_with_state(
        membership_rows=[
            {
                "account_id": "acc_default",
                "display_name": "Default",
                "is_active": True,
                "is_default": True,
            }
        ],
        login_user={
            "id": 7,
            "username": "e2e_admin",
            "account_id": "acc_default",
            "display_name": "E2E Admin",
            "password_hash": "hash",
            "is_active": True,
            "roles": ["admin"],
            "account_memberships": [
                {
                    "account_id": "acc_default",
                    "display_name": "Default",
                    "is_active": True,
                    "is_default": True,
                }
            ],
        },
    )

    response = client.post(
        "/api/auth/login",
        json={"username": "e2e_admin", "password": "supersecret"},
    )

    assert response.status_code == 422


def test_login_rejects_username_alias_before_identifier_normalization() -> None:
    client, _cursor = _client_with_state()

    response = client.post(
        "/api/auth/login",
        json={"username": "bad login!", "password": "supersecret"},
    )

    assert response.status_code == 422


def test_login_rejects_malformed_email_identifier() -> None:
    client, _cursor = _client_with_state()

    response = client.post(
        "/api/auth/login",
        json={"email": "ada@", "password": "supersecret"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Invalid email address"


def test_login_unknown_user_still_runs_password_verification() -> None:
    verify_calls: list[tuple[str, str]] = []
    client, _cursor = _client_with_state(verify_password_calls=verify_calls)

    response = client.post(
        "/api/auth/login",
        json={"email": "missing@example.com", "password": "supersecret"},
    )

    assert response.status_code == 401
    assert len(verify_calls) == 1
    assert verify_calls[0][0] == "supersecret"
    assert verify_calls[0][1].startswith("pbkdf2_sha256$")


@pytest.mark.parametrize("secure_cookies", [False, True])
@pytest.mark.parametrize("remember_me,expected_max_age", [(False, 86400), (True, 604800)])
def test_login_keeps_session_private_and_csrf_readable(
    secure_cookies: bool, remember_me: bool, expected_max_age: int
) -> None:
    membership = {
        "account_id": "acc_default",
        "display_name": "Default",
        "is_active": True,
        "is_default": True,
    }
    client, cursor = _client_with_state(
        secure_cookies=secure_cookies,
        membership_rows=[membership],
        login_user={
            "id": 7,
            "username": "admin@example.com",
            "account_id": "acc_default",
            "display_name": "Admin",
            "password_hash": "hash",
            "is_active": True,
            "roles": ["admin"],
            "account_memberships": [membership],
        },
    )
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "supersecret", "remember_me": remember_me},
    )

    assert response.status_code == 200
    assert any("INSERT INTO auth_sessions" in query for query in cursor.executed_queries)
    cookies: SimpleCookie = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        cookies.load(header)
    assert set(cookies) == {"fs_session", "fs_csrf"}
    assert cookies["fs_session"]["httponly"] is True
    assert cookies["fs_csrf"]["httponly"] == ""
    assert cookies["fs_csrf"].value == response.json()["csrf_token"]
    assert cookies["fs_session"].value
    assert cookies["fs_session"].value != cookies["fs_csrf"].value
    assert cookies["fs_session"].value not in response.text
    for cookie in cookies.values():
        assert bool(cookie["secure"]) is secure_cookies
        assert cookie["samesite"] == "lax"
        assert cookie["path"] == "/"
        assert cookie["max-age"] == str(expected_max_age)


# --- ADR 0085 (E): sign-in lands only where the person has a role ------------------------


def _login_user(memberships: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": 7,
        "username": "pat@example.com",
        "account_id": "acc_home",
        "display_name": "Pat",
        "password_hash": "hash",
        "is_active": True,
        "roles": [],
        "account_memberships": memberships,
    }


_HOME_WITHOUT_ROLE = {
    "account_id": "acc_home",
    "display_name": "Home",
    "is_active": True,
    "is_default": True,
    "role": "",
}
_OTHER_WITH_ROLE = {
    "account_id": "acc_other",
    "display_name": "Other",
    "is_active": True,
    "is_default": False,
    "role": "viewer",
}


def test_login_without_a_role_anywhere_is_refused_with_the_no_role_message() -> None:
    audit_events: list[dict[str, Any]] = []
    client, cursor = _client_with_state(
        membership_rows=[_HOME_WITHOUT_ROLE],
        login_user=_login_user([_HOME_WITHOUT_ROLE]),
        audit_events=audit_events,
    )

    response = client.post(
        "/api/auth/login", json={"email": "pat@example.com", "password": "supersecret"}
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": "You have no role in this company. Ask its administrator.",
        "code": "no_role_in_account",
    }
    assert not any("INSERT INTO auth_sessions" in query for query in cursor.executed_queries)
    assert audit_events[-1]["detail"]["reason"] == "no_role_in_account"


def test_a_person_without_a_role_signs_in_to_answer_an_open_invitation_link() -> None:
    """ADR 0085, P5: the link's token lets a person with a role nowhere sign in; the
    session lands in their home account, where they have no role."""
    audit_events: list[dict[str, Any]] = []
    client, cursor = _client_with_state(
        membership_rows=[_HOME_WITHOUT_ROLE],
        login_user=_login_user([_HOME_WITHOUT_ROLE]),
        audit_events=audit_events,
        open_invitation_tokens=frozenset({"tok-open"}),
    )

    response = client.post(
        "/api/auth/login",
        json={
            "email": "pat@example.com",
            "password": "supersecret",
            "invitation_token": "tok-open",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["invitation_only"] is True
    assert response.json()["user"]["account_id"] == "acc_home"
    assert any("INSERT INTO auth_sessions" in query for query in cursor.executed_queries)
    assert audit_events[-1]["detail"]["invitation_only"] is True


@pytest.mark.parametrize("token", ["", "tok-used"], ids=["no-token", "closed-token"])
def test_without_an_open_invitation_a_person_without_a_role_is_still_refused(token: str) -> None:
    client, cursor = _client_with_state(
        membership_rows=[_HOME_WITHOUT_ROLE],
        login_user=_login_user([_HOME_WITHOUT_ROLE]),
        open_invitation_tokens=frozenset({"tok-open"}),
    )

    response = client.post(
        "/api/auth/login",
        json={"email": "pat@example.com", "password": "supersecret", "invitation_token": token},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "no_role_in_account"
    assert not any("INSERT INTO auth_sessions" in query for query in cursor.executed_queries)


def test_a_person_with_a_role_signs_in_as_usual_with_an_invitation_token() -> None:
    memberships = [_HOME_WITHOUT_ROLE, _OTHER_WITH_ROLE]
    client, _cursor = _client_with_state(
        membership_rows=memberships,
        login_user=_login_user(memberships),
        open_invitation_tokens=frozenset({"tok-open"}),
    )

    response = client.post(
        "/api/auth/login",
        json={
            "email": "pat@example.com",
            "password": "supersecret",
            "invitation_token": "tok-open",
        },
    )

    assert response.status_code == 200
    assert response.json()["user"]["account_id"] == "acc_other"
    assert "invitation_only" not in response.json()


@pytest.mark.parametrize("chosen", [None, "acc_home"], ids=["default", "chosen-without-role"])
def test_login_lands_in_another_account_where_the_person_has_a_role(chosen: str | None) -> None:
    memberships = [_HOME_WITHOUT_ROLE, _OTHER_WITH_ROLE]
    client, cursor = _client_with_state(
        membership_rows=memberships, login_user=_login_user(memberships)
    )
    body: dict[str, Any] = {"email": "pat@example.com", "password": "supersecret"}
    if chosen:
        body["account_id"] = chosen

    response = client.post("/api/auth/login", json=body)

    assert response.status_code == 200, response.text
    # The landed account is the session user's account, as in every login answer.
    assert response.json()["user"]["account_id"] == "acc_other"
    assert any("INSERT INTO auth_sessions" in query for query in cursor.executed_queries)


def test_the_account_choice_offers_only_accounts_with_a_role() -> None:
    third = {**_OTHER_WITH_ROLE, "account_id": "acc_third", "display_name": "Third"}
    memberships = [_HOME_WITHOUT_ROLE, _OTHER_WITH_ROLE, third]
    client, _cursor = _client_with_state(
        membership_rows=memberships, login_user=_login_user(memberships)
    )

    response = client.post(
        "/api/auth/login", json={"email": "pat@example.com", "password": "supersecret"}
    )

    assert response.status_code == 409
    assert [item["account_id"] for item in response.json()["account_options"]] == [
        "acc_other",
        "acc_third",
    ]
