"""The neutral external sign-in seam against a fake sign-in method (ADR 0083).

Core implements no sign-in protocol. The tests register ``acme-ticket``, a method a
platform module could provide, that redirects out and comes back with a ticket, and
a trusted header source. The database is an in-memory stand-in for the repository
functions the service calls; nothing leaves the process.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from core.application.auth import external_sign_in_repository as repository
from core.application.auth.external_sign_in_ports import (
    BeginResult,
    ExternalIdentityResolution,
    ExternalSignInError,
    ExternalSignInPort,
    SignInOption,
    VerifiedExternalIdentity,
)
from core.application.auth.external_sign_in_service import (
    ExternalSignInRefused,
    ExternalSignInService,
    safe_return_path,
    value_hash,
)
from core.infrastructure.security import secret_codec

ISSUER = "https://ticket.acme.test"
STORED_ISSUER = f"acme-ticket:{ISSUER}"
CALLBACK = "https://flow.acme.test/api/auth/external/callback"
ACCOUNT_ID = str(uuid.UUID(int=7))
USER_ID = str(uuid.UUID(int=8))


# --- the fake method a module would register ------------------------------------------


class AcmeTicketMethod:
    """Redirects out with the state and comes back with a ticket."""

    kind = "acme-ticket"

    def __init__(self, *, feature: str = "") -> None:
        self.feature = feature
        self.completed: list[dict[str, Any]] = []
        self.subject = "ticket-user"

    def options(self):
        return [
            SignInOption(
                key="main", display_name="Acme Ticket", account_id=ACCOUNT_ID, feature=self.feature
            )
        ]

    def option(self, key):
        return next((item for item in self.options() if item.key == key), None)

    def begin(self, key, *, callback_url, state):
        return BeginResult(
            redirect_url=f"https://ticket.acme.test/login?state={state}&back={callback_url}",
            data={"expect": "ticket-1"},
        )

    def complete(self, key, *, params, headers, data, callback_url):
        self.completed.append({"params": dict(params), "data": dict(data)})
        if params.get("ticket") != data["expect"]:
            raise ExternalSignInError("ticket_invalid")
        return VerifiedExternalIdentity(
            method="spoofed",
            option_key="spoofed",
            account_id=ACCOUNT_ID,
            issuer=ISSUER,
            subject=self.subject,
            claims={"email": "pat@acme.test"},
        )


# --- the in-memory store --------------------------------------------------------------


@dataclass
class FakeStore:
    states: dict[str, dict[str, Any]] = field(default_factory=dict)
    identities: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    members: set[tuple[str, str]] = field(default_factory=set)
    admins: set[str] = field(default_factory=set)
    accounts: dict[str, str] = field(default_factory=dict)
    sessions: list[dict[str, Any]] = field(default_factory=list)

    def insert_state(self, _cur, *, state_hash, **values):
        self.states[state_hash] = {**values, "consumed": False, "expires": time.time() + 600}

    def consume_state(self, _cur, state_hash):
        row = self.states.get(state_hash)
        if row is None or row["consumed"] or row["expires"] < time.time():
            return None
        row["consumed"] = True
        return dict(row)

    def find_identity(self, _cur, *, account_id, issuer, subject):
        return self.identities.get((account_id, issuer, subject))

    def find_identities(self, _cur, *, issuer, subject):
        return [
            row
            for (_account, row_issuer, row_subject), row in self.identities.items()
            if (row_issuer, row_subject) == (issuer, subject)
        ]

    def link_identity(
        self, _cur, *, account_id, user_id, method, option_key, issuer, subject, created_by
    ):
        row = self.identities.setdefault(
            (account_id, issuer, subject),
            {
                "id": str(uuid.uuid4()),
                "account_id": account_id,
                "user_id": user_id,
                "method": method,
                "option_key": option_key,
            },
        )
        if row["user_id"] != user_id:
            raise repository.IdentityLinkedElsewhereError("elsewhere")
        return row

    def touch_identity(self, _cur, _identity_id):
        return None

    def load_user(self, _cur, user_id):
        return self.users.get(user_id)

    def user_in_account(self, _cur, *, user_id, account_id):
        return (user_id, account_id) in self.members

    def user_holds_admin_role(self, _cur, user_id):
        return user_id in self.admins

    def account_id_for_hint(self, _cur, hint):
        return self.accounts.get(hint)

    def insert_session(self, _cur, **values):
        self.sessions.append(values)


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, *_args):
        return None


class _Conn:
    def cursor(self, **_kwargs):
        return _Cursor()


@contextmanager
def _connection() -> Iterator[_Conn]:
    yield _Conn()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "external-sign-in-test"})


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake = FakeStore()
    for name in (
        "insert_state",
        "consume_state",
        "find_identity",
        "find_identities",
        "link_identity",
        "touch_identity",
        "load_user",
        "user_in_account",
        "user_holds_admin_role",
        "account_id_for_hint",
        "insert_session",
    ):
        monkeypatch.setattr(repository, name, getattr(fake, name))
    fake.users[USER_ID] = {
        "id": USER_ID,
        "username": "pat@acme.test",
        "is_active": True,
        "account_id": ACCOUNT_ID,
    }
    fake.members.add((USER_ID, ACCOUNT_ID))
    fake.accounts.update({"acme": ACCOUNT_ID, ACCOUNT_ID: ACCOUNT_ID})
    return fake


@dataclass
class Harness:
    service: ExternalSignInService
    port: ExternalSignInPort
    method: AcmeTicketMethod
    store: FakeStore
    audit: list[dict[str, Any]]

    def start(self, next_path: str = "/jobs", **kwargs: Any) -> tuple[str, str]:
        started = self.service.start(
            method="acme-ticket", key="main", next_path=next_path, callback_url=CALLBACK, **kwargs
        )
        state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
        return state, started.browser_binding

    def callback(self, state: str, binding: str, ticket: str = "ticket-1"):
        return self.service.callback(
            params={"state": state, "ticket": ticket}, browser_binding=binding
        )

    def link(self, subject: str = "ticket-user", user_id: str = USER_ID) -> None:
        self.store.identities[(ACCOUNT_ID, STORED_ISSUER, subject)] = {
            "id": "linked",
            "account_id": ACCOUNT_ID,
            "user_id": user_id,
        }


@pytest.fixture
def harness(store: FakeStore) -> Harness:
    port = ExternalSignInPort(_connection)
    method = AcmeTicketMethod()
    port.register_sign_in_method(method)
    audit: list[dict[str, Any]] = []
    service = ExternalSignInService(
        connection=_connection,
        port=port,
        entitled=lambda _account, _feature: False,
        audit=lambda **entry: audit.append(entry),
        session_ttl_hours=12,
    )
    return Harness(service, port, method, store, audit)


# --- one path for every method --------------------------------------------------------


def test_a_module_method_signs_in_through_cores_attempt_callback_and_session(
    harness: Harness,
) -> None:
    harness.link()
    buttons = harness.service.sign_in_options(ACCOUNT_ID)
    assert buttons == [
        {
            "provider_key": "acme-ticket:main",
            "display_name": "Acme Ticket",
            "login_path": "/api/auth/external/start?method=acme-ticket&key=main",
        }
    ]

    state, binding = harness.start()
    stored = harness.store.states[value_hash(state)]
    assert stored["method"] == "acme-ticket" and stored["option_key"] == "main"
    assert "ticket-1" not in stored["method_data_encrypted"]
    result = harness.callback(state, binding)

    assert result.session_token and result.return_path == "/jobs"
    assert harness.method.completed[-1]["data"] == {"expect": "ticket-1"}
    session = harness.store.sessions[-1]
    assert "auth_method" not in session  # the repository stores 'external'
    assert session["external_identity_id"] == "linked"
    assert harness.audit[-1]["status"] == "ok"
    assert harness.audit[-1]["detail"]["method"] == "acme-ticket"


def test_external_sessions_are_stored_as_external() -> None:
    executed: list[tuple[str, tuple]] = []

    class Cursor:
        def execute(self, sql, params=()):
            executed.append((sql, params))

    repository.insert_session(
        Cursor(),
        user_id=USER_ID,
        account_id=ACCOUNT_ID,
        token_hash="h",
        ttl_hours=1,
        external_identity_id="linked",
    )

    assert executed[0][1][4] == repository.EXTERNAL_AUTH_METHOD == "external"


def test_core_names_the_method_and_the_issuer_namespace(harness: Harness) -> None:
    seen: list[VerifiedExternalIdentity] = []

    class Resolver:
        def resolve(self, identity):
            seen.append(identity)
            return ExternalIdentityResolution(user_id=USER_ID, account_id=ACCOUNT_ID)

    harness.port.set_identity_resolver(Resolver())
    state, binding = harness.start()
    harness.callback(state, binding)

    assert seen[0].method == "acme-ticket" and seen[0].option_key == "main"
    assert seen[0].issuer == STORED_ISSUER
    assert seen[0].claims["email"] == "pat@acme.test"
    assert (
        harness.store.identities[(ACCOUNT_ID, STORED_ISSUER, "ticket-user")]["method"]
        == "acme-ticket"
    )


def test_another_browser_cannot_finish_the_attempt(harness: Harness) -> None:
    harness.link()
    state, _binding = harness.start()

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, "another-browser")

    assert refused.value.reason == "browser_mismatch"
    assert harness.method.completed == []


def test_a_state_is_used_once(harness: Harness) -> None:
    harness.link()
    state, binding = harness.start()
    harness.callback(state, binding)

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding)

    assert refused.value.reason == "state_invalid_or_used"


def test_an_expired_state_is_refused(harness: Harness) -> None:
    harness.link()
    state, binding = harness.start()
    harness.store.states[value_hash(state)]["expires"] = time.time() - 1

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding)

    assert refused.value.reason == "state_invalid_or_used"


def test_a_method_refusal_keeps_its_reason(harness: Harness) -> None:
    harness.link()
    state, binding = harness.start()

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding, ticket="forged")

    assert refused.value.reason == "ticket_invalid"
    assert harness.store.sessions == []


def test_an_unknown_method_is_refused(harness: Harness) -> None:
    with pytest.raises(ExternalSignInRefused) as refused:
        harness.service.start(method="nope", key="main", next_path="/", callback_url=CALLBACK)

    assert refused.value.reason == "method_unavailable"


# --- who signs in ---------------------------------------------------------------------


def test_an_unlinked_identity_is_refused_by_default(harness: Harness) -> None:
    state, binding = harness.start()

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding)

    assert refused.value.reason == "identity_not_linked"
    assert harness.store.sessions == []
    assert harness.store.identities == {}


def test_a_matching_email_never_links_an_identity(harness: Harness) -> None:
    harness.link(subject="someone-else")
    state, binding = harness.start()

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding)

    assert refused.value.reason == "identity_not_linked"


def test_an_inactive_user_is_refused(harness: Harness) -> None:
    harness.link()
    harness.store.users[USER_ID]["is_active"] = False
    state, binding = harness.start()

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding)

    assert refused.value.reason == "user_inactive"


def test_a_user_outside_the_identitys_account_is_refused(harness: Harness) -> None:
    harness.link()
    harness.store.members.clear()
    state, binding = harness.start()

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding)

    assert refused.value.reason == "account_unavailable"


def test_a_module_resolver_decides_and_core_links_the_identity(harness: Harness) -> None:
    class Resolver:
        def resolve(self, identity):
            return ExternalIdentityResolution(user_id=USER_ID, account_id=ACCOUNT_ID)

    harness.port.set_identity_resolver(Resolver())
    state, binding = harness.start()

    assert harness.callback(state, binding).session_token
    link = harness.store.identities[(ACCOUNT_ID, STORED_ISSUER, "ticket-user")]
    assert link["user_id"] == USER_ID and link["account_id"] == ACCOUNT_ID


def test_an_identity_linked_to_another_user_is_refused(harness: Harness) -> None:
    harness.link(user_id=str(uuid.uuid4()))

    class Resolver:
        def resolve(self, identity):
            return ExternalIdentityResolution(user_id=USER_ID, account_id=ACCOUNT_ID)

    harness.port.set_identity_resolver(Resolver())
    state, binding = harness.start()

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding)

    assert refused.value.reason == "identity_linked_to_another_user"


def test_an_option_with_a_feature_is_offered_only_when_entitled(store: FakeStore) -> None:
    port = ExternalSignInPort(_connection)
    port.register_sign_in_method(AcmeTicketMethod(feature="acme_sign_in"))
    entitled: dict[str, bool] = {"value": False}
    service = ExternalSignInService(
        connection=_connection,
        port=port,
        entitled=lambda _account, feature: entitled["value"] and feature == "acme_sign_in",
        audit=lambda **_entry: None,
        session_ttl_hours=1,
    )

    assert service.sign_in_options(ACCOUNT_ID) == []
    with pytest.raises(ExternalSignInRefused) as refused:
        service.start(method="acme-ticket", key="main", next_path="/", callback_url=CALLBACK)
    assert refused.value.reason == "option_unavailable"

    entitled["value"] = True
    assert [item["provider_key"] for item in service.sign_in_options(ACCOUNT_ID)] == [
        "acme-ticket:main"
    ]


def test_linking_attaches_the_identity_to_the_person_who_started(harness: Harness) -> None:
    state, binding = harness.start(next_path="/settings", link_user_id=USER_ID)

    result = harness.callback(state, binding)

    assert result.linked and not result.session_token
    link = harness.store.identities[(ACCOUNT_ID, STORED_ISSUER, "ticket-user")]
    assert link["user_id"] == USER_ID and link["account_id"] == ACCOUNT_ID
    assert harness.store.sessions == []


# --- the trusted header kind ----------------------------------------------------------


class _HeaderSource:
    feature = "trusted_header"
    display_name = "Company sign-in"

    def resolve(self, headers):
        if headers.get("x-acme-user") != "pat":
            return None
        return VerifiedExternalIdentity(
            method="header",
            option_key="ignored",
            account_id=ACCOUNT_ID,
            issuer=ISSUER,
            subject="ticket-user",
        )


def test_the_trusted_header_kind_signs_in_at_once_in_its_own_namespace(
    harness: Harness,
) -> None:
    class Resolver:
        def resolve(self, identity):
            assert identity.issuer == f"header:{ISSUER}"
            return ExternalIdentityResolution(user_id=USER_ID, account_id=ACCOUNT_ID)

    harness.port.register_header_source(_HeaderSource())
    harness.port.set_identity_resolver(Resolver())
    harness.service._entitled = lambda _account, feature: feature == "trusted_header"

    assert harness.service.sign_in_options()[0]["login_path"] == (
        "/api/auth/external/start?method=header&key=trusted"
    )
    with pytest.raises(ExternalSignInRefused):
        harness.service.start(method="header", key="trusted", next_path="/", callback_url="")
    result = harness.service.start(
        method="header",
        key="trusted",
        next_path="/jobs",
        callback_url="",
        headers={"x-acme-user": "pat"},
    )

    assert result.session_token and result.return_path == "/jobs"
    assert (ACCOUNT_ID, f"header:{ISSUER}", "ticket-user") in harness.store.identities
    assert (ACCOUNT_ID, STORED_ISSUER, "ticket-user") not in harness.store.identities


def test_the_header_kind_without_a_source_offers_nothing(harness: Harness) -> None:
    with pytest.raises(ExternalSignInRefused) as refused:
        harness.service.start(method="header", key="trusted", next_path="/", callback_url="")

    assert refused.value.reason == "option_unavailable"
    assert all(
        item["provider_key"] != "header:trusted" for item in harness.service.sign_in_options()
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/jobs?tab=1", "/jobs?tab=1"),
        ("/oauth/consent?request=abc", "/oauth/consent?request=abc"),
        ("https://attacker.example/", "/"),
        ("//attacker.example/", "/"),
        ("/\\attacker.example", "/"),
        ("/jobs\r\nSet-Cookie: x", "/"),
        ("/api/auth/external/start?method=x", "/"),
        ("", "/"),
        (None, "/"),
    ],
)
def test_the_return_path_is_a_same_origin_relative_path(raw, expected) -> None:
    assert safe_return_path(raw) == expected


# --- the port -------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["local", "external", "header", "acme-ticket", "Not-A-Kind", ""])
def test_reserved_taken_or_malformed_method_kinds_are_refused(harness: Harness, kind: str) -> None:
    method = AcmeTicketMethod()
    method.kind = kind

    with pytest.raises((TypeError, ValueError)):
        harness.port.register_sign_in_method(method)


def test_the_port_rolls_back_to_a_checkpoint(store: FakeStore) -> None:
    port = ExternalSignInPort(_connection)
    checkpoint = port.loading_checkpoint()

    class Resolver:
        def resolve(self, identity):
            raise AssertionError

    port.set_identity_resolver(Resolver())
    port.register_header_source(_HeaderSource())
    port.register_sign_in_method(AcmeTicketMethod())
    port.restore_loading_checkpoint(checkpoint)

    assert port.sign_in_methods == ()
    assert port.header_source is None
    assert type(port.resolver).__name__ == "LinkedIdentityResolver"


def test_a_second_resolver_or_header_source_is_refused(harness: Harness) -> None:
    class Resolver:
        def resolve(self, identity):
            return None

    harness.port.set_identity_resolver(Resolver())
    with pytest.raises(ValueError):
        harness.port.set_identity_resolver(Resolver())
    harness.port.register_header_source(_HeaderSource())
    with pytest.raises(ValueError):
        harness.port.register_header_source(_HeaderSource())


def test_the_port_never_grants_the_administrator_role(harness: Harness) -> None:
    with pytest.raises(ValueError):
        harness.port.grant_roles(account_id=ACCOUNT_ID, user_id=USER_ID, roles=["admin"])
    with pytest.raises(ValueError):
        harness.port.create_user(account_id=ACCOUNT_ID, email="a@acme.test", roles=["admin"])


def test_a_module_link_is_stored_in_the_methods_namespace(harness: Harness) -> None:
    identity_id = harness.port.link_identity(
        account_id=ACCOUNT_ID,
        user_id=USER_ID,
        method="acme-ticket",
        issuer=ISSUER,
        subject="ticket-user",
    )

    assert harness.store.identities[(ACCOUNT_ID, STORED_ISSUER, "ticket-user")]["id"] == identity_id
    assert (
        harness.port.linked_user(account_id=ACCOUNT_ID, issuer=STORED_ISSUER, subject="ticket-user")
        == USER_ID
    )
    assert (
        harness.port.linked_user(
            account_id=str(uuid.uuid4()), issuer=STORED_ISSUER, subject="ticket-user"
        )
        is None
    )
    with pytest.raises(LookupError):
        harness.port.link_identity(
            account_id=str(uuid.uuid4()),
            user_id=USER_ID,
            method="acme-ticket",
            issuer=ISSUER,
            subject="ticket-user",
        )
    with pytest.raises(ValueError):
        harness.port.link_identity(
            account_id=ACCOUNT_ID, user_id=USER_ID, method="local", issuer=ISSUER, subject="x"
        )


def test_the_unusable_password_marker_never_verifies() -> None:
    from core.application.auth.password import verify_password

    assert repository.is_unusable_password(repository.UNUSABLE_PASSWORD_MARKER)
    assert not verify_password("", repository.UNUSABLE_PASSWORD_MARKER)
    assert not verify_password(
        repository.UNUSABLE_PASSWORD_MARKER, repository.UNUSABLE_PASSWORD_MARKER
    )


# --- security review: no reach beyond the issuing account -----------------------------

INSTALLATION_ACCOUNT = str(uuid.UUID(int=1))
ROOT_ID = str(uuid.UUID(int=9))


MULTI_ID = str(uuid.UUID(int=10))


def _member_of_two_accounts(store: FakeStore) -> None:
    """A person who is no administrator, lives in another account and is a member here."""
    store.users[MULTI_ID] = {
        "id": MULTI_ID,
        "username": "sam@elsewhere.test",
        "is_active": True,
        "account_id": INSTALLATION_ACCOUNT,
    }
    store.members.update({(MULTI_ID, INSTALLATION_ACCOUNT), (MULTI_ID, ACCOUNT_ID)})


def _root_member_of_the_account(store: FakeStore) -> None:
    """The installation administrator, who is also a member of the account."""
    store.users[ROOT_ID] = {
        "id": ROOT_ID,
        "username": "root@install.test",
        "is_active": True,
        "account_id": INSTALLATION_ACCOUNT,
    }
    store.members.update({(ROOT_ID, INSTALLATION_ACCOUNT), (ROOT_ID, ACCOUNT_ID)})
    store.admins.add(ROOT_ID)


def test_an_account_cannot_link_its_identity_to_the_installation_administrator(
    harness: Harness,
) -> None:
    """The takeover: the account's admin links their IdP identity to the installation admin."""
    _root_member_of_the_account(harness.store)

    with pytest.raises(PermissionError, match="admin_not_external"):
        harness.port.link_identity(
            account_id=ACCOUNT_ID,
            user_id=ROOT_ID,
            method="acme-ticket",
            issuer=ISSUER,
            subject="ticket-user",
        )

    assert harness.store.identities == {}


