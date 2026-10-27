from __future__ import annotations

import uuid

import pytest

from core.application.management.errors import ServiceConflictError
from core.application.workflows.workflow_deletion_guard import (
    assert_workflow_has_no_nonterminal_jobs,
)
from core.infrastructure.db.platform_stores import PlatformJobStore
from tests._db_env import resolve_test_db_dsn


@pytest.mark.integration
def test_workflow_deletion_guard_blocks_live_job_then_allows_terminal_job(pg_conn) -> None:
    account_id = "00000000-0000-0000-0000-000000000002"
    project_id = uuid.uuid4()
    workflow_id = f"deletion-guard-{uuid.uuid4().hex}"
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM workspaces WHERE account_id::text = %s ORDER BY created_at LIMIT 1",
            (account_id,),
        )
        workspace_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO registry_workflows(workflow_key,display_name,account_id) VALUES (%s,%s,%s)",
            (workflow_id, "Deletion guard fixture", account_id),
        )
        cur.execute(
            """INSERT INTO projects (id, workspace_id, account_id, name)
               VALUES (%s, %s, %s, %s)""",
            (project_id, workspace_id, account_id, f"Workflow deletion guard {project_id}"),
        )
        job_id = uuid.uuid4()
        cur.execute(
            """INSERT INTO jobs
               (id, account_id, workspace_id, project_id, task_id, status,
                selected_workflow_key, meta)
               VALUES (%s, %s, %s, %s, %s, 'awaiting_approval', %s, %s::jsonb)""",
            (
                job_id,
                account_id,
                workspace_id,
                project_id,
                f"workflow-delete-guard-{job_id}",
                workflow_id,
                '{"selected_workflow_id": "' + str(workflow_id) + '"}',
            ),
        )
    pg_conn.commit()

    store = PlatformJobStore(resolve_test_db_dsn())
    try:
        with pytest.raises(ServiceConflictError) as exc_info:
            assert_workflow_has_no_nonterminal_jobs(
                store,
                workflow_id=str(workflow_id),
                account_id=account_id,
            )
        assert exc_info.value.payload["blocking_jobs"] == [
            {"job_id": str(job_id), "status": "awaiting_approval"}
        ]

        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'completed', finished_at = now() WHERE id = %s",
                (job_id,),
            )
        pg_conn.commit()

        assert_workflow_has_no_nonterminal_jobs(
            store,
            workflow_id=str(workflow_id),
            account_id=account_id,
        )
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
            cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
            cur.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (workflow_id,))
        pg_conn.commit()
        if store._pool is not None:
            store._pool.closeall()
        else:
            store.conn.close()
