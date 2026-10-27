"""Persisted job errors must reach management status and detail summaries."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from core.application.management.management_runtime import create_job_query_service
from core.entrypoints.cli.platform.service_bindings.jobs import jobs_show, jobs_status
from tests._db_env import resolve_test_db_dsn
from tests.support.platform_test_store import PlatformTestStore


@pytest.fixture(autouse=True)
def require_audit_database(pg_conn):
    """Fail before default-account or product fixtures can write to another DB."""
    assert pg_conn.info.dbname == "flow_steward_audit"
    assert pg_conn.info.host == "pg-audit"


@pytest.fixture
def job_store(pg_conn, default_account):
    store = PlatformTestStore(resolve_test_db_dsn())
    try:
        yield store
    finally:
        if store._direct_conn is not None:
            store._direct_conn.close()


@pytest.fixture
def persisted_job(pg_conn, default_account):
    project_id = uuid.uuid4()
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, "Diagnostic summary project", default_account),
        )
    pg_conn.commit()

    def create_job(task_summary, status):
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (project_id, task_id, task_summary, status, account_id) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (project_id, str(uuid.uuid4()), task_summary, status, default_account),
            )
            job_id = cur.fetchone()[0]
        pg_conn.commit()
        return str(job_id)

    try:
        yield create_job
    finally:
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        pg_conn.commit()


@pytest.mark.parametrize("surface", ["status", "detail", "public"])
def test_management_failure_summary_uses_persisted_error(job_store, persisted_job, surface):
    bl = job_store
    job_id = persisted_job("Validate workflow output", "failed")
    assert (
        bl.log_job_error(
            str(job_id),
            source="provider",
            category="provider",
            retryable=True,
            error_message="An earlier provider attempt failed",
        )
        is not None
    )
    error = bl.log_job_error(
        str(job_id),
        source="system",
        category="contract",
        retryable=False,
        error_message="Workflow execution did not complete successfully",
    )
    assert error is not None
    service = create_job_query_service(get_logger=lambda: bl)
    recorded = service.list_errors(str(job_id)).dump()["errors"]
    assert recorded[0]["category"] == "contract"
    logs = service.get_logs_page(str(job_id)).dump()
    assert logs["job_failure_reason"] == "Workflow execution did not complete successfully"
    incident = service.get_incident_drilldown(str(job_id)).dump()
    assert incident["failure_context"]["last_structured_error"]["id"] == str(error["id"])

    runtime = SimpleNamespace(job_query_service=service)
    if surface == "status":
        payload = asyncio.run(jobs_status(runtime, {"job_id": str(job_id)})).dump()
    else:
        result = asyncio.run(
            jobs_show(
                runtime,
                {"job_id": str(job_id), "include_details": surface == "detail"},
            )
        )
        payload = result.dump() if surface == "detail" else result

    summary = payload["failure_summary"]
    assert summary["kind"] == "workflow_guardrail"
    assert summary["detail"] == "Workflow execution did not complete successfully"
    assert summary["source"] == "system"
    assert summary["retryable"] is False
    if surface != "public":
        assert summary["category"] == "contract"
        assert summary["raw_diagnostics_available"] is True
    else:
        assert "last_structured_error" not in payload["runtime"]


def test_completed_management_job_does_not_report_old_error(job_store, persisted_job):
    bl = job_store
    job_id = persisted_job("Completed workflow", "completed")
    assert (
        bl.log_job_error(
            str(job_id),
            source="system",
            category="contract",
            retryable=False,
            error_message="A previous attempt failed",
        )
        is not None
    )
    service = create_job_query_service(get_logger=lambda: bl)

    payload = asyncio.run(service.get_job_status(str(job_id))).dump()

    assert payload["failure_summary"]["present"] is False
    assert payload["status"] == "completed"
