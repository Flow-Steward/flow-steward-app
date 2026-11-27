"""Installation-wide sign-in on the disposable audit database (ADR 0083 amendment).

Covers the SQL the fakes in ``tests/unit/test_external_sign_in_review_fixes.py``
cannot: ``create_own_account`` with a module's ``before``/``after`` hooks (limit,
refusal, hooks committing with the account), ``discard_new_person`` (one-shot, only
what the port created, never a person with a session or a password, the account
removed or disabled), settling an invitation newcomer on acceptance (home and
default, marker, identity links with a duplicate dropped, revoked installation
sessions and tokens, the placeholder membership; nobody with an installation role is
moved), the awaiting-marker filter of user lists and the installation-role refusal.

Every test runs in a transaction that is rolled back; the port's own transactions
are savepoints inside it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from core.application.auth import external_sign_in_repository as repository
from core.application.auth import invitation_policies
from core.application.auth.account_limit import AccountLimitError
from core.application.auth.external_sign_in_ports import ExternalSignInPort
from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID
from core.application.auth.membership_roles import account_role_keys, set_membership_role
from core.application.auth.password import session_token_hash
from core.application.auth.user_admin.account_invitations import (
    decide,
    invitation_token_hash,
    invite,
)
from core.application.auth.user_admin.account_members import remove_member
from core.application.auth.user_admin.command_service import (
    EXTERNAL_ONLY_INSTALLATION_ROLE_MESSAGE,
    UserAdminCommandService,
)
from core.application.auth.user_admin.contracts import UserUpdateInput
from core.application.auth.user_admin.repository import list_auth_users
from core.application.management.errors import ServiceForbiddenError
from core.infrastructure.config.platform_identity import DEFAULT_TENANT_ID
from tests.db.test_account_members_postgres import _NoCommit
from tests.db.test_roles_per_account_postgres import _account, _person, _Store

INSTALLATION = str(INSTALLATION_ACCOUNT_ID)
ISSUER = "wide:https://id.example.test"


def _id() -> str:
    return str(uuid.uuid4())


class _Limits:
    """The entitlement service's ``accounts`` limit."""

    def __init__(self, limit: int) -> None:
        self.value = limit

    def limit(self, _account_id: str, _name: str) -> int:
        return self.value


@dataclass
class Db:
    conn: Any
    company: str  # an ordinary company
    company_admin: str
    limits: _Limits
    port: ExternalSignInPort

    def active_accounts(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM accounts WHERE is_active").fetchone()[0])

    def one(self, sql: str, params: tuple = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None


@pytest.fixture
def db(disposable_db_conn) -> Iterator[Db]:
    conn = disposable_db_conn
    with conn.transaction(force_rollback=True):
        conn.execute(
            """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
               VALUES (%s, %s, 'default', 'Default Account', true)
               ON CONFLICT DO NOTHING""",
            (INSTALLATION, DEFAULT_TENANT_ID),
        )
        conn.execute("UPDATE accounts SET is_active = true WHERE id = %s", (INSTALLATION,))
        company, _project = _account(conn, "company")
        company_admin = _person(conn, "company-admin", company, {company: "admin"})

        @contextmanager
        def connection():
            with conn.transaction():
                yield conn

        limits = _Limits(1_000_000)
        yield Db(
            conn=conn,
            company=company,
            company_admin=company_admin,
            limits=limits,
            port=ExternalSignInPort(connection, entitlement_service=lambda: limits),
        )


def _email(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:8]}@example.invalid"


# --- create_own_account with a module's hooks ----------------------------------------


def _hook_table(db: Db) -> None:
    db.conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS own_account_hook (account_id text, user_id text)"
    )


