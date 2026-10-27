"""PostgreSQL coverage for visible Job counts on project surfaces."""

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
def cli_projects_with_visible_and_trashed_jobs(pg_conn, default_account):
    mixed_project_id = uuid4()
    trashed_only_project_id = uuid4()
    live_job_id = uuid4()
    mixed_trashed_job_id = uuid4()
    trashed_only_job_id = uuid4()
    mixed_project_name = f"Visible Job count mixed {mixed_project_id}"
    trashed_only_project_name = f"Visible Job count trashed {trashed_only_project_id}"
    project_store = None
    job_store = None

    try:
        project_store = PlatformProjectStore(resolve_test_db_dsn())
        job_store = PlatformJobStore(resolve_test_db_dsn())
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s), (%s, %s, %s)",
                (
                    mixed_project_id,
                    mixed_project_name,
                    default_account,
                    trashed_only_project_id,
                    trashed_only_project_name,
                    default_account,
                ),
            )
            cur.execute(
                "INSERT INTO jobs "
                "(id, project_id, task_id, task_summary, status, account_id, deleted_at, deleted_by) "
                "VALUES (%s, %s, %s, %s, 'completed', %s, NULL, NULL), "
                "(%s, %s, %s, %s, 'completed', %s, NOW(), %s), "
                "(%s, %s, %s, %s, 'completed', %s, NOW(), %s)",
                (
                    live_job_id,
                    mixed_project_id,
                    f"visible-job-{live_job_id}",
                    "Visible Job remains on project surfaces",
                    default_account,
                    mixed_trashed_job_id,
                    mixed_project_id,
                    f"trashed-job-{mixed_trashed_job_id}",
                    "Trashed Job is hidden from project surfaces",
                    default_account,
                    "project-visible-job-count-regression",
                    trashed_only_job_id,
                    trashed_only_project_id,
                    f"trashed-job-{trashed_only_job_id}",
                    "Only trashed Job is hidden from project surfaces",
                    default_account,
                    "project-visible-job-count-regression",
                ),
            )
        pg_conn.commit()

        runtime = CLIRuntime.__new__(CLIRuntime)
        runtime.get_project_store = lambda: project_store
        runtime.get_job_store = lambda: job_store
        service = runtime.project_query_service
        # Workflow catalog projection is unrelated to visible Job counts. Keep the
        # project and Job repositories real while isolating only that collaborator.
        service._list_workflow_choices = lambda **_kwargs: []
        yield SimpleNamespace(
            service=service,
            account_id=str(default_account),
            mixed_project_id=str(mixed_project_id),
            mixed_project_name=mixed_project_name,
            live_job_id=str(live_job_id),
            trashed_only_project_id=str(trashed_only_project_id),
            trashed_only_project_name=trashed_only_project_name,
        )
    finally:
        pg_conn.rollback()
        try:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM jobs WHERE id IN (%s, %s, %s)",
                    (live_job_id, mixed_trashed_job_id, trashed_only_job_id),
                )
                cur.execute(
                    "DELETE FROM projects WHERE id IN (%s, %s)",
                    (mixed_project_id, trashed_only_project_id),
                )
            pg_conn.commit()
        finally:
            if job_store is not None and job_store._direct_conn is not None:
                job_store._direct_conn.close()
            if project_store is not None and project_store._direct_conn is not None:
                project_store._direct_conn.close()


def _project_scope(fixture, scope_kind: str) -> ScopeContext:
    if scope_kind == "project_allowlist":
        return ScopeContext(
            account_id=fixture.account_id,
            allowed_project_ids=frozenset(
                {
                    fixture.mixed_project_id,
                    fixture.trashed_only_project_id,
                }
            ),
        )
    return ScopeContext(account_id=fixture.account_id)


@pytest.mark.parametrize("scope_kind", ["account_only", "project_allowlist"])
def test_project_list_and_detail_count_only_visible_jobs(
    cli_projects_with_visible_and_trashed_jobs,
    scope_kind,
):
    fixture = cli_projects_with_visible_and_trashed_jobs
    scope = _project_scope(fixture, scope_kind)

    projects_payload = fixture.service.list_projects(
        search=fixture.mixed_project_name,
        scope=scope,
    ).dump()
    detail = fixture.service.get_project_detail(fixture.mixed_project_id, scope=scope).dump()

    assert projects_payload["total"] == 1
    assert [(row["id"], row["job_count"]) for row in projects_payload["projects"]] == [
        (fixture.mixed_project_id, 1)
    ]
    assert detail["total_jobs"] == 1
    assert [row["id"] for row in detail["jobs"]] == [fixture.live_job_id]


@pytest.mark.parametrize("scope_kind", ["account_only", "project_allowlist"])
def test_project_with_only_trashed_job_has_zero_visible_jobs(
    cli_projects_with_visible_and_trashed_jobs,
    scope_kind,
):
    fixture = cli_projects_with_visible_and_trashed_jobs
    scope = _project_scope(fixture, scope_kind)

    projects_payload = fixture.service.list_projects(
        search=fixture.trashed_only_project_name,
        scope=scope,
    ).dump()
    detail = fixture.service.get_project_detail(
        fixture.trashed_only_project_id,
        scope=scope,
    ).dump()
    projects_with_jobs_payload = fixture.service.list_projects(
        search=fixture.trashed_only_project_name,
        has_jobs="yes",
        scope=scope,
    ).dump()
    projects_without_jobs_payload = fixture.service.list_projects(
        search=fixture.trashed_only_project_name,
        has_jobs="no",
        scope=scope,
    ).dump()

    assert projects_payload["total"] == 1
    assert [(row["id"], row["job_count"]) for row in projects_payload["projects"]] == [
        (fixture.trashed_only_project_id, 0)
    ]
    assert detail["total_jobs"] == 0
    assert detail["jobs"] == []
    assert projects_with_jobs_payload["total"] == 0
    assert projects_with_jobs_payload["projects"] == []
    assert projects_without_jobs_payload["total"] == 1
    assert [row["id"] for row in projects_without_jobs_payload["projects"]] == [
        fixture.trashed_only_project_id
    ]
