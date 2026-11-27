"""The people of the acting account: routes, service and helpers (ADR 0085)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.route_permissions import route_permission
from core.application.auth.user_admin import account_members
from core.application.auth.user_admin.account_members import (
    AccountMemberService,
    search_candidates,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceValidationError,
)
from core.entrypoints.api.auth_users import create_auth_users_router

ACCOUNT_A = "00000000-0000-0000-0000-00000000000a"
ACCOUNT_B = "00000000-0000-0000-0000-00000000000b"
PERSON = "00000000-0000-0000-0000-000000000077"
ADMIN = {
    "id": "00000000-0000-0000-0000-000000000001",
    "username": "admin@example.com",
    "roles": ["admin"],
    "account_id": ACCOUNT_A,
    "account_memberships": [
        {"account_id": ACCOUNT_A, "role": "admin"},
        {"account_id": ACCOUNT_B, "role": "viewer"},
    ],
}


class _RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error

    def candidates(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("candidates", kwargs)
        return [{"id": PERSON, "email": "lina@example.com", "display_name": "Lina"}]

    def add(self, **kwargs: Any) -> dict[str, Any]:
        self._record("add", kwargs)
        return {"user_id": kwargs["user_id"], "account_id": kwargs["account_id"], "role": "viewer"}

    def remove(self, **kwargs: Any) -> dict[str, Any]:
        self._record("remove", kwargs)
        return {"user_id": kwargs["user_id"], "account_id": kwargs["account_id"]}


def _client(service: _RecordingService, admin: dict[str, Any] | None = None) -> TestClient:
    admin_user = dict(admin or ADMIN)
    router = create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: False,
        get_logger_only=lambda: SimpleNamespace(conn=None),
        auth_tables_available=lambda _bl: True,
        require_authenticated_user=lambda _request, **_kwargs: dict(admin_user),
        request_auth_user_payload=lambda request: getattr(request.state, "auth_user", None),
        api_error_response=lambda exc: JSONResponse({"api_error": str(exc)}, status_code=500),
        log_auth_audit_event=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures=lambda _username, *, bl=None: None,
        get_user_by_id=lambda _bl, _user_id: None,
        get_user_by_username=lambda _bl, _username: None,
        normalize_requested_roles=lambda roles: list(roles or []),
        seed_default_roles=lambda _bl: None,
        password_hash=lambda value: value,
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
        account_member_service=service,  # type: ignore[arg-type]
    )
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.auth_user = dict(admin_user)
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/auth/users/candidates"),
        ("POST", f"/api/auth/users/{PERSON}/membership"),
        ("DELETE", f"/api/auth/users/{PERSON}/membership"),
    ],
)
def test_the_routes_need_user_management_as_an_administrator(method: str, path: str) -> None:
    match = route_permission(method, path)
    assert match is not None
    assert match.permission == "users.manage"
    assert match.rule.admin_only is True


def test_candidates_are_searched_in_the_acting_account_with_the_managed_accounts() -> None:
    service = _RecordingService()

    response = _client(service).get("/api/auth/users/candidates", params={"q": "lin"})

    assert response.status_code == 200
    assert response.json() == {
        "people": [{"id": PERSON, "email": "lina@example.com", "display_name": "Lina"}]
    }
    # Only accounts where the administrator's role is admin are managed (B is viewer).
    assert service.calls == [
        (
            "candidates",
            {
                "query": "lin",
                "account_id": ACCOUNT_A,
                "managed_account_ids": [ACCOUNT_A],
                "admin_user_id": ADMIN["id"],
            },
        )
    ]


def test_a_person_is_added_to_the_acting_account_with_the_chosen_role() -> None:
    service = _RecordingService()

    response = _client(service).post(
        f"/api/auth/users/{PERSON}/membership", json={"role": "operator"}
    )

    assert response.status_code == 200
    name, kwargs = service.calls[0]
    assert name == "add"
    assert kwargs["user_id"] == PERSON
    assert kwargs["role"] == "operator"
    assert "email" not in kwargs
    assert kwargs["account_id"] == ACCOUNT_A
    assert kwargs["managed_account_ids"] == [ACCOUNT_A]


@pytest.mark.parametrize("extra", [{"account_id": ACCOUNT_B}, {"email": "lina@example.com"}])
def test_the_add_request_refuses_unknown_fields(extra: dict[str, str]) -> None:
    """An exact email no longer reaches people outside the managed accounts (ADR 0085 A)."""
    response = _client(_RecordingService()).post(
        f"/api/auth/users/{PERSON}/membership", json={"role": "viewer", **extra}
    )
    assert response.status_code == 422


def test_a_person_is_removed_from_the_acting_account_only() -> None:
    service = _RecordingService()

    response = _client(service).delete(f"/api/auth/users/{PERSON}/membership")

    assert response.status_code == 200
    assert service.calls[0][0] == "remove"
    assert service.calls[0][1]["account_id"] == ACCOUNT_A
    assert service.calls[0][1]["actor_user_id"] == ADMIN["id"]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ServiceConflictError("The last active administrator of this company"), 409),
        (ServiceForbiddenError("You cannot remove yourself from this company"), 403),
    ],
)
def test_a_refused_removal_says_why(error: Exception, status: int) -> None:
    service = _RecordingService()
    service.error = error

    response = _client(service).delete(f"/api/auth/users/{PERSON}/membership")

    assert response.status_code == status
    assert response.json() == {"error": str(error)}


def test_the_users_list_is_the_acting_accounts_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/auth/users lists the members of the acting account with their role there."""
    from core.application.auth.user_admin import query_service

    seen: dict[str, Any] = {}

    def _list_users(self, **kwargs: Any):
        seen.update(kwargs)
        return SimpleNamespace(users=[], total=0)

    monkeypatch.setattr(query_service.UserAdminQueryService, "list_users", _list_users)

    response = _client(_RecordingService()).get("/api/auth/users")

    assert response.status_code == 200
    assert seen["member_account_id"] == ACCOUNT_A
    assert seen["role_account_id"] == ACCOUNT_A


