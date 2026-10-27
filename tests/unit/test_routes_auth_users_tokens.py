from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.entrypoints.api.auth_tokens_accounts import create_auth_tokens_accounts_router
from core.entrypoints.api.auth_users import create_auth_users_router

_USER_77 = "00000000-0000-0000-0000-000000000077"
_USER_88 = "00000000-0000-0000-0000-000000000088"


def _clear_user_login_failures(_username: str, *, bl: Any = None) -> None:
    _ = bl


class _Cursor:
    def __init__(self) -> None:
        self.rowcount = 0
        self._next_fetchone: Any = None
        self._next_fetchall: list[Any] = []
        self.executed_queries: list[str] = []
        self.executed_params: list[Any] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, query: str, _params: Any = None) -> None:
        normalized = " ".join(str(query).split())
        self.executed_queries.append(normalized)
        self.executed_params.append(_params)
        self.rowcount = 0
        self._next_fetchone = None
        self._next_fetchall = []
        if "SELECT timezone FROM users WHERE id =" in normalized:
            self._next_fetchone = ("Europe/Vilnius",)
            return
        if "SELECT locale FROM users WHERE id =" in normalized:
            self._next_fetchone = ("ru",)
            return
        if "SELECT COUNT(*) AS total" in normalized:
            self._next_fetchone = {"total": 2 if "WHERE deleted_at IS NULL" in normalized else 1}
            return
        if "SELECT u.id::text AS id, u.is_active" in normalized:
            self._next_fetchone = {"id": _USER_77, "is_active": True, "is_admin": False}
            return
        if "GROUP BY u.id" in normalized:
            self._next_fetchall = [
                {
                    "id": 1,
                    "username": "admin",
                    "display_name": "Admin",
                    "is_active": True,
                    "must_change_password": False,
                    "account_id": "acc-default",
                    "roles": ["admin"],
                    "created_at": None,
                    "updated_at": None,
                }
            ]
            return
        if "INSERT INTO users" in normalized and "RETURNING id" in normalized:
            self._next_fetchone = (_USER_77,)
            return
        if "UPDATE auth_sessions" in normalized:
            self.rowcount = 2

    def fetchone(self) -> Any:
        value = self._next_fetchone
        self._next_fetchone = None
        return value

    def fetchall(self) -> list[Any]:
        value = list(self._next_fetchall)
        self._next_fetchall = []
        return value


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.commits = 0

    def cursor(self, **_kwargs: Any) -> _Cursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1


def _api_error_response(exc: Exception, *_args: Any, **_kwargs: Any) -> JSONResponse:
    return JSONResponse({"api_error": str(exc)}, status_code=500)


def _client_with_state(
    router,
    *,
    auth_user: dict[str, Any] | None = None,
    auth_source: str = "",
) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _inject_state(request: Request, call_next):
        if auth_user is not None:
            request.state.auth_user = auth_user
        if auth_source:
            request.state.auth_source = auth_source
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_auth_introspection_returns_bounded_effective_principal() -> None:
    auth_user = {
        "id": 1,
        "username": "mcp-agent",
        "roles": ["operator"],
        "principal_type": "service_account",
        "service_account_key": "mcp-functional",
        "account_id": "acc-default",
        "scopes": ["code.read", "projects.read"],
        "allowed_project_ids": ["project-1"],
        "allowed_workflow_ids": ["workflow-1"],
    }
    router = create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: False,
        get_logger_only=lambda: SimpleNamespace(conn=_Conn(_Cursor())),
        auth_tables_available=lambda _bl: True,
        require_authenticated_user=lambda _request, **_kwargs: dict(auth_user),
        request_auth_user_payload=lambda request: getattr(request.state, "auth_user", None),
        api_error_response=_api_error_response,
        log_auth_audit_event=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures=_clear_user_login_failures,
        get_user_by_id=lambda _bl, _user_id: dict(auth_user),
        get_user_by_username=lambda _bl, _username: dict(auth_user),
        normalize_requested_roles=lambda roles: list(roles or []),
        seed_default_roles=lambda _bl: None,
        password_hash=lambda value: f"hash:{value}",
        auth_user_column_exists=lambda _bl, _column: True,
        real_dict_cursor_kwargs=dict,
        auth_admin_roles={"admin"},
        err_authentication_required="auth-required",
        err_database_not_configured="db-missing",
        err_auth_tables_not_available="auth-tables-missing",
        err_admin_role_required="admin-required",
        err_local_auth_disabled="local-auth-disabled",
        err_invalid_json_body="invalid-json",
        auth_csrf_cookie_name="csrf_cookie",
    )
    client = _client_with_state(router, auth_user=auth_user, auth_source="machine")

    response = client.get("/api/auth/introspect")

    assert response.status_code == 200
    assert response.json() == {
        "active": True,
        "principal": {
            "id": "1",
            "username": "mcp-agent",
            "principal_type": "service_account",
            "service_account_key": "mcp-functional",
            "auth_source": "machine",
            "account_id": "acc-default",
            "roles": ["operator"],
            "scopes": ["code.read", "projects.read"],
            "allowed_project_ids": ["project-1"],
            "allowed_workflow_ids": ["workflow-1"],
            "expires_at": None,
        },
    }

    interactive_user = {
        **auth_user,
        "principal_type": "user",
        "service_account_key": "",
        "roles": ["admin"],
        "scopes": [],
    }
    interactive = _client_with_state(
        router,
        auth_user=interactive_user,
        auth_source="local",
    ).get("/api/auth/introspect")
    assert interactive.status_code == 200
    assert interactive.json()["principal"]["auth_source"] == "local"
    assert interactive.json()["principal"]["scopes"] == ["admin:all"]


