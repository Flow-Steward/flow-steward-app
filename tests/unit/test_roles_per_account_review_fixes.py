"""Independent review of roles per account (ADR 0085, decisions B to G).

B  A role change revokes only what the person holds in that account.
C  The users list shows only the memberships of accounts the administrator manages.
D  A personal access token or OAuth grant manages its own account only.
E  A removed membership or role ends that account's OAuth grants too; sign-in lands
   only where the person has a role.
F  A session of external sign-in does not change the person's profile and sees only
   its own account's tokens and grants.
G  No Core writer depends on a global role row.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.account_memberships import memberships_with_role
from core.application.auth.membership_roles import account_with_role, revoke_account_access
from core.application.auth.user_admin import account_members
from core.application.auth.user_admin.projection import limit_view_to_accounts
from core.entrypoints.api.auth_users import admin_managed_account_ids, create_auth_users_router

ROOT = Path(__file__).resolve().parents[2]
USER = "11111111-1111-1111-1111-111111111111"
ACCOUNT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ACCOUNT_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class _Cursor:
    def __init__(self, *, rows: list[Any] | None = None, many: list[Any] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.rowcount = 0
        self._rows = list(rows or [])
        self._many = list(many or [])
        self._last = ""

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._last = " ".join(str(sql).split())
        self.calls.append((self._last, params))

    def fetchone(self) -> Any:
        if "to_regclass('public.oauth_grants')" in self._last:
            return (True,)
        if "to_regclass('public.external_identities')" in self._last:
            return (True,)
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._many)


# --- B / E: what one account's change revokes ---------------------------------------------


def test_account_access_is_revoked_in_that_account_only() -> None:
    cur = _Cursor()

    revoke_account_access(cur, user_id=USER, account_id=ACCOUNT_A, reason="account_role_changed")

    updates = [(sql, params) for sql, params in cur.calls if sql.startswith("UPDATE")]
    assert [sql.split(" SET ")[0] for sql, _ in updates] == [
        "UPDATE auth_sessions",
        "UPDATE personal_access_tokens",
        "UPDATE oauth_grants",
    ]
    for sql, params in updates:
        assert "CAST(account_id AS TEXT) = %s" in sql
        assert ACCOUNT_A in params and USER in params
    assert updates[-1][1][0] == "account_role_changed"


def test_nothing_is_revoked_without_an_account() -> None:
    cur = _Cursor()
    assert revoke_account_access(cur, user_id=USER, account_id="", reason="x") == 0
    assert cur.calls == []


def _member_cursor() -> _Cursor:
    # _person, then the person's memberships (two), then the last-admin probes.
    return _Cursor(rows=[(USER, "pat@x.test", ACCOUNT_A)], many=[(ACCOUNT_A,), (ACCOUNT_B,)])


def test_removing_a_membership_ends_that_accounts_oauth_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(account_members, "require_other_admins", lambda *_a, **_k: None)
    cur = _member_cursor()

    account_members.remove_member(cur, user_id=USER, account_id=ACCOUNT_B, actor_user_id="admin")

    grants = [params for sql, params in cur.calls if sql.startswith("UPDATE oauth_grants")]
    assert grants == [("account_membership_removed", USER, ACCOUNT_B)]
    sessions = [params for sql, params in cur.calls if sql.startswith("UPDATE auth_sessions")]
    assert sessions == [(USER, ACCOUNT_B)]


def test_adding_an_administrator_revokes_external_sign_in_of_that_account_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revoked: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        account_members,
        "revoke_external_sign_in_grants",
        lambda _cur, ids, *, account_id=None: revoked.append((list(ids), account_id)),
    )
    cur = _Cursor(rows=[(USER, "pat@x.test", ACCOUNT_A), (USER,)], many=[(ACCOUNT_A,)])

    account_members.add_member(
        cur, user_id=USER, account_id=ACCOUNT_B, role="admin", managed_account_ids=[ACCOUNT_A]
    )

    assert revoked == [([USER], ACCOUNT_B)]


def test_a_directory_sync_removes_a_role_in_one_account(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.application.auth import external_sign_in_repository, membership_roles
    from core.application.auth.external_sign_in_ports import ExternalSignInPort

    cur = _Cursor(rows=[("membership",)])
    monkeypatch.setattr(external_sign_in_repository, "user_in_account", lambda *_a, **_k: True)
    monkeypatch.setattr(membership_roles, "require_other_admins", lambda *_a, **_k: None)

    @contextmanager
    def _connection():
        yield SimpleNamespace(cursor=lambda: cur)

    port = ExternalSignInPort(_connection)
    assert port.remove_role(account_id=ACCOUNT_B, user_id=USER) == []

    role_write = next(
        params for sql, params in cur.calls if sql.startswith("UPDATE user_account_memberships")
    )
    assert role_write == (None, USER, ACCOUNT_B)
    grants = [params for sql, params in cur.calls if sql.startswith("UPDATE oauth_grants")]
    assert grants == [("account_role_removed", USER, ACCOUNT_B)]
    # ADR 0085, C4: an administrator without the role there no longer invites to it.
    assert [
        params for sql, params in cur.calls if sql.startswith("UPDATE account_invitations")
    ] == [(USER,)]


def test_a_directory_sync_cannot_remove_the_last_administrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.auth import external_sign_in_repository, membership_roles
    from core.application.auth.external_sign_in_ports import ExternalSignInPort

    cur = _Cursor()
    monkeypatch.setattr(external_sign_in_repository, "user_in_account", lambda *_a, **_k: True)

    def _refuse(*_a: Any, **_k: Any) -> None:
        raise membership_roles.LastAccountAdminError(ACCOUNT_B)

    monkeypatch.setattr(membership_roles, "require_other_admins", _refuse)

    @contextmanager
    def _connection():
        yield SimpleNamespace(cursor=lambda: cur)

    with pytest.raises(ValueError):
        ExternalSignInPort(_connection).remove_role(account_id=ACCOUNT_B, user_id=USER)
    assert not any(sql.startswith("UPDATE") for sql, _ in cur.calls)


# --- E: sign-in lands where the person has a role ---------------------------------------


def test_the_sign_in_account_prefers_the_home_account_with_a_role() -> None:
    cur = _Cursor(rows=[(ACCOUNT_B,)])

    assert account_with_role(cur, USER, ACCOUNT_A) == ACCOUNT_B
    sql, params = cur.calls[0]
    assert "m.role_id IS NOT NULL" in sql
    assert "ORDER BY (CAST(m.account_id AS TEXT) = %s) DESC, m.is_default DESC" in sql
    assert params == (USER, ACCOUNT_A)
    assert account_with_role(_Cursor(), USER, ACCOUNT_A) == ""


def test_memberships_without_a_role_are_not_offered_at_sign_in() -> None:
    home = {"account_id": ACCOUNT_A, "role": ""}
    other = {"account_id": ACCOUNT_B, "role": "viewer"}

    assert memberships_with_role([home, other]) == ([other], False)
    assert memberships_with_role([home]) == ([], True)
    # Storage without roles per account says nothing about roles.
    assert memberships_with_role([{"account_id": ACCOUNT_A}]) == (
        [{"account_id": ACCOUNT_A}],
        False,
    )


# --- C: the users list --------------------------------------------------------------------


def test_a_listed_person_shows_only_the_managed_accounts() -> None:
    view = {
        "id": USER,
        "account_id": ACCOUNT_B,
        "account_display_name": "Secret Co",
        "account_ids": [ACCOUNT_B, ACCOUNT_A],
        "account_memberships": [
            {"account_id": ACCOUNT_B, "display_name": "Secret Co", "role": "admin"},
            {"account_id": ACCOUNT_A, "display_name": "Acme", "role": "viewer"},
        ],
    }

    limited = limit_view_to_accounts(view, [ACCOUNT_A])

    assert limited["account_ids"] == [ACCOUNT_A]
    assert limited["account_memberships"] == [
        {"account_id": ACCOUNT_A, "display_name": "Acme", "role": "viewer"}
    ]
    assert limited["account_id"] == ACCOUNT_A
    assert limited["account_display_name"] == "Acme"
    assert "Secret Co" not in repr(limited)


def test_the_roles_shown_are_the_visible_accounts_never_another_accounts() -> None:
    """C3: the view's roles were the home account's (B, admin); A's administrator sees
    the person's role in A."""
    view = {
        "id": USER,
        "account_id": ACCOUNT_B,
        "roles": ["admin"],
        "account_ids": [ACCOUNT_B, ACCOUNT_A],
        "account_memberships": [
            {"account_id": ACCOUNT_B, "display_name": "Secret Co", "role": "admin"},
            {"account_id": ACCOUNT_A, "display_name": "Acme", "role": "viewer"},
        ],
    }

    assert limit_view_to_accounts(view, [ACCOUNT_A])["roles"] == ["viewer"]
    # PUT names the account the role changed in: that account's role is returned.
    both = limit_view_to_accounts(view, [ACCOUNT_A, ACCOUNT_B], role_account_id=ACCOUNT_A)
    assert both["roles"] == ["viewer"]
    # A role account outside the managed ones falls back to a visible one.
    assert limit_view_to_accounts(view, [ACCOUNT_A], role_account_id=ACCOUNT_B)["roles"] == [
        "viewer"
    ]