def test_an_account_links_its_identity_to_a_member_who_also_belongs_elsewhere(
    harness: Harness,
) -> None:
    """ADR 0083 amendment (ADR 0085): the link signs in only to this account, with the
    person's role here; memberships of other accounts do not matter."""
    _member_of_two_accounts(harness.store)

    link_id = harness.port.link_identity(
        account_id=ACCOUNT_ID,
        user_id=MULTI_ID,
        method="acme-ticket",
        issuer=ISSUER,
        subject="ticket-user",
    )

    assert link_id
    assert [row["user_id"] for row in harness.store.identities.values()] == [MULTI_ID]


def test_an_account_cannot_link_its_identity_to_a_person_outside_it(harness: Harness) -> None:
    outsider = str(uuid.uuid4())
    harness.store.users[outsider] = {"id": outsider, "is_active": True, "account_id": "x"}

    with pytest.raises(LookupError):
        harness.port.link_identity(
            account_id=ACCOUNT_ID,
            user_id=outsider,
            method="acme-ticket",
            issuer=ISSUER,
            subject="ticket-user",
        )
    assert harness.store.identities == {}


def test_an_account_cannot_link_its_identity_to_an_administrator(harness: Harness) -> None:
    harness.store.admins.add(USER_ID)

    with pytest.raises(PermissionError, match="admin_not_external"):
        harness.port.link_identity(
            account_id=ACCOUNT_ID,
            user_id=USER_ID,
            method="acme-ticket",
            issuer=ISSUER,
            subject="ticket-user",
        )


