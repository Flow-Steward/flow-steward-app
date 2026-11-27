"""Invitations to join an account: membership only with the person's consent (ADR 0085).

An administrator adds directly only people of the accounts they manage. Anyone else is
invited by email: the invitation carries a single-use, unguessable link token (stored
hashed), sent by system email or shown once to the administrator. Whoever opens the
link while signed in as a person answers it; the typed email address is not the
authority. Answers never say whether a person exists; invitations are rate-limited in
the shared store per administrator, per account and per recipient.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.membership_roles import (
    INVITER_IS_ADMIN_PREDICATE,
    cancel_invitations_of_former_admins,
)
from core.application.auth.route_permissions import route_permission
from core.application.auth.user_admin import account_invitations, account_members
from core.application.auth.user_admin.account_invitations import (
    INVITATION_CHANGED_MESSAGE,
    INVITATION_INVALID_MESSAGE,
    INVITATION_SENT_MESSAGE,
    AccountInvitationService,
    cancel,
    decide,
    delete_expired,
    invitation_token_hash,
    invite,
    invited_address,
    new_invitation_token,
    preview,
    sent_invitations,
)
from core.application.auth.user_admin.member_lookup_throttle import (
    CANDIDATE_LOOKUP_ACCOUNT_MAX,
    CANDIDATE_LOOKUP_MAX,
    INVITATION_ACCOUNT_MAX,
    INVITATION_MAX,
    INVITATION_RECIPIENT_MAX,
    LookupThrottledError,
    ThrottleLimit,
    candidate_limits,
    consume,
    invitation_limits,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.entrypoints.api.auth_users import create_auth_users_router

ACCOUNT_A = "00000000-0000-0000-0000-00000000000a"
ACCOUNT_B = "00000000-0000-0000-0000-00000000000b"
PERSON = "00000000-0000-0000-0000-000000000077"
INVITATION = "00000000-0000-0000-0000-0000000000f1"
TOKEN = "t" * 43
ADMIN = {
    "id": "00000000-0000-0000-0000-000000000001",
    "username": "admin@example.com",
    "roles": ["admin"],
    "auth_source": "local",
    "account_id": ACCOUNT_A,
    "account_memberships": [{"account_id": ACCOUNT_A, "role": "admin"}],
}
OPEN_ROW = (INVITATION, ACCOUNT_B, "operator", "Northwind", "Bob", "2026-12-11")


class _Cursor:
    """Records SQL and answers ``fetchone``/``fetchall`` from queues."""

    def __init__(self, rows: list[Any] | None = None, many: list[Any] | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._rows = list(rows or [])
        self._many = list(many or [])

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self) -> Any:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._many.pop(0) if self._many else []


# --- the token and the address -----------------------------------------------------------


def test_a_link_token_is_unguessable_and_only_its_hash_is_stored() -> None:
    tokens = {new_invitation_token() for _ in range(50)}

    assert len(tokens) == 50
    assert all(len(token) >= 43 for token in tokens)
    digest = invitation_token_hash(TOKEN)
    assert len(digest) == 64 and TOKEN not in digest
    assert invitation_token_hash(f" {TOKEN} ") == digest


@pytest.mark.parametrize(
    "raw",
    [
        "lina",
        "",
        "lina@exam" + chr(0x212A) + "ple.test",  # a Kelvin sign folds to "k"
        "lina" + chr(0xFF20) + "x.test",  # a full-width "@"
        "li" + chr(0xED) + "na@x.test",
        "a" * 330 + "@x.test",
    ],
)
def test_an_address_that_could_look_like_another_is_refused(raw: str) -> None:
    with pytest.raises(ServiceValidationError):
        invited_address(raw)


def test_an_address_is_kept_as_typed_and_compared_normalized() -> None:
    assert invited_address(" Lina@X.test ") == ("Lina@X.test", "lina@x.test")


# --- storing an invitation ------------------------------------------------------------------


def test_an_invitation_stores_the_token_hash_and_renews_the_token() -> None:
    """A repeated invitation (a new role, say) gets a new token: the old link dies (P1)."""
    cur = _Cursor(rows=[(INVITATION, "2026-12-11")])

    stored = invite(
        cur,
        account_id=ACCOUNT_A,
        email="Lina@X.test",
        role="operator",
        invited_by="a",
        token_hash="h" * 64,
    )

    assert stored == {"id": INVITATION, "expires_at": "2026-12-11"}
    sql, params = cur.executed[0]
    assert sql.startswith("INSERT INTO account_invitations")
    assert "ON CONFLICT (account_id, lower(invited_email)) WHERE status = 'pending'" in sql
    assert "token_hash = EXCLUDED.token_hash" in sql
    assert "role = EXCLUDED.role" in sql
    # The address is stored as typed; nothing about people is read.
    assert params[:5] == (ACCOUNT_A, "Lina@X.test", "operator", "a", "h" * 64)
    assert not any("FROM users" in text for text, _ in cur.executed)
    assert not any("user_account_memberships (" in text for text, _ in cur.executed)


def test_expired_invitations_are_removed_and_reported() -> None:
    cur = _Cursor(many=[[(INVITATION, "old@x.test", "viewer")]])

    assert delete_expired(cur, ACCOUNT_A) == [
        {"id": INVITATION, "invited_email": "old@x.test", "role": "viewer"}
    ]
    sql, params = cur.executed[0]
    assert sql.startswith("DELETE FROM account_invitations")
    assert "expires_at <= now()" in sql and "RETURNING" in sql
    assert params == (ACCOUNT_A,)


def test_the_administrators_list_never_carries_a_token() -> None:
    cur = _Cursor(many=[[(INVITATION, "nobody@x.test", "viewer", "Bob", "c", "e")]])

    [row] = sent_invitations(cur, ACCOUNT_A)

    assert row == {
        "id": INVITATION,
        "email": "nobody@x.test",
        "role": "viewer",
        "invited_by": "Bob",
        "created_at": "c",
        "expires_at": "e",
    }
    assert "token" not in cur.executed[0][0]


def test_cancelling_closes_an_open_invitation_of_the_account_only() -> None:
    cur = _Cursor(rows=[(INVITATION, "lina@x.test", "viewer")])

    assert cancel(cur, account_id=ACCOUNT_A, invitation_id=INVITATION) == {
        "id": INVITATION,
        "invited_email": "lina@x.test",
        "role": "viewer",
    }
    sql, params = cur.executed[0]
    assert "SET status = 'cancelled'" in sql and "status = 'pending'" in sql
    assert params == (INVITATION, ACCOUNT_A)
    with pytest.raises(ServiceNotFoundError):
        cancel(_Cursor(rows=[None]), account_id=ACCOUNT_B, invitation_id=INVITATION)


# --- opening and answering a link ----------------------------------------------------------


def test_a_link_is_open_only_while_pending_unexpired_and_its_inviter_is_admin() -> None:
    cur = _Cursor(rows=[OPEN_ROW])

    assert preview(cur, "h" * 64) == {
        "account_name": "Northwind",
        "role": "operator",
        "invited_by": "Bob",
        "expires_at": "2026-12-11",
    }
    sql, params = cur.executed[0]
    assert "i.token_hash = %s" in sql
    assert "i.status = 'pending'" in sql and "i.expires_at > now()" in sql
    assert "a.is_active" in sql
    # C4: the inviter must still be an active admin of that account.
    assert " ".join(INVITER_IS_ADMIN_PREDICATE.split()) in sql
    # The typed address is not the authority: nothing compares it with anyone.
    assert "invited_email" not in sql
    assert params == ("h" * 64,)
    with pytest.raises(ServiceNotFoundError) as refused:
        preview(_Cursor(rows=[None]), "h" * 64)
    assert refused.value.message == INVITATION_INVALID_MESSAGE


def test_whoever_holds_the_link_accepts_it_with_the_role_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added: list[dict[str, Any]] = []
    monkeypatch.setattr(
        account_invitations, "add_member", lambda _cur, **kwargs: added.append(kwargs)
    )
    cur = _Cursor(rows=[OPEN_ROW])

    result = decide(cur, token_hash="h" * 64, user_id=PERSON, accept=True, expected_role="operator")

    assert result["status"] == "accepted" and result["role"] == "operator"
    assert result["account_id"] == ACCOUNT_B and result["account_name"] == "Northwind"
    assert added == [
        {
            "user_id": PERSON,
            "account_id": ACCOUNT_B,
            "role": "operator",
            "managed_account_ids": None,
        }
    ]
    assert "FOR UPDATE OF i" in cur.executed[0][0]
    # Single use: the invitation is closed and names who answered.
    assert cur.executed[-1][1] == ("accepted", PERSON, INVITATION)


def test_accepting_a_role_that_changed_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        account_invitations, "add_member", lambda *_a, **_k: pytest.fail("no membership")
    )
    cur = _Cursor(rows=[OPEN_ROW])

    with pytest.raises(ServiceConflictError) as refused:
        decide(cur, token_hash="h" * 64, user_id=PERSON, accept=True, expected_role="viewer")

    assert refused.value.message == INVITATION_CHANGED_MESSAGE
    assert not any(sql.startswith("UPDATE") for sql, _ in cur.executed)


def test_declining_creates_no_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        account_invitations, "add_member", lambda *_a, **_k: pytest.fail("no membership")
    )
    cur = _Cursor(rows=[OPEN_ROW])

    assert decide(cur, token_hash="h" * 64, user_id=PERSON, accept=False)["status"] == "declined"
    assert cur.executed[-1][1] == ("declined", PERSON, INVITATION)


def test_a_used_unknown_or_orphaned_link_is_no_longer_valid() -> None:
    with pytest.raises(ServiceNotFoundError) as refused:
        decide(_Cursor(rows=[None]), token_hash="h" * 64, user_id=PERSON, accept=True)
    assert refused.value.message == INVITATION_INVALID_MESSAGE


def test_an_inviter_who_is_no_admin_any_more_has_their_invitations_cancelled() -> None:
    cur = _Cursor(many=[[(INVITATION, ACCOUNT_A, "lina@x.test", "viewer")]])

    assert cancel_invitations_of_former_admins(cur, ADMIN["id"]) == [
        {
            "id": INVITATION,
            "account_id": ACCOUNT_A,
            "invited_email": "lina@x.test",
            "role": "viewer",
        }
    ]
    sql, params = cur.executed[0]
    assert "SET status = 'cancelled'" in sql and "i.status = 'pending'" in sql
    assert "AND NOT EXISTS" in sql and "ir.key = 'admin'" in sql
    assert params == (ADMIN["id"],)
    assert cancel_invitations_of_former_admins(_Cursor(), "") == []


def test_a_direct_add_refuses_a_person_outside_the_managed_accounts() -> None:
    cur = _Cursor(rows=[(PERSON, "lina@x.test", ACCOUNT_B)], many=[[(ACCOUNT_B,)]])
    with pytest.raises(ServiceNotFoundError):
        account_members.add_member(
            cur,
            user_id=PERSON,
            account_id=ACCOUNT_A,
            role="viewer",
            managed_account_ids=[ACCOUNT_A],
        )
    assert not any(sql.startswith("INSERT") for sql, _ in cur.executed)


# --- the shared throttle -----------------------------------------------------------------


class _LimitCursor(_Cursor):
    """An ``auth_rate_limits`` table in memory, behind the SQL the throttle runs."""

    def __init__(self, table: dict[str, list[float]]) -> None:
        super().__init__()
        self.table = table
        self._pending: Any = None

    def execute(self, sql: str, params: Any = None) -> None:
        super().execute(sql, params)
        if "FROM public.auth_rate_limits" in sql:
            stored = self.table.get(params[0])
            self._pending = None if stored is None else {"attempts_jsonb": list(stored)}
        elif "INSERT INTO public.auth_rate_limits" in sql:
            self.table[params[0]] = json.loads(params[1])

    def fetchone(self) -> Any:
        return self._pending


def test_invitations_are_limited_per_admin_account_and_recipient_in_the_shared_store() -> None:
    table: dict[str, list[float]] = {}
    limits = invitation_limits(admin_user_id="a1", account_id=ACCOUNT_A, recipient="Lina@X.test")

    assert [limit.max_attempts for limit in limits] == [
        INVITATION_MAX,
        INVITATION_ACCOUNT_MAX,
        INVITATION_RECIPIENT_MAX,
    ]
    assert limits[2].key.endswith(":recipient:lina@x.test")
    for _ in range(INVITATION_RECIPIENT_MAX):
        consume(_LimitCursor(table), limits, now=1000.0)
    # The recipient is full: another administrator of another account is refused too.
    other = invitation_limits(admin_user_id="a2", account_id=ACCOUNT_B, recipient="lina@x.test")
    with pytest.raises(LookupThrottledError) as refused:
        consume(_LimitCursor(table), other, now=1001.0)
    assert refused.value.status_code == 429 and refused.value.retry_after > 0
    # Nothing was counted for the refused attempt.
    assert not any(key.endswith(":admin:a2") for key in table)


def test_candidate_searches_are_limited_per_admin_and_account() -> None:
    table: dict[str, list[float]] = {}
    limits = candidate_limits(admin_user_id="a1", account_id=ACCOUNT_A)

    assert [limit.max_attempts for limit in limits] == [
        CANDIDATE_LOOKUP_MAX,
        CANDIDATE_LOOKUP_ACCOUNT_MAX,
    ]
    for _ in range(CANDIDATE_LOOKUP_MAX):
        consume(_LimitCursor(table), limits, now=50.0)
    with pytest.raises(LookupThrottledError):
        consume(_LimitCursor(table), limits, now=51.0)
    # Another administrator of the account still has their own count.
    consume(_LimitCursor(table), candidate_limits(admin_user_id="a2", account_id=ACCOUNT_A))


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self, **_kwargs: Any) -> _Cursor:
        return self._cursor


def test_candidate_searches_count_in_the_shared_store(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def _transaction(conn):
        yield conn

    counted: list[list[ThrottleLimit]] = []
    monkeypatch.setattr(account_members, "transaction_connection", _transaction)
    service = account_members.AccountMemberService(
        get_logger=lambda: SimpleNamespace(conn=_Conn(_Cursor())),
        audit=lambda *_a, **_k: None,
        throttle=lambda _cur, limits: counted.append(limits),
    )

    service.candidates(
        query="lina", account_id=ACCOUNT_A, managed_account_ids=[ACCOUNT_A], admin_user_id="a"
    )

    assert counted == [candidate_limits(admin_user_id="a", account_id=ACCOUNT_A)]


# --- the service ----------------------------------------------------------------------------


def _service(
    monkeypatch: pytest.MonkeyPatch,
    cursor: _Cursor,
    audits: list[dict[str, Any]],
    *,
    sent: list[dict[str, Any]] | None = None,
    delivered: bool = True,
    throttled: list[list[ThrottleLimit]] | None = None,
) -> AccountInvitationService:
    @contextmanager
    def _transaction(conn):
        yield conn

    def _send(**kwargs: Any) -> dict[str, Any]:
        if sent is not None:
            sent.append(kwargs)
        return (
            {"ok": True, "reason": "sent"}
            if delivered
            else {"ok": False, "reason": "disabled_or_unconfigured"}
        )

    counted = throttled if throttled is not None else []
    monkeypatch.setattr(account_invitations, "transaction_connection", _transaction)
    return AccountInvitationService(
        get_logger=lambda: SimpleNamespace(conn=_Conn(cursor)),
        audit=lambda *_args, **kwargs: audits.append(kwargs),
        send_invitation=_send,
        throttle=lambda _cur, limits: counted.append(limits),
        new_token=lambda: TOKEN,
    )


def _invite_cursor() -> _Cursor:
    # expired (none) -> stored row -> names
    return _Cursor(rows=[(INVITATION, "2026-12-11"), ("Acme", "Bob Admin")], many=[[]])


@pytest.mark.parametrize("email", ["nobody@x.test", "lina@x.test"], ids=["unknown", "exists"])
def test_the_link_is_emailed_and_the_answer_is_the_same_for_any_address(
    monkeypatch: pytest.MonkeyPatch, email: str
) -> None:
    audits: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []
    throttled: list[list[ThrottleLimit]] = []
    cursor = _invite_cursor()
    service = _service(monkeypatch, cursor, audits, sent=sent, throttled=throttled)

    answer = service.invite(
        email=email,
        role="viewer",
        account_id=ACCOUNT_A,
        managed_account_ids=[ACCOUNT_A],
        admin_user_id=ADMIN["id"],
        actor="admin",
        link_base="https://fs.example/",
    )

    assert answer == {"status": "ok", "message": INVITATION_SENT_MESSAGE, "delivery": "email"}
    # The token goes only into the email; the database holds its hash.
    assert sent == [
        {
            "recipient": email,
            "inviter": "Bob Admin",
            "company": "Acme",
            "role": "viewer",
            "invitation_url": f"https://fs.example/invitations/{TOKEN}",
            "expires_in_days": account_invitations.INVITATION_LIFETIME_DAYS,
            "locale": "",
        }
    ]
    stored_params = next(params for sql, params in cursor.executed if sql.startswith("INSERT"))
    assert invitation_token_hash(TOKEN) in stored_params and TOKEN not in stored_params
    # The same statements run for any address.
    assert [sql.split()[0] for sql, _ in cursor.executed] == ["DELETE", "INSERT", "SELECT"]
    assert throttled == [
        invitation_limits(admin_user_id=ADMIN["id"], account_id=ACCOUNT_A, recipient=email)
    ]
    # P2: the audit names the address as typed, the role, the inviter and the expiry.
    detail = audits[-1]["detail"]
    assert audits[-1]["action"] == "account_member_invite"
    assert detail["invited_email"] == email and detail["role"] == "viewer"
    assert detail["invited_by"] == ADMIN["id"] and detail["expires_at"] == "2026-12-11"
    assert detail["delivery"] == "email"
    assert TOKEN not in str(audits)


def test_without_system_email_the_link_is_returned_once(monkeypatch: pytest.MonkeyPatch) -> None:
    audits: list[dict[str, Any]] = []
    service = _service(monkeypatch, _invite_cursor(), audits, delivered=False)

    answer = service.invite(
        email="Lina@X.test",
        role="operator",
        account_id=ACCOUNT_A,
        managed_account_ids=[ACCOUNT_A],
        admin_user_id=ADMIN["id"],
        actor="admin",
        link_base="https://fs.example",
    )

    assert answer == {
        "status": "ok",
        "message": INVITATION_SENT_MESSAGE,
        "delivery": "link",
        "invitation_link": f"https://fs.example/invitations/{TOKEN}",
    }
    assert audits[-1]["detail"]["delivery"] == "link"
    assert audits[-1]["detail"]["invited_email"] == "Lina@X.test"
    assert TOKEN not in str(audits)


def test_expired_invitations_removed_by_a_new_one_are_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, Any]] = []
    cursor = _Cursor(
        rows=[(INVITATION, "2026-12-11"), ("Acme", "Bob")],
        many=[[("old-id", "old@x.test", "viewer")]],
    )
    service = _service(monkeypatch, cursor, audits)

    service.invite(
        email="lina@x.test",
        role="viewer",
        account_id=ACCOUNT_A,
        managed_account_ids=[ACCOUNT_A],
        admin_user_id=ADMIN["id"],
        actor="admin",
    )

    assert audits[0]["action"] == "account_invitation_expire"
    assert audits[0]["detail"] == {
        "account_id": ACCOUNT_A,
        "invitations": [{"id": "old-id", "invited_email": "old@x.test", "role": "viewer"}],
    }


def test_the_service_lists_and_cancels_only_in_a_managed_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, Any]] = []
    cursor = _Cursor(rows=[(INVITATION, "lina@x.test", "viewer")])
    service = _service(monkeypatch, cursor, audits)

    with pytest.raises(ServiceForbiddenError):
        service.sent(account_id=ACCOUNT_B, managed_account_ids=[ACCOUNT_A])
    with pytest.raises(ServiceForbiddenError):
        service.cancel(
            invitation_id=INVITATION,
            account_id=ACCOUNT_B,
            managed_account_ids=[ACCOUNT_A],
            actor="admin",
        )
    assert cursor.executed == []

    assert service.cancel(
        invitation_id=INVITATION,
        account_id=ACCOUNT_A,
        managed_account_ids=[ACCOUNT_A],
        actor="admin",
    ) == {"id": INVITATION, "status": "cancelled"}
    assert audits[-1]["action"] == "account_invitation_cancel"
    assert audits[-1]["detail"] == {
        "account_id": ACCOUNT_A,
        "invitation_id": INVITATION,
        "invited_email": "lina@x.test",
        "role": "viewer",
        "reason": "cancelled_by_administrator",
    }


def test_the_service_refuses_an_unmanaged_account_a_bad_role_or_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(monkeypatch, _Cursor(), [])
    common = {
        "managed_account_ids": [ACCOUNT_A],
        "admin_user_id": ADMIN["id"],
        "actor": "admin",
    }
    with pytest.raises(ServiceForbiddenError):
        service.invite(email="a@x.test", role="viewer", account_id=ACCOUNT_B, **common)
    with pytest.raises(ServiceValidationError):
        service.invite(email="a@x.test", role="owner", account_id=ACCOUNT_A, **common)
    with pytest.raises(ServiceValidationError):
        service.invite(email="lina", role="viewer", account_id=ACCOUNT_A, **common)


def test_the_answer_records_who_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(account_invitations, "add_member", lambda *_a, **_k: None)
    cursor = _Cursor(rows=[OPEN_ROW])
    service = _service(monkeypatch, cursor, audits)

    result = service.decide(
        token=TOKEN, user_id=PERSON, username="lina@x.test", accept=True, role="operator"
    )

    assert result["status"] == "accepted"
    assert cursor.executed[0][1] == (invitation_token_hash(TOKEN),)
    assert audits[-1]["action"] == "account_invitation_accept"
    assert audits[-1]["detail"]["user_id"] == PERSON
    with pytest.raises(ServiceNotFoundError):
        service.decide(token="", user_id=PERSON, username="", accept=True, role="operator")


# --- routes -----------------------------------------------------------------------------


class _RecordingInvitations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None

    def invite(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("invite", kwargs))
        if self.error is not None:
            raise self.error
        return {"status": "ok", "message": INVITATION_SENT_MESSAGE, "delivery": "email"}

    def sent(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("sent", kwargs))
        return [{"id": INVITATION, "email": "Nobody@X.test", "role": "viewer"}]

    def cancel(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("cancel", kwargs))
        if self.error is not None:
            raise self.error
        return {"id": INVITATION, "status": "cancelled"}

    def preview(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("preview", kwargs))
        if self.error is not None:
            raise self.error
        return {"account_name": "Northwind", "role": "viewer", "invited_by": "Bob"}

    def decide(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("decide", kwargs))
        if self.error is not None:
            raise self.error
        return {"id": INVITATION, "status": "accepted", "account_id": ACCOUNT_B, "role": "viewer"}


def _client(
    service: _RecordingInvitations,
    user: dict[str, Any] | None = None,
    *,
    require: Any = None,
) -> TestClient:
    current = dict(user or ADMIN)
    router = create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: False,
        get_logger_only=lambda: SimpleNamespace(conn=None),
        auth_tables_available=lambda _bl: True,
        require_authenticated_user=require or (lambda _request, **_kwargs: dict(current)),
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
        account_invitation_service=service,  # type: ignore[arg-type]
    )
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.auth_user = dict(current)
        request.state.auth_source = current.get("auth_source")
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_the_routes_have_their_permissions() -> None:
    for method, path in (
        ("POST", "/api/auth/users/invitations"),
        ("GET", "/api/auth/users/invitations"),
        ("POST", f"/api/auth/users/invitations/{INVITATION}/cancel"),
    ):
        rule = route_permission(method, path)
        assert rule is not None and rule.permission == "users.manage", path
    # Any signed-in person opens a link, also one with a role nowhere (P5).
    for action in ("preview", "accept", "decline"):
        rule = route_permission("POST", f"/api/auth/invitations/{action}")
        assert rule is not None and rule.permission is None, action
    # The listing by email address is gone: no own-account rule maps it any more.
    listing = route_permission("GET", "/api/auth/invitations")
    assert listing is not None and listing.permission == "platform.read"


def test_an_invitation_goes_to_the_acting_account_with_a_link_base() -> None:
    service = _RecordingInvitations()

    response = _client(service).post(
        "/api/auth/users/invitations", json={"email": "lina@x.test", "role": "operator"}
    )

    assert response.status_code == 200
    assert response.json()["message"] == INVITATION_SENT_MESSAGE
    name, kwargs = service.calls[0]
    assert name == "invite"
    assert kwargs["account_id"] == ACCOUNT_A
    assert kwargs["managed_account_ids"] == [ACCOUNT_A]
    assert kwargs["admin_user_id"] == ADMIN["id"]
    assert kwargs["link_base"].startswith("http")


def test_administrators_list_and_cancel_the_acting_accounts_invitations() -> None:
    service = _RecordingInvitations()
    client = _client(service)

    listed = client.get("/api/auth/users/invitations")
    cancelled = client.post(f"/api/auth/users/invitations/{INVITATION}/cancel")

    assert listed.status_code == 200
    assert listed.json()["invitations"][0]["email"] == "Nobody@X.test"
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


def test_a_throttled_administrator_is_told_when_to_retry() -> None:
    service = _RecordingInvitations()
    service.error = LookupThrottledError(42)

    response = _client(service).post(
        "/api/auth/users/invitations", json={"email": "lina@x.test", "role": "viewer"}
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"


@pytest.mark.parametrize("roles", [["viewer"], []], ids=["with-role", "no-role-anywhere"])
def test_the_signed_in_person_opens_and_accepts_the_link(roles: list[str]) -> None:
    service = _RecordingInvitations()
    person = {**ADMIN, "id": PERSON, "username": "someone@else.test", "roles": roles}
    client = _client(service, person)

    shown = client.post("/api/auth/invitations/preview", json={"token": TOKEN})
    accepted = client.post("/api/auth/invitations/accept", json={"token": TOKEN, "role": "viewer"})
    declined = client.post("/api/auth/invitations/decline", json={"token": TOKEN})

    assert shown.status_code == 200 and shown.json()["account_name"] == "Northwind"
    assert accepted.status_code == 200 and declined.status_code == 200
    assert service.calls[0] == ("preview", {"token": TOKEN})
    # The token is the authority, not the person's address.
    assert service.calls[1][1] == {
        "token": TOKEN,
        "user_id": PERSON,
        "username": "someone@else.test",
        "accept": True,
        "role": "viewer",
    }
    assert service.calls[2][1]["accept"] is False


def test_an_external_sign_in_session_answers_a_link() -> None:
    service = _RecordingInvitations()
    client = _client(service, {**ADMIN, "id": PERSON, "auth_source": "external"})

    assert client.post("/api/auth/invitations/preview", json={"token": TOKEN}).status_code == 200


@pytest.mark.parametrize("source", ["pat", "oauth"], ids=["token", "oauth"])
def test_a_token_never_answers_a_link(source: str) -> None:
    service = _RecordingInvitations()
    client = _client(service, {**ADMIN, "auth_source": source})

    shown = client.post("/api/auth/invitations/preview", json={"token": TOKEN})
    accepted = client.post("/api/auth/invitations/accept", json={"token": TOKEN, "role": "viewer"})

    assert shown.status_code == 403 and accepted.status_code == 403
    assert service.calls == []


def test_an_invalid_or_changed_link_is_answered_plainly() -> None:
    service = _RecordingInvitations()
    client = _client(service, {**ADMIN, "id": PERSON})

    service.error = ServiceNotFoundError(INVITATION_INVALID_MESSAGE)
    gone = client.post("/api/auth/invitations/preview", json={"token": TOKEN})
    service.error = ServiceConflictError(INVITATION_CHANGED_MESSAGE)
    changed = client.post("/api/auth/invitations/accept", json={"token": TOKEN, "role": "admin"})

    assert gone.status_code == 404 and gone.json() == {"error": INVITATION_INVALID_MESSAGE}
    assert changed.status_code == 409


def test_accepting_needs_the_role_that_was_shown() -> None:
    service = _RecordingInvitations()
    client = _client(service, {**ADMIN, "id": PERSON})

    assert client.post("/api/auth/invitations/accept", json={"token": TOKEN}).status_code == 422
    assert service.calls == []


# --- C2/C3: uniform answers and per-account roles on the user routes ------------------------


class _UserView:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return dict(self._payload)


class _Users:
    """``create_user`` refuses a taken address as the real service does."""

    def __init__(self, *, taken: bool) -> None:
        self.taken = taken
        self.view = {
            "id": PERSON,
            "account_id": ACCOUNT_B,
            "roles": ["admin"],
            "account_ids": [ACCOUNT_A, ACCOUNT_B],
            "account_memberships": [
                {"account_id": ACCOUNT_A, "role": "operator"},
                {"account_id": ACCOUNT_B, "role": "admin"},
            ],
        }

    def create_user(self, _command: Any, **_kwargs: Any) -> Any:
        if self.taken:
            raise ServiceConflictError("Email already exists")
        return SimpleNamespace(user=_UserView(self.view), warnings=[])

    def update_user(self, _command: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(user=_UserView(self.view))


def _user_client(service: _RecordingInvitations, users: _Users) -> TestClient:
    router = create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: False,
        get_logger_only=lambda: SimpleNamespace(conn=None),
        auth_tables_available=lambda _bl: True,
        require_authenticated_user=lambda _request, **_kwargs: dict(ADMIN),
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
        user_command_service=users,  # type: ignore[arg-type]
        account_invitation_service=service,  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


_NEW_USER = {
    "email": "taken@x.test",
    "password": "a-long-enough-password-1",  # pragma: allowlist secret - fixture
    "display_name": "Someone",
    "role": "operator",
}


def test_creating_a_user_with_a_taken_address_invites_them_instead() -> None:
    service = _RecordingInvitations()

    response = _user_client(service, _Users(taken=True)).post("/api/auth/users", json=_NEW_USER)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "message": INVITATION_SENT_MESSAGE,
        "delivery": "email",
    }
    name, kwargs = service.calls[0]
    assert name == "invite"
    assert kwargs["email"] == "taken@x.test" and kwargs["role"] == "operator"
    assert kwargs["account_id"] == ACCOUNT_A
    assert "user" not in response.json()


def test_a_new_users_view_carries_the_role_of_the_account_it_was_made_in() -> None:
    response = _user_client(_RecordingInvitations(), _Users(taken=False)).post(
        "/api/auth/users", json=_NEW_USER
    )

    assert response.status_code == 200
    user = response.json()["user"]
    # The home account (B) is not managed: neither it nor its admin role is shown.
    assert user["roles"] == ["operator"]
    assert [item["account_id"] for item in user["account_memberships"]] == [ACCOUNT_A]


def test_an_updated_view_returns_the_role_of_the_named_account_only() -> None:
    response = _user_client(_RecordingInvitations(), _Users(taken=False)).put(
        f"/api/auth/users/{PERSON}", json={"role": "operator", "role_account_id": ACCOUNT_A}
    )

    assert response.status_code == 200
    assert response.json()["user"]["roles"] == ["operator"]


def test_the_person_listing_by_email_address_is_gone() -> None:
    client = _client(_RecordingInvitations(), {**ADMIN, "id": PERSON})

    assert client.get("/api/auth/invitations").status_code in {404, 405}
    assert client.post(f"/api/auth/invitations/{INVITATION}/accept").status_code in {404, 405}


# --- C4: removing an administrator ends their invitations there -----------------------------


def test_removing_an_administrator_cancels_and_audits_their_invitations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = [
        {"id": INVITATION, "account_id": ACCOUNT_A, "invited_email": "x@y.test", "role": "viewer"}
    ]
    calls: list[str] = []

    def _remove(_cur: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["user_id"])
        return {"id": PERSON, "username": "bob@x.test", "cancelled_invitations": cancelled}

    @contextmanager
    def _transaction(conn):
        yield conn

    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(account_members, "remove_member", _remove)
    monkeypatch.setattr(account_members, "transaction_connection", _transaction)
    service = account_members.AccountMemberService(
        get_logger=lambda: SimpleNamespace(conn=_Conn(_Cursor())),
        audit=lambda *_a, **kwargs: audits.append(kwargs),
    )

    service.remove(
        user_id=PERSON,
        account_id=ACCOUNT_A,
        managed_account_ids=[ACCOUNT_A],
        actor_user_id=ADMIN["id"],
        actor="admin",
    )

    assert calls == [PERSON]
    assert [audit["action"] for audit in audits] == [
        "account_member_remove",
        "account_invitation_cancel",
    ]
    assert audits[-1]["detail"] == {
        "account_id": ACCOUNT_A,
        "invitation_id": INVITATION,
        "invited_email": "x@y.test",
        "role": "viewer",
        "reason": "inviter_no_longer_admin",
    }


def test_remove_member_cancels_the_invitations_of_the_removed_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        account_members,
        "cancel_invitations_of_former_admins",
        lambda _cur, user_id: seen.append(user_id) or [],
    )
    monkeypatch.setattr(account_members, "require_other_admins", lambda *_a, **_k: None)
    monkeypatch.setattr(account_members, "revoke_account_access", lambda *_a, **_k: 0)
    cur = _Cursor(rows=[(PERSON, "bob@x.test", ACCOUNT_B)], many=[[(ACCOUNT_A,), (ACCOUNT_B,)]])

    person = account_members.remove_member(
        cur, user_id=PERSON, account_id=ACCOUNT_A, actor_user_id=ADMIN["id"]
    )

    assert seen == [PERSON]
    assert person["cancelled_invitations"] == []


def test_a_sign_in_without_a_role_checks_the_link_by_its_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P5: the sign-in asks whether the link names an open invitation."""
    open_cursor = _Cursor(rows=[OPEN_ROW])

    assert _service(monkeypatch, open_cursor, []).token_is_open(token=TOKEN) is True
    assert open_cursor.executed[0][1] == (invitation_token_hash(TOKEN),)
    assert _service(monkeypatch, _Cursor(rows=[None]), []).token_is_open(token=TOKEN) is False
    untouched = _Cursor()
    assert _service(monkeypatch, untouched, []).token_is_open(token=" ") is False
    assert untouched.executed == []


def test_the_link_routes_are_public_writes_that_name_no_permission() -> None:
    """ADR 0076 §6: a write without a permission must be public; the route checks the
    session and the token itself."""
    from core.application.auth.enforce import is_public_auth_path

    for action in ("preview", "accept", "decline"):
        assert is_public_auth_path(f"/api/auth/invitations/{action}") is True
    assert is_public_auth_path("/api/auth/users/invitations") is False


def test_signing_in_is_required_to_answer_a_link() -> None:
    service = _RecordingInvitations()

    def _anonymous(_request: Any, **_kwargs: Any) -> dict[str, Any]:
        raise PermissionError("authentication required")

    client = _client(service, require=_anonymous)

    assert client.post("/api/auth/invitations/preview", json={"token": TOKEN}).status_code == 401
    assert service.calls == []