def test_own_account_hooks_commit_with_the_account(db: Db) -> None:
    _hook_table(db)
    calls: list[str] = []

    def before(cur: Any) -> None:
        calls.append("before")
        cur.execute("SELECT count(*) FROM own_account_hook")

    def after(cur: Any, created: Any) -> None:
        calls.append("after")
        cur.execute(
            "INSERT INTO own_account_hook VALUES (%s, %s)",
            (created["account_id"], created["user_id"]),
        )

    email = _email("pat")
    created = db.port.create_own_account(
        email=email,
        display_name="Pat Doe",
        account_name="Pat Doe",
        created_by="self-service:test",
        before=before,
        after=after,
    )

    assert calls == ["before", "after"]
    account_id, user_id = created["account_id"], created["user_id"]
    assert db.one("SELECT is_active FROM accounts WHERE id = %s", (account_id,)) is True
    assert db.one("SELECT display_name FROM accounts WHERE id = %s", (account_id,)) == "Pat Doe"
    assert db.one("SELECT password_hash FROM users WHERE id = %s", (user_id,)) == (
        repository.UNUSABLE_PASSWORD_MARKER
    )
    assert db.one("SELECT account_id::text FROM users WHERE id = %s", (user_id,)) == account_id
    assert account_role_keys(_Store(db.conn), user_id, account_id) == ["admin"]
    assert db.conn.execute("SELECT account_id, user_id FROM own_account_hook").fetchall() == [
        (account_id, user_id)
    ]
    assert (
        db.one(
            """SELECT count(*) FROM platform_action_log
                WHERE action_type = 'auth_account_create' AND result->>'account_id' = %s""",
            (account_id,),
        )
        == 1
    )


def test_a_refusing_before_hook_creates_nothing(db: Db) -> None:
    _hook_table(db)
    accounts = db.active_accounts()
    email = _email("refused")

    class QuotaReached(Exception):
        pass

    def before(_cur: Any) -> None:
        raise QuotaReached

    with pytest.raises(QuotaReached):
        db.port.create_own_account(
            email=email,
            created_by="self-service:test",
            before=before,
            after=lambda cur, made: cur.execute(
                "INSERT INTO own_account_hook VALUES (%s, %s)",
                (made["account_id"], made["user_id"]),
            ),
        )

    assert db.active_accounts() == accounts
    assert db.one("SELECT count(*) FROM users WHERE username = %s", (email,)) == 0
    assert db.one("SELECT count(*) FROM own_account_hook") == 0


def test_the_account_limit_refuses_before_any_hook(db: Db) -> None:
    db.limits.value = db.active_accounts()
    email = _email("full")
    called: list[str] = []

    with pytest.raises(AccountLimitError) as refused:
        db.port.create_own_account(
            email=email,
            created_by="self-service:test",
            before=lambda _cur: called.append("before"),
        )

    assert refused.value.status_code == 409
    assert called == []
    assert db.one("SELECT count(*) FROM users WHERE username = %s", (email,)) == 0


def test_a_taken_address_is_refused(db: Db) -> None:
    taken = db.one("SELECT username FROM users WHERE id = %s", (db.company_admin,))
    with pytest.raises(ValueError, match="already exists"):
        db.port.create_own_account(email=taken, created_by="self-service:test")


# --- discard_new_person -----------------------------------------------------------------


def _account_state(db: Db, account_id: str) -> str:
    active = db.one("SELECT is_active FROM accounts WHERE id = %s", (account_id,))
    if active is None:
        return "removed"
    return "active" if active else "disabled"


