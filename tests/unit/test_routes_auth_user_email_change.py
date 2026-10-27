from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import core.entrypoints.api.auth_users as auth_users_routes
from core.entrypoints.api.auth_users import create_auth_users_router


class _EmailChangeCursor:
    def __init__(self) -> None:
        self.rowcount = 0
        self.user_email = "ada@example.com"
        self.pending_requests: list[dict[str, Any]] = []
        self._next_fetchone: Any = None
        self._next_fetchall: list[Any] = []

    def __enter__(self) -> _EmailChangeCursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(str(query).split())
        self.rowcount = 0
        self._next_fetchone = None
        self._next_fetchall = []

        if "SELECT timezone FROM users WHERE id =" in normalized:
            self._next_fetchone = ("Europe/Vilnius",)
            return

        if "SELECT new_email, expires_at, created_at FROM auth_email_change_requests" in normalized:
            user_id = int((params or [0])[0] or 0)
            pending = next(
                (
                    row
                    for row in reversed(self.pending_requests)
                    if int(row.get("user_id") or 0) == user_id and row.get("used_at") is None
                ),
                None,
            )
            if pending:
                self._next_fetchone = {
                    "new_email": pending.get("new_email"),
                    "expires_at": pending.get("expires_at"),
                    "created_at": pending.get("created_at"),
                }
            return

        if (
            "UPDATE auth_email_change_requests SET used_at = now(), invalidated_reason = 'superseded'"
            in normalized
        ):
            user_id = int((params or [0])[0] or 0)
            for row in self.pending_requests:
                if int(row.get("user_id") or 0) == user_id and row.get("used_at") is None:
                    row["used_at"] = "superseded"
                    row["invalidated_reason"] = "superseded"
                    self.rowcount += 1
            return

        if "INSERT INTO auth_email_change_requests" in normalized and "RETURNING id" in normalized:
            values = list(params or [])
            next_id = len(self.pending_requests) + 1
            record = {
                "id": next_id,
                "user_id": int(values[0]),
                "current_email": str(values[1]),
                "new_email": str(values[2]),
                "token_hash": str(values[3]),
                "expires_at": "2026-04-05T14:00:00+00:00",
                "created_at": "2026-04-05T13:00:00+00:00",
                "requested_ip": str(values[5]),
                "used_at": None,
                "verified_at": None,
                "invalidated_reason": "",
            }
            self.pending_requests.append(record)
            self._next_fetchone = {"id": next_id}
            return

        if (
            "UPDATE auth_email_change_requests SET used_at = now(), invalidated_reason = 'delivery_failed'"
            in normalized
        ):
            token_hash = str((params or [""])[0] or "")
            for row in self.pending_requests:
                if str(row.get("token_hash") or "") == token_hash and row.get("used_at") is None:
                    row["used_at"] = "delivery_failed"
                    row["invalidated_reason"] = "delivery_failed"
                    self.rowcount = 1
                    break
            return

        if "FROM auth_email_change_requests e JOIN users u ON u.id = e.user_id" in normalized:
            token_hash = str((params or [""])[0] or "")
            pending = next(
                (
                    row
                    for row in self.pending_requests
                    if str(row.get("token_hash") or "") == token_hash and row.get("used_at") is None
                ),
                None,
            )
            if pending:
                self._next_fetchone = {
                    "id": pending.get("id"),
                    "user_id": pending.get("user_id"),
                    "current_email": pending.get("current_email"),
                    "new_email": pending.get("new_email"),
                    "expires_at": pending.get("expires_at"),
                    "username": self.user_email,
                    "is_active": True,
                }
            return

        if normalized == "UPDATE users SET username = %s WHERE id = %s":
            values = list(params or [])
            self.user_email = str(values[0] or "")
            self.rowcount = 1
            return

        if (
            "UPDATE auth_email_change_requests SET used_at = now(), verified_at = now(), invalidated_reason = 'verified'"
            in normalized
        ):
            request_id = int((params or [0])[0] or 0)
            for row in self.pending_requests:
                if int(row.get("id") or 0) == request_id and row.get("used_at") is None:
                    row["used_at"] = "verified"
                    row["verified_at"] = "2026-04-05T13:05:00+00:00"
                    row["invalidated_reason"] = "verified"
                    self.rowcount = 1
                    break
            return

        if (
            "UPDATE auth_email_change_requests SET used_at = now(), invalidated_reason = 'email_taken'"
            in normalized
        ):
            request_id = int((params or [0])[0] or 0)
            for row in self.pending_requests:
                if int(row.get("id") or 0) == request_id and row.get("used_at") is None:
                    row["used_at"] = "email_taken"
                    row["invalidated_reason"] = "email_taken"
                    self.rowcount = 1
                    break
            return

    def fetchone(self) -> Any:
        value = self._next_fetchone
        self._next_fetchone = None
        return value

    def fetchall(self) -> list[Any]:
        value = list(self._next_fetchall)
        self._next_fetchall = []
        return value


