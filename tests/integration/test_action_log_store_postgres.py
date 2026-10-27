"""Real PostgreSQL contracts for action-log late-writer handling."""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.infrastructure.db import action_log_store, psycopg_runtime
from tests._db_env import resolve_test_db_dsn

pytestmark = pytest.mark.integration


def test_late_action_log_for_deleted_job_is_dropped_with_psycopg3_sqlstate(
    pg_conn,
) -> None:
    with pg_conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        assert cur.fetchone()[0] == "flow_steward_audit"

    conn = psycopg_runtime.connect(resolve_test_db_dsn())
    try:
        row = action_log_store.log_action(
            conn=conn,
            project_id=None,
            job_id=str(uuid4()),
            task_id="rs5-072-late-writer",
            task_summary="late action after job deletion",
            lifecycle_id=None,
            agent="runtime",
            action_type="node_end",
            status="failed",
        )

        assert row is None
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1
    finally:
        conn.rollback()
        conn.close()