def test_an_administrator_acting_where_they_are_not_admin_lists_nobody() -> None:
    acting_in_b = {**ADMIN, "account_id": ACCOUNT_B, "roles": ["viewer"]}

    response = _client(_RecordingService(), acting_in_b).get("/api/auth/users")

    assert response.status_code == 403


# --- the service ---------------------------------------------------------------------


class _Cursor:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._rows = rows or []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self) -> list[Any]:
        return list(self._rows)


def test_a_short_search_lists_nobody() -> None:
    cur = _Cursor()
    assert search_candidates(cur, query="a", account_id=ACCOUNT_A, managed_account_ids=[]) == []
    assert cur.executed == []


def test_a_search_escapes_wildcards_and_limits_the_answer() -> None:
    cur = _Cursor(rows=[(PERSON, "lina@example.com", "Lina")])

    people = search_candidates(
        cur, query="li%_na", account_id=ACCOUNT_A, managed_account_ids=[ACCOUNT_A]
    )

    assert people == [{"id": PERSON, "email": "lina@example.com", "display_name": "Lina"}]
    sql, params = cur.executed[0]
    # ADR 0085 (A): no exact-email match among everyone; managed accounts only.
    assert "lower(u.username) = lower(%s)" not in sql
    assert "u.is_active" in sql
    assert params[0] == ACCOUNT_A
    assert params[1] == [ACCOUNT_A]
    assert params[3] == "%li\\%\\_na%"
    assert params[-1] == account_members.CANDIDATE_LIMIT


def test_a_search_without_managed_accounts_lists_nobody() -> None:
    cur = _Cursor(rows=[(PERSON, "lina@example.com", "Lina")])
    assert (
        search_candidates(
            cur, query="lina@example.com", account_id=ACCOUNT_A, managed_account_ids=[]
        )
        == []
    )
    assert cur.executed == []


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self, **_kwargs: Any) -> _Cursor:
        return self._cursor


def _service(monkeypatch: pytest.MonkeyPatch) -> AccountMemberService:
    @contextmanager
    def _transaction(conn):
        yield conn

    monkeypatch.setattr(account_members, "transaction_connection", _transaction)
    return AccountMemberService(
        get_logger=lambda: SimpleNamespace(conn=_Conn(_Cursor())),
        audit=lambda *_args, **_kwargs: None,
    )


def test_the_service_refuses_an_account_the_administrator_does_not_manage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(monkeypatch)
    with pytest.raises(ServiceForbiddenError):
        service.candidates(query="lina", account_id=ACCOUNT_B, managed_account_ids=[ACCOUNT_A])
    with pytest.raises(ServiceForbiddenError):
        service.add(
            user_id=PERSON,
            role="viewer",
            account_id=ACCOUNT_B,
            managed_account_ids=[ACCOUNT_A],
            actor="admin",
        )
    with pytest.raises(ServiceForbiddenError):
        service.remove(
            user_id=PERSON,
            account_id=ACCOUNT_B,
            managed_account_ids=[ACCOUNT_A],
            actor_user_id=ADMIN["id"],
            actor="admin",
        )


def test_the_service_refuses_a_role_that_is_not_a_system_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ServiceValidationError):
        _service(monkeypatch).add(
            user_id=PERSON,
            role="owner",
            account_id=ACCOUNT_A,
            managed_account_ids=[ACCOUNT_A],
            actor="admin",
        )


def test_nobody_removes_themselves() -> None:
    cur = _Cursor()
    with pytest.raises(ServiceForbiddenError):
        account_members.remove_member(
            cur, user_id=ADMIN["id"], account_id=ACCOUNT_A, actor_user_id=ADMIN["id"]
        )
    assert cur.executed == []