class _Conn:
    def __init__(self, cursor: _EmailChangeCursor) -> None:
        self._cursor = cursor
        self.commits = 0

    def cursor(self, **_kwargs: Any) -> _EmailChangeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1


def _api_error_response(exc: Exception, *_args: Any, **_kwargs: Any) -> JSONResponse:
    return JSONResponse({"api_error": str(exc)}, status_code=500)


def _client(
    cursor: _EmailChangeCursor,
    *,
    duplicate_email: str = "",
    roles: list[str] | None = None,
    auth_source: str = "local",
) -> tuple[TestClient, list[str]]:
    effective_roles = list(roles or ["admin"])
    bl = SimpleNamespace(
        conn=_Conn(cursor),
        _table_exists=lambda table: table in {"users", "auth_email_change_requests"},
    )
    cleared_usernames: list[str] = []

    def _clear_user_login_failures(username: str, *, bl: Any = None) -> None:
        _ = bl
        cleared_usernames.append(str(username or ""))

    def _get_user_by_username(_bl: Any, username: str) -> dict[str, Any] | None:
        normalized = str(username or "").strip().lower()
        if normalized == cursor.user_email.lower():
            return {
                "id": 1,
                "username": cursor.user_email,
                "email": cursor.user_email,
                "display_name": "Ada",
                "roles": effective_roles,
                "is_active": True,
            }
        if duplicate_email and normalized == duplicate_email.lower():
            return {
                "id": 99,
                "username": duplicate_email,
                "email": duplicate_email,
                "display_name": "Taken",
                "roles": ["viewer"],
                "is_active": True,
            }
        return None

    router = create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: False,
        get_logger_only=lambda: bl,
        auth_tables_available=lambda _bl: True,
        require_authenticated_user=lambda _request, **_kwargs: {
            "id": 1,
            "username": cursor.user_email,
            "email": cursor.user_email,
            "display_name": "Ada",
            "roles": effective_roles,
        },
        request_auth_user_payload=lambda request: getattr(request.state, "auth_user", None),
        api_error_response=_api_error_response,
        log_auth_audit_event=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures=_clear_user_login_failures,
        get_user_by_id=lambda _bl, user_id: (
            {
                "id": 1,
                "username": cursor.user_email,
                "email": cursor.user_email,
                "display_name": "Ada",
                "roles": effective_roles,
                "is_active": True,
            }
            if int(user_id) == 1
            else None
        ),
        get_user_by_username=_get_user_by_username,
        normalize_requested_roles=lambda roles: [
            str(value).strip() for value in (roles or []) if str(value).strip()
        ],
        seed_default_roles=lambda _bl: None,
        password_hash=lambda value: f"hash:{value}",
        auth_user_column_exists=lambda _bl, column: (
            column in {"must_change_password", "deleted_at"}
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

    app = FastAPI()

    @app.middleware("http")
    async def _inject_state(request: Request, call_next):
        request.state.auth_user = {
            "id": 1,
            "username": cursor.user_email,
            "email": cursor.user_email,
            "display_name": "Ada",
            "roles": effective_roles,
        }
        request.state.auth_source = auth_source
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False), cleared_usernames