def test_sign_in_never_reaches_the_installation_administrator(harness: Harness) -> None:
    _root_member_of_the_account(harness.store)
    harness.link(user_id=ROOT_ID)  # a link written before the rule, or by a resolver
    state, binding = harness.start()

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding)

    assert refused.value.reason == "admin_not_external"
    assert harness.store.sessions == []


def test_sign_in_reaches_a_member_who_also_belongs_elsewhere_in_this_account_only(
    harness: Harness,
) -> None:
    _member_of_two_accounts(harness.store)
    harness.link(user_id=MULTI_ID)
    state, binding = harness.start()

    result = harness.callback(state, binding)

    assert result.user_id == MULTI_ID
    assert result.account_id == ACCOUNT_ID
    assert harness.store.sessions[-1]["account_id"] == ACCOUNT_ID


def test_sign_in_never_signs_in_an_administrator_of_another_account(harness: Harness) -> None:
    _member_of_two_accounts(harness.store)
    harness.store.admins.add(MULTI_ID)  # admin in the other account, a member here
    harness.link(user_id=MULTI_ID)
    state, binding = harness.start()

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding)

    assert refused.value.reason == "admin_not_external"
    assert harness.store.sessions == []


def test_sign_in_never_signs_in_an_administrator(harness: Harness) -> None:
    harness.link()
    harness.store.admins.add(USER_ID)
    state, binding = harness.start()

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding)

    assert refused.value.reason == "admin_not_external"
    assert harness.store.sessions == []