def test_auth_users_router_success_paths() -> None:
    cursor = _Cursor()
    bl = SimpleNamespace(conn=_Conn(cursor))
    auth_user = {
        "id": 1,
        "username": "admin",
        "display_name": "Admin",
        "roles": ["admin"],
        "timezone": "",
        "locale": "",
        "account_id": "acc-default",
        "account_ids": ["acc-default"],
        "account_memberships": [{"account_id": "acc-default", "is_active": True}],
    }

    def _get_user_by_id(_bl: Any, user_id: int | str) -> dict[str, Any] | None:
        if str(user_id) == "1":
            return dict(auth_user)
        if str(user_id) == _USER_77:
            return {
                "id": _USER_77,
                "username": "newuser@example.com",
                "password_hash": "$argon2id$must-never-leave-application",
                "display_name": "New User",
                "is_active": True,
                "must_change_password": False,
                "roles": ["viewer"],
                "account_id": "acc-default",
                "account_ids": ["acc-default"],
                "account_memberships": [{"account_id": "acc-default", "is_active": True}],
            }
        return None

    router = create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: False,
        get_logger_only=lambda: bl,
        auth_tables_available=lambda _bl: True,
        require_authenticated_user=lambda _request, **_kwargs: {
            "id": 1,
            "username": "admin",
            "roles": ["admin"],
            "account_id": "acc-default",
            "account_ids": ["acc-default"],
            "account_memberships": [{"account_id": "acc-default", "is_active": True}],
        },
        request_auth_user_payload=lambda request: getattr(request.state, "auth_user", None),
        api_error_response=_api_error_response,
        log_auth_audit_event=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures=_clear_user_login_failures,
        get_user_by_id=_get_user_by_id,
        get_user_by_username=lambda _bl, username: (
            dict(auth_user) if str(username) == "admin" else None
        ),
        normalize_requested_roles=lambda roles: [
            str(x).strip() for x in (roles or []) if str(x).strip()
        ],
        seed_default_roles=lambda _bl: None,
        password_hash=lambda value: f"hash:{value}",
        auth_user_column_exists=lambda _bl, column: (
            column in {"must_change_password", "deleted_at", "account_id"}
        ),
        real_dict_cursor_kwargs=dict,
        auth_admin_roles={"admin"},
        err_authentication_required="auth-required",
        err_database_not_configured="db-missing",
        err_auth_tables_not_available="auth-tables-missing",
        err_admin_role_required="admin-required",
        err_local_auth_disabled="local-auth-disabled",
        err_invalid_json_body="invalid-json",
        auth_csrf_cookie_name="csrf_cookie",
    )
    client = _client_with_state(router, auth_user=auth_user)

    client.cookies.set("csrf_cookie", "csrf-1")
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["timezone"] == "Europe/Vilnius"
    assert me.json()["user"]["locale"] == ""
    assert me.json()["csrf_token"] == "csrf-1"

    profile = client.patch("/api/users/me/profile", json={"display_name": "Admin Updated"})
    assert profile.status_code == 200
    assert profile.json()["display_name"] == "Admin Updated"

    timezone_update = client.patch("/api/users/me/timezone", json={"timezone": "UTC"})
    assert timezone_update.status_code == 200
    assert timezone_update.json()["timezone"] == "UTC"

    locale_update = client.patch("/api/users/me/locale", json={"locale": "en-US"})
    assert locale_update.status_code == 200
    assert locale_update.json()["locale"] == "en"

    auth_user["timezone"] = ""
    detect = client.post("/api/users/me/detect-timezone", json={"timezone": "Europe/Vilnius"})
    assert detect.status_code == 200
    assert detect.json()["status"] == "ok"
    auth_user["timezone"] = "Europe/Vilnius"
    already = client.post("/api/users/me/detect-timezone", json={"timezone": "UTC"})
    assert already.status_code == 200
    assert already.json()["status"] == "already_set"

    listed = client.get("/api/auth/users?page=1&per_page=10&search=adm&role=admin&active=true")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["users"][0]["email"] == "admin"
    list_queries = [query for query in cursor.executed_queries if "GROUP BY u.id" in query]
    assert list_queries
    assert ", ," not in list_queries[-1]
    assert "must_change_password" in list_queries[-1]
    assert "u.created_at" in list_queries[-1]
    assert "u.account_id = ANY" in list_queries[-1]

    created = client.post(
        "/api/auth/users",
        json={
            "email": "newuser@example.com",
            "password": "Password123!",
            "display_name": "New User",
            "roles": ["viewer"],
            "is_active": True,
            "must_change_password": False,
        },
    )
    assert created.status_code == 200
    assert created.json()["user"]["id"] == _USER_77
    assert "password_hash" not in created.json()["user"]

    updated = client.put(
        f"/api/auth/users/{_USER_77}",
        json={
            "display_name": "Updated Name",
            "password": "Password999!",
            "roles": ["admin", "viewer"],
            "is_active": True,
            "must_change_password": True,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["user"]["username"] == "newuser@example.com"
    assert "password_hash" not in updated.json()["user"]
    assert any(
        "UPDATE auth_sessions SET revoked = true" in query for query in cursor.executed_queries
    )
    assert any(
        "UPDATE personal_access_tokens SET revoked = true" in query
        for query in cursor.executed_queries
    )

    unlocked = client.post(f"/api/auth/users/{_USER_77}/unlock")
    assert unlocked.status_code == 200
    assert unlocked.json()["status"] == "unlocked"

    force_reset = client.post(f"/api/auth/users/{_USER_77}/force-password-reset")
    assert force_reset.status_code == 200
    assert force_reset.json()["revoked_sessions"] == 2

    deleted = client.delete(f"/api/auth/users/{_USER_77}")
    assert deleted.status_code == 200
    assert deleted.json() == {
        "status": "deleted",
        "user_id": _USER_77,
        "username": "newuser@example.com",
        "revoked_sessions": 2,
    }
    assert any(
        "SET is_active = false, deleted_at = now()" in query for query in cursor.executed_queries
    )

    invalid_email = client.post(
        "/api/auth/users",
        json={
            "email": "not-an-email",
            "password": "Password123!",
            "roles": ["viewer"],
        },
    )
    assert invalid_email.status_code == 400
    assert "not-an-email" not in str(invalid_email.json())


def test_auth_users_router_error_paths() -> None:
    class _FailCursor(_Cursor):
        def execute(self, query: str, params: Any = None) -> None:
            _ = params
            normalized = " ".join(str(query).split()).lower()
            if "update users set timezone" in normalized:
                raise RuntimeError('column "timezone" of relation "users" does not exist')
            if "update users set locale" in normalized:
                raise RuntimeError('column "locale" of relation "users" does not exist')
            super().execute(query, params)

    bl = SimpleNamespace(conn=_Conn(_FailCursor()))

    router = create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: False,
        get_logger_only=lambda: bl,
        auth_tables_available=lambda _bl: True,
        require_authenticated_user=lambda _request, **_kwargs: (_ for _ in ()).throw(
            PermissionError("denied")
        ),
        request_auth_user_payload=lambda request: getattr(request.state, "auth_user", None),
        api_error_response=_api_error_response,
        log_auth_audit_event=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures=_clear_user_login_failures,
        get_user_by_id=lambda _bl, _user_id: None,
        get_user_by_username=lambda _bl, _username: None,
        normalize_requested_roles=lambda roles: [
            str(x).strip() for x in (roles or []) if str(x).strip()
        ],
        seed_default_roles=lambda _bl: None,
        password_hash=lambda value: f"hash:{value}",
        auth_user_column_exists=lambda _bl, column: (
            column in {"must_change_password", "deleted_at", "account_id"}
        ),
        real_dict_cursor_kwargs=dict,
        auth_admin_roles={"admin"},
        err_authentication_required="auth-required",
        err_database_not_configured="db-missing",
        err_auth_tables_not_available="auth-tables-missing",
        err_admin_role_required="admin-required",
        err_local_auth_disabled="local-auth-disabled",
        err_invalid_json_body="invalid-json",
        auth_csrf_cookie_name="csrf_cookie",
    )
    client = _client_with_state(router)

    no_me = client.get("/api/auth/me")
    assert no_me.status_code == 401

    bad_tz_user = _client_with_state(
        router,
        auth_user={"id": 0, "username": "nobody", "roles": [], "timezone": ""},
    )
    assert bad_tz_user.patch("/api/users/me/timezone", json={"timezone": "UTC"}).status_code == 401

    auth_client = _client_with_state(
        router,
        auth_user={"id": 1, "username": "admin", "roles": ["admin"], "timezone": ""},
    )
    bad_json = auth_client.patch(
        "/api/users/me/timezone",
        content="not-json",
        headers={"content-type": "application/json"},
    )
    assert bad_json.status_code == 400
    invalid_tz = auth_client.patch("/api/users/me/timezone", json={"timezone": "Bad/Timezone"})
    assert invalid_tz.status_code == 422
    column_missing = auth_client.patch("/api/users/me/timezone", json={"timezone": "UTC"})
    assert column_missing.status_code == 503
    bad_locale = auth_client.patch("/api/users/me/locale", json={"locale": "zz"})
    assert bad_locale.status_code == 422
    locale_column_missing = auth_client.patch("/api/users/me/locale", json={"locale": "en"})
    assert locale_column_missing.status_code == 503


def test_auth_users_admin_routes_are_account_scoped() -> None:
    cursor = _Cursor()
    bl = SimpleNamespace(conn=_Conn(cursor))
    admin_user = {
        "id": 1,
        "username": "admin-a@example.com",
        "roles": ["admin"],
        "account_id": "acc-a",
        "account_ids": ["acc-a"],
        "account_memberships": [{"account_id": "acc-a", "is_active": True}],
    }
    target_a = {
        "id": _USER_77,
        "username": "user-a@example.com",
        "display_name": "User A",
        "is_active": True,
        "must_change_password": False,
        "roles": ["viewer"],
        "account_id": "acc-a",
        "account_ids": ["acc-a"],
        "account_memberships": [{"account_id": "acc-a", "is_active": True}],
    }
    target_b = {
        "id": _USER_88,
        "username": "user-b@example.com",
        "display_name": "User B",
        "is_active": True,
        "must_change_password": False,
        "roles": ["viewer"],
        "account_id": "acc-b",
        "account_ids": ["acc-b"],
        "account_memberships": [{"account_id": "acc-b", "is_active": True}],
    }

    def _get_user_by_id(_bl: Any, user_id: int | str) -> dict[str, Any] | None:
        if str(user_id) == "1":
            return dict(admin_user)
        if str(user_id) == _USER_77:
            return dict(target_a)
        if str(user_id) == _USER_88:
            return dict(target_b)
        return None

    router = create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: False,
        get_logger_only=lambda: bl,
        auth_tables_available=lambda _bl: True,
        require_authenticated_user=lambda _request, **_kwargs: dict(admin_user),
        request_auth_user_payload=lambda request: getattr(request.state, "auth_user", None),
        api_error_response=_api_error_response,
        log_auth_audit_event=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures=_clear_user_login_failures,
        get_user_by_id=_get_user_by_id,
        get_user_by_username=lambda _bl, _username: None,
        normalize_requested_roles=lambda roles: [
            str(x).strip() for x in (roles or []) if str(x).strip()
        ],
        seed_default_roles=lambda _bl: None,
        password_hash=lambda value: f"hash:{value}",
        auth_user_column_exists=lambda _bl, column: (
            column in {"must_change_password", "deleted_at", "account_id"}
        ),
        real_dict_cursor_kwargs=dict,
        auth_admin_roles={"admin"},
        err_authentication_required="auth-required",
        err_database_not_configured="db-missing",
        err_auth_tables_not_available="auth-tables-missing",
        err_admin_role_required="admin-required",
        err_local_auth_disabled="local-auth-disabled",
        err_invalid_json_body="invalid-json",
        auth_csrf_cookie_name="csrf_cookie",
    )
    client = _client_with_state(router, auth_user=admin_user)

    listed = client.get("/api/auth/users")
    assert listed.status_code == 200
    list_params = [params for params in cursor.executed_params if isinstance(params, tuple)]
    assert any("acc-a" in str(params) for params in list_params)

    cross_account_create = client.post(
        "/api/auth/users",
        json={
            "email": "new-b@example.com",
            "password": "Password123!",
            "display_name": "New B",
            "roles": ["viewer"],
            "primary_account_id": "acc-b",
            "account_ids": ["acc-b"],
        },
    )
    assert cross_account_create.status_code == 403
    assert cross_account_create.json()["error"] == "account access denied"

    cross_account_update = client.put(f"/api/auth/users/{_USER_88}", json={"display_name": "Nope"})
    assert cross_account_update.status_code == 403
    assert cross_account_update.json()["error"] == "account access denied"

    cross_account_unlock = client.post(f"/api/auth/users/{_USER_88}/unlock")
    assert cross_account_unlock.status_code == 403
    assert cross_account_unlock.json()["error"] == "account access denied"

    cross_account_reset = client.post(f"/api/auth/users/{_USER_88}/force-password-reset")
    assert cross_account_reset.status_code == 403
    assert cross_account_reset.json()["error"] == "account access denied"


