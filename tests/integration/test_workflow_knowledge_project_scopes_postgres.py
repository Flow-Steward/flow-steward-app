"""PostgreSQL coverage for the project scopes the workflow-knowledge sync requests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.infrastructure.db.platform_stores import PlatformProjectStore
from tests._db_env import resolve_test_db_dsn


@pytest.fixture(autouse=True)
def require_audit_database(pg_conn):
    """Fail before this fixture can write outside the disposable audit database."""
    assert pg_conn.info.dbname == "flow_steward_audit"
    assert pg_conn.info.host == "pg-audit"


def test_live_projects_are_listed_newest_first_with_their_scope(pg_conn, default_account):
    older, newer, trashed = uuid4(), uuid4(), uuid4()
    store = PlatformProjectStore(resolve_test_db_dsn())
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id, created_at, deleted_at) VALUES "
                "(%s, %s, %s, now() + interval '1 hour', NULL),"
                "(%s, %s, %s, now() + interval '2 hours', NULL),"
                "(%s, %s, %s, now() + interval '3 hours', now())",
                (
                    older,
                    f"Knowledge scope older {older}",
                    default_account,
                    newer,
                    f"Knowledge scope newer {newer}",
                    default_account,
                    trashed,
                    f"Knowledge scope trashed {trashed}",
                    default_account,
                ),
            )
        pg_conn.commit()

        scopes = store.list_workflow_knowledge_project_scopes(limit=2)

        assert [scope["project_id"] for scope in scopes] == [str(newer), str(older)]
        assert scopes[0]["account_id"] == str(default_account)
        assert scopes[0]["tenant_id"] == "00000000-0000-0000-0000-000000000001"
    finally:
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE id = ANY(%s)", ([older, newer, trashed],))
        pg_conn.commit()
        if store._direct_conn is not None:
            store._direct_conn.close()
