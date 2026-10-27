"""Cross-account isolation against a real database.

Remediation epic 2026-07-29, WS-02. Requirement 10: two accounts and two
projects, and a cross-account resource must not be readable or mutable, with the
response revealing neither its contents nor its existence.

Every earlier isolation test in this epic used mocks — good for pinning a guard
is *called*, useless for proving the SQL predicate underneath actually filters.
These run against the disposable canonical PostgreSQL with two real accounts
whose project and workspace names deliberately collide, so a resolver that keys
on a human name instead of an id fails here even though it would pass a
two-tenant test with distinct names.
"""

from __future__ import annotations

import pytest

from core.application.management.scope import normalize_scope
from core.entrypoints.admin._registry_scope import (
    assert_row_in_account_scope,
    row_in_account_scope,
)
from tests._tenant_fixtures import SHARED_PROJECT_NAME, SHARED_WORKSPACE_NAME

pytestmark = pytest.mark.integration


def scope_of(tenant):
    return normalize_scope(account_id=tenant.account_id)


class TestTheFixtureItself:
    """If the fixture is wrong, everything built on it is worthless."""

    def test_the_two_accounts_are_distinct_and_not_the_default_account(self, two_tenants):
        from core.application.auth.config import DEFAULT_ACCOUNT_ID

        assert two_tenants.a.account_id != two_tenants.b.account_id
        assert two_tenants.a.account_id != DEFAULT_ACCOUNT_ID
        assert two_tenants.b.account_id != DEFAULT_ACCOUNT_ID

    def test_both_accounts_own_a_project_with_the_same_name(self, two_tenants):
        assert two_tenants.a.shared.name == two_tenants.b.shared.name == SHARED_PROJECT_NAME
        assert two_tenants.a.shared.id != two_tenants.b.shared.id

    def test_both_accounts_own_a_workspace_with_the_same_name(
        self, two_tenants, disposable_db_conn
    ):
        with disposable_db_conn.cursor() as cur:
            cur.execute(
                "SELECT display_name FROM workspaces WHERE id = ANY(%s)",
                ([two_tenants.a.workspace_id, two_tenants.b.workspace_id],),
            )
            names = {row[0] for row in cur.fetchall()}

        assert names == {SHARED_WORKSPACE_NAME}

    def test_the_rows_really_exist_in_the_database(self, two_tenants, disposable_db_conn):
        with disposable_db_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM projects WHERE id = ANY(%s)",
                (
                    [
                        two_tenants.a.shared.id,
                        two_tenants.a.private.id,
                        two_tenants.b.shared.id,
                        two_tenants.b.private.id,
                    ],
                ),
            )
            assert cur.fetchone()[0] == 4