def test_auth_user_display_name_update_does_not_revoke_existing_tokens() -> None:
    cursor = _Cursor()
    bl = SimpleNamespace(conn=_Conn(cursor))
    admin_user = {
        "id": 1,
        "username": "admin@example.com",
        "roles": ["admin"],
        "account_id": "acc-default",
        "account_ids": ["acc-default"],
        "account_memberships": [{"account_id": "acc-default", "is_active": True}],
    }
    target_user = {
        "id": _USER_77,
        "username": "user@example.com",
        "display_name": "User",
        "is_active": True,
        "must_change_password": False,
        "roles": ["viewer"],
        "account_id": "acc-default",
        "account_ids": ["acc-default"],
        "account_memberships": [{"account_id": "acc-default", "is_active": True}],
    }

    def _get_user_by_id(_bl: Any, user_id: int | str) -> dict[str, Any] | None:
        if str(user_id) == "1":
            return dict(admin_user)
        if str(user_id) == _USER_77:
            return dict(target_user)
        return None

    router = create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: False,
        get_logger_only=lambda: bl,
        auth_tables_available=lambda _bl: True,
        require_authenticated_user=lambda _request, **_kwargs: dict(admin_user),
        request_auth_user_payload=lambda request: getattr(request.state, "auth_user", None),
        api_error_response=_api_error_response,
        log_auth_audit_event=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures=_clear_user_login_failures,
        get_user_by_id=_get_user_by_id,
        get_user_by_username=lambda _bl, _username: None,
        normalize_requested_roles=lambda roles: [
            str(x).strip() for x in (roles or []) if str(x).strip()
        ],
        seed_default_roles=lambda _bl: None,
        password_hash=lambda value: f"hash:{value}",
        auth_user_column_exists=lambda _bl, column: (
            column in {"must_change_password", "deleted_at", "account_id"}
        ),
        real_dict_cursor_kwargs=dict,
        auth_admin_roles={"admin"},
        err_authentication_required="auth-required",
        err_database_not_configured="db-missing",
        err_auth_tables_not_available="auth-tables-missing",
        err_admin_role_required="admin-required",
        err_local_auth_disabled="local-auth-disabled",
        err_invalid_json_body="invalid-json",
        auth_csrf_cookie_name="csrf_cookie",
    )
    client = _client_with_state(router, auth_user=admin_user)

    response = client.put(f"/api/auth/users/{_USER_77}", json={"display_name": "Only Renamed"})

    assert response.status_code == 200
    assert not any(
        "UPDATE auth_sessions SET revoked = true" in query for query in cursor.executed_queries
    )
    assert not any(
        "UPDATE personal_access_tokens SET revoked = true" in query
        for query in cursor.executed_queries
    )

    same_account_update = client.put(
        f"/api/auth/users/{_USER_77}", json={"display_name": "Allowed"}
    )
    assert same_account_update.status_code == 200


