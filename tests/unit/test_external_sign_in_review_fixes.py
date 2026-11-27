"""Review fixes of the installation-wide sign-in seam (ADR 0083 amendment of 2026-11-27).

- An administrator cannot set the password of a person who signs in only externally
  (an account takeover path: set a password, then sign in as them and link another
  sign-in).
- An invitation to the installation account is not accepted from an external session.
- What a resolver created is undone when Core refuses the sign-in afterwards, and
  its ``after_session`` runs only once the session exists.
- A method that asks for it gets the client's address at start and callback.
- Module invitation limit policies add to Core's invitation limits; a failing one
  refuses the invitation.
- Linking needs a password sign-in at most ten minutes old.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from core.application.auth import external_sign_in_repository as repository
from core.application.auth import invitation_policies
from core.application.auth.external_sign_in_ports import (
    BeginResult,
    ExternalIdentityResolution,
    ExternalSignInPort,
    SignInOption,
    VerifiedExternalIdentity,
)
from core.application.auth.external_sign_in_service import (
    ExternalSignInRefused,
    ExternalSignInService,
)
from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID
from core.application.auth.user_admin import account_invitations
from core.application.auth.user_admin.account_invitations import decide
from core.application.auth.user_admin.command_service import (
    EXTERNAL_ONLY_PASSWORD_MESSAGE,
    UserAdminCommandService,
)
from core.application.auth.user_admin.contracts import UserUpdateInput
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceUnavailableError,
)
from tests.unit.test_account_invitations import (
    ADMIN,
    INVITATION,
    PERSON,
    TOKEN,
    _client,
    _Cursor,
    _RecordingInvitations,
)
from tests.unit.test_external_sign_in_service import (  # noqa: F401 - fixtures
    ACCOUNT_ID,
    CALLBACK,
    USER_ID,
    FakeStore,
    _connection,
    _encryption_key,
    store,
)

# --- HIGH-1 (b): no password for a person who signs in only externally -------------------


def _users_service(password_hash: str) -> UserAdminCommandService:
    user = {
        "id": PERSON,
        "username": "pat@example.test",
        "password_hash": password_hash,
        "is_active": True,
        "account_id": INSTALLATION_ACCOUNT_ID,
        "account_ids": [INSTALLATION_ACCOUNT_ID],
    }
    return UserAdminCommandService(
        get_logger=lambda: object(),
        auth_tables_available_fn=lambda _bl: True,
        get_user_by_id_fn=lambda _bl, _id: dict(user),
    )


def test_an_administrator_cannot_set_the_password_of_an_external_only_person() -> None:
    service = _users_service(repository.UNUSABLE_PASSWORD_MARKER)
    with pytest.raises(ServiceForbiddenError) as refused:
        service.update_user(
            UserUpdateInput(user_id=PERSON, password="A-long-enough-password-1"),
            allowed_account_ids=[INSTALLATION_ACCOUNT_ID],
        )
    assert refused.value.message == EXTERNAL_ONLY_PASSWORD_MESSAGE


# --- LOW-8: the installation account is joined with a password ----------------------------


def _open_row(account: str) -> tuple:
    return (INVITATION, account, "operator", "Ops", "Bob", "2026-12-11")


def test_an_external_session_cannot_join_the_installation_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        account_invitations, "add_member", lambda *_a, **_k: pytest.fail("no membership")
    )
    cur = _Cursor(rows=[_open_row(str(INSTALLATION_ACCOUNT_ID))])
    with pytest.raises(ServiceForbiddenError):
        decide(
            cur,
            token_hash="h" * 64,
            user_id=PERSON,
            accept=True,
            expected_role="operator",
            external_session=True,
        )
    assert not any(sql.startswith("UPDATE") for sql, _ in cur.executed)


def test_an_external_session_joins_an_ordinary_company_with_the_invited_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH-2: the person was no member before; accepting adds the invited role."""
    added: list[dict[str, Any]] = []
    monkeypatch.setattr(
        account_invitations, "add_member", lambda _cur, **kwargs: added.append(kwargs)
    )
    other = "00000000-0000-0000-0000-00000000000b"
    cur = _Cursor(rows=[_open_row(other)])
    decide(
        cur,
        token_hash="h" * 64,
        user_id=PERSON,
        accept=True,
        expected_role="operator",
        external_session=True,
    )
    assert added == [
        {"user_id": PERSON, "account_id": other, "role": "operator", "managed_account_ids": None}
    ]