def test_discard_removes_what_the_port_created_once(db: Db) -> None:
    created = db.port.create_own_account(email=_email("undo"), created_by="self-service:test")
    user_id, account_id = created["user_id"], created["account_id"]

    # Not with another account than the one created with.
    assert not db.port.discard_new_person(user_id=user_id, account_id=_id(), reason="x")
    # That attempt used the one chance: nothing is removed any more.
    assert not db.port.discard_new_person(user_id=user_id, account_id=account_id, reason="x")
    assert db.one("SELECT count(*) FROM users WHERE id = %s", (user_id,)) == 1

    again = db.port.create_own_account(email=_email("undo2"), created_by="self-service:test")
    assert db.port.discard_new_person(
        user_id=again["user_id"], account_id=again["account_id"], reason="refused"
    )
    assert db.one("SELECT count(*) FROM users WHERE id = %s", (again["user_id"],)) == 0
    assert (
        db.one(
            "SELECT count(*) FROM user_account_memberships WHERE user_id = %s", (again["user_id"],)
        )
        == 0
    )
    assert _account_state(db, again["account_id"]) in {"removed", "disabled"}
    assert (
        db.one(
            """SELECT count(*) FROM platform_action_log
                WHERE action_type = 'auth_external_person_discard'
                  AND result->>'user_id' = %s""",
            (again["user_id"],),
        )
        == 1
    )
    # One shot.
    assert not db.port.discard_new_person(
        user_id=again["user_id"], account_id=again["account_id"], reason="again"
    )


def test_discard_never_touches_someone_the_port_did_not_create(db: Db) -> None:
    stranger = _person(db.conn, "stranger", db.company, {db.company: "viewer"})
    db.conn.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (repository.UNUSABLE_PASSWORD_MARKER, stranger),
    )
    assert not db.port.discard_new_person(user_id=stranger, account_id="", reason="x")
    assert db.one("SELECT count(*) FROM users WHERE id = %s", (stranger,)) == 1


def test_discard_keeps_a_person_with_a_session_or_a_password(db: Db) -> None:
    with_session = db.port.create_user(
        account_id=db.company, email=_email("session"), roles=("viewer",)
    )
    db.conn.execute(
        """INSERT INTO auth_sessions (user_id, account_id, session_token_hash, expires_at)
           VALUES (%s, %s, %s, now() + interval '1 hour')""",
        (with_session, db.company, session_token_hash(f"s-{uuid.uuid4().hex}")),
    )
    with_password = db.port.create_user(
        account_id=db.company, email=_email("password"), roles=("viewer",)
    )
    db.conn.execute(
        "UPDATE users SET password_hash = 'a-real-hash' WHERE id = %s", (with_password,)
    )

    for person in (with_session, with_password):
        assert not db.port.discard_new_person(user_id=person, account_id="", reason="x")
        assert db.one("SELECT count(*) FROM users WHERE id = %s", (person,)) == 1


def test_discard_keeps_a_company_someone_else_joined(db: Db) -> None:
    created = db.port.create_own_account(email=_email("shared"), created_by="self-service:test")
    _person(db.conn, "joined", db.company, {created["account_id"]: "viewer"})

    assert db.port.discard_new_person(
        user_id=created["user_id"], account_id=created["account_id"], reason="refused"
    )
    assert db.one("SELECT count(*) FROM users WHERE id = %s", (created["user_id"],)) == 0
    assert _account_state(db, created["account_id"]) == "active"


# --- settling an invitation newcomer ------------------------------------------------------


def _newcomer(db: Db) -> str:
    return db.port.create_user(
        account_id=INSTALLATION,
        email=_email("newcomer"),
        display_name="Newcomer",
        roles=(),
        awaiting_invitation=True,
    )


def _link(db: Db, user_id: str, account_id: str, subject: str) -> None:
    db.conn.execute(
        """INSERT INTO external_identities
               (account_id, method, option_key, issuer, subject, user_id, created_by)
           VALUES (%s, 'wide', 'one', %s, %s, %s, 'test')""",
        (account_id, ISSUER, subject, user_id),
    )


def _invite_to_company(db: Db, token: str, role: str = "operator") -> None:
    with db.conn.cursor() as cur:
        invite(
            cur,
            account_id=db.company,
            email=_email("invited"),
            role=role,
            invited_by=db.company_admin,
            token_hash=invitation_token_hash(token),
        )


def _accept(db: Db, token: str, user_id: str, role: str = "operator") -> None:
    with db.conn.cursor() as cur:
        decide(
            cur,
            token_hash=invitation_token_hash(token),
            user_id=user_id,
            accept=True,
            expected_role=role,
            external_session=True,
        )