def test_no_role_in_the_visible_account_shows_none() -> None:
    view = {
        "id": USER,
        "account_id": ACCOUNT_B,
        "roles": ["admin"],
        "account_memberships": [
            {"account_id": ACCOUNT_B, "role": "admin"},
            {"account_id": ACCOUNT_A, "role": ""},
        ],
    }

    assert limit_view_to_accounts(view, [ACCOUNT_A])["roles"] == []
    # Nothing visible at all: no role either.
    assert limit_view_to_accounts(view, [])["roles"] == []


# --- D: tokens manage their own account ----------------------------------------------------


@pytest.mark.parametrize("source", ["pat", "oauth", "external"])
def test_a_token_or_external_principal_manages_its_own_account_only(source: str) -> None:
    principal = {
        "auth_source": source,
        "account_id": ACCOUNT_A,
        "roles": ["admin"],
        "account_memberships": [
            {"account_id": ACCOUNT_A, "role": "admin"},
            {"account_id": ACCOUNT_B, "role": "admin"},
        ],
    }

    assert admin_managed_account_ids(principal) == [ACCOUNT_A]
    assert admin_managed_account_ids({**principal, "roles": ["viewer"]}) == []


def test_a_password_session_manages_every_account_it_administers() -> None:
    principal = {
        "auth_source": "local",
        "account_id": ACCOUNT_A,
        "roles": ["admin"],
        "account_memberships": [
            {"account_id": ACCOUNT_A, "role": "admin"},
            {"account_id": ACCOUNT_B, "role": "admin"},
        ],
    }
    assert admin_managed_account_ids(principal) == [ACCOUNT_A, ACCOUNT_B]