def test_a_resolver_cannot_move_the_session_to_another_account(harness: Harness) -> None:
    _root_member_of_the_account(harness.store)

    class Resolver:
        def resolve(self, identity):
            return ExternalIdentityResolution(user_id=ROOT_ID, account_id=INSTALLATION_ACCOUNT)

    harness.port.set_identity_resolver(Resolver())
    state, binding = harness.start()

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding)

    assert refused.value.reason == "account_mismatch"
    assert harness.store.sessions == []


def test_the_session_belongs_to_the_issuing_account(harness: Harness) -> None:
    harness.link()
    state, binding = harness.start()

    result = harness.callback(state, binding)

    assert result.account_id == ACCOUNT_ID
    assert harness.store.sessions[-1]["account_id"] == ACCOUNT_ID


def test_self_linking_refuses_the_installation_administrator(harness: Harness) -> None:
    _root_member_of_the_account(harness.store)
    state, binding = harness.start(link_user_id=ROOT_ID)

    with pytest.raises(ExternalSignInRefused) as refused:
        harness.callback(state, binding)

    assert refused.value.reason == "admin_not_external"
    assert harness.store.identities == {}


def test_roles_are_granted_to_a_member_who_also_belongs_elsewhere(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0085: the grant writes the role of this account's membership only."""
    from core.application.auth import external_sign_in_ports

    _member_of_two_accounts(harness.store)
    granted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        external_sign_in_ports,
        "_grant",
        lambda _cur, **values: granted.append(values) or ["operator"],
    )

    assert harness.port.grant_roles(
        account_id=ACCOUNT_ID, user_id=MULTI_ID, roles=["operator"]
    ) == ["operator"]
    assert granted == [{"user_id": MULTI_ID, "account_id": ACCOUNT_ID, "roles": ["operator"]}]