def _memberships(db: Db, user_id: str) -> dict[str, tuple[str | None, bool]]:
    rows = db.conn.execute(
        """SELECT m.account_id::text, r.key, m.is_default
             FROM user_account_memberships m LEFT JOIN roles r ON r.id = m.role_id
            WHERE m.user_id = %s""",
        (user_id,),
    ).fetchall()
    return {account: (role, bool(default)) for account, role, default in rows}


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """A module's durable record of its newcomers; its links are method ``wide``."""
    people: set[str] = set()
    monkeypatch.setattr(
        invitation_policies,
        "_NEWCOMER_SOURCES",
        [invitation_policies.NewcomerSource(lambda user_id: user_id in people, ("wide",))],
    )
    return people


def _account_link(db: Db, user_id: str, account_id: str, subject: str) -> None:
    """A link of a per-account sign-in method (a company sign-in)."""
    db.conn.execute(
        """INSERT INTO external_identities
               (account_id, method, option_key, issuer, subject, user_id, created_by)
           VALUES (%s, 'corp', 'main', 'corp:https://corp.example.test', %s, %s, 'test')""",
        (account_id, subject, user_id),
    )


def test_accepting_settles_a_waiting_newcomer(db: Db, recorded: set[str]) -> None:
    newcomer = _newcomer(db)
    assert db.one("SELECT password_hash FROM users WHERE id = %s", (newcomer,)) == (
        repository.AWAITING_INVITATION_MARKER
    )
    assert _memberships(db, newcomer) == {INSTALLATION: (None, True)}
    _link(db, newcomer, INSTALLATION, "kept")
    # A link of the installation's own (per-account) sign-in never moves.
    _account_link(db, newcomer, INSTALLATION, "corp-newcomer")
    _link(db, newcomer, INSTALLATION, "duplicate")
    # The company already has that identity (another person): the newcomer's copy goes.
    _link(db, db.company_admin, db.company, "duplicate")
    session = f"s-{uuid.uuid4().hex}"
    db.conn.execute(
        """INSERT INTO auth_sessions (user_id, account_id, session_token_hash, expires_at)
           VALUES (%s, %s, %s, now() + interval '1 hour')""",
        (newcomer, INSTALLATION, session_token_hash(session)),
    )
    db.conn.execute(
        """INSERT INTO personal_access_tokens (user_id, account_id, token_hash)
           VALUES (%s, %s, %s)""",
        (newcomer, INSTALLATION, f"pat-{uuid.uuid4().hex}"),
    )
    token = f"link-{uuid.uuid4().hex}"
    _invite_to_company(db, token)

    _accept(db, token, newcomer)

    assert db.one("SELECT account_id::text FROM users WHERE id = %s", (newcomer,)) == db.company
    assert db.one("SELECT password_hash FROM users WHERE id = %s", (newcomer,)) == (
        repository.UNUSABLE_PASSWORD_MARKER
    )
    assert _memberships(db, newcomer) == {db.company: ("operator", True)}
    links = sorted(
        db.conn.execute(
            "SELECT account_id::text, subject FROM external_identities WHERE user_id = %s",
            (newcomer,),
        ).fetchall()
    )
    assert links == sorted([(db.company, "kept"), (INSTALLATION, "corp-newcomer")])
    assert (
        db.one(
            """SELECT user_id::text FROM external_identities
            WHERE account_id = %s AND issuer = %s AND subject = 'duplicate'""",
            (db.company, ISSUER),
        )
        == db.company_admin
    )
    assert (
        db.one(
            "SELECT revoked FROM auth_sessions WHERE session_token_hash = %s",
            (session_token_hash(session),),
        )
        is True
    )
    assert (
        db.one(
            "SELECT bool_and(revoked) FROM personal_access_tokens WHERE user_id = %s", (newcomer,)
        )
        is True
    )


