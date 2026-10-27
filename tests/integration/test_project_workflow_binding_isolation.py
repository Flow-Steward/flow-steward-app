from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

import pytest

from core.application.registry.db_store_workflows_mixin import (
    RegistryDBStoreWorkflowsMixin,
)


class _BindingStore(RegistryDBStoreWorkflowsMixin):
    def __init__(self, conn) -> None:
        self.conn = conn

    @contextmanager
    def _transaction(self):
        with self.conn.cursor() as cur:
            yield cur


@pytest.mark.integration
def test_add_binding_preserves_every_existing_project_workflow_byte(
    disposable_db_conn,
) -> None:
    project_id = uuid4()
    workflow_ids = ("binding_default", "binding_disabled", "binding_deleted", "binding_new")
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (project_id, f"binding-isolation-{project_id}"),
        )
        for workflow_id in workflow_ids:
            cur.execute(
                """INSERT INTO registry_workflows
                   (workflow_key, display_name, account_id, scope_kind)
                   VALUES (%s, %s, %s, 'account')""",
                (workflow_id, workflow_id, "00000000-0000-0000-0000-000000000002"),
            )
        cur.execute(
            """INSERT INTO project_workflows
               (project_id, workflow_id, is_default, priority, enabled, deleted_at)
               VALUES
                 (%s, %s, true, 17, true, NULL),
                 (%s, %s, false, 9, false, NULL),
                 (%s, %s, false, 3, false, now())""",
            (
                project_id,
                workflow_ids[0],
                project_id,
                workflow_ids[1],
                project_id,
                workflow_ids[2],
            ),
        )
        cur.execute(
            """SELECT workflow_id, is_default, priority, enabled, deleted_at,
                      created_at, updated_at
               FROM project_workflows
               WHERE project_id = %s
               ORDER BY workflow_id""",
            (project_id,),
        )
        before = cur.fetchall()

    _BindingStore(disposable_db_conn).add_project_workflow_binding(str(project_id), workflow_ids[3])

    with disposable_db_conn.cursor() as cur:
        cur.execute(
            """SELECT workflow_id, is_default, priority, enabled, deleted_at,
                      created_at, updated_at
               FROM project_workflows
               WHERE project_id = %s
               ORDER BY workflow_id""",
            (project_id,),
        )
        after = cur.fetchall()

    assert [row for row in after if row[0] != workflow_ids[3]] == before
    added = next(row for row in after if row[0] == workflow_ids[3])
    assert added[1:5] == (False, 0, True, None)