def test_roles_are_never_granted_to_a_person_outside_the_account(harness: Harness) -> None:
    outsider = str(uuid.uuid4())

    with pytest.raises(LookupError):
        harness.port.grant_roles(account_id=ACCOUNT_ID, user_id=outsider, roles=["operator"])


def test_an_identity_two_accounts_linked_is_ambiguous_for_a_method_without_an_account(
    harness: Harness,
) -> None:
    from core.application.auth.external_sign_in_ports import (
        ExternalIdentityRefused,
        LinkedIdentityResolver,
    )

    other = str(uuid.uuid4())
    harness.store.identities[(ACCOUNT_ID, STORED_ISSUER, "s")] = {
        "id": "a",
        "account_id": ACCOUNT_ID,
        "user_id": USER_ID,
    }
    harness.store.identities[(other, STORED_ISSUER, "s")] = {
        "id": "b",
        "account_id": other,
        "user_id": str(uuid.uuid4()),
    }
    identity = VerifiedExternalIdentity(
        method="acme-ticket", option_key="main", account_id="", issuer=STORED_ISSUER, subject="s"
    )
    resolver = LinkedIdentityResolver(_connection)

    with pytest.raises(ExternalIdentityRefused) as refused:
        resolver.resolve(identity)
    assert refused.value.reason == "identity_ambiguous"

    pinned = VerifiedExternalIdentity(
        method="acme-ticket",
        option_key="main",
        account_id=ACCOUNT_ID,
        issuer=STORED_ISSUER,
        subject="s",
    )
    assert resolver.resolve(pinned).account_id == ACCOUNT_ID


