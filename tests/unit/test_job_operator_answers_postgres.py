"""Operator job answers on PostgreSQL: status groups, attention pages, project jobs.

The application service runs against the real job queries, so a status group,
a page and its total are proved where the rows are filtered, not on a mock.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from core.application.jobs.query_scope import job_scope_conditions
from core.application.management.query_services import JobQueryService
from core.application.management.scope import ScopeContext
from core.application.observability_vocabulary import job_attention_group
from core.infrastructure.db import job_queries_store
from tests.unit.test_workflow_definition_snapshot_repository import _RealDictConnection


def _all_columns(_table: str, _column: str) -> bool:
    return True


class _JobStore:
    """The job reads JobQueryService makes, served by the real SQL in job_queries_store."""

    def __init__(self, conn, project: dict) -> None:
        self._conn = conn
        self._rows = _RealDictConnection(conn)
        self._project = project

    @staticmethod
    def _scope(scope):
        return job_scope_conditions(scope) if scope is not None else None

    def list_jobs_for_attention(self, *, scope=None, **filters):
        return job_queries_store.list_jobs_for_attention(
            self._rows, column_exists=_all_columns, scope_conditions=self._scope(scope), **filters
        )

    def count_jobs_for_attention(self, *, scope=None, **filters):
        return job_queries_store.count_jobs_for_attention(
            self._conn, column_exists=_all_columns, scope_conditions=self._scope(scope), **filters
        )

    def get_project(self, _project_id):
        return self._project

    def get_jobs(self, project_id, limit=50, offset=0, **filters):
        return job_queries_store.get_jobs(
            self._rows,
            column_exists=_all_columns,
            project_id=project_id,
            limit=limit,
            offset=offset,
            **filters,
        )

    def count_jobs(self, project_id, **filters):
        return job_queries_store.count_jobs(
            self._conn, column_exists=_all_columns, project_id=project_id, **filters
        )


def _present(row: dict) -> dict:
    return {key: value for key, value in row.items() if value is not None}


def _setup(audit_db):
    _store, conn, workflow_id, project_id = audit_db
    account, tenant, workspace = conn.execute(
        "SELECT account_id, tenant_id, workspace_id FROM projects WHERE id = %s", (project_id,)
    ).fetchone()
    project = {
        "id": str(project_id),
        "account_id": str(account),
        "tenant_id": str(tenant),
        "workspace_id": str(workspace),
    }
    service = JobQueryService(
        get_logger=lambda: _JobStore(conn, project),
        # The store leaves absent columns as NULL; the views take them as unset.
        serialize_attention_job_row=_present,
        serialize_awaiting_approval_job_row=_present,
        job_attention_group=job_attention_group,
        build_runtime_payload_for_api=AsyncMock(return_value={}),
    )
    scope = ScopeContext(account_id=str(account), tenant_id=str(tenant))
    return conn, workflow_id, project_id, service, scope


def _job(conn, project_id, status: str, *, workflow_id: str | None = None, ordinal: int = 0) -> str:
    return str(
        conn.execute(
            """
            INSERT INTO jobs (project_id, task_id, status, selected_workflow, started_at)
            VALUES (%s, %s, %s, %s,
                    '2026-10-31T08:00:00Z'::timestamptz + %s * interval '1 second')
            RETURNING id
            """,
            (project_id, uuid.uuid4().hex, status, workflow_id, ordinal),
        ).fetchone()[0]
    )


def test_stuck_and_failed_groups_list_every_stored_status_with_a_matching_total(audit_db):
    conn, _workflow_id, project_id, service, scope = _setup(audit_db)
    jobs = {
        status: _job(conn, project_id, status)
        for status in (
            "stuck",
            "orphaned",
            "failed",
            "max_iterations",
            "triage_needed",
            "awaiting_timer",
            "completed",
        )
    }

    stuck = service.list_jobs(project_id=str(project_id), status="stuck", scope=scope)
    failed = service.list_jobs(project_id=str(project_id), status="failed", scope=scope)

    assert {job.job_id or job.id for job in stuck.jobs} == {jobs["stuck"], jobs["orphaned"]}
    assert stuck.total == 2
    assert {job.job_id or job.id for job in failed.jobs} == {
        jobs["failed"],
        jobs["max_iterations"],
        jobs["triage_needed"],
    }
    assert failed.total == 3


def test_attention_pages_cover_every_scoped_run_and_say_when_more_remain(audit_db):
    conn, _workflow_id, project_id, service, scope = _setup(audit_db)
    created = [
        _job(conn, project_id, "orphaned" if index % 3 == 0 else "awaiting_timer", ordinal=index)
        for index in range(7)
    ]

    pages = [
        service.list_jobs(
            group_by="attention", project_id=str(project_id), page=page, per_page=3, scope=scope
        ).dump()
        for page in (1, 2, 3)
    ]

    assert [(page["total"], page["total_pages"], page["has_more"]) for page in pages] == [
        (7, 3, True),
        (7, 3, True),
        (7, 3, False),
    ]
    listed = [
        str(row.get("job_id") or row.get("id"))
        for page in pages
        for group in page["groups"].values()
        for row in group
    ]
    assert sorted(listed) == sorted(created)
    # A run waiting on its timer stays with the running work, never needs-attention.
    waiting = [
        row
        for page in pages
        for row in page["groups"]["needs_attention"]
        if row["status"] == "awaiting_timer"
    ]
    assert waiting == []


def test_project_jobs_honour_status_and_a_workflow_restricted_token(audit_db):
    conn, workflow_id, project_id, service, scope = _setup(audit_db)
    own_failed = _job(conn, project_id, "failed", workflow_id=workflow_id, ordinal=1)
    own_retries = _job(conn, project_id, "max_iterations", workflow_id=workflow_id, ordinal=2)
    _job(conn, project_id, "completed", workflow_id=workflow_id, ordinal=3)
    _job(conn, project_id, "failed", workflow_id="another-workflow", ordinal=4)
    _job(conn, project_id, "failed", workflow_id=None, ordinal=5)
    restricted = ScopeContext(
        account_id=scope.account_id,
        tenant_id=scope.tenant_id,
        allowed_workflow_ids=frozenset({workflow_id}),
    )

    first = service.list_project_jobs(
        str(project_id), page=1, per_page=1, status="failed", scope=restricted
    )
    second = service.list_project_jobs(
        str(project_id), page=2, per_page=1, status="failed", scope=restricted
    )
    everything = service.list_project_jobs(str(project_id), status="failed", scope=scope)

    assert (first.total, first.total_pages) == (2, 2)
    assert [first.jobs[0].id, second.jobs[0].id] == [own_retries, own_failed]
    assert everything.total == 4
