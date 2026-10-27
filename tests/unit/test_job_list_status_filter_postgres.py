"""B6: one jobs status filter selects several statuses (Failed + Stuck) in PostgreSQL."""

from __future__ import annotations

import uuid

from core.infrastructure.db import job_queries_store
from tests.unit.test_workflow_definition_snapshot_repository import _RealDictConnection


def _jobs(conn, project_id, *rows: tuple[str, bool]) -> dict[str, str]:
    created: dict[str, str] = {}
    for status, acknowledged in rows:
        label = f"{status}{'_ack' if acknowledged else ''}"
        created[label] = str(
            conn.execute(
                """
                INSERT INTO jobs (project_id, task_id, status, acknowledged_at)
                VALUES (%s, %s, %s, CASE WHEN %s THEN now() END)
                RETURNING id
                """,
                (project_id, uuid.uuid4().hex, status, acknowledged),
            ).fetchone()[0]
        )
    return created


def _listed(conn, project_id, status) -> tuple[set[str], int]:
    adapter = _RealDictConnection(conn)
    rows = job_queries_store.list_jobs_for_attention(
        adapter, column_exists=lambda _t, _c: True, project_id=project_id, status=status
    )
    total = job_queries_store.count_jobs_for_attention(
        conn, column_exists=lambda _t, _c: True, project_id=project_id, status=status
    )
    return {str(row["id"]) for row in rows}, total


def test_several_statuses_return_jobs_of_each(audit_db) -> None:
    _store, conn, _workflow_id, project_id = audit_db
    jobs = _jobs(
        conn,
        project_id,
        ("failed", False),
        ("failed", True),
        ("stuck", False),
        ("completed", False),
        ("in_progress", False),
    )

    listed, total = _listed(conn, project_id, ("failed", "stuck"))
    assert listed == {jobs["failed"], jobs["failed_ack"], jobs["stuck"]}
    assert total == 3

    listed, total = _listed(conn, project_id, "stuck")
    assert listed == {jobs["stuck"]}
    assert total == 1


def test_acknowledged_stays_a_derived_status_inside_a_list(audit_db) -> None:
    _store, conn, _workflow_id, project_id = audit_db
    jobs = _jobs(conn, project_id, ("failed", False), ("failed", True), ("stuck", False))

    listed, total = _listed(conn, project_id, ("acknowledged", "stuck"))

    assert listed == {jobs["failed_ack"], jobs["stuck"]}
    assert total == 2


def test_workflow_test_history_filters_before_latest_eight_and_matching_total(audit_db):
    from core.application.jobs.query_scope import job_scope_conditions
    from core.application.management.scope import ScopeContext

    _store, conn, workflow_id, project_id = audit_db
    account, tenant, workspace = conn.execute(
        "SELECT account_id, tenant_id, workspace_id FROM projects WHERE id = %s", (project_id,)
    ).fetchone()
    scope = ScopeContext(
        account_id=str(account),
        tenant_id=str(tenant),
        project_id=str(project_id),
        workspace_id=str(workspace),
        allowed_workflow_ids=frozenset({workflow_id}),
    )
    expected = []
    sources = [("workflow_test_run", workflow_id, i, False) for i in range(12)] + [
        ("step_test_run", workflow_id, 100, False),
        ("manual", workflow_id, 101, False),
        ("invoke_workflow", workflow_id, 102, False),
        ("workflow_test_run", "another-workflow", 103, False),
        ("workflow_test_run", workflow_id, 104, True),
    ]
    for source, selected_workflow, ordinal, deleted in sources:
        job_id = str(
            conn.execute(
                """
            INSERT INTO jobs (project_id, task_id, task_source, selected_workflow, status,
                              started_at, finished_at, duration_ms, deleted_at, meta)
            VALUES (%s, %s, %s, %s, 'completed',
                    '2026-10-18T12:00:00Z'::timestamptz + %s * interval '1 second',
                    '2026-10-18T12:00:00Z'::timestamptz + %s * interval '1 second',
                    0, CASE WHEN %s THEN now() END,
                    '{"test_mode":true,"workflow_test_run":true}'::jsonb)
            RETURNING id
            """,
                (
                    project_id,
                    uuid.uuid4().hex,
                    source,
                    selected_workflow,
                    ordinal,
                    ordinal,
                    deleted,
                ),
            ).fetchone()[0]
        )
        if source == "workflow_test_run" and selected_workflow == workflow_id and not deleted:
            expected.append(job_id)
    filters = {
        "column_exists": lambda _t, _c: True,
        "project_id": str(project_id),
        "workflow_id": workflow_id,
        "task_source": "workflow_test_run",
        "scope_conditions": job_scope_conditions(scope),
    }
    adapter = _RealDictConnection(conn)
    first = job_queries_store.list_jobs_for_attention(adapter, **filters, limit=8)
    second = job_queries_store.list_jobs_for_attention(adapter, **filters, limit=8, offset=8)
    assert [str(row["id"]) for row in first] == list(reversed(expected))[:8]
    assert [str(row["id"]) for row in second] == list(reversed(expected))[8:]
    assert len(first) == 8
    assert job_queries_store.count_jobs_for_attention(conn, **filters) == 12
    assert all(
        row["task_source"] == "workflow_test_run" and row["duration_ms"] == 0 for row in first
    )
    wrong_scope = ScopeContext(
        account_id=str(uuid.uuid4()), tenant_id=str(tenant), project_id=str(project_id)
    )
    denied = {**filters, "scope_conditions": job_scope_conditions(wrong_scope)}
    assert job_queries_store.list_jobs_for_attention(adapter, **denied, limit=8) == []
    assert job_queries_store.count_jobs_for_attention(conn, **denied) == 0
