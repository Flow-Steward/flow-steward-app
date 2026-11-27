"""Scoped role limits on the disposable audit database (ADR 0076 section 8, ADR 0082).

The scoped roles port writes the limit row and the project rows in one transaction,
refuses what does not belong to the caller's account, and a limited person stays
limited when the last listed project is deleted. Every test runs in a transaction
that is rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.application.auth.scoped_assignments import ScopeLimit, load_scope_limit
from core.application.auth.scoped_role_ports import ScopedRolesPort

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261126120000_scoped_role_users.sql"
#: Keys the limit per account (ADR 0085); it follows the table's creation.
PER_ACCOUNT_MIGRATION = ROOT / "db/migrations/20261127170000_roles_per_account.sql"
TENANT_ID = "00000000-0000-0000-0000-000000000001"


@dataclass(frozen=True)
class Account:
    id: str
    projects: tuple[str, str]
    operator: str
    viewer: str
    admin: str


def _id() -> str:
    return str(uuid.uuid4())


def _account(conn) -> Account:
    account_id, workspace_id = _id(), _id()
    suffix = uuid.uuid4().hex[:8]
    conn.execute(
        """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
           VALUES (%s, %s, %s, %s, true)""",
        (account_id, TENANT_ID, f"scoped-{suffix}", f"Scoped {suffix}"),
    )
    conn.execute(
        """INSERT INTO workspaces (id, tenant_id, account_id, workspace_key, display_name)
           VALUES (%s, %s, %s, %s, %s)""",
        (workspace_id, TENANT_ID, account_id, f"ws-{suffix}", "Workspace"),
    )
    projects = []
    for name in ("Alpha", "Beta"):
        project_id = _id()
        conn.execute(
            """INSERT INTO projects (id, tenant_id, workspace_id, account_id, name, description)
               VALUES (%s, %s, %s, %s, %s, '')""",
            (project_id, TENANT_ID, workspace_id, account_id, f"{name} {suffix}"),
        )
        projects.append(project_id)
    people = {}
    for role in ("operator", "viewer", "admin"):
        user_id = _id()
        conn.execute(
            """INSERT INTO users (id, username, password_hash, account_id)
               VALUES (%s, %s, 'x', %s)""",
            (user_id, f"{role}-{suffix}@example.invalid", account_id),
        )
        # The role is held on the account membership (ADR 0085).
        conn.execute(
            """INSERT INTO user_account_memberships (user_id, account_id, is_default, role_id)
               SELECT %s, %s, true, id FROM roles WHERE key = %s""",
            (user_id, account_id, role),
        )
        people[role] = user_id
    return Account(account_id, (projects[0], projects[1]), **people)


@pytest.fixture
def db(disposable_db_conn) -> Iterator[tuple[object, Account, Account, ScopedRolesPort]]:
    conn = disposable_db_conn
    with conn.transaction(force_rollback=True):
        a, b = _account(conn), _account(conn)

        @contextmanager
        def connection():
            # A savepoint per call: a refused write leaves nothing behind, like the
            # composition root's transaction_connection.
            with conn.transaction():
                yield conn

        yield conn, a, b, ScopedRolesPort(connection)


def _rows(conn, sql: str, params: tuple) -> list[tuple]:
    return conn.execute(sql, params).fetchall()


def _limit_rows(conn, user_id: str) -> list[tuple]:
    return _rows(
        conn,
        "SELECT account_id::text, created_by FROM scoped_role_users WHERE user_id = %s",
        (user_id,),
    )


def _project_rows(conn, user_id: str) -> list[tuple]:
    return _rows(
        conn,
        """SELECT ur.project_id::text, r.key FROM user_roles ur JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_id = %s AND ur.project_id IS NOT NULL ORDER BY 1""",
        (user_id,),
    )


def _global_roles(conn, user_id: str) -> list[str]:
    """The person's account roles: the roles of their memberships (ADR 0085)."""
    return [
        row[0]
        for row in _rows(
            conn,
            """SELECT r.key FROM user_account_memberships m JOIN roles r ON r.id = m.role_id
                WHERE m.user_id = %s""",
            (user_id,),
        )
    ]


