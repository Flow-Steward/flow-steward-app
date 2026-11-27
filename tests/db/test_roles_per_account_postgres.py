"""Roles per account on the disposable audit database (ADR 0085).

A person's role is held on the account membership. Alice is admin in account A and
viewer in account B; the acting account's role applies to a session, switching the
session's account changes the roles on the next read, a personal access token
carries its own account's role read at use time, a scope limit narrows in its own
account only, and every account keeps its last active administrator. Every test
runs in a transaction that is rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace

import psycopg
import pytest

from core.application.auth import resolve
from core.application.auth.membership_roles import (
    LastAccountAdminError,
    account_role_keys,
    require_other_admins,
    set_membership_role,
)
from core.application.auth.password import session_token_hash
from core.application.auth.scoped_assignments import load_scope_limit
from core.application.auth.scoped_role_ports import ScopedRoleGrantInvalidError, ScopedRolesPort
from core.application.management.approval_ports import decide_candidates
from core.infrastructure.db.logger_extensions_accounts_pat_mixin import (
    PlatformLoggerPersonalAccessTokensMixin,
)
from core.infrastructure.db.psycopg_runtime._connection import ConnectionAdapter

TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class World:
    conn: object
    a: str
    b: str
    project_a: str
    project_b: str
    alice: str  # admin in A, viewer in B
    bob: str  # admin in A
    carol: str  # admin in B, the only one
    dave: str  # operator in A and in B


class _Store(PlatformLoggerPersonalAccessTokensMixin):
    """The principal resolvers' view of the database, answered from its catalog."""

    def __init__(self, conn) -> None:
        self.conn = ConnectionAdapter(conn)
        self._raw = conn

    def _column_exists(self, table: str, column: str) -> bool:
        return bool(
            self._raw.execute(
                """SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s AND column_name = %s""",
                (table, column),
            ).fetchone()
        )

    def _table_exists(self, table: str) -> bool:
        return bool(self._raw.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0])


def _account(conn, label: str) -> tuple[str, str]:
    account_id, workspace_id, project_id = _id(), _id(), _id()
    suffix = uuid.uuid4().hex[:8]
    conn.execute(
        """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
           VALUES (%s, %s, %s, %s, true)""",
        (account_id, TENANT_ID, f"{label}-{suffix}", f"{label} {suffix}"),
    )
    conn.execute(
        """INSERT INTO workspaces (id, tenant_id, account_id, workspace_key, display_name)
           VALUES (%s, %s, %s, %s, 'Workspace')""",
        (workspace_id, TENANT_ID, account_id, f"ws-{suffix}"),
    )
    conn.execute(
        """INSERT INTO projects (id, tenant_id, workspace_id, account_id, name, description)
           VALUES (%s, %s, %s, %s, %s, '')""",
        (project_id, TENANT_ID, workspace_id, account_id, f"Project {suffix}"),
    )
    return account_id, project_id


def _person(conn, name: str, home: str, roles: dict[str, str]) -> str:
    user_id = _id()
    conn.execute(
        """INSERT INTO users (id, username, password_hash, account_id)
           VALUES (%s, %s, 'x', %s)""",
        (user_id, f"{name}-{uuid.uuid4().hex[:8]}@example.invalid", home),
    )
    for account_id, role in roles.items():
        conn.execute(
            """INSERT INTO user_account_memberships (user_id, account_id, is_default, role_id)
               VALUES (%s, %s, %s, (SELECT id FROM roles WHERE key = %s))""",
            (user_id, account_id, account_id == home, role),
        )
    return user_id


@pytest.fixture
def world(disposable_db_conn) -> Iterator[World]:
    conn = disposable_db_conn
    with conn.transaction(force_rollback=True):
        a, project_a = _account(conn, "a")
        b, project_b = _account(conn, "b")
        yield World(
            conn=conn,
            a=a,
            b=b,
            project_a=project_a,
            project_b=project_b,
            alice=_person(conn, "alice", a, {a: "admin", b: "viewer"}),
            bob=_person(conn, "bob", a, {a: "admin"}),
            carol=_person(conn, "carol", b, {b: "admin"}),
            dave=_person(conn, "dave", a, {a: "operator", b: "operator"}),
        )


def _bl(world: World) -> SimpleNamespace:
    return SimpleNamespace(conn=world.conn)


def test_the_role_is_the_acting_accounts(world: World) -> None:
    bl = _bl(world)

    assert account_role_keys(bl, world.alice, world.a) == ["admin"]
    assert account_role_keys(bl, world.alice, world.b) == ["viewer"]
    # No account, an unknown account or no membership: no role, never a fallback.
    assert account_role_keys(bl, world.alice, "") == []
    assert account_role_keys(bl, world.alice, _id()) == []
    assert account_role_keys(bl, world.bob, world.b) == []


def test_an_inactive_account_grants_no_role(world: World) -> None:
    world.conn.execute("UPDATE accounts SET is_active = false WHERE id = %s", (world.b,))

    assert account_role_keys(_bl(world), world.alice, world.b) == []
    assert account_role_keys(_bl(world), world.alice, world.a) == ["admin"]