# --- F: external sessions -----------------------------------------------------------------


def _profile_client(source: str) -> TestClient:
    user = {"id": USER, "username": "pat@x.test", "auth_source": source, "account_id": ACCOUNT_A}
    router = create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: True,
        get_logger_only=lambda: SimpleNamespace(conn=None),
        auth_tables_available=lambda _bl: True,
        require_authenticated_user=lambda _request, **_kwargs: dict(user),
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
    )
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.auth_user = dict(user)
        request.state.auth_source = source
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_an_external_session_does_not_change_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.application.auth import user_profile_repository

    writes: list[str] = []
    monkeypatch.setattr(
        user_profile_repository,
        "update_user_display_name",
        lambda _bl, _user_id, name: writes.append(name),
    )

    refused = _profile_client("external").patch(
        "/api/users/me/profile", json={"display_name": "Mallory"}
    )
    allowed = _profile_client("local").patch("/api/users/me/profile", json={"display_name": "Pat"})

    assert refused.status_code == 403
    assert refused.json()["code"] == "external_session_profile_read_only"
    assert allowed.status_code == 200
    assert writes == ["Pat"]


def test_an_external_session_lists_and_revokes_only_its_accounts_tokens() -> None:
    from core.entrypoints.api.auth_personal_token_routes import pinned_account_id

    external = SimpleNamespace(state=SimpleNamespace(auth_source="external"))
    local = SimpleNamespace(state=SimpleNamespace(auth_source="local"))

    assert pinned_account_id(external, {"account_id": ACCOUNT_A}) == ACCOUNT_A  # type: ignore[arg-type]
    # Without an account it matches nothing rather than everything.
    assert pinned_account_id(external, {}) == "-"  # type: ignore[arg-type]
    assert pinned_account_id(local, {"account_id": ACCOUNT_A}) is None  # type: ignore[arg-type]


