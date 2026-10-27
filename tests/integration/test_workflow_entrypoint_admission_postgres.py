"""Real registry owner-state filtering used by authenticated webhook admission."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from psycopg.rows import dict_row

from core.application.workflows.workflow_entrypoint_repository import load_workflow_admission_state

pytestmark = pytest.mark.integration


def test_owner_admission_reads_enabled_and_excludes_deleted_workflows(disposable_db_conn):
    workflow_id = f"webhook-owner-{uuid.uuid4()}"
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO registry_workflows (workflow_key, display_name, enabled) "
            "VALUES (%s, 'Admission owner regression', false)",
            (workflow_id,),
        )
    # A dict-row cursor is the normal repository boundary; retain the fixture's
    # rollback so this test leaves no workflow behind in the disposable database.
    disposable_db_conn.row_factory = dict_row
    logger = SimpleNamespace(conn=disposable_db_conn)

    assert load_workflow_admission_state(logger, workflow_id) == {"enabled": False}
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "UPDATE registry_workflows SET enabled = true WHERE workflow_id = %s", (workflow_id,)
        )
    assert load_workflow_admission_state(logger, workflow_id) == {"enabled": True}
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "UPDATE registry_workflows SET deleted_at = now() WHERE workflow_id = %s",
            (workflow_id,),
        )
    assert load_workflow_admission_state(logger, workflow_id) == {}
    assert load_workflow_admission_state(logger, f"missing-{uuid.uuid4()}") == {}
