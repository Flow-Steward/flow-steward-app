"""B3: a temporary password can only replace itself, never sign in on its own.

An admin creates a user with "Require password change at next sign-in". The
user must be able to set a permanent password at the sign-in page without the
email reset flow, and the temporary password must never open a session.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.password import password_hash, verify_password
from core.entrypoints.api.auth_sessions import create_auth_sessions_router

_EMAIL = "new.user@example.com"
_TEMPORARY = "temporary-pass-1"
_PERMANENT = "permanent-pass-22"
_MIN_LENGTH = 12


class _Store:
    def __init__(self, *, must_change_password: bool = True) -> None:
        self.user: dict[str, Any] = {
            "id": 41,
            "username": _EMAIL,
            "email": _EMAIL,
            "account_id": "acc_default",
            "display_name": "New User",
            "password_hash": password_hash(_TEMPORARY),
            "must_change_password": must_change_password,
            "is_active": True,
            "roles": ["member"],
        }
        self.sessions_inserted = 0
        self.sessions_revoked_for: list[str] = []
        self.failures: list[tuple[str, str]] = []
        self.audit: list[dict[str, Any]] = []


class _Cursor:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self.rowcount = 0

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(str(query).split())
        self.rowcount = 0
        if normalized.startswith("UPDATE users SET password_hash"):
            new_hash, user_id = params
            assert str(user_id) == str(self._store.user["id"])
            self._store.user["password_hash"] = new_hash
            if "must_change_password = false" in normalized:
                self._store.user["must_change_password"] = False
            self.rowcount = 1
        elif normalized.startswith("UPDATE auth_sessions SET revoked = true"):
            self._store.sessions_revoked_for.append(str(params[0]))
            self.rowcount = 2
        elif normalized.startswith("INSERT INTO auth_sessions"):
            self._store.sessions_inserted += 1
            self.rowcount = 1

    def fetchall(self) -> list[Any]:
        return []

    def fetchone(self) -> Any:
        return None


def _client(store: _Store) -> TestClient:
    bl = SimpleNamespace(
        conn=SimpleNamespace(cursor=lambda **_kwargs: _Cursor(store)),
        _column_exists=lambda _table, _column: False,
        _table_exists=lambda _table: False,
    )

    def _record_failure(username: str, client_ip: str) -> tuple[bool, int]:
        store.failures.append((username, client_ip))
        return (len(store.failures) >= 3, 60)

    router = create_auth_sessions_router(
        get_logger_only=lambda: bl,
        session_token_hash=lambda token: f"hash:{token}",
        auth_cookie_secure=lambda: False,
        log_auth_audit_event=lambda _bl, **kwargs: store.audit.append(kwargs),
        local_auth_enabled=lambda: True,
        bootstrap_request_allowed=lambda _request: (True, ""),
        auth_tables_available=lambda _bl: True,
        resolve_actor_username=lambda _request, _requested, default="auth": default,
        get_user_by_username=lambda _bl, username: dict(store.user) if username == _EMAIL else None,
        request_client_ip=lambda _request: "198.51.100.7",
        auth_login_lock_state=lambda _username, _client_ip: (False, 0),
        auth_login_record_failure=_record_failure,
        verify_password=verify_password,
        password_hash=password_hash,
        auth_user_column_exists=lambda _bl, column: column == "must_change_password",
        auth_login_clear_failures=lambda _username, _client_ip: None,
        csrf_request_valid=lambda _request: True,
        api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        err_database_not_configured="db-missing",
        err_local_auth_disabled="local-disabled",
        err_auth_tables_not_available="auth-missing",
        err_too_many_login_attempts="Too many login attempts",
        auth_password_min_length=_MIN_LENGTH,
        auth_session_ttl_hours=24,
        auth_remember_session_ttl_hours=168,
        auth_cookie_name="fs_session",
        auth_csrf_cookie_name="fs_csrf",
        first_admin_service_factory=lambda _bl: None,
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _login(client: TestClient, password: str, **extra: Any):
    return client.post("/api/auth/login", json={"email": _EMAIL, "password": password, **extra})


def _has_session_cookie(response: Any) -> bool:
    return any(
        header.startswith("fs_session=") for header in response.headers.get_list("set-cookie")
    )


def test_wrong_temporary_password_is_refused_and_counted_toward_throttling() -> None:
    store = _Store()
    client = _client(store)

    first = _login(client, "not-the-temporary", new_password=_PERMANENT)
    assert first.status_code == 401
    assert store.failures == [(_EMAIL, "198.51.100.7")]

    _login(client, "not-the-temporary")
    throttled = _login(client, "still-wrong", new_password=_PERMANENT)
    assert throttled.status_code == 429
    assert store.user["must_change_password"] is True
    assert verify_password(_TEMPORARY, store.user["password_hash"])
    assert store.sessions_inserted == 0


def test_temporary_password_alone_never_signs_in() -> None:
    store = _Store()
    client = _client(store)

    response = _login(client, _TEMPORARY)

    assert response.status_code == 403
    body = response.json()
    assert body["password_change_required"] is True
    assert body["password_min_length"] == _MIN_LENGTH
    assert "password_reset_required" not in body
    assert not _has_session_cookie(response)
    assert store.sessions_inserted == 0
    assert store.failures == []
    assert store.audit[-1]["detail"]["reason"] == "password_change_required"


@pytest.mark.parametrize(
    ("new_password", "message"),
    [
        ("short-1", "at least 12 characters"),
        (_TEMPORARY, "must differ from the temporary password"),
    ],
)
def test_unusable_new_password_is_refused_without_a_session(
    new_password: str, message: str
) -> None:
    store = _Store()
    client = _client(store)

    response = _login(client, _TEMPORARY, new_password=new_password)

    assert response.status_code == 400
    assert message in response.json()["error"]
    assert response.json()["field"] == "new_password"
    assert not _has_session_cookie(response)
    assert store.user["must_change_password"] is True
    assert verify_password(_TEMPORARY, store.user["password_hash"])


def test_new_password_replaces_the_temporary_one_and_signs_in() -> None:
    store = _Store()
    client = _client(store)

    response = _login(client, _TEMPORARY, new_password=_PERMANENT)

    assert response.status_code == 200, response.text
    assert _has_session_cookie(response)
    assert store.sessions_inserted == 1
    assert store.user["must_change_password"] is False
    assert verify_password(_PERMANENT, store.user["password_hash"])
    assert store.sessions_revoked_for == [str(store.user["id"])]
    actions = [(event["action"], event["status"]) for event in store.audit]
    assert ("password_change", "ok") in actions
    assert actions[-1] == ("login", "ok")

    old = _login(client, _TEMPORARY)
    assert old.status_code == 401
    assert not _has_session_cookie(old)

    again = _login(client, _PERMANENT)
    assert again.status_code == 200
    assert store.sessions_inserted == 2


def test_new_password_is_not_a_second_way_to_change_an_ordinary_password() -> None:
    store = _Store(must_change_password=False)
    client = _client(store)

    response = _login(client, _TEMPORARY, new_password=_PERMANENT)

    assert response.status_code == 400
    assert "only when a password change is required" in response.json()["error"]
    assert not _has_session_cookie(response)
    assert verify_password(_TEMPORARY, store.user["password_hash"])