# --- the public option list -----------------------------------------------------------


def test_the_public_option_list_hides_account_options_without_a_hint(harness: Harness) -> None:
    assert harness.service.public_options() == {
        "options": [],
        "account_hint_supported": True,
    }
    assert harness.service.public_options("unknown-company")["options"] == []


def test_an_account_hint_lists_that_accounts_options(harness: Harness) -> None:
    for hint in ("acme", ACCOUNT_ID):
        payload = harness.service.public_options(hint)
        assert [item["provider_key"] for item in payload["options"]] == ["acme-ticket:main"]


def test_installation_wide_options_are_listed_without_a_hint(harness: Harness) -> None:
    harness.port.register_header_source(_HeaderSource())
    harness.service._entitled = lambda _account, _feature: True

    payload = harness.service.public_options()

    assert [item["provider_key"] for item in payload["options"]] == ["header:trusted"]


def _captured_auth_audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from core.application.auth import scoped_role_ports

    entries: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scoped_role_ports,
        "record_scope_limit_audit",
        lambda _cur, **entry: entries.append(entry),
    )
    return entries


def test_a_refused_link_is_written_to_the_auth_audit(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _captured_auth_audit(monkeypatch)
    _root_member_of_the_account(harness.store)

    with pytest.raises(PermissionError, match="admin_not_external"):
        harness.port.link_identity(
            account_id=ACCOUNT_ID,
            user_id=ROOT_ID,
            method="acme-ticket",
            issuer=ISSUER,
            subject="ticket-user",
            created_by="account-admin",
        )

    assert [(e["action"], e["status"], e["actor"]) for e in entries] == [
        ("external_identity_link", "rejected", "account-admin")
    ]
    assert entries[0]["detail"]["reason"] == "admin_not_external"
    assert entries[0]["detail"]["user_id"] == ROOT_ID
    assert entries[0]["detail"]["account_id"] == ACCOUNT_ID


def test_a_role_grant_to_a_member_of_several_accounts_writes_no_refusal_audit(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.application.auth import external_sign_in_ports

    entries = _captured_auth_audit(monkeypatch)
    _member_of_two_accounts(harness.store)
    monkeypatch.setattr(external_sign_in_ports, "_grant", lambda _cur, **_values: ["operator"])

    harness.port.grant_roles(account_id=ACCOUNT_ID, user_id=MULTI_ID, roles=["operator"])

    assert entries == []


def test_an_allowed_link_writes_no_refusal_audit(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _captured_auth_audit(monkeypatch)

    harness.port.link_identity(
        account_id=ACCOUNT_ID,
        user_id=USER_ID,
        method="acme-ticket",
        issuer=ISSUER,
        subject="ticket-user",
    )

    assert entries == []


class _AccountHeaderSource(_HeaderSource):
    """A proxy configured for one account (the deployment names it)."""

    account_id = ACCOUNT_ID


def test_a_header_source_of_one_account_is_listed_only_with_that_accounts_hint(
    harness: Harness,
) -> None:
    harness.port.register_header_source(_AccountHeaderSource())
    harness.service._entitled = lambda _account, _feature: True

    assert harness.service.public_options()["options"] == []
    assert harness.service.public_options("unknown-company")["options"] == []
    listed = harness.service.public_options("acme")["options"]
    assert "header:trusted" in [item["provider_key"] for item in listed]
    assert harness.service.public_options()["account_hint_supported"] is True


def test_a_header_source_option_carries_the_sources_account() -> None:
    from core.application.auth.external_sign_in_methods import HeaderSignInMethod

    port = ExternalSignInPort(_connection)
    port.register_header_source(_AccountHeaderSource())

    option = HeaderSignInMethod(port).option("trusted")

    assert option is not None and option.account_id == ACCOUNT_ID
