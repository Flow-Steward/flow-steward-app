"""Workspace, project and workflow grants on the disposable audit database (ADR 0076 §8a).

A workspace grant covers every live project of the workspace, a project created after
the grant included; a workflow grant must name a workflow of the account linked to the
named project; the limit is read only in the account it was set in. Every test runs in
a transaction that is rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.application.auth.scoped_assignments import (
    load_account_scope_limits,
    load_scope_limit,
)
from core.application.auth.scoped_role_ports import ScopedRolesPort

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261126210000_multilevel_scoped_roles.sql"
TENANT_ID = "00000000-0000-0000-0000-000000000001"


@dataclass(frozen=True)
class Account:
    id: str
    accounting: str
    marketing: str
    ledger: str
    payroll: str
    campaigns: str
    invoice_flow: str
    report_flow: str
    person: str
    admin: str


def _id() -> str:
    return str(uuid.uuid4())


def _workspace(conn, account_id: str, name: str) -> str:
    workspace_id = _id()
    conn.execute(
        """INSERT INTO workspaces (id, tenant_id, account_id, workspace_key, display_name)
           VALUES (%s, %s, %s, %s, %s)""",
        (workspace_id, TENANT_ID, account_id, f"ws-{uuid.uuid4().hex[:8]}", name),
    )
    return workspace_id


def _project(conn, account_id: str, workspace_id: str, name: str) -> str:
    project_id = _id()
    conn.execute(
        """INSERT INTO projects (id, tenant_id, workspace_id, account_id, name, description)
           VALUES (%s, %s, %s, %s, %s, '')""",
        (project_id, TENANT_ID, workspace_id, account_id, name),
    )
    return project_id


def _workflow(conn, account_id: str, project_ids: list[str], name: str) -> str:
    key = f"wf-{name.lower()}-{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO registry_workflows (workflow_key, display_name, account_id) VALUES (%s, %s, %s)",
        (key, name, account_id),
    )
    for project_id in project_ids:
        conn.execute(
            "INSERT INTO project_workflows (project_id, workflow_id) VALUES (%s, %s)",
            (project_id, key),
        )
    return key


def _person(conn, account_id: str, role: str) -> str:
    user_id = _id()
    conn.execute(
        "INSERT INTO users (id, username, password_hash, account_id) VALUES (%s, %s, 'x', %s)",
        (user_id, f"{role}-{uuid.uuid4().hex[:8]}@example.invalid", account_id),
    )
    # The role is held on the account membership (ADR 0085).
    conn.execute(
        """INSERT INTO user_account_memberships (user_id, account_id, is_default, role_id)
           SELECT %s, %s, true, id FROM roles WHERE key = %s""",
        (user_id, account_id, role),
    )
    return user_id


def _account(conn) -> Account:
    account_id = _id()
    suffix = uuid.uuid4().hex[:8]
    conn.execute(
        """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
           VALUES (%s, %s, %s, %s, true)""",
        (account_id, TENANT_ID, f"multi-{suffix}", f"Multi {suffix}"),
    )
    accounting = _workspace(conn, account_id, "Accounting")
    marketing = _workspace(conn, account_id, "Marketing")
    ledger = _project(conn, account_id, accounting, "Ledger")
    payroll = _project(conn, account_id, accounting, "Payroll")
    campaigns = _project(conn, account_id, marketing, "Campaigns")
    return Account(
        id=account_id,
        accounting=accounting,
        marketing=marketing,
        ledger=ledger,
        payroll=payroll,
        campaigns=campaigns,
        invoice_flow=_workflow(conn, account_id, [ledger, campaigns], "Invoice"),
        report_flow=_workflow(conn, account_id, [campaigns], "Report"),
        person=_person(conn, account_id, "operator"),
        admin=_person(conn, account_id, "admin"),
    )


@pytest.fixture
def db(disposable_db_conn) -> Iterator[tuple[object, Account, Account, ScopedRolesPort]]:
    conn = disposable_db_conn
    with conn.transaction(force_rollback=True):
        a, b = _account(conn), _account(conn)

        @contextmanager
        def connection():
            with conn.transaction():
                yield conn

        yield conn, a, b, ScopedRolesPort(connection)


def _scoped_rows(conn, user_id: str) -> list[tuple]:
    return conn.execute(
        """SELECT COALESCE(ur.workspace_id::text, ''), COALESCE(ur.project_id::text, ''),
                  COALESCE(ur.workflow_id, ''), r.key
             FROM user_roles ur JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_id = %s
              AND (ur.workspace_id IS NOT NULL OR ur.project_id IS NOT NULL)
            ORDER BY 1, 2, 3""",
        (user_id,),
    ).fetchall()


def test_the_migration_is_idempotent(db) -> None:
    conn, *_ = db
    sql = MIGRATION.read_text(encoding="utf-8")
    conn.execute("DROP INDEX public.idx_user_roles_workspace")
    conn.execute("DROP INDEX public.idx_user_roles_workflow")

    conn.execute(sql)
    conn.execute(sql)

    names = {
        row[0]
        for row in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'user_roles'"
        ).fetchall()
    }
    assert {"idx_user_roles_workspace", "idx_user_roles_workflow"} <= names


def test_all_three_scopes_are_stored_and_read_back(db) -> None:
    conn, a, _, port = db

    port.replace_user_grants(
        a.id,
        a.person,
        [
            {"scope": "workspace", "id": a.accounting, "role": "viewer"},
            {"scope": "project", "id": a.payroll, "role": "operator"},
            {"scope": "workflow", "id": a.report_flow, "project_id": a.campaigns, "role": "viewer"},
        ],
        created_by="alice",
    )

    assert _scoped_rows(conn, a.person) == sorted(
        [
            ("", a.campaigns, a.report_flow, "viewer"),
            ("", a.payroll, "", "operator"),
            (a.accounting, "", "", "viewer"),
        ]
    )
    limit = load_scope_limit(conn, account_id=a.id, user_id=a.person)
    assert limit is not None
    assert limit.project_ids == {a.ledger, a.payroll, a.campaigns}
    assert limit.workspace_ids == {a.accounting, a.marketing}
    # Most specific grant: project operator wins over the workspace viewer.
    assert limit.project_level_roles(a.payroll) == {"operator"}
    assert limit.project_level_roles(a.ledger) == {"viewer"}
    assert limit.workflow_only_projects() == {a.campaigns: frozenset({a.report_flow})}

    grants = port.list_grants(a.id, user_id=a.person)
    assert [(g["scope"], g["id"], g["name"], g["role"]) for g in grants] == [
        ("workspace", a.accounting, "Accounting", "viewer"),
        ("project", a.payroll, "Payroll", "operator"),
        ("workflow", a.report_flow, "Report", "viewer"),
    ]
    assert grants[2]["project_id"] == a.campaigns
    assert grants[2]["workspace_name"] == "Marketing"
    assert {g["scope"] for g in port.list_grants(a.id, project_id=a.campaigns)} == {"workflow"}


def test_a_project_created_after_a_workspace_grant_is_covered(db) -> None:
    conn, a, _, port = db
    port.replace_user_grants(
        a.id,
        a.person,
        [{"scope": "workspace", "id": a.accounting, "role": "viewer"}],
        created_by="x",
    )

    later = _project(conn, a.id, a.accounting, "Later")

    limit = load_scope_limit(conn, account_id=a.id, user_id=a.person)
    assert limit is not None
    assert limit.project_ids == {a.ledger, a.payroll, later}
    assert a.campaigns not in limit.project_ids
    assert a.marketing not in limit.workspace_ids


@pytest.mark.parametrize(
    "grant",
    [
        "workflow_not_linked_to_the_project",
        "workflow_of_another_account",
        "workspace_of_another_account",
        "project_of_another_account",
        "unknown_workflow",
    ],
)
def test_a_place_outside_the_account_is_refused_with_nothing_written(db, grant: str) -> None:
    conn, a, b, port = db
    item = {
        "workflow_not_linked_to_the_project": {
            "scope": "workflow",
            "id": a.report_flow,
            "project_id": a.ledger,
        },
        "workflow_of_another_account": {
            "scope": "workflow",
            "id": b.invoice_flow,
            "project_id": a.ledger,
        },
        "workspace_of_another_account": {"scope": "workspace", "id": b.accounting},
        "project_of_another_account": {"scope": "project", "id": b.ledger},
        "unknown_workflow": {"scope": "workflow", "id": "wf-none", "project_id": a.ledger},
    }[grant]

    with pytest.raises(LookupError):
        port.replace_user_grants(a.id, a.person, [{**item, "role": "viewer"}], created_by="m")
    assert _scoped_rows(conn, a.person) == []
    assert load_scope_limit(conn, account_id=a.id, user_id=a.person) is None


def test_a_limit_applies_only_in_the_account_it_was_set_in(db) -> None:
    conn, a, b, port = db
    port.replace_user_grants(
        a.id, a.person, [{"scope": "project", "id": a.ledger, "role": "viewer"}], created_by="x"
    )

    assert load_scope_limit(conn, account_id=b.id, user_id=a.person) is None
    assert set(load_account_scope_limits(conn, account_id=a.id)) == {a.person}
    assert load_account_scope_limits(conn, account_id=b.id) == {}


def test_deleting_a_workspace_or_workflow_removes_its_grants_but_keeps_the_limit(db) -> None:
    conn, a, _, port = db
    port.replace_user_grants(
        a.id,
        a.person,
        [
            {"scope": "workspace", "id": a.marketing, "role": "viewer"},
            {"scope": "workflow", "id": a.invoice_flow, "project_id": a.ledger, "role": "viewer"},
        ],
        created_by="x",
    )

    conn.execute("DELETE FROM project_workflows WHERE workflow_id = %s", (a.invoice_flow,))
    conn.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (a.invoice_flow,))
    conn.execute("DELETE FROM projects WHERE workspace_id = %s", (a.marketing,))
    conn.execute("DELETE FROM workspaces WHERE id = %s", (a.marketing,))

    assert _scoped_rows(conn, a.person) == []
    limit = load_scope_limit(conn, account_id=a.id, user_id=a.person)
    assert limit is not None and limit.project_ids == frozenset()


def test_candidates_offer_workspaces_projects_and_linked_workflows(db) -> None:
    _, a, _, port = db

    found = port.candidates(a.id)

    assert {i["workspace_id"] for i in found if i["kind"] == "workspace"} == {
        a.accounting,
        a.marketing,
    }
    projects = {i["project_id"]: i["workspace_id"] for i in found if i["kind"] == "project"}
    assert projects == {a.ledger: a.accounting, a.payroll: a.accounting, a.campaigns: a.marketing}
    assert sorted(
        (i["workflow_id"], i["project_id"]) for i in found if i["kind"] == "workflow"
    ) == sorted(
        [(a.invoice_flow, a.ledger), (a.invoice_flow, a.campaigns), (a.report_flow, a.campaigns)]
    )
    assert a.admin not in {i.get("user_id") for i in found}


def test_lift_removes_every_scoped_row(db) -> None:
    conn, a, _, port = db
    port.replace_user_grants(
        a.id,
        a.person,
        [
            {"scope": "workspace", "id": a.accounting, "role": "viewer"},
            {"scope": "workflow", "id": a.report_flow, "project_id": a.campaigns, "role": "viewer"},
        ],
        created_by="x",
    )

    port.lift_limit(a.id, a.person, created_by="x")

    assert _scoped_rows(conn, a.person) == []
    assert load_scope_limit(conn, account_id=a.id, user_id=a.person) is None


def test_a_workflow_grant_reads_the_workflow_of_its_own_account(db) -> None:
    """Another account's workflow with the same id never names or keeps a grant (ADR 0020, C4b)."""
    conn, a, b, port = db
    conn.execute(
        "INSERT INTO registry_workflows (workflow_key, display_name, account_id) VALUES (%s, %s, %s)",
        (a.report_flow, "Other account", b.id),
    )
    port.replace_user_grants(
        a.id,
        a.person,
        [{"scope": "workflow", "id": a.report_flow, "project_id": a.campaigns, "role": "viewer"}],
        created_by="x",
    )

    grants = port.list_grants(a.id, user_id=a.person)
    assert [(g["id"], g["name"]) for g in grants] == [(a.report_flow, "Report")]

    conn.execute(
        "UPDATE registry_workflows SET deleted_at = now() WHERE account_id = %s AND workflow_id = %s",
        (a.id, a.report_flow),
    )

    assert port.list_grants(a.id, user_id=a.person) == []
    limit = load_scope_limit(conn, account_id=a.id, user_id=a.person)
    assert limit is not None and limit.project_ids == frozenset()