def test_the_accept_route_says_whether_the_session_is_external() -> None:
    service = _RecordingInvitations()
    client = _client(service, {**ADMIN, "id": PERSON, "auth_source": "external"})
    client.post("/api/auth/invitations/accept", json={"token": TOKEN, "role": "viewer"})
    assert service.calls[-1][1]["external_session"] is True
    local = _RecordingInvitations()
    _client(local, {**ADMIN, "id": PERSON}).post(
        "/api/auth/invitations/accept", json={"token": TOKEN, "role": "viewer"}
    )
    assert local.calls[-1][1]["external_session"] is False


# --- MEDIUM-3: undo and after_session -----------------------------------------------------


class AddressMethod:
    kind = "wide"
    installation_wide = True
    wants_client_address = True

    def __init__(self) -> None:
        self.addresses: list[tuple[str, str]] = []

    def options(self):
        return [SignInOption(key="one", display_name="One")]

    def option(self, key):
        return self.options()[0] if key == "one" else None

    def begin(self, key, *, callback_url, state, client_address=""):
        self.addresses.append(("begin", client_address))
        return BeginResult(redirect_url=f"https://wide.example.test/?state={state}")

    def complete(self, key, *, params, headers, data, callback_url, client_address=""):
        self.addresses.append(("complete", client_address))
        return VerifiedExternalIdentity(
            method="wide", option_key=key, account_id="", issuer="https://w", subject="s"
        )


class Resolver:
    def __init__(self) -> None:
        self.events: list[str] = []

    def resolve(self, identity):
        return ExternalIdentityResolution(
            user_id=USER_ID,
            account_id=ACCOUNT_ID,
            undo=lambda: self.events.append("undo"),
            after_session=lambda: self.events.append("after_session"),
        )


def _wide(guard: Any = None) -> tuple[ExternalSignInService, AddressMethod, Resolver]:
    port = ExternalSignInPort(_connection)
    method = AddressMethod()
    resolver = Resolver()
    port.register_sign_in_method(method)
    port.set_identity_resolver(resolver)
    service = ExternalSignInService(
        connection=_connection,
        port=port,
        entitled=lambda *_: True,
        audit=lambda **_: None,
        session_ttl_hours=1,
        session_guard=guard,
    )
    return service, method, resolver


