"""PostgreSQL coverage for the CLI project detail Job projection."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.application.management.scope import ScopeContext
from core.entrypoints.cli.runtime import CLIRuntime
from core.infrastructure.db.platform_stores import PlatformJobStore, PlatformProjectStore
from tests._db_env import resolve_test_db_dsn


@pytest.fixture(autouse=True)
def require_audit_database(pg_conn):
    """Fail before this fixture can write outside the disposable audit database."""
    assert pg_conn.info.dbname == "flow_steward_audit"
    assert pg_conn.info.host == "pg-audit"


@pytest.fixture
def cli_project_with_job(pg_conn, default_account):
    project_id = uuid4()
    job_id = uuid4()
    project_store = None
    job_store = None

    try:
        project_store = PlatformProjectStore(resolve_test_db_dsn())
        job_store = PlatformJobStore(resolve_test_db_dsn())
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (project_id, f"Project count {project_id}", default_account),
            )
            cur.execute(
                "INSERT INTO jobs "
                "(id, project_id, task_id, task_summary, status, account_id) "
                "VALUES (%s, %s, %s, %s, 'completed', %s)",
                (
                    job_id,
                    project_id,
                    f"project-count-{job_id}",
                    "Verify the CLI project Job count",
                    default_account,
                ),
            )
        pg_conn.commit()

        runtime = CLIRuntime.__new__(CLIRuntime)
        runtime.get_project_store = lambda: project_store
        runtime.get_job_store = lambda: job_store
        service = runtime.project_query_service
        # The catalog is a separate collaborator. This regression exercises only
        # the real project and Job stores composed by the CLI runtime.
        service._list_workflow_choices = lambda **_kwargs: []
        yield SimpleNamespace(
            service=service,
            project_id=str(project_id),
            job_id=str(job_id),
            account_id=str(default_account),
        )
    finally:
        pg_conn.rollback()
        try:
            with pg_conn.cursor() as cur:
                cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
                cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
            pg_conn.commit()
        finally:
            if job_store is not None and job_store._direct_conn is not None:
                job_store._direct_conn.close()
            if project_store is not None and project_store._direct_conn is not None:
                project_store._direct_conn.close()


def test_cli_project_detail_uses_job_store_for_count_and_rows(cli_project_with_job):
    fixture = cli_project_with_job
    scope = ScopeContext(
        account_id=fixture.account_id,
        allowed_project_ids=frozenset({fixture.project_id}),
    )

    projects = fixture.service.list_projects(scope=scope).dump()["projects"]
    project = next(row for row in projects if row["id"] == fixture.project_id)
    detail = fixture.service.get_project_detail(fixture.project_id, scope=scope).dump()

    assert project["job_count"] == 1
    assert detail["total_jobs"] == 1
    assert [row["id"] for row in detail["jobs"]] == [fixture.job_id]
