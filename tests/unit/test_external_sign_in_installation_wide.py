"""Installation-wide sign-in methods, invitation answers and own accounts (ADR 0083
amendment of 2026-11-27).

A method declared ``installation_wide`` is set up once for the installation: its
options belong to no account, its feature is the installation account's, and its
identity may sign in an administrator of an ordinary account but never of the
installation account. The harness is the in-memory one of
``test_external_sign_in_service``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from core.application.auth import external_sign_in_repository as repository
from core.application.auth.account_limit import AccountLimitError
from core.application.auth.external_sign_in_ports import (
    BeginResult,
    ExternalIdentityRefused,
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
from tests.unit.test_external_sign_in_service import (  # noqa: F401 - fixtures
    ACCOUNT_ID,
    CALLBACK,
    USER_ID,
    FakeStore,
    _connection,
    _encryption_key,
    store,
)

FEATURE = "wide_feature"


class WideMethod:
    """An installation-wide method; its option even claims an account, which Core ignores."""

    kind = "wide"
    installation_wide = True

    def __init__(self) -> None:
        self.subject = "wide-person"

    def options(self):
        return [SignInOption(key="one", display_name="One", account_id=ACCOUNT_ID, feature=FEATURE)]

    def option(self, key):
        return next((item for item in self.options() if item.key == key), None)

    def begin(self, key, *, callback_url, state):
        return BeginResult(redirect_url=f"https://wide.example.test/?state={state}")

    def complete(self, key, *, params, headers, data, callback_url):
        return VerifiedExternalIdentity(
            method="wide",
            option_key=key,
            account_id=ACCOUNT_ID,  # Core drops it
            issuer="https://wide.example.test",
            subject=self.subject,
            claims={"email": "pat@example.test"},
        )


class Recorder:
    def __init__(self, resolution: Any = None, refusal: Exception | None = None) -> None:
        self.seen: list[VerifiedExternalIdentity] = []
        self.resolution = resolution or ExternalIdentityResolution(
            user_id=USER_ID, account_id=ACCOUNT_ID
        )
        self.refusal = refusal

    def resolve(self, identity):
        self.seen.append(identity)
        if self.refusal is not None:
            raise self.refusal
        return self.resolution


def _service(entitled: set[tuple[str, str]], resolver: Recorder) -> ExternalSignInService:
    port = ExternalSignInPort(_connection)
    port.register_sign_in_method(WideMethod())
    port.set_identity_resolver(resolver)
    return ExternalSignInService(
        connection=_connection,
        port=port,
        entitled=lambda account, feature: (account, feature) in entitled,
        audit=lambda **_entry: None,
        session_ttl_hours=12,
    )


def _sign_in(service: ExternalSignInService, next_path: str = "/") -> Any:
    started = service.start(method="wide", key="one", next_path=next_path, callback_url=CALLBACK)
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    return service.callback(
        params={"state": state, "code": "x"}, browser_binding=started.browser_binding
    )


@pytest.fixture
def wide_store(store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> FakeStore:  # noqa: F811
    store.installation_admins = set()  # type: ignore[attr-defined]
    monkeypatch.setattr(
        repository,
        "user_holds_installation_admin_role",
        lambda _cur, user_id: user_id in store.installation_admins,  # type: ignore[attr-defined]
    )
    return store


def test_options_belong_to_no_account_and_need_the_installation_feature(
    wide_store: FakeStore,
) -> None:
    resolver = Recorder()
    assert _service(set(), resolver).sign_in_options("") == []
    service = _service({(INSTALLATION_ACCOUNT_ID, FEATURE)}, resolver)
    buttons = service.sign_in_options("")
    assert [button["provider_key"] for button in buttons] == ["wide:one"]
    # Listed with any account hint too, and it is no account's option.
    assert service.sign_in_options(str(uuid.uuid4())) == buttons
    assert service.has_account_options() is False


def test_the_identity_names_no_account_and_is_marked_installation_wide(
    wide_store: FakeStore,
) -> None:
    resolver = Recorder()
    _sign_in(_service({(INSTALLATION_ACCOUNT_ID, FEATURE)}, resolver))
    identity = resolver.seen[0]
    assert identity.account_id == "" and identity.installation_wide is True
    assert identity.issuer == "wide:https://wide.example.test"


def test_the_feature_is_checked_for_the_installation_not_the_session_account(
    wide_store: FakeStore,
) -> None:
    # The person's own account holds no feature; the installation does.
    result = _sign_in(_service({(INSTALLATION_ACCOUNT_ID, FEATURE)}, Recorder()))
    assert result.session_token and result.account_id == ACCOUNT_ID


def test_an_administrator_of_an_ordinary_account_signs_in(wide_store: FakeStore) -> None:
    wide_store.admins.add(USER_ID)
    result = _sign_in(_service({(INSTALLATION_ACCOUNT_ID, FEATURE)}, Recorder()))
    assert result.session_token


def test_an_installation_administrator_is_refused(wide_store: FakeStore) -> None:
    wide_store.admins.add(USER_ID)
    wide_store.installation_admins.add(USER_ID)  # type: ignore[attr-defined]
    with pytest.raises(ExternalSignInRefused) as refused:
        _sign_in(_service({(INSTALLATION_ACCOUNT_ID, FEATURE)}, Recorder()))
    assert refused.value.reason == repository.REFUSED_ADMIN


def test_an_account_method_still_refuses_every_administrator(wide_store: FakeStore) -> None:
    class AccountMethod(WideMethod):
        installation_wide = False

    wide_store.admins.add(USER_ID)
    port = ExternalSignInPort(_connection)
    port.register_sign_in_method(AccountMethod())
    port.set_identity_resolver(Recorder())
    service = ExternalSignInService(
        connection=_connection,
        port=port,
        entitled=lambda _account, _feature: True,
        audit=lambda **_entry: None,
        session_ttl_hours=12,
    )
    with pytest.raises(ExternalSignInRefused) as refused:
        _sign_in(service)
    assert refused.value.reason == repository.REFUSED_ADMIN


def test_the_resolver_learns_the_account_of_an_open_invitation(
    wide_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def open_invitation_account(_cur, token):
        asked.append(token)
        return "invited-account" if token == "t" * 43 else ""

    monkeypatch.setattr(account_invitations, "open_invitation_account", open_invitation_account)
    resolver = Recorder()
    service = _service({(INSTALLATION_ACCOUNT_ID, FEATURE)}, resolver)
    _sign_in(service, next_path="/invitations/" + "t" * 43)
    _sign_in(service, next_path="/invitations/" + "u" * 43)
    _sign_in(service, next_path="/jobs")
    assert [identity.invitation_account_id for identity in resolver.seen] == [
        "invited-account",
        "",
        "",
    ]
    assert asked == ["t" * 43, "u" * 43]


def test_a_resolver_sentence_reaches_the_sign_in_page(wide_store: FakeStore) -> None:
    refusal = ExternalIdentityRefused("no_access", public_message="Ask for an invitation." * 30)
    with pytest.raises(ExternalSignInRefused) as refused:
        _sign_in(_service({(INSTALLATION_ACCOUNT_ID, FEATURE)}, Recorder(refusal=refusal)))
    assert refused.value.reason == "no_access"
    assert refused.value.public_message.startswith("Ask for an invitation.")
    assert len(refused.value.public_message) == 240


# --- create_own_account ----------------------------------------------------------------


class ScriptedCursor:
    """Answers the statements of ``create_own_account`` from a small script."""

    def __init__(self, *, users_exist=True, taken=False, active_accounts=0) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self._answer: Any = None
        self.users_exist = users_exist
        self.taken = taken
        self.active_accounts = active_accounts

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), tuple(params)))
        text = " ".join(sql.split())
        if text.startswith("SELECT EXISTS (SELECT 1 FROM users)"):
            self._answer = (self.users_exist,)
        elif text.startswith("SELECT 1 FROM users"):
            self._answer = (1,) if self.taken else None
        elif text.startswith("SELECT count(*) FROM accounts"):
            self._answer = (self.active_accounts,)
        elif "INSERT INTO users" in text:
            self._answer = (USER_ID,)
        elif "UPDATE user_account_memberships" in text:
            self._answer = ("membership",)
        else:
            self._answer = None

    def fetchone(self):
        return self._answer


class Limits:
    def __init__(self, limit: int) -> None:
        self._limit = limit

    def limit(self, _account, _name):
        return self._limit


def _own_port(cursor: ScriptedCursor, limit: int = 3) -> ExternalSignInPort:
    class Conn:
        def cursor(self, **_kwargs):
            return cursor

    @contextmanager
    def connection() -> Iterator[Conn]:
        yield Conn()

    return ExternalSignInPort(connection, entitlement_service=lambda: Limits(limit))


def test_an_own_account_is_created_with_its_person_as_administrator() -> None:
    cursor = ScriptedCursor(active_accounts=1)
    created = _own_port(cursor).create_own_account(
        email=" Pat@Example.test ", display_name="Pat", created_by="self-service"
    )
    assert created["user_id"] == USER_ID and uuid.UUID(created["account_id"])
    statements = [sql for sql, _params in cursor.executed]
    assert any("INSERT INTO accounts" in sql for sql in statements)
    role = next(
        params for sql, params in cursor.executed if "UPDATE user_account_memberships" in sql
    )
    assert role[0] == "admin"
    user = next(params for sql, params in cursor.executed if "INSERT INTO users" in sql)
    assert user[0] == "pat@example.test" and user[1] == repository.UNUSABLE_PASSWORD_MARKER
    audit = next(params for sql, params in cursor.executed if "platform_action_log" in sql)
    assert audit[2] == "auth_account_create" and '"self_service"' in audit[4]
    # The account lifecycle lock is taken before anything is counted.
    assert "pg_advisory_xact_lock" in statements[0]


def test_an_own_account_respects_the_account_limit() -> None:
    cursor = ScriptedCursor(active_accounts=3)
    with pytest.raises(AccountLimitError) as refused:
        _own_port(cursor, limit=3).create_own_account(
            email="pat@example.test", created_by="self-service"
        )
    assert refused.value.status_code == 409
    assert not any("INSERT INTO accounts" in sql for sql, _params in cursor.executed)


def test_an_own_account_never_takes_an_address_someone_has() -> None:
    cursor = ScriptedCursor(taken=True)
    with pytest.raises(ValueError, match="already exists"):
        _own_port(cursor).create_own_account(email="pat@example.test", created_by="self-service")


def test_an_own_account_needs_a_limit_source() -> None:
    port = ExternalSignInPort(_connection)
    with pytest.raises(AccountLimitError):
        port.create_own_account(email="pat@example.test", created_by="self-service")