def _run(service: ExternalSignInService) -> Any:
    started = service.start(
        method="wide", key="one", next_path="/", callback_url=CALLBACK, client_address="192.0.2.4"
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    return service.callback(
        params={"state": state},
        browser_binding=started.browser_binding,
        client_address="192.0.2.5",
    )


@pytest.fixture
def wide_store(store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> FakeStore:  # noqa: F811
    monkeypatch.setattr(repository, "user_holds_installation_admin_role", lambda *_: False)
    return store


def test_after_session_runs_once_the_session_exists(wide_store: FakeStore) -> None:
    service, method, resolver = _wide()
    assert _run(service).session_token
    assert resolver.events == ["after_session"]
    assert method.addresses == [("begin", "192.0.2.4"), ("complete", "192.0.2.5")]


def test_a_refusal_after_resolving_calls_undo_and_never_after_session(
    wide_store: FakeStore,
) -> None:
    service, _method, resolver = _wide(guard=lambda *_: "session_guard_refused")
    with pytest.raises(ExternalSignInRefused):
        _run(service)
    assert resolver.events == ["undo"]


def test_a_method_that_does_not_ask_gets_no_address(wide_store: FakeStore) -> None:
    class Plain(AddressMethod):
        wants_client_address = False

        def begin(self, key, *, callback_url, state):
            return BeginResult(redirect_url=f"https://wide.example.test/?state={state}")

        def complete(self, key, *, params, headers, data, callback_url):
            return VerifiedExternalIdentity(
                method="wide", option_key=key, account_id="", issuer="https://w", subject="s"
            )

    port = ExternalSignInPort(_connection)
    port.register_sign_in_method(Plain())
    port.set_identity_resolver(Resolver())
    service = ExternalSignInService(
        connection=_connection,
        port=port,
        entitled=lambda *_: True,
        audit=lambda **_: None,
        session_ttl_hours=1,
    )
    assert _run(service).session_token


class _DiscardCursor:
    def __init__(self, *, eligible: bool, account_free: bool) -> None:
        self.executed: list[str] = []
        self._eligible = eligible
        self._account_free = account_free
        self._answer: Any = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=()):
        text = " ".join(sql.split())
        self.executed.append(text)
        if "FROM users u" in text:
            self._answer = (1,) if self._eligible else None
        elif "FROM accounts a" in text:
            self._answer = (1,) if self._account_free else None
        else:
            self._answer = None

    def fetchone(self):
        return self._answer


def _discard_port(cursor: _DiscardCursor) -> ExternalSignInPort:
    from contextlib import contextmanager

    class Conn:
        def cursor(self, **_kwargs):
            return cursor

    @contextmanager
    def connection():
        yield Conn()

    return ExternalSignInPort(connection)


def test_discarding_removes_only_a_fresh_person_without_a_password() -> None:
    cursor = _DiscardCursor(eligible=True, account_free=True)
    account = str(uuid.uuid4())
    assert _discard_port(cursor).discard_new_person(
        user_id=str(uuid.uuid4()), account_id=account, reason="refused"
    )
    joined = "\n".join(cursor.executed)
    assert "password_hash = %s" in joined and "NOT EXISTS (SELECT 1 FROM auth_sessions" in joined
    assert "DELETE FROM users" in joined and "DELETE FROM accounts" in joined
    assert "platform_action_log" in joined

    kept = _DiscardCursor(eligible=False, account_free=True)
    assert not _discard_port(kept).discard_new_person(
        user_id=str(uuid.uuid4()), account_id=account, reason="refused"
    )
    assert not any(sql.startswith("DELETE") for sql in kept.executed)


# --- MEDIUM-5: module invitation limits ---------------------------------------------------


def test_a_module_invitation_limit_is_counted_with_cores_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = invitation_policies.loading_checkpoint()
    counted: list[Any] = []
    try:
        invitation_policies.register_invitation_limits(
            lambda account: [(f"cap:{account}", 5, 86_400)]
        )
        service = account_invitations.AccountInvitationService(
            get_logger=lambda: type("Bl", (), {"conn": None})(),
            audit=lambda *_a, **_k: None,
            send_invitation=lambda **_k: {"ok": True},
            throttle=lambda _cur, limits: counted.extend(limits),
            new_token=lambda: TOKEN,
        )
        monkeypatch.setattr(account_invitations, "transaction_connection", _fake_transaction)
        monkeypatch.setattr(account_invitations, "delete_expired", lambda *_a: [])
        monkeypatch.setattr(
            account_invitations,
            "invite",
            lambda *_a, **_k: {"id": INVITATION, "expires_at": "x"},
        )
        monkeypatch.setattr(
            account_invitations, "invitation_names", lambda *_a, **_k: ("Co", "Bob")
        )
        service.invite(
            email="new@example.test",
            role="viewer",
            account_id=ACCOUNT_ID,
            managed_account_ids=[ACCOUNT_ID],
            admin_user_id=PERSON,
            actor="admin",
        )
        keys = [limit.key for limit in counted]
        assert f"module:cap:{ACCOUNT_ID}" in keys and len(keys) == 4

        def broken(_account):
            raise RuntimeError("down")

        invitation_policies.register_invitation_limits(broken)
        with pytest.raises(ServiceUnavailableError):
            service.invite(
                email="new@example.test",
                role="viewer",
                account_id=ACCOUNT_ID,
                managed_account_ids=[ACCOUNT_ID],
                admin_user_id=PERSON,
                actor="admin",
            )
    finally:
        invitation_policies.restore_loading_checkpoint(checkpoint)
    assert invitation_policies.extra_invitation_limits(ACCOUNT_ID) == []


def _fake_transaction(_conn):
    from contextlib import contextmanager

    @contextmanager
    def run():
        class Conn:
            def cursor(self, **_kwargs):
                return _Cursor()

        yield Conn()

    return run()


def test_the_invitation_policy_registry_is_rolled_back_with_a_failed_module() -> None:
    from core.infrastructure.platform_modules import registration_rollback

    assert (
        "core.application.auth.invitation_policies"
        in registration_rollback._CORE_GLOBAL_REGISTRY_MODULES
    )


# --- LOW-6: linking needs a fresh password sign-in ----------------------------------------


def test_a_session_is_recent_only_within_the_window() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.params: Any = None

        def execute(self, sql, params):
            self.params = params

        def fetchone(self):
            return (True,)

    cursor = Cursor()
    assert repository.session_is_recent(cursor, "hash", 600)
    assert cursor.params == (600, "hash")


@pytest.mark.parametrize("recent", [True, False])
def test_link_start_needs_a_recent_password_session(
    monkeypatch: pytest.MonkeyPatch, recent: bool
) -> None:
    from contextlib import contextmanager

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from core.entrypoints.api import external_sign_in_routes as routes
    from tests.unit.test_external_sign_in_routes import _FakeService

    _FakeService.calls = []
    _FakeService.refuse = ""
    monkeypatch.setattr(routes, "ExternalSignInService", _FakeService)
    monkeypatch.setattr(repository, "session_is_recent", lambda *_a: recent)

    @contextmanager
    def connection(_bl):
        class Conn:
            def cursor(self, **_kwargs):
                return _Cursor()

        yield Conn()

    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request: Request, call_next):
        request.state.auth_user = {"id": PERSON, "username": "pat@example.test"}
        request.state.auth_source = "local"
        return await call_next(request)

    app.include_router(
        routes.create_external_sign_in_router(
            get_logger_only=lambda: object(),
            connection_for=connection,
            external_sign_in_port=lambda: object(),
            entitlement_service=lambda: None,
            session_issue_guard=lambda **_kwargs: None,
            request_scope=lambda request: None,
            service_error_response=lambda exc: None,
            api_error_response=lambda exc: None,
            log_auth_audit_event=lambda _bl, **_kwargs: None,
            public_base_url=lambda: "https://flow.acme.test",
            auth_cookie_secure=lambda: True,
            auth_cookie_name="fs_session",
            auth_csrf_cookie_name="fs_csrf",
            auth_session_ttl_hours=12,
        )
    )
    client = TestClient(app, base_url="https://flow.acme.test", follow_redirects=False)
    client.cookies.set("fs_session", "token")
    response = client.get("/api/auth/external/start?method=wide&key=one&intent=link")
    assert response.status_code == 303
    if recent:
        [(_name, kwargs)] = _FakeService.calls
        assert kwargs["link_user_id"] == PERSON and kwargs["client_address"]
    else:
        assert _FakeService.calls == []
        assert "password+again" in response.headers["location"]


def test_the_inviting_administrator_does_not_manage_a_person_before_acceptance() -> None:
    """HIGH-1: an invitee of an installation-wide sign-in is only a member of the
    installation account (no role) until they accept, so the inviting company's
    administrator manages none of their accounts and cannot change them."""
    from core.application.auth.user_admin.policies import require_user_access

    invitee = {
        "id": PERSON,
        "account_id": INSTALLATION_ACCOUNT_ID,
        "account_ids": [INSTALLATION_ACCOUNT_ID],
    }
    with pytest.raises(ServiceForbiddenError):
        require_user_access(invitee, ["00000000-0000-0000-0000-00000000000b"])