class _NoopCursor:
    rowcount = 0

    def __enter__(self) -> _NoopCursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, _query: str, _params: Any = None) -> None:
        return None


class _TokenBL:
    def __init__(self) -> None:
        self.conn = SimpleNamespace(cursor=lambda **_kwargs: _NoopCursor())
        self._next_pat_id = 2
        self._next_machine_token_id = 100
        self.pats: list[dict[str, Any]] = [{"id": 1, "label": "seed", "expires_at": None}]
        self.accounts: dict[str, dict[str, Any]] = {
            "acc-default": {
                "account_id": "acc-default",
                "display_name": "Default",
                "is_active": True,
            }
        }
        self.service_accounts: dict[str, dict[str, Any]] = {
            "robot": {
                "account_id": "acc-default",
                "service_account_key": "robot",
                "is_active": True,
                "scopes": [],
            }
        }
        self.machine_tokens: dict[str, list[dict[str, Any]]] = {"robot": []}
        self.last_machine_token_update: dict[str, Any] | None = None

    def count_user_pats(self, _user_id: int) -> int:
        return len(self.pats)

    def list_user_pats_paginated(
        self, _user_id: int, *, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        return list(self.pats[offset : offset + limit])

    def list_user_pats(self, _user_id: int) -> list[dict[str, Any]]:
        return list(self.pats)

    def create_pat(self, **kwargs: Any) -> dict[str, Any]:
        account_id = str(kwargs.get("account_id") or "").strip()
        if not account_id:
            raise ValueError("account_id is required")
        token = {
            "id": self._next_pat_id,
            "account_id": account_id,
            "label": kwargs.get("label", ""),
            "expires_at": None,
        }
        self._next_pat_id += 1
        self.pats.append(token)
        return token

    def revoke_pat(self, token_id: int, _user_id: int) -> dict[str, Any] | None:
        for token in self.pats:
            if int(token.get("id") or 0) == int(token_id):
                return token
        return None

    def list_accounts(
        self, *, include_inactive: bool, search: str, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        _ = include_inactive, search
        rows = list(self.accounts.values())
        return rows[offset : offset + limit]

    def create_account(
        self, *, account_id: str, display_name: str, is_active: bool
    ) -> dict[str, Any]:
        normalized = str(account_id).strip()
        if normalized in self.accounts:
            raise ValueError("account already exists")
        account = {
            "account_id": normalized,
            "display_name": display_name,
            "is_active": bool(is_active),
        }
        self.accounts[normalized] = account
        return account

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        return self.accounts.get(str(account_id).strip())

    def update_account(self, account_id: str, *, display_name: str) -> dict[str, Any] | None:
        normalized = str(account_id).strip()
        account = self.accounts.get(normalized)
        if not account:
            return None
        clean_name = str(display_name).strip()
        if not clean_name:
            raise ValueError("display_name is required")
        account["display_name"] = clean_name
        return account

    def set_account_active(self, account_id: str, *, is_active: bool) -> dict[str, Any] | None:
        account = self.accounts.get(str(account_id).strip())
        if not account:
            return None
        account["is_active"] = bool(is_active)
        return account

    def list_service_accounts(
        self, *, account_id: str | None = None, include_inactive: bool
    ) -> list[dict[str, Any]]:
        _ = include_inactive
        return [
            dict(row)
            for row in self.service_accounts.values()
            if not account_id or row.get("account_id") == account_id
        ]

    def create_service_account(self, **kwargs: Any) -> dict[str, Any] | None:
        key = str(kwargs.get("service_account_key") or "").strip().lower()
        if key == "bad":
            raise ValueError("invalid service account")
        if key == "none":
            return None
        account = {
            "account_id": str(kwargs.get("account_id") or "acc-default"),
            "service_account_key": key,
            "is_active": bool(kwargs.get("is_active", True)),
            "scopes": list(kwargs.get("scopes") or []),
        }
        self.service_accounts[key] = account
        self.machine_tokens.setdefault(key, [])
        return account

    def update_service_account(
        self, service_account_key: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        key = str(service_account_key or "").strip().lower()
        account = self.service_accounts.get(key)
        account_id = str(kwargs.get("account_id") or "").strip()
        if not account:
            return None
        if account_id and account.get("account_id") != account_id:
            return None
        account.update(
            {
                "display_name": str(kwargs.get("display_name") or key),
                "description": str(kwargs.get("description") or ""),
                "is_active": bool(kwargs.get("is_active", True)),
                "scopes": list(kwargs.get("scopes") or []),
                "allowed_project_ids": list(kwargs.get("allowed_project_ids") or []),
                "allowed_workflow_ids": list(kwargs.get("allowed_workflow_ids") or []),
            }
        )
        return account

    def delete_service_account(
        self, service_account_key: str, *, account_id: str | None = None
    ) -> dict[str, Any] | None:
        key = str(service_account_key or "").strip().lower()
        account = self.service_accounts.get(key)
        if not account:
            return None
        if account_id and account.get("account_id") != account_id:
            return None
        self.machine_tokens.pop(key, None)
        return self.service_accounts.pop(key)

    def list_service_account_tokens(
        self,
        service_account_key: str,
        *,
        account_id: str | None = None,
        include_revoked: bool,
    ) -> list[dict[str, Any]]:
        _ = include_revoked
        key = str(service_account_key).strip().lower()
        service_account = self.service_accounts.get(key) or {}
        if account_id and service_account.get("account_id") != account_id:
            return []
        return list(self.machine_tokens.get(key, []))

    def create_machine_token(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs.get("service_account_key") or "").strip().lower()
        if key not in self.service_accounts:
            raise ValueError("Unknown service account")
        account_id = str(kwargs.get("account_id") or "acc-default")
        if self.service_accounts[key].get("account_id") != account_id:
            raise ValueError("Unknown service account")
        if key == "bad-machine":
            raise ValueError("invalid machine token")
        token = {
            "id": self._next_machine_token_id,
            "account_id": account_id,
            "label": str(kwargs.get("label") or ""),
            "scopes": list(kwargs.get("scopes") or []),
            "allowed_project_ids": list(kwargs.get("allowed_project_ids") or []),
            "allowed_workflow_ids": list(kwargs.get("allowed_workflow_ids") or []),
            "revoked": False,
        }
        self._next_machine_token_id += 1
        self.machine_tokens.setdefault(key, []).append(token)
        return token

    def update_machine_token(self, token_id: int, **kwargs: Any) -> dict[str, Any] | None:
        self.last_machine_token_update = dict(kwargs)
        account_id = str(kwargs.get("account_id") or "").strip()
        for tokens in self.machine_tokens.values():
            for token in tokens:
                if int(token.get("id") or 0) == int(token_id):
                    if account_id and token.get("account_id") != account_id:
                        return None
                    if bool(token.get("revoked", False)):
                        raise ValueError("Cannot update a revoked machine token")
                    token["label"] = str(kwargs.get("label") or token.get("label") or "")
                    if kwargs.get("scopes") is not None:
                        token["scopes"] = list(kwargs["scopes"])
                    if kwargs.get("allowed_project_ids") is not None:
                        token["allowed_project_ids"] = list(kwargs["allowed_project_ids"])
                    if kwargs.get("allowed_workflow_ids") is not None:
                        token["allowed_workflow_ids"] = list(kwargs["allowed_workflow_ids"])
                    return token
        return None

    def revoke_machine_token(
        self, token_id: int, *, account_id: str | None = None
    ) -> dict[str, Any] | None:
        for tokens in self.machine_tokens.values():
            for token in tokens:
                if int(token.get("id") or 0) == int(token_id):
                    if account_id and token.get("account_id") != account_id:
                        return None
                    token["revoked"] = True
                    return token
        return None

    def delete_machine_token(
        self, token_id: int, *, account_id: str | None = None
    ) -> dict[str, Any] | None:
        for key, tokens in self.machine_tokens.items():
            for idx, token in enumerate(tokens):
                if int(token.get("id") or 0) == int(token_id):
                    if account_id and token.get("account_id") != account_id:
                        return None
                    removed = tokens.pop(idx)
                    return {"id": int(removed.get("id") or token_id), "service_account_key": key}
        return None


def _token_router(
    bl: _TokenBL,
    *,
    local_auth_enabled: bool = True,
    allow_auth: bool = True,
    auth_user: dict[str, Any] | None = None,
):
    auth_payload = auth_user or {
        "id": 1,
        "username": "admin",
        "roles": ["admin"],
        "account_id": "acc-default",
        "account_memberships": [
            {
                "account_id": "acc-default",
                "display_name": "Default",
                "is_active": True,
                "is_default": True,
            }
        ],
    }

    def _require_auth(_request: Request, **kwargs: Any) -> dict[str, Any]:
        _ = kwargs
        if not allow_auth:
            raise PermissionError("denied")
        return dict(auth_payload)

    return create_auth_tokens_accounts_router(
        local_auth_enabled=lambda: local_auth_enabled,
        get_logger_only=lambda: bl,
        auth_tables_available=lambda _bl: True,
        auth_pat_tables_available=lambda _bl: True,
        auth_account_tables_available=lambda _bl: True,
        auth_service_account_tables_available=lambda _bl: True,
        auth_machine_token_tables_available=lambda _bl: True,
        require_authenticated_user=_require_auth,
        api_error_response=_api_error_response,
        log_auth_audit_event=lambda *_args, **_kwargs: None,
        auth_admin_roles={"admin"},
        err_local_auth_disabled="local-auth-disabled",
        err_database_not_configured="db-missing",
        err_auth_tables_not_available="auth-tables-missing",
        err_pat_tables_not_available="pat-tables-missing",
        err_authentication_required="auth-required",
        err_token_not_found="token-not-found",
        err_account_tables_not_available="account-tables-missing",
        err_admin_role_required="admin-required",
        err_service_account_key_required="service-account-key-required",
        err_machine_token_tables_not_available="machine-token-tables-missing",
    )


def test_auth_tokens_accounts_router_success_paths() -> None:
    bl = _TokenBL()
    client = _client_with_state(_token_router(bl))

    list_tokens = client.get("/api/auth/tokens?page=1&per_page=1")
    assert list_tokens.status_code == 200
    assert list_tokens.json()["total"] >= 1

    created_pat = client.post(
        "/api/auth/tokens",
        json={"label": "dev-token", "expires_in_days": 7, "scopes": ["read:*"]},
    )
    assert created_pat.status_code == 200
    pat_id = int(created_pat.json()["token_id"])
    revoked_pat = client.post(f"/api/auth/tokens/{pat_id}/revoke")
    assert revoked_pat.status_code == 200
    assert revoked_pat.json()["status"] == "revoked"

    accounts = client.get("/api/auth/accounts")
    assert accounts.status_code == 200
    assert len(accounts.json()["accounts"]) >= 1

    current_account = client.get("/api/auth/accounts/current")
    assert current_account.status_code == 200
    assert current_account.json()["account"]["account_id"] == "acc-default"

    updated_current_account = client.patch(
        "/api/auth/accounts/current",
        json={"display_name": "Baltic Blues"},
    )
    assert updated_current_account.status_code == 200
    assert updated_current_account.json()["account"]["display_name"] == "Baltic Blues"

    created_account = client.post(
        "/api/auth/accounts",
        json={"account_id": "acc-1", "display_name": "Team A", "is_active": True},
    )
    assert created_account.status_code == 200
    disabled_account = client.post(
        "/api/auth/accounts/acc-1/disable", json={"reason": "maintenance"}
    )
    assert disabled_account.status_code == 200
    assert disabled_account.json()["status"] == "disabled"

    service_accounts = client.get("/api/auth/service-accounts")
    assert service_accounts.status_code == 200

    created_service_account = client.post(
        "/api/auth/service-accounts",
        json={
            "service_account_key": "robot2",
            "display_name": "Robot Two",
            "scopes": ["command:*"],
        },
    )
    assert created_service_account.status_code == 200
    assert created_service_account.json()["service_account"]["service_account_key"] == "robot2"

    updated_service_account = client.patch(
        "/api/auth/service-accounts/robot2",
        json={
            "display_name": "Robot Two Updated",
            "scopes": ["jobs.read"],
            "allowed_workflow_ids": ["wf-a"],
            "is_active": False,
        },
    )
    assert updated_service_account.status_code == 200
    assert updated_service_account.json()["service_account"]["is_active"] is False
    assert updated_service_account.json()["service_account"]["allowed_workflow_ids"] == ["wf-a"]

    list_machine_tokens = client.get("/api/auth/service-accounts/robot2/tokens")
    assert list_machine_tokens.status_code == 200
    assert list_machine_tokens.json()["tokens"] == []

    create_machine = client.post(
        "/api/auth/service-accounts/robot2/tokens",
        json={"label": "robot2-token", "expires_in_days": 14, "scopes": ["read:*"]},
    )
    assert create_machine.status_code == 200
    machine_token_id = int(create_machine.json()["token_id"])

    update_machine = client.patch(
        f"/api/auth/service-accounts/robot2/tokens/{machine_token_id}",
        json={
            "label": "robot2-token-updated",
            "scopes": ["jobs.read"],
            "allowed_workflow_ids": ["wf-a"],
        },
    )
    assert update_machine.status_code == 200
    assert update_machine.json()["status"] == "updated"
    assert update_machine.json()["token"]["label"] == "robot2-token-updated"
    assert update_machine.json()["token"]["allowed_workflow_ids"] == ["wf-a"]

    revoke_machine = client.post(
        f"/api/auth/service-accounts/robot2/tokens/{machine_token_id}/revoke"
    )
    assert revoke_machine.status_code == 200
    assert revoke_machine.json()["status"] == "revoked"

    create_machine_2 = client.post(
        "/api/auth/service-accounts/robot2/tokens",
        json={"label": "robot2-token-2", "expires_in_days": 14},
    )
    machine_token_id_2 = int(create_machine_2.json()["token_id"])
    deleted_machine = client.delete(
        f"/api/auth/service-accounts/robot2/tokens/{machine_token_id_2}"
    )
    assert deleted_machine.status_code == 200
    assert deleted_machine.json()["status"] == "deleted"

    create_machine_3 = client.post(
        "/api/auth/service-accounts/robot2/tokens",
        json={"label": "robot2-token-3", "expires_in_days": 14},
    )
    machine_token_id_3 = int(create_machine_3.json()["token_id"])
    deleted_legacy = client.post(
        f"/api/auth/service-accounts/robot2/tokens/{machine_token_id_3}/delete"
    )
    assert deleted_legacy.status_code == 404

    deleted_machine_3 = client.delete(
        f"/api/auth/service-accounts/robot2/tokens/{machine_token_id_3}"
    )
    assert deleted_machine_3.status_code == 200
    assert deleted_machine_3.json()["status"] == "deleted"

    deleted_service_account = client.delete("/api/auth/service-accounts/robot2")
    assert deleted_service_account.status_code == 200
    assert deleted_service_account.json()["service_account"]["service_account_key"] == "robot2"
    assert "robot2" not in bl.service_accounts


def test_machine_token_label_only_patch_preserves_all_authority_fields() -> None:
    bl = _TokenBL()
    bl.service_accounts["robot"].update(
        {
            "scopes": ["jobs.read", "jobs.write"],
            "allowed_project_ids": ["1", "2"],
            "allowed_workflow_ids": ["wf-a", "wf-b"],
        }
    )
    bl.machine_tokens["robot"] = [
        {
            "id": 31,
            "account_id": "acc-default",
            "label": "narrow token",
            "scopes": ["jobs.read"],
            "allowed_project_ids": ["1"],
            "allowed_workflow_ids": ["wf-a"],
            "revoked": False,
        }
    ]
    client = _client_with_state(_token_router(bl))

    response = client.patch(
        "/api/auth/service-accounts/robot/tokens/31",
        json={"label": "renamed token"},
    )

    assert response.status_code == 200
    assert bl.machine_tokens["robot"][0] == {
        "id": 31,
        "account_id": "acc-default",
        "label": "renamed token",
        "scopes": ["jobs.read"],
        "allowed_project_ids": ["1"],
        "allowed_workflow_ids": ["wf-a"],
        "revoked": False,
    }
    assert bl.last_machine_token_update is not None
    assert bl.last_machine_token_update["scopes"] is None
    assert bl.last_machine_token_update["allowed_project_ids"] is None
    assert bl.last_machine_token_update["allowed_workflow_ids"] is None


def test_machine_token_patch_can_explicitly_remove_all_scopes() -> None:
    bl = _TokenBL()
    bl.service_accounts["robot"]["scopes"] = ["jobs:*"]
    bl.machine_tokens["robot"] = [
        {
            "id": 31,
            "account_id": "acc-default",
            "label": "narrow token",
            "scopes": ["jobs.read"],
            "allowed_project_ids": [],
            "allowed_workflow_ids": [],
            "revoked": False,
        }
    ]
    client = _client_with_state(_token_router(bl))

    response = client.patch(
        "/api/auth/service-accounts/robot/tokens/31",
        json={"label": "no authority", "scopes": []},
    )

    assert response.status_code == 200
    assert bl.machine_tokens["robot"][0]["scopes"] == []
    assert bl.last_machine_token_update is not None
    assert bl.last_machine_token_update["scopes"] == []
    assert bl.last_machine_token_update["allowed_project_ids"] is None
    assert bl.last_machine_token_update["allowed_workflow_ids"] is None


def test_pat_creation_uses_authenticated_account_scope() -> None:
    bl = _TokenBL()
    auth_user = {
        "id": 41,
        "username": "member@example.com",
        "roles": ["operator"],
        "account_id": "acc-other",
        "account_memberships": [
            {
                "account_id": "acc-other",
                "display_name": "Other",
                "is_active": True,
            }
        ],
    }
    client = _client_with_state(_token_router(bl, auth_user=auth_user))

    created = client.post(
        "/api/auth/tokens",
        json={"label": "scoped-token", "expires_in_days": 7, "scopes": ["read:*"]},
    )

    assert created.status_code == 200
    assert created.json()["token"]["account_id"] == "acc-other"
    token_id = int(created.json()["token_id"])
    stored = next(token for token in bl.pats if int(token.get("id") or 0) == token_id)
    assert stored["account_id"] == "acc-other"


def test_pat_creation_rejects_missing_account_scope() -> None:
    bl = _TokenBL()
    auth_user = {
        "id": 41,
        "username": "member@example.com",
        "roles": ["operator"],
        "account_id": "",
        "account_memberships": [],
    }
    client = _client_with_state(_token_router(bl, auth_user=auth_user))

    created = client.post(
        "/api/auth/tokens",
        json={"label": "unscoped-token", "expires_in_days": 7, "scopes": ["read:*"]},
    )

    assert created.status_code == 403
    assert created.json()["error"] == "Current account not found"
    assert all(token.get("label") != "unscoped-token" for token in bl.pats)


def test_pat_creation_rejects_operator_session_special_machine_scope() -> None:
    bl = _TokenBL()
    auth_user = {
        "id": 41,
        "username": "operator@example.com",
        "roles": ["operator"],
        "account_id": "acc-default",
        "account_memberships": [{"account_id": "acc-default", "is_active": True}],
    }
    client = _client_with_state(_token_router(bl, auth_user=auth_user))

    created = client.post(
        "/api/auth/tokens",
        json={"label": "notify-escalation", "scopes": ["notify:dispatch"]},
    )

    assert created.status_code == 403
    assert all(token.get("label") != "notify-escalation" for token in bl.pats)


def test_pat_creation_rejects_scopes_beyond_restricted_caller_authority() -> None:
    # CAND-061: a scope-restricted PAT caller cannot mint a broader PAT.
    bl = _TokenBL()
    restricted_caller = {
        "id": 71,
        "username": "ci-bot",
        "roles": ["operator"],
        "auth_source": "pat",
        "scopes": ["jobs.read"],
        "account_id": "acc-default",
        "account_memberships": [{"account_id": "acc-default", "is_active": True}],
    }
    client = _client_with_state(_token_router(bl, auth_user=restricted_caller))

    created = client.post(
        "/api/auth/tokens",
        json={"label": "escalation", "expires_in_days": 7, "scopes": ["admin:all"]},
    )

    assert created.status_code == 403
    assert all(token.get("label") != "escalation" for token in bl.pats)


def test_pat_creation_rejects_project_allowlist_beyond_restricted_caller() -> None:
    # CAND-061: a project-restricted caller cannot widen the allowlist.
    bl = _TokenBL()
    restricted_caller = {
        "id": 72,
        "username": "scoped-operator",
        "roles": ["operator"],
        "auth_source": "pat",
        "scopes": ["jobs.read"],
        "allowed_project_ids": ["1"],
        "account_id": "acc-default",
        "account_memberships": [{"account_id": "acc-default", "is_active": True}],
    }
    client = _client_with_state(_token_router(bl, auth_user=restricted_caller))

    created = client.post(
        "/api/auth/tokens",
        json={
            "label": "widen-projects",
            "scopes": ["jobs.read"],
            "allowed_project_ids": ["1", "2"],
        },
    )

    assert created.status_code == 403
    assert all(token.get("label") != "widen-projects" for token in bl.pats)


def test_pat_creation_allows_scopes_within_restricted_caller_authority() -> None:
    # CAND-061 positive: same-or-narrower grant still succeeds and persists.
    bl = _TokenBL()
    restricted_caller = {
        "id": 73,
        "username": "scoped-operator",
        "roles": ["operator"],
        "auth_source": "pat",
        "scopes": ["jobs.read", "projects.read"],
        "allowed_project_ids": ["1", "2"],
        "account_id": "acc-default",
        "account_memberships": [{"account_id": "acc-default", "is_active": True}],
    }
    client = _client_with_state(_token_router(bl, auth_user=restricted_caller))

    created = client.post(
        "/api/auth/tokens",
        json={
            "label": "within-authority",
            "scopes": ["jobs.read"],
            "allowed_project_ids": ["1"],
        },
    )

    assert created.status_code == 200
    assert any(token.get("label") == "within-authority" for token in bl.pats)


def test_service_account_creation_rejects_scopes_beyond_restricted_caller() -> None:
    # CAND-063: even an operator-equivalent service-account caller cannot mint
    # a broader service account than its own scope authority.
    bl = _TokenBL()
    restricted_caller = {
        "id": 74,
        "username": "robot-issuer",
        "roles": ["operator"],
        "auth_source": "machine",
        "principal_type": "service_account",
        "scopes": ["command:run"],
        "account_id": "acc-default",
        "account_memberships": [{"account_id": "acc-default", "is_active": True}],
    }
    client = _client_with_state(_token_router(bl, auth_user=restricted_caller))

    created = client.post(
        "/api/auth/service-accounts",
        json={"service_account_key": "escalated", "scopes": ["admin:all"]},
    )

    assert created.status_code == 403
    assert "escalated" not in bl.service_accounts


def test_machine_token_creation_rejects_workflow_allowlist_beyond_caller() -> None:
    # CAND-063: a workflow-restricted caller cannot widen a child machine token.
    bl = _TokenBL()
    restricted_caller = {
        "id": 75,
        "username": "robot-issuer",
        "roles": ["operator"],
        "auth_source": "machine",
        "principal_type": "service_account",
        "scopes": ["command:run"],
        "allowed_workflow_ids": ["wf-a"],
        "account_id": "acc-default",
        "account_memberships": [{"account_id": "acc-default", "is_active": True}],
    }
    client = _client_with_state(_token_router(bl, auth_user=restricted_caller))

    created = client.post(
        "/api/auth/service-accounts/robot/tokens",
        json={"label": "widen-wf", "allowed_workflow_ids": ["wf-a", "wf-b"]},
    )

    assert created.status_code == 403
    assert all(token.get("label") != "widen-wf" for token in bl.machine_tokens.get("robot", []))


def test_machine_token_update_rejects_workflow_allowlist_beyond_caller() -> None:
    # CAND-063: editing a token cannot widen authority beyond the caller either.
    bl = _TokenBL()
    bl.machine_tokens["robot"].append(
        {
            "id": 77,
            "account_id": "acc-default",
            "label": "robot-token",
            "allowed_workflow_ids": ["wf-a"],
            "revoked": False,
        }
    )
    restricted_caller = {
        "id": 76,
        "username": "robot-issuer",
        "roles": ["operator"],
        "auth_source": "machine",
        "principal_type": "service_account",
        "scopes": ["command:run"],
        "allowed_workflow_ids": ["wf-a"],
        "account_id": "acc-default",
        "account_memberships": [{"account_id": "acc-default", "is_active": True}],
    }
    client = _client_with_state(_token_router(bl, auth_user=restricted_caller))

    updated = client.patch(
        "/api/auth/service-accounts/robot/tokens/77",
        json={"label": "widen-wf", "allowed_workflow_ids": ["wf-a", "wf-b"]},
    )

    assert updated.status_code == 403
    assert bl.machine_tokens["robot"][0]["label"] == "robot-token"


def test_service_account_and_machine_token_creation_within_caller_authority() -> None:
    # CAND-063 positive: an admin caller (full authority) still provisions freely.
    bl = _TokenBL()
    client = _client_with_state(_token_router(bl))  # default caller has admin role

    created_sa = client.post(
        "/api/auth/service-accounts",
        json={"service_account_key": "robot-ok", "scopes": ["admin:all"]},
    )
    assert created_sa.status_code == 200
    assert "robot-ok" in bl.service_accounts

    created_token = client.post(
        "/api/auth/service-accounts/robot-ok/tokens",
        json={"label": "ok-token", "allowed_workflow_ids": ["wf-x"]},
    )
    assert created_token.status_code == 200
    assert any(token.get("label") == "ok-token" for token in bl.machine_tokens.get("robot-ok", []))


def test_auth_tokens_accounts_router_error_paths() -> None:
    bl = _TokenBL()
    disabled_client = _client_with_state(_token_router(bl, local_auth_enabled=False))
    assert disabled_client.get("/api/auth/tokens").status_code == 400

    denied_client = _client_with_state(_token_router(bl, allow_auth=False))
    assert denied_client.get("/api/auth/tokens").status_code == 401
    assert denied_client.get("/api/auth/accounts").status_code == 403
    assert denied_client.get("/api/auth/accounts/current").status_code == 401

    client = _client_with_state(_token_router(bl))
    assert (
        client.post("/api/auth/tokens", json={"label": "bad", "expires_in_days": 0}).status_code
        == 400
    )
    assert client.post("/api/auth/tokens/999/revoke").status_code == 404

    assert (
        client.post(
            "/api/auth/accounts",
            json={"account_id": "acc-default", "display_name": "Duplicate", "is_active": True},
        ).status_code
        == 409
    )
    assert (
        client.post("/api/auth/accounts/missing/disable", json={"reason": "none"}).status_code
        == 404
    )
    assert client.patch("/api/auth/accounts/current", json={"display_name": ""}).status_code == 422

    assert (
        client.post("/api/auth/service-accounts", json={"service_account_key": ""}).status_code
        == 400
    )
    assert (
        client.post("/api/auth/service-accounts", json={"service_account_key": "bad"}).status_code
        == 400
    )
    assert (
        client.post("/api/auth/service-accounts", json={"service_account_key": "none"}).status_code
        == 500
    )

    assert client.get("/api/auth/service-accounts/%20/tokens").status_code == 400
    assert (
        client.post(
            "/api/auth/service-accounts/unknown/tokens", json={"label": "x", "expires_in_days": 5}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/auth/service-accounts/robot/tokens", json={"label": "x", "expires_in_days": 0}
        ).status_code
        == 400
    )
    assert client.post("/api/auth/service-accounts/robot/tokens/999/revoke").status_code == 404
    assert client.delete("/api/auth/service-accounts/robot/tokens/999").status_code == 404