def test_a_newcomer_who_set_a_password_is_settled_only_through_the_record(
    db: Db, recorded: set[str]
) -> None:
    unrecorded = _newcomer(db)
    db.conn.execute("UPDATE users SET password_hash = 'a-real-hash' WHERE id = %s", (unrecorded,))
    first = f"link-{uuid.uuid4().hex}"
    _invite_to_company(db, first)
    _accept(db, first, unrecorded)
    # Without the durable record nothing tells it from any password person: stays.
    assert db.one("SELECT account_id::text FROM users WHERE id = %s", (unrecorded,)) == (
        INSTALLATION
    )
    assert _memberships(db, unrecorded)[INSTALLATION] == (None, True)

    newcomer = _newcomer(db)
    recorded.add(newcomer)
    db.conn.execute("UPDATE users SET password_hash = 'a-real-hash' WHERE id = %s", (newcomer,))
    token = f"link-{uuid.uuid4().hex}"
    _invite_to_company(db, token)

    _accept(db, token, newcomer)

    assert db.one("SELECT account_id::text FROM users WHERE id = %s", (newcomer,)) == db.company
    assert db.one("SELECT password_hash FROM users WHERE id = %s", (newcomer,)) == "a-real-hash"
    assert _memberships(db, newcomer) == {db.company: ("operator", True)}


@pytest.mark.parametrize("password_hash", [repository.UNUSABLE_PASSWORD_MARKER, "a-real-hash"])
def test_a_role_less_installation_person_who_is_no_newcomer_is_not_moved(
    db: Db, recorded: set[str], password_hash: str
) -> None:
    """A person a company sign-in provisioned role-less into the installation account,
    or one with a local password, keeps home, links and access when joining X."""
    person = _person(db.conn, "role-less", INSTALLATION, {})
    db.conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, person))
    db.conn.execute(
        """INSERT INTO user_account_memberships (user_id, account_id, is_default)
           VALUES (%s, %s, true)""",
        (person, INSTALLATION),
    )
    _account_link(db, person, INSTALLATION, "corp-person")
    _link(db, person, INSTALLATION, "wide-person")
    session = f"s-{uuid.uuid4().hex}"
    db.conn.execute(
        """INSERT INTO auth_sessions (user_id, account_id, session_token_hash, expires_at)
           VALUES (%s, %s, %s, now() + interval '1 hour')""",
        (person, INSTALLATION, session_token_hash(session)),
    )
    token = f"link-{uuid.uuid4().hex}"
    _invite_to_company(db, token)

    _accept(db, token, person)

    assert db.one("SELECT account_id::text FROM users WHERE id = %s", (person,)) == INSTALLATION
    assert db.one("SELECT password_hash FROM users WHERE id = %s", (person,)) == password_hash
    memberships = _memberships(db, person)
    assert memberships[INSTALLATION] == (None, True)
    assert memberships[db.company][0] == "operator"
    assert (
        db.one(
            "SELECT count(*) FROM external_identities WHERE user_id = %s AND account_id = %s",
            (person, INSTALLATION),
        )
        == 2
    )
    assert (
        db.one(
            "SELECT revoked FROM auth_sessions WHERE session_token_hash = %s",
            (session_token_hash(session),),
        )
        is False
    )


def test_a_person_with_an_installation_role_is_not_moved(db: Db, recorded: set[str]) -> None:
    member = _person(db.conn, "installation-member", INSTALLATION, {INSTALLATION: "viewer"})
    db.conn.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (repository.UNUSABLE_PASSWORD_MARKER, member),
    )
    _link(db, member, INSTALLATION, "member")
    token = f"link-{uuid.uuid4().hex}"
    _invite_to_company(db, token)

    _accept(db, token, member)

    assert db.one("SELECT account_id::text FROM users WHERE id = %s", (member,)) == INSTALLATION
    memberships = _memberships(db, member)
    assert memberships[INSTALLATION] == ("viewer", True)
    assert memberships[db.company][0] == "operator"
    assert (
        db.one("SELECT account_id::text FROM external_identities WHERE user_id = %s", (member,))
        == INSTALLATION
    )