def test_switching_the_session_account_changes_the_roles(world: World) -> None:
    token = f"session-{uuid.uuid4().hex}"
    world.conn.execute(
        """INSERT INTO auth_sessions (user_id, account_id, session_token_hash, expires_at)
           VALUES (%s, %s, %s, now() + interval '1 hour')""",
        (world.alice, world.a, session_token_hash(token)),
    )
    store = _Store(world.conn)

    in_a = resolve.get_user_by_session_token(store, token)
    assert in_a is not None
    assert in_a["account_id"] == world.a
    assert in_a["roles"] == ["admin"]

    # What POST /api/auth/switch-account does after its membership check.
    world.conn.execute(
        "UPDATE auth_sessions SET account_id = %s WHERE session_token_hash = %s",
        (world.b, session_token_hash(token)),
    )
    in_b = resolve.get_user_by_session_token(store, token)
    assert in_b is not None
    assert in_b["account_id"] == world.b
    assert in_b["roles"] == ["viewer"]


def test_a_personal_token_carries_its_own_accounts_role_at_use_time(world: World) -> None:
    store = _Store(world.conn)
    minted_in_a = store.create_pat(user_id=world.alice, account_id=world.a, scopes=["admin:all"])
    minted_in_b = store.create_pat(user_id=world.alice, account_id=world.b, scopes=["admin:all"])

    principal_a = store.resolve_pat(minted_in_a["raw_token"])
    principal_b = store.resolve_pat(minted_in_b["raw_token"])
    assert (principal_a["account_id"], principal_a["roles"]) == (world.a, ["admin"])
    assert (principal_b["account_id"], principal_b["roles"]) == (world.b, ["viewer"])

    # A downgrade in A reaches the token minted in A on its next use, and only it.
    with world.conn.cursor() as cur:
        set_membership_role(cur, user_id=world.alice, account_id=world.a, role="operator")
    assert store.resolve_pat(minted_in_a["raw_token"])["roles"] == ["operator"]
    assert store.resolve_pat(minted_in_b["raw_token"])["roles"] == ["viewer"]

    # A removed membership leaves the token of that account without a role.
    world.conn.execute(
        "DELETE FROM user_account_memberships WHERE user_id = %s AND account_id = %s",
        (world.alice, world.b),
    )
    assert store.resolve_pat(minted_in_b["raw_token"])["roles"] == []


def test_a_global_role_row_is_refused(world: World) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), world.conn.transaction():
        world.conn.execute(
            "INSERT INTO user_roles (user_id, role_id)"
            " SELECT %s, id FROM roles WHERE key = 'admin'",
            (world.dave,),
        )


def _port(world: World) -> ScopedRolesPort:
    @contextmanager
    def connection():
        with world.conn.transaction():
            yield world.conn

    return ScopedRolesPort(connection)


def test_a_scope_limit_narrows_in_its_own_account_only(world: World) -> None:
    port = _port(world)

    port.replace_user_grants(
        world.b,
        world.dave,
        [{"scope": "project", "id": world.project_b, "role": "viewer"}],
        created_by="carol",
    )
    port.replace_user_grants(
        world.a,
        world.dave,
        [{"scope": "project", "id": world.project_a, "role": "operator"}],
        created_by="bob",
    )

    limit_b = load_scope_limit(world.conn, account_id=world.b, user_id=world.dave)
    limit_a = load_scope_limit(world.conn, account_id=world.a, user_id=world.dave)
    assert limit_b is not None and limit_b.project_ids == frozenset({world.project_b})
    assert limit_a is not None and limit_a.project_ids == frozenset({world.project_a})

    # Lifting the limit in B keeps A's limit and A's scoped rows.
    port.lift_limit(world.b, world.dave, created_by="carol")
    assert load_scope_limit(world.conn, account_id=world.b, user_id=world.dave) is None
    limit_a = load_scope_limit(world.conn, account_id=world.a, user_id=world.dave)
    assert limit_a is not None and limit_a.project_ids == frozenset({world.project_a})


def test_an_administrator_is_never_limited_where_they_administer(world: World) -> None:
    port = _port(world)
    grant = [{"scope": "project", "id": world.project_b, "role": "viewer"}]

    with pytest.raises(ScopedRoleGrantInvalidError):
        port.replace_user_grants(
            world.a,
            world.alice,
            [{"scope": "project", "id": world.project_a, "role": "viewer"}],
            created_by="bob",
        )
    # Alice is a viewer in B, so B may limit her there.
    port.replace_user_grants(world.b, world.alice, grant, created_by="carol")
    assert load_scope_limit(world.conn, account_id=world.b, user_id=world.alice) is not None
    # Candidates of B are its non-admin members, whatever their home account.
    people = {row["user_id"] for row in port.candidates(world.b) if row.get("kind") == "user"}
    assert people == {world.alice, world.dave}


def test_approval_candidates_are_members_by_membership(world: World) -> None:
    found = {row["user_id"] for row in decide_candidates(world.conn, world.b)}

    # Dave lives in A and is an operator member of B; Alice is a viewer in B.
    assert world.dave in found
    assert world.carol in found
    assert world.alice not in found


def test_the_last_administrator_is_kept_per_account(world: World) -> None:
    with world.conn.cursor() as cur:
        # Carol is B's only administrator.
        with pytest.raises(LastAccountAdminError):
            require_other_admins(cur, user_id=world.carol, account_ids=[world.b])
        # Alice is not an administrator of B: nothing to keep there.
        require_other_admins(cur, user_id=world.alice, account_ids=[world.b])
        # A keeps Bob.
        require_other_admins(cur, user_id=world.alice, account_ids=[world.a])
        cur.execute("UPDATE users SET is_active = false WHERE id = %s", (world.bob,))
        with pytest.raises(LastAccountAdminError):
            require_other_admins(cur, user_id=world.alice, account_ids=[world.a])
