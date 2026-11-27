"""External sign-in storage on the disposable audit database (ADR 0083).

A sign-in attempt is used once, an identity names one user, a session records how it
was issued and the local password rules hold only local sessions. The port creates
users without a local password and never grants the administrator role. Every test
runs in a transaction that is rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.application.auth import external_sign_in_repository as repository
from core.application.auth.external_sign_in_ports import ExternalSignInPort
from core.application.auth.local_credentials import local_session_credential_filter

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261126150000_external_sign_in.sql"
TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def db(disposable_db_conn) -> Iterator[tuple[object, str, str, ExternalSignInPort]]:
    conn = disposable_db_conn
    with conn.transaction(force_rollback=True):
        account_id, user_id = _id(), _id()
        suffix = uuid.uuid4().hex[:8]
        conn.execute(
            """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
               VALUES (%s, %s, %s, %s, true)""",
            (account_id, TENANT_ID, f"ext-{suffix}", f"Ext {suffix}"),
        )
        conn.execute(
            """INSERT INTO users (id, username, password_hash, account_id, password_changed_at)
               VALUES (%s, %s, 'x', %s, now() - interval '400 days')""",
            (user_id, f"pat-{suffix}@acme.test", account_id),
        )

        @contextmanager
        def connection():
            with conn.transaction():
                yield conn

        yield conn, account_id, user_id, ExternalSignInPort(connection)


def test_the_migration_is_idempotent(db) -> None:
    conn, *_ = db
    sql = MIGRATION.read_text(encoding="utf-8")

    conn.execute(sql)
    conn.execute(sql)


def test_an_attempt_is_consumed_once(db) -> None:
    conn, *_ = db
    with conn.cursor() as cur:
        repository.insert_state(
            cur,
            state_hash="state-1",
            method="acme-ticket",
            option_key="p",
            browser_binding_hash="b",
            method_data_encrypted="encrypted",
            redirect_uri="https://flow.acme.test/api/auth/external/callback",
            return_path="/jobs",
            link_user_id=None,
        )
        first = repository.consume_state(cur, "state-1")
        second = repository.consume_state(cur, "state-1")
        cur.execute(
            "UPDATE external_sign_in_states SET consumed_at = NULL,"
            " expires_at = now() - interval '1 second'"
            " WHERE state_hash = 'state-1'"
        )
        expired = repository.consume_state(cur, "state-1")

    assert first is not None and first["return_path"] == "/jobs"
    assert first["method"] == "acme-ticket" and first["method_data_encrypted"] == "encrypted"
    assert second is None
    assert expired is None


def test_an_identity_names_one_user(db) -> None:
    conn, account_id, user_id, port = db
    other = port.create_user(account_id=account_id, email=f"o-{_id()[:8]}@acme.test")
    with conn.cursor() as cur:
        row = repository.link_identity(
            cur,
            account_id=account_id,
            user_id=user_id,
            method="acme-ticket",
            option_key="p",
            issuer="https://id.acme.test",
            subject="s",
            created_by="t",
        )
        again = repository.link_identity(
            cur,
            account_id=account_id,
            user_id=user_id,
            method="acme-ticket",
            option_key="p",
            issuer="https://id.acme.test",
            subject="s",
            created_by="t",
        )
        with pytest.raises(repository.IdentityLinkedElsewhereError):
            repository.link_identity(
                cur,
                account_id=account_id,
                user_id=other,
                method="acme-ticket",
                option_key="p",
                issuer="https://id.acme.test",
                subject="s",
                created_by="t",
            )

    assert row["id"] == again["id"]
    assert (
        port.linked_user(account_id=account_id, issuer="https://id.acme.test", subject="s")
        == user_id
    )
    assert port.linked_user(account_id=_id(), issuer="https://id.acme.test", subject="s") is None


def test_an_external_session_ignores_the_local_password_age(db) -> None:
    conn, account_id, user_id, _port = db

    class _Store:
        def _column_exists(self, table: str, column: str) -> bool:
            return True

        def _table_exists(self, table: str) -> bool:
            return True

    condition = local_session_credential_filter(_Store())
    with conn.cursor() as cur:
        identity = repository.link_identity(
            cur,
            account_id=account_id,
            user_id=user_id,
            method="acme-ticket",
            option_key="p",
            issuer="https://id.acme.test",
            subject="s",
            created_by="t",
        )
        cur.execute(
            """INSERT INTO auth_sessions (user_id, account_id, session_token_hash, expires_at)
               VALUES (%s, %s, 'local-token', now() + interval '1 hour')""",
            (user_id, account_id),
        )
        repository.insert_session(
            cur,
            user_id=user_id,
            account_id=account_id,
            token_hash="external-token",
            ttl_hours=1,
            external_identity_id=identity["id"],
        )
        cur.execute(
            "SELECT s.session_token_hash FROM auth_sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.user_id = %s" + condition + " ORDER BY 1",
            (user_id,),
        )
        live = [row[0] for row in cur.fetchall()]
        repository.delete_identities_of_user(cur, user_id)
        cur.execute("SELECT count(*) FROM auth_sessions WHERE user_id = %s", (user_id,))
        remaining = cur.fetchone()[0]

    assert live == ["external-token"]
    assert remaining == 1  # unlinking removed the session the identity issued


def test_the_port_creates_users_without_a_local_password(db) -> None:
    conn, account_id, _user_id, port = db
    email = f"new-{_id()[:8]}@acme.test"

    created = port.create_user(account_id=account_id, email=email, roles=["operator"])
    roles = port.grant_roles(account_id=account_id, user_id=created, roles=["viewer"])

    row = conn.execute(
        "SELECT password_hash, account_id::text FROM users WHERE id = %s", (created,)
    ).fetchone()
    assert repository.is_unusable_password(row[0])
    assert row[1] == account_id
    assert roles == ["operator"]  # the higher role stays
    with pytest.raises(ValueError):
        port.create_user(account_id=account_id, email=email)
    with pytest.raises(LookupError):
        port.grant_roles(account_id=_id(), user_id=created, roles=["viewer"])


def test_a_session_is_either_local_or_external(db) -> None:
    conn, account_id, user_id, _port = db

    def insert(auth_method: str) -> None:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """INSERT INTO auth_sessions (
                       user_id, account_id, session_token_hash, expires_at, auth_method
                   ) VALUES (%s, %s, %s, now() + interval '1 hour', %s)""",
                (user_id, account_id, f"token-{auth_method}", auth_method),
            )

    insert("local")
    insert("external")
    with pytest.raises(Exception, match="auth_sessions_auth_method_check"):
        insert("acme-ticket")


def test_an_account_reaches_only_the_identities_linked_in_it(db) -> None:
    """A membership in account A does not hand A's administrator B's links."""
    conn, home_account, user_id, _port = db
    other_account = _id()
    suffix = uuid.uuid4().hex[:8]
    conn.execute(
        """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
           VALUES (%s, %s, %s, %s, true)""",
        (other_account, TENANT_ID, f"ext-b-{suffix}", f"Ext B {suffix}"),
    )
    # The user's home is the fixture account (B here); they are also a member of A.
    conn.execute(
        "INSERT INTO user_account_memberships (user_id, account_id) VALUES (%s, %s)",
        (user_id, other_account),
    )
    with conn.cursor() as cur:
        identity_id = repository.link_identity(
            cur,
            account_id=home_account,
            user_id=user_id,
            method="acme-ticket",
            option_key="",
            issuer="acme-ticket:https://id-b.acme.test",
            subject=f"s-{suffix}",
            created_by="t",
        )["id"]
        seen_by_a = repository.list_identities(cur, account_id=other_account)
        deleted_by_a = repository.delete_identity(cur, identity_id, account_id=other_account)
        seen_by_b = repository.list_identities(cur, account_id=home_account)

    assert identity_id not in {row["id"] for row in seen_by_a}
    assert deleted_by_a is False
    assert {row["issuer"] for row in seen_by_b if row["id"] == identity_id} == {
        "acme-ticket:https://id-b.acme.test"
    }


def _second_account(conn) -> str:
    account_id = _id()
    suffix = uuid.uuid4().hex[:8]
    conn.execute(
        """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
           VALUES (%s, %s, %s, %s, true)""",
        (account_id, TENANT_ID, f"ext-c-{suffix}", f"Ext C {suffix}"),
    )
    return account_id


def test_one_identity_may_be_linked_in_two_accounts(db) -> None:
    """The key is (account, issuer, subject): one account cannot block another."""
    conn, account_id, user_id, port = db
    other_account = _second_account(conn)
    other_user = port.create_user(account_id=other_account, email=f"c-{_id()[:8]}@acme.test")

    first = port.link_identity(
        account_id=account_id, user_id=user_id, method="acme-ticket", issuer="i", subject="s"
    )
    second = port.link_identity(
        account_id=other_account, user_id=other_user, method="acme-ticket", issuer="i", subject="s"
    )

    assert first != second
    assert port.linked_user(account_id=account_id, issuer="acme-ticket:i", subject="s") == user_id
    assert (
        port.linked_user(account_id=other_account, issuer="acme-ticket:i", subject="s")
        == other_user
    )
    with conn.cursor() as cur:
        assert len(repository.find_identities(cur, issuer="acme-ticket:i", subject="s")) == 2


def _membership(conn, user_id: str, account_id: str, role: str) -> None:
    conn.execute(
        """INSERT INTO user_account_memberships (user_id, account_id, role_id)
           SELECT %s, %s, id FROM roles WHERE key = %s""",
        (user_id, account_id, role),
    )


def test_an_account_links_and_grants_to_a_member_who_also_belongs_elsewhere(db) -> None:
    """ADR 0083 amendment (ADR 0085): the grant writes this account's role only, and the
    link signs in to this account only, so other memberships do not matter."""
    conn, home_account, user_id, port = db
    other_account = _second_account(conn)
    _membership(conn, user_id, home_account, "viewer")
    _membership(conn, user_id, other_account, "viewer")

    assert port.link_identity(
        account_id=other_account,
        user_id=user_id,
        method="acme-ticket",
        issuer="https://idp-b.acme.test",
        subject="member",
    )
    assert port.grant_roles(account_id=other_account, user_id=user_id, roles=["operator"]) == [
        "operator"
    ]

    roles = dict(
        conn.execute(
            """SELECT m.account_id::text, r.key FROM user_account_memberships m
                 JOIN roles r ON r.id = m.role_id WHERE m.user_id = %s""",
            (user_id,),
        ).fetchall()
    )
    assert roles == {home_account: "viewer", other_account: "operator"}


def test_an_administrator_of_another_account_is_never_linked_and_outsiders_never_reached(
    db,
) -> None:
    conn, home_account, user_id, port = db
    other_account = _second_account(conn)
    third_account = _second_account(conn)
    _membership(conn, user_id, home_account, "admin")
    _membership(conn, user_id, other_account, "viewer")

    with pytest.raises(PermissionError, match="admin_not_external"):
        port.link_identity(
            account_id=other_account,
            user_id=user_id,
            method="acme-ticket",
            issuer="https://idp-b.acme.test",
            subject="victim",
        )
    with pytest.raises(LookupError):
        port.link_identity(
            account_id=third_account,
            user_id=user_id,
            method="acme-ticket",
            issuer="https://idp-c.acme.test",
            subject="victim",
        )
    with pytest.raises(LookupError):
        port.grant_roles(account_id=third_account, user_id=user_id, roles=["operator"])
    count = conn.execute(
        "SELECT count(*) FROM external_identities WHERE user_id = %s", (user_id,)
    ).fetchone()[0]
    assert count == 0


def test_an_administrator_is_never_linked(db) -> None:
    conn, account_id, user_id, port = db
    # The role is held on the account membership (ADR 0085).
    conn.execute(
        """INSERT INTO user_account_memberships (user_id, account_id, role_id)
           SELECT %s, %s, id FROM roles WHERE key = 'admin'""",
        (user_id, account_id),
    )

    with pytest.raises(PermissionError, match="admin_not_external"):
        port.link_identity(
            account_id=account_id, user_id=user_id, method="acme-ticket", issuer="i", subject="a"
        )
    with conn.cursor() as cur:
        assert repository.user_holds_admin_role(cur, user_id)