def test_the_migration_is_idempotent_on_a_database_without_and_with_the_table(db) -> None:
    conn, a, _, port = db
    sql = MIGRATION.read_text(encoding="utf-8")
    conn.execute("DROP TABLE public.scoped_role_users")
    conn.execute("DROP INDEX public.idx_user_roles_project")

    conn.execute(sql)
    conn.execute(sql)
    conn.execute(PER_ACCOUNT_MIGRATION.read_text(encoding="utf-8"))

    port.replace_user_grants(a.id, a.viewer, [], created_by="alice")
    assert _limit_rows(conn, a.viewer) == [(a.id, "alice")]


def test_a_write_stores_the_limit_row_and_the_project_rows_together(db) -> None:
    conn, a, _, port = db
    first, second = a.projects

    port.replace_user_grants(
        a.id,
        a.operator,
        [
            {"scope": "project", "id": first, "role": "operator"},
            {"scope": "project", "id": second, "role": "viewer"},
        ],
        created_by="alice",
    )

    assert _limit_rows(conn, a.operator) == [(a.id, "alice")]
    assert _project_rows(conn, a.operator) == sorted([(first, "operator"), (second, "viewer")])
    assert _global_roles(conn, a.operator) == ["operator"]
    limit = load_scope_limit(conn, account_id=a.id, user_id=a.operator)
    assert limit is not None
    assert limit.project_roles == {first: frozenset({"operator"}), second: frozenset({"viewer"})}
    assert limit.workspace_roles == {}
    assert limit.workflow_roles == {}
    grants = port.list_grants(a.id, user_id=a.operator)
    assert [(item["project_id"], item["role"]) for item in grants] == [
        (first, "operator"),
        (second, "viewer"),
    ]
    assert port.list_grants(a.id, project_id=second) == [grants[1]]

    port.replace_user_grants(
        a.id, a.operator, [{"scope": "project", "id": second, "role": "operator"}], created_by="bob"
    )
    assert _project_rows(conn, a.operator) == [(second, "operator")]
    # The limit row names who set the current limit: bob's write replaced alice's.
    assert _limit_rows(conn, a.operator) == [(a.id, "bob")]


def test_a_person_without_a_limit_row_has_no_limit(db) -> None:
    conn, a, _, _ = db

    assert load_scope_limit(conn, account_id=a.id, user_id=a.viewer) is None


@pytest.mark.parametrize("whose", ["other_account_user", "admin", "unknown_user"])
def test_a_person_who_cannot_be_limited_is_refused_with_nothing_written(db, whose: str) -> None:
    conn, a, b, port = db
    user = {"other_account_user": b.viewer, "admin": a.admin, "unknown_user": _id()}[whose]
    expected = ValueError if whose == "admin" else LookupError

    with pytest.raises(expected):
        port.replace_user_grants(
            a.id,
            user,
            [{"scope": "project", "id": a.projects[0], "role": "viewer"}],
            created_by="alice",
        )
    assert _limit_rows(conn, user) == []
    assert _project_rows(conn, user) == []


@pytest.mark.parametrize("which", ["other_account_project", "unknown_project"])
def test_a_project_outside_the_account_is_refused_with_nothing_written(db, which: str) -> None:
    conn, a, b, port = db
    project = {"other_account_project": b.projects[0], "unknown_project": _id()}[which]
    port.replace_user_grants(
        a.id,
        a.viewer,
        [{"scope": "project", "id": a.projects[0], "role": "viewer"}],
        created_by="alice",
    )

    with pytest.raises(LookupError):
        port.replace_user_grants(
            a.id,
            a.viewer,
            [
                {"scope": "project", "id": a.projects[1], "role": "viewer"},
                {"scope": "project", "id": project, "role": "viewer"},
            ],
            created_by="mallory",
        )
    assert _project_rows(conn, a.viewer) == [(a.projects[0], "viewer")]
    assert _limit_rows(conn, a.viewer) == [(a.id, "alice")]


@pytest.mark.parametrize(
    "grant",
    [
        {"role": "viewer", "workspace_id": "x"},
        {"role": "viewer", "workflow_id": "wf-1"},
        {"role": "admin"},
        {"role": "unknown"},
        {"role": "viewer", "scope": "account"},
    ],
)
def test_an_unknown_scope_or_role_is_refused(db, grant: dict) -> None:
    conn, a, _, port = db

    with pytest.raises(ValueError):
        port.replace_user_grants(
            a.id, a.viewer, [{"scope": "project", "id": a.projects[0], **grant}], created_by="alice"
        )
    assert _limit_rows(conn, a.viewer) == []