def test_email_change_request_and_verify_flow(monkeypatch) -> None:
    cursor = _EmailChangeCursor()
    client, cleared_usernames = _client(cursor)
    sent_email: dict[str, Any] = {}

    monkeypatch.setattr(auth_users_routes.secrets, "token_urlsafe", lambda _length: "verify-token")
    monkeypatch.setattr(
        auth_users_routes,
        "public_base_url",
        lambda: "https://prod.example.com",
    )

    def _send_email_change_verification_email(**kwargs: Any) -> dict[str, Any]:
        sent_email.update(kwargs)
        return {"ok": True, "reason": "sent"}

    monkeypatch.setattr(
        auth_users_routes,
        "send_email_change_verification_email",
        _send_email_change_verification_email,
    )

    start = client.post(
        "/api/users/me/email-change/request",
        json={"email": "new-address@example.com"},
    )
    assert start.status_code == 200
    assert start.json()["status"] == "verification_sent"
    assert start.json()["email"] == "new-address@example.com"
    assert sent_email["recipient"] == "new-address@example.com"
    assert "verify-email-change?token=verify-token" in sent_email["verification_url"]
    assert (
        sent_email["verification_url"]
        == "https://prod.example.com/verify-email-change?token=verify-token"
    )

    status = client.get("/api/users/me/email-change-status")
    assert status.status_code == 200
    assert status.json()["pending"] is True
    assert status.json()["new_email"] == "new-address@example.com"

    verify = client.get("/verify-email-change?token=verify-token", follow_redirects=False)
    assert verify.status_code == 303
    assert verify.headers["location"] == "/settings/account?email_change=verified"
    assert cursor.user_email == "new-address@example.com"
    assert cleared_usernames == ["ada@example.com", "new-address@example.com"]

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "new-address@example.com"

    status_after = client.get("/api/users/me/email-change-status")
    assert status_after.status_code == 200
    assert status_after.json() == {"pending": False}


def test_viewer_can_request_own_email_change_and_get_value_specific_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        auth_users_routes,
        "send_email_change_verification_email",
        lambda **_kwargs: {"ok": True, "reason": "sent"},
    )
    cursor = _EmailChangeCursor()
    client, _cleared = _client(
        cursor,
        duplicate_email="taken@example.com",
        roles=["viewer"],
    )

    invalid = client.post("/api/users/me/email-change/request", json={"email": "not-an-email"})
    same = client.post("/api/users/me/email-change/request", json={"email": "ada@example.com"})
    duplicate = client.post(
        "/api/users/me/email-change/request", json={"email": "taken@example.com"}
    )
    accepted = client.post(
        "/api/users/me/email-change/request", json={"email": "viewer-new@example.com"}
    )

    assert invalid.status_code == 400
    assert "valid email" in invalid.json()["error"].lower()
    assert same.status_code == 400
    assert "different" in same.json()["error"].lower()
    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == "Email already exists"
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "verification_sent"


def test_external_sign_in_user_cannot_request_local_email_change() -> None:
    client, _cleared = _client(
        _EmailChangeCursor(),
        roles=["viewer"],
        auth_source="external",
    )

    response = client.post("/api/users/me/email-change/request", json={"email": "new@example.com"})

    assert response.status_code == 400
    assert response.json()["error"] == "Email changes are only available for local-auth accounts"


def test_email_change_verify_rejects_invalid_token(monkeypatch) -> None:
    cursor = _EmailChangeCursor()
    client, _cleared = _client(cursor)

    monkeypatch.setattr(
        auth_users_routes, "send_email_change_verification_email", lambda **_kwargs: {"ok": True}
    )

    verify = client.get("/verify-email-change?token=bad-token", follow_redirects=False)
    assert verify.status_code == 303
    assert verify.headers["location"] == "/settings/account?email_change=invalid"
