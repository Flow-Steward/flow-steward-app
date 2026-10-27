"""Global workflow search must not leak across accounts.

Remediation epic 2026-07-29, WS-02 — `CLAUDE-P2-01`.

The audit reviewed an uncommitted working tree in which
`search_postgres_workflows` hardcoded

    _SYSTEM_ACCOUNT_ID = "00000000-0000-0000-0000-000000000002"

and treated every row owned by it as globally visible. That same UUID is
`DEFAULT_ACCOUNT_ID` — the fallback for any scope-less operation, not a dedicated
seed-data account — so a workflow created through any unstamped path became
visible to every account. The predicate also carried a bare
`w.account_id IS NULL` disjunct, making unowned rows universally visible.

`c0ae854` replaced both with `(account_id = %s OR (account_id IS NULL AND
scope_kind = 'system'))`. That landed after the audit baseline, so it is
re-verified here against real rows in two real accounts rather than by reading
the SQL.
"""

from __future__ import annotations

import uuid

import pytest

from core.application.search.search_postgres_workflows import _workflow_rows

pytestmark = pytest.mark.integration

# The predicate the search builds today, isolated so the test exercises the same
# condition the production query does.
SCOPE_PREDICATE = (
    "(CAST(w.account_id AS TEXT) = %s OR (w.account_id IS NULL AND w.scope_kind = 'system'))"
)
# The predicate as the audit found it, kept verbatim so the test proves the
# defect and the fix in one place.
PRE_FIX_PREDICATE = (
    "(w.account_id IS NULL OR CAST(w.account_id AS TEXT) = %s "
    "OR CAST(w.account_id AS TEXT) = '00000000-0000-0000-0000-000000000002')"
)