# --- user lists and the installation-role refusal --------------------------------------------


def _store(db: Db) -> _Store:
    store = _Store(db.conn)
    store.conn = _NoCommit(store.conn)
    return store


def _company_sign_in_person(db: Db, role: str | None) -> str:
    """A person a company sign-in provisioned into the installation account."""
    roles = {INSTALLATION: role} if role else {}
    person = _person(db.conn, f"company-signin-{role or 'none'}", INSTALLATION, roles)
    db.conn.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (repository.UNUSABLE_PASSWORD_MARKER, person),
    )
    if not role:
        db.conn.execute(
            """INSERT INTO user_account_memberships (user_id, account_id, is_default)
               VALUES (%s, %s, true)""",
            (person, INSTALLATION),
        )
    return person


def test_only_waiting_newcomers_are_left_out_of_user_lists(db: Db) -> None:
    newcomer = _newcomer(db)
    with_role = _company_sign_in_person(db, "viewer")
    without_role = _company_sign_in_person(db, None)

    rows, _total, _limit, _offset = list_auth_users(
        _store(db),
        search="",
        include_inactive=True,
        include_deleted=False,
        limit=200,
        offset=0,
        role="",
        active_filter=None,
        managed_account_ids=None,
        role_account_id=INSTALLATION,
        member_account_id=INSTALLATION,
    )
    listed = {str(row["id"]) for row in rows}

    assert with_role in listed and without_role in listed
    assert newcomer not in listed


class _RecordingRepository:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update_auth_user(self, _bl: Any, **kwargs: Any) -> int:
        self.updates.append(kwargs)
        return 0


def _users(db: Db, repository_stub: _RecordingRepository) -> UserAdminCommandService:
    return UserAdminCommandService(
        get_logger=lambda: _store(db),
        auth_tables_available_fn=lambda _bl: True,
        seed_default_roles_fn=lambda _bl: None,
        log_auth_audit_event_fn=lambda *_args, **_kwargs: None,
        repository=repository_stub,  # type: ignore[arg-type]
    )


def test_a_waiting_newcomer_gets_no_installation_role(db: Db) -> None:
    newcomer = _newcomer(db)
    stub = _RecordingRepository()

    with pytest.raises(ServiceForbiddenError) as refused:
        _users(db, stub).update_user(
            UserUpdateInput(user_id=newcomer, role="viewer", actor_user_id=db.company_admin)
        )

    assert refused.value.message == EXTERNAL_ONLY_INSTALLATION_ROLE_MESSAGE
    assert stub.updates == []


def test_a_company_sign_in_person_can_be_demoted_and_removed(db: Db) -> None:
    person = _company_sign_in_person(db, "operator")
    stub = _RecordingRepository()

    _users(db, stub).update_user(
        UserUpdateInput(user_id=person, role="viewer", actor_user_id=db.company_admin)
    )

    assert [update["roles"] for update in stub.updates] == [["viewer"]]
    # The write itself, and removing the membership, as Core's own paths do them.
    with db.conn.cursor() as cur:
        set_membership_role(cur, user_id=person, account_id=INSTALLATION, role="viewer")
    assert account_role_keys(_Store(db.conn), person, INSTALLATION) == ["viewer"]
    db.conn.execute(
        """INSERT INTO user_account_memberships (user_id, account_id, is_default, role_id)
           VALUES (%s, %s, false, (SELECT id FROM roles WHERE key = 'viewer'))""",
        (person, db.company),
    )
    admin = _person(db.conn, "installation-admin", INSTALLATION, {INSTALLATION: "admin"})
    with db.conn.cursor() as cur:
        remove_member(cur, user_id=person, account_id=INSTALLATION, actor_user_id=admin)
    assert account_role_keys(_Store(db.conn), person, INSTALLATION) == []