def test_lift_limit_restores_the_global_role(db) -> None:
    conn, a, _, port = db
    port.replace_user_grants(
        a.id,
        a.operator,
        [{"scope": "project", "id": a.projects[0], "role": "viewer"}],
        created_by="alice",
    )

    port.lift_limit(a.id, a.operator, created_by="alice")

    assert _limit_rows(conn, a.operator) == []
    assert _project_rows(conn, a.operator) == []
    assert _global_roles(conn, a.operator) == ["operator"]
    assert load_scope_limit(conn, account_id=a.id, user_id=a.operator) is None


def test_lift_limit_refuses_another_accounts_person(db) -> None:
    conn, a, b, port = db
    port.replace_user_grants(b.id, b.viewer, [], created_by="bob")

    with pytest.raises(LookupError):
        port.lift_limit(a.id, b.viewer, created_by="mallory")
    assert _limit_rows(conn, b.viewer) == [(b.id, "bob")]


def test_deleting_the_last_project_keeps_the_person_limited(db) -> None:
    conn, a, _, port = db
    port.replace_user_grants(
        a.id,
        a.viewer,
        [{"scope": "project", "id": a.projects[0], "role": "viewer"}],
        created_by="alice",
    )

    conn.execute("DELETE FROM projects WHERE id = %s", (a.projects[0],))

    assert _project_rows(conn, a.viewer) == []
    assert _limit_rows(conn, a.viewer) == [(a.id, "alice")]
    assert load_scope_limit(conn, account_id=a.id, user_id=a.viewer) == ScopeLimit()


def test_deleting_the_person_removes_the_limit_row(db) -> None:
    conn, a, _, port = db
    port.replace_user_grants(a.id, a.viewer, [], created_by="alice")

    conn.execute("DELETE FROM users WHERE id = %s", (a.viewer,))

    assert _limit_rows(conn, a.viewer) == []


def test_candidates_are_non_admin_people_and_projects_of_the_account(db) -> None:
    _, a, _, port = db

    found = port.candidates(a.id)

    assert {item["user_id"] for item in found if item["kind"] == "user"} == {a.operator, a.viewer}
    assert {item["project_id"] for item in found if item["kind"] == "project"} == set(a.projects)


def test_list_limited_users_includes_a_person_with_no_project(db) -> None:
    _, a, b, port = db
    port.replace_user_grants(a.id, a.viewer, [], created_by="alice")
    port.replace_user_grants(b.id, b.viewer, [], created_by="bob")

    assert [item["user_id"] for item in port.list_limited_users(a.id)] == [a.viewer]
    assert port.list_grants(a.id) == []


# --- review fixes: audit in the write's transaction, last setter, self-change ------------


def _audit_rows(conn, action: str, user_id: str) -> list[tuple]:
    return _rows(
        conn,
        """SELECT agent, payload ->> 'username', result ->> 'user_id', result
             FROM platform_action_log
            WHERE action_type = %s AND result ->> 'user_id' = %s
            ORDER BY "timestamp", id""",
        (f"auth_{action}", user_id),
    )


def _username(conn, user_id: str) -> str:
    return _rows(conn, "SELECT username FROM users WHERE id = %s", (user_id,))[0][0]


def test_a_repeated_write_records_the_last_setter_and_time(db) -> None:
    conn, a, _, port = db
    port.replace_user_grants(a.id, a.viewer, [], created_by="alice")
    conn.execute(
        "UPDATE scoped_role_users SET created_at = now() - interval '1 day' WHERE user_id = %s",
        (a.viewer,),
    )

    port.replace_user_grants(a.id, a.viewer, [], created_by="bob")

    rows = _rows(
        conn,
        """SELECT created_by, created_at > now() - interval '1 hour'
             FROM scoped_role_users WHERE user_id = %s""",
        (a.viewer,),
    )
    assert rows == [("bob", True)]