def test_the_token_storage_filters_by_account() -> None:
    from core.infrastructure.db.logger_extensions_accounts_pat_mixin import (
        PlatformLoggerPersonalAccessTokensMixin,
    )

    cur = _Cursor(rows=[(0,), {"id": "t1", "label": "x", "revoked": True}])

    class _Store(PlatformLoggerPersonalAccessTokensMixin):
        conn = SimpleNamespace(cursor=lambda **_kwargs: cur)

    store = _Store()
    store.count_user_pats(USER, account_id=ACCOUNT_A)
    store.revoke_pat("t1", USER, account_id=ACCOUNT_A)

    count_sql, count_params = cur.calls[0]
    revoke_sql, revoke_params = cur.calls[1]
    assert "account_id::text = %s" in count_sql and count_params == (USER, ACCOUNT_A)
    assert "account_id::text = %s" in revoke_sql and revoke_params == ("t1", USER, ACCOUNT_A)


def test_an_external_session_sees_only_its_accounts_oauth_grants() -> None:
    from core.application.auth import inbound_oauth_repository as repo
    from core.application.auth.inbound_oauth_service import InboundOAuthService

    assert InboundOAuthService._pinned_account(
        {"auth_source": "external", "account_id": ACCOUNT_A}
    ) == {"account_id": ACCOUNT_A}
    assert InboundOAuthService._pinned_account({"auth_source": "local"}) == {}

    cur = _Cursor()
    conn = SimpleNamespace(cursor=lambda: cur)
    repo.list_user_grants(conn, USER, account_id=ACCOUNT_A)
    repo.get_user_grant(conn, "g1", USER, account_id=ACCOUNT_A)
    assert "g.account_id::text = %s" in cur.calls[0][0]
    assert cur.calls[0][1] == (USER, ACCOUNT_A)
    assert cur.calls[1][1] == ("g1", USER, ACCOUNT_A)


# --- G: no Core writer depends on a global non-system role row -----------------------------


def test_core_writes_user_roles_only_with_a_scope_and_never_stores_system_actor() -> None:
    writers = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"INSERT INTO (?:public\.)?user_roles\b", text):
            writers.append(str(path.relative_to(ROOT)))
        # system_actor is a principal type built in code, never a stored role.
        assert not re.search(r"INSERT INTO (?:public\.)?roles[^;]*system_actor", text, re.S), path
    # The scoped roles port is the one writer; its rows name a workspace, project or
    # workflow (the check constraint refuses anything else).
    assert writers == ["core/application/auth/scoped_role_ports.py"]


# --- a session row from a cursor that returns tuples ---------------------------------------


def test_a_session_row_is_read_by_column_names_from_a_tuple_cursor() -> None:
    """A connection that ignores the dict cursor request returns tuples; the reader
    names them from the cursor's columns instead of failing on ``dict(row)``."""
    from core.application.auth.resolve import _row_mapping

    cur = SimpleNamespace(description=[("id",), ("account_id",)])
    assert _row_mapping(cur, (USER, ACCOUNT_A)) == {"id": USER, "account_id": ACCOUNT_A}
    assert _row_mapping(cur, {"id": USER}) == {"id": USER}
