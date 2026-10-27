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
) -> tuple[TestClient, _Cursor]:
    cursor = _Cursor(membership_rows=membership_rows)
    bl = SimpleNamespace(
        conn=_Conn(cursor),
        _column_exists=lambda table, column: table == "auth_sessions" and column == "account_id",
        _table_exists=lambda table: table in {"user_account_memberships", "accounts"},
    )
    router = create_auth_sessions_router(
        get_logger_only=lambda: bl,
        session_token_hash=lambda token: f"hash:{token}",
        auth_cookie_secure=lambda: secure_cookies,
        log_auth_audit_event=lambda *_args, **_kwargs: None,
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