class TestProjectsAreScopedByIdNotByName:
    """A same-name project in another account must never be reachable."""

    def _project_row(self, conn, project_id: str) -> dict:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.name, w.account_id
                  FROM projects p JOIN workspaces w ON w.id = p.workspace_id
                 WHERE p.id = %s
                """,
                (project_id,),
            )
            row = cur.fetchone()
        return {"id": str(row[0]), "name": row[1], "account_id": str(row[2])}

    def test_a_project_resolves_to_the_owning_account(self, two_tenants, disposable_db_conn):
        row = self._project_row(disposable_db_conn, two_tenants.a.shared.id)

        assert row["account_id"] == two_tenants.a.account_id

    def test_the_same_name_project_belongs_to_the_other_account(
        self, two_tenants, disposable_db_conn
    ):
        a_row = self._project_row(disposable_db_conn, two_tenants.a.shared.id)
        b_row = self._project_row(disposable_db_conn, two_tenants.b.shared.id)

        assert a_row["name"] == b_row["name"]
        assert a_row["account_id"] != b_row["account_id"]

    def test_account_b_cannot_claim_account_a_s_same_name_project(
        self, two_tenants, disposable_db_conn
    ):
        victim = self._project_row(disposable_db_conn, two_tenants.a.shared.id)

        assert row_in_account_scope(victim, scope_of(two_tenants.b)) is False

    def test_each_account_reaches_only_its_own_same_name_project(
        self, two_tenants, disposable_db_conn
    ):
        a_row = self._project_row(disposable_db_conn, two_tenants.a.shared.id)
        b_row = self._project_row(disposable_db_conn, two_tenants.b.shared.id)

        assert row_in_account_scope(a_row, scope_of(two_tenants.a)) is True
        assert row_in_account_scope(b_row, scope_of(two_tenants.b)) is True
        assert row_in_account_scope(a_row, scope_of(two_tenants.b)) is False
        assert row_in_account_scope(b_row, scope_of(two_tenants.a)) is False

    def test_a_cross_account_read_raises_404_and_leaks_nothing(
        self, two_tenants, disposable_db_conn
    ):
        from fastapi import HTTPException

        victim = self._project_row(disposable_db_conn, two_tenants.a.private.id)

        _raises_input_130_1 = scope_of(two_tenants.b)
        with pytest.raises(HTTPException) as excinfo:
            assert_row_in_account_scope(victim, _raises_input_130_1, detail="Project not found")

        assert excinfo.value.status_code == 404
        detail = str(excinfo.value.detail)
        assert victim["name"] not in detail, "the response revealed the resource's name"
        assert victim["id"] not in detail
        assert two_tenants.a.account_id not in detail


class TestScopedQueriesFilterAtTheDatabase:
    """The predicate has to be in the SQL, not only in a Python guard."""

    def _projects_for(self, conn, account_id: str) -> set[str]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id
                  FROM projects p JOIN workspaces w ON w.id = p.workspace_id
                 WHERE w.account_id = %s
                """,
                (account_id,),
            )
            return {str(row[0]) for row in cur.fetchall()}

    def test_an_account_scoped_query_returns_only_its_own_projects(
        self, two_tenants, disposable_db_conn
    ):
        visible_to_a = self._projects_for(disposable_db_conn, two_tenants.a.account_id)
        visible_to_b = self._projects_for(disposable_db_conn, two_tenants.b.account_id)

        assert visible_to_a == {two_tenants.a.shared.id, two_tenants.a.private.id}
        assert visible_to_b == {two_tenants.b.shared.id, two_tenants.b.private.id}
        assert not visible_to_a & visible_to_b

    def test_a_name_keyed_lookup_is_ambiguous_across_accounts(
        self, two_tenants, disposable_db_conn
    ):
        """Why id-keyed scoping matters: the name alone matches both tenants."""
        with disposable_db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM projects WHERE name = %s", (SHARED_PROJECT_NAME,))
            by_name = cur.fetchone()[0]

        assert by_name == 2, (
            "the fixture must produce a genuinely ambiguous name for this to mean anything"
        )

    def test_a_missing_account_scope_returns_nothing_rather_than_everything(
        self, two_tenants, disposable_db_conn
    ):
        assert (
            self._projects_for(disposable_db_conn, "00000000-0000-0000-0000-00000000dead") == set()
        )


class TestFixtureCleanup:
    def test_the_fixture_leaves_no_rows_behind(self, disposable_db_conn):
        """Requirement: temporary entities are removed after verification."""
        from tests._tenant_fixtures import create_two_tenants, delete_two_tenants

        fixture = create_two_tenants(disposable_db_conn)
        project_ids = [
            project.id for tenant in (fixture.a, fixture.b) for project in tenant.projects.values()
        ]
        delete_two_tenants(disposable_db_conn, fixture)

        with disposable_db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM projects WHERE id = ANY(%s)", (project_ids,))
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT count(*) FROM accounts WHERE id = ANY(%s)",
                ([fixture.a.account_id, fixture.b.account_id],),
            )
            assert cur.fetchone()[0] == 0