def test_each_write_records_an_audit_entry_naming_the_person(db) -> None:
    conn, a, _, port = db
    first = a.projects[0]

    port.replace_user_grants(
        a.id, a.viewer, [{"scope": "project", "id": first, "role": "viewer"}], created_by="alice"
    )
    port.lift_limit(a.id, a.viewer, created_by="bob")

    replaced = _audit_rows(conn, "scoped_roles_replace", a.viewer)
    lifted = _audit_rows(conn, "scoped_roles_lift", a.viewer)
    username = _username(conn, a.viewer)
    assert [(agent, name, user) for agent, name, user, _ in replaced] == [
        ("alice", username, a.viewer)
    ]
    assert replaced[0][3]["grants"] == [{"scope": "project", "id": first, "role": "viewer"}]
    assert replaced[0][3]["account_id"] == a.id
    assert [(agent, name, user) for agent, name, user, _ in lifted] == [("bob", username, a.viewer)]
    assert lifted[0][3]["was_limited"] is True


@pytest.mark.parametrize("operation", ["replace", "lift"])
def test_a_failed_audit_write_undoes_the_change(db, operation: str) -> None:
    conn, a, _, port = db
    port.replace_user_grants(
        a.id,
        a.viewer,
        [{"scope": "project", "id": a.projects[0], "role": "viewer"}],
        created_by="alice",
    )
    conn.execute(
        """ALTER TABLE platform_action_log
           ADD CONSTRAINT test_refuse_scoped_roles_audit
           CHECK (action_type NOT LIKE 'auth_scoped_roles_%%') NOT VALID"""
    )

    with pytest.raises(Exception):  # noqa: B017 - the database refuses the audit entry
        if operation == "replace":
            port.replace_user_grants(
                a.id,
                a.viewer,
                [{"scope": "project", "id": a.projects[1], "role": "viewer"}],
                created_by="bob",
            )
        else:
            port.lift_limit(a.id, a.viewer, created_by="bob")

    assert _limit_rows(conn, a.viewer) == [(a.id, "alice")]
    assert _project_rows(conn, a.viewer) == [(a.projects[0], "viewer")]


def test_a_person_cannot_change_their_own_limit(db) -> None:
    conn, a, _, port = db
    port.replace_user_grants(
        a.id,
        a.viewer,
        [{"scope": "project", "id": a.projects[0], "role": "viewer"}],
        created_by="alice",
    )

    with pytest.raises(ValueError):
        port.replace_user_grants(
            a.id,
            a.viewer,
            [{"scope": "project", "id": a.projects[1], "role": "viewer"}],
            created_by="viewer",
            acting_user_id=a.viewer,
        )
    with pytest.raises(ValueError):
        port.lift_limit(a.id, a.viewer, created_by="viewer", acting_user_id=a.viewer)

    assert _limit_rows(conn, a.viewer) == [(a.id, "alice")]
    assert _project_rows(conn, a.viewer) == [(a.projects[0], "viewer")]


def test_promoting_a_limited_person_to_admin_lifts_the_limit_and_audits_it(db) -> None:
    from core.application.auth.role_assignments import (
        global_assignments,
        replace_user_role_assignments,
    )
    from core.application.auth.user_admin.repository import _lift_scope_limit

    conn, a, _, port = db
    port.replace_user_grants(
        a.id,
        a.operator,
        [{"scope": "project", "id": a.projects[0], "role": "viewer"}],
        created_by="alice",
    )

    with conn.cursor() as cur:
        replace_user_role_assignments(
            cur, user_id=a.operator, account_id=a.id, assignments=global_assignments(["admin"])
        )
        lifted = _lift_scope_limit(
            cur, a.operator, a.id, actor="root", username=_username(conn, a.operator)
        )

    assert lifted is True
    assert _limit_rows(conn, a.operator) == []
    assert _project_rows(conn, a.operator) == []
    assert _global_roles(conn, a.operator) == ["admin"]
    audit = _audit_rows(conn, "scoped_roles_lift", a.operator)
    assert [(agent, user) for agent, _, user, _ in audit] == [("root", a.operator)]
    assert audit[0][3]["reason"] == "promoted_to_admin"
    with conn.cursor() as cur:
        assert _lift_scope_limit(cur, a.viewer, a.id, actor="root", username="") is False