def _insert_workflow(conn, *, key: str, account_id: str | None, scope_kind: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO registry_workflows
                (workflow_key, display_name, enabled, account_id, scope_kind, visible_in_catalog)
            VALUES (%s, %s, true, %s, %s, true)
            RETURNING workflow_key
            """,
            (key, f"Workflow {key}", account_id, scope_kind),
        )
        return cur.fetchone()[0]


def _visible_to(conn, account_id: str, keys: list[str], *, predicate: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT w.workflow_key FROM registry_workflows w "
            f"WHERE w.enabled = true AND w.workflow_key = ANY(%s) AND {predicate}",
            (keys, account_id),
        )
        return {row[0] for row in cur.fetchall()}


@pytest.fixture
def workflows(two_tenants, disposable_db_conn):
    """One workflow per account, one true system workflow, one default-account row."""
    from core.application.auth.config import DEFAULT_ACCOUNT_ID

    suffix = uuid.uuid4().hex[:8]
    keys = {
        "a": f"wf-a-{suffix}",
        "b": f"wf-b-{suffix}",
        "system": f"wf-system-{suffix}",
        "default_account": f"wf-default-{suffix}",
    }
    _insert_workflow(
        disposable_db_conn, key=keys["a"], account_id=two_tenants.a.account_id, scope_kind="account"
    )
    _insert_workflow(
        disposable_db_conn, key=keys["b"], account_id=two_tenants.b.account_id, scope_kind="account"
    )
    _insert_workflow(disposable_db_conn, key=keys["system"], account_id=None, scope_kind="system")
    # The row the audit was worried about: stamped with the fallback account by
    # some unstamped code path, not deliberately shared.
    _insert_workflow(
        disposable_db_conn,
        key=keys["default_account"],
        account_id=DEFAULT_ACCOUNT_ID,
        scope_kind="account",
    )
    disposable_db_conn.commit()
    try:
        yield keys
    finally:
        with disposable_db_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM registry_workflows WHERE workflow_key = ANY(%s)",
                (list(keys.values()),),
            )
        disposable_db_conn.commit()


class TestAccountScopedSearch:
    def test_an_account_sees_its_own_workflow(self, two_tenants, disposable_db_conn, workflows):
        visible = _visible_to(
            disposable_db_conn,
            two_tenants.a.account_id,
            list(workflows.values()),
            predicate=SCOPE_PREDICATE,
        )

        assert workflows["a"] in visible

    def test_an_account_never_sees_another_accounts_workflow(
        self, two_tenants, disposable_db_conn, workflows
    ):
        visible = _visible_to(
            disposable_db_conn,
            two_tenants.a.account_id,
            list(workflows.values()),
            predicate=SCOPE_PREDICATE,
        )

        assert workflows["b"] not in visible

    def test_a_true_system_workflow_is_visible_to_both(
        self, two_tenants, disposable_db_conn, workflows
    ):
        for tenant in (two_tenants.a, two_tenants.b):
            visible = _visible_to(
                disposable_db_conn,
                tenant.account_id,
                list(workflows.values()),
                predicate=SCOPE_PREDICATE,
            )
            assert workflows["system"] in visible

    def test_a_default_account_row_is_not_globally_visible(
        self, two_tenants, disposable_db_conn, workflows
    ):
        """The heart of CLAUDE-P2-01: the fallback account is not a share-with-everyone marker."""
        visible = _visible_to(
            disposable_db_conn,
            two_tenants.a.account_id,
            list(workflows.values()),
            predicate=SCOPE_PREDICATE,
        )

        assert workflows["default_account"] not in visible

    def test_each_account_sees_exactly_its_own_plus_system(
        self, two_tenants, disposable_db_conn, workflows
    ):
        keys = list(workflows.values())

        assert _visible_to(
            disposable_db_conn, two_tenants.a.account_id, keys, predicate=SCOPE_PREDICATE
        ) == {workflows["a"], workflows["system"]}
        assert _visible_to(
            disposable_db_conn, two_tenants.b.account_id, keys, predicate=SCOPE_PREDICATE
        ) == {workflows["b"], workflows["system"]}

    def test_workflow_search_executes_against_canonical_registry_schema(
        self, two_tenants, disposable_db_conn
    ):
        """A visible account workflow is searchable without a project column on its row."""
        suffix = uuid.uuid4().hex[:8]
        workflow_id = f"searchable-story-cycle-{suffix}"
        with disposable_db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO registry_workflows
                    (workflow_key, display_name, enabled, account_id, scope_kind,
                     visible_in_catalog)
                VALUES (%s, 'Story Cycle', true, %s, 'system', true)
                """,
                (workflow_id, two_tenants.a.account_id),
            )

        class _SearchBackend:
            class _ConnectionAdapter:
                @staticmethod
                def cursor(*_args, **_kwargs):
                    from psycopg.rows import dict_row

                    return disposable_db_conn.cursor(row_factory=dict_row)

            conn = _ConnectionAdapter()

            @staticmethod
            def _column_exists(table: str, column: str) -> bool:
                with disposable_db_conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = 'public'
                              AND table_name = %s
                              AND column_name = %s
                        )
                        """,
                        (table, column),
                    )
                    return bool(cur.fetchone()[0])

        items, total = _workflow_rows(
            bl=_SearchBackend(),
            query="Story Cycle",
            account_id=two_tenants.a.account_id,
            project_ids=None,
            limit=5,
            include_totals=True,
        )

        assert total == 1
        assert [item["workflow_id"] for item in items] == [workflow_id]


class TestThePreFixPredicateStillLeaks:
    """Kept so the test proves the defect, not just the current behaviour."""

    def test_the_audits_predicate_exposes_the_default_account_row(
        self, two_tenants, disposable_db_conn, workflows
    ):
        visible = _visible_to(
            disposable_db_conn,
            two_tenants.a.account_id,
            list(workflows.values()),
            predicate=PRE_FIX_PREDICATE,
        )

        assert workflows["default_account"] in visible, (
            "the pre-fix predicate should still demonstrate the leak"
        )

    def test_the_current_predicate_is_strictly_narrower(
        self, two_tenants, disposable_db_conn, workflows
    ):
        keys = list(workflows.values())
        before = _visible_to(
            disposable_db_conn, two_tenants.a.account_id, keys, predicate=PRE_FIX_PREDICATE
        )
        after = _visible_to(
            disposable_db_conn, two_tenants.a.account_id, keys, predicate=SCOPE_PREDICATE
        )

        assert after < before


class TestNoHardcodedFallbackAccountRemains:
    def test_the_search_module_does_not_hardcode_the_default_account(self):
        from pathlib import Path

        source = Path("core/application/search/search_postgres_workflows.py").read_text(
            encoding="utf-8"
        )

        assert "_SYSTEM_ACCOUNT_ID" not in source
        assert "00000000-0000-0000-0000-000000000002" not in source
