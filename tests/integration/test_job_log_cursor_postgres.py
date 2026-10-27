"""PostgreSQL coverage for directional Job log cursors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.application.management.management_runtime import create_job_query_service
from core.infrastructure.db.platform_stores import PlatformJobStore
from tests._db_env import resolve_test_db_dsn


@pytest.fixture(autouse=True)
def require_audit_database(pg_conn):
    """Fail before this fixture can write outside the disposable audit database."""
    assert pg_conn.info.dbname == "flow_steward_audit"
    assert pg_conn.info.host == "pg-audit"


@pytest.fixture
def persisted_log_case(pg_conn, default_account):
    project_id = uuid4()
    owned_job_ids: list[str] = []
    owned_log_ids: list[str] = []
    store = None

    try:
        store = PlatformJobStore(resolve_test_db_dsn())
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (project_id, "Job log cursor integration project", default_account),
            )
        pg_conn.commit()

        def create(status: str) -> SimpleNamespace:
            job_id = uuid4()
            log_ids = [uuid4() for _ in range(5)]
            owned_job_ids.append(str(job_id))
            owned_log_ids.extend(str(log_id) for log_id in log_ids)
            timestamps = [
                datetime(2026, 10, 4, 12, 0, tzinfo=UTC) + timedelta(seconds=index)
                for index in range(5)
            ]
            with pg_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO jobs "
                    "(id, project_id, task_id, task_summary, status, account_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        job_id,
                        project_id,
                        f"cursor-{job_id}",
                        "Verify directional Job log pagination",
                        status,
                        default_account,
                    ),
                )
                for ordinal, (log_id, timestamp) in enumerate(
                    zip(log_ids, timestamps, strict=True), 1
                ):
                    cur.execute(
                        "INSERT INTO platform_action_log "
                        "(id, project_id, job_id, task_id, task_summary, timestamp, "
                        "agent, action_type, status, payload) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                        (
                            log_id,
                            project_id,
                            job_id,
                            f"cursor-{job_id}",
                            "Verify directional Job log pagination",
                            timestamp,
                            "cursor-fixture",
                            f"log-{ordinal}",
                            "ok",
                            f'{{"ordinal": {ordinal}}}',
                        ),
                    )
            pg_conn.commit()
            return SimpleNamespace(
                job_id=str(job_id),
                log_ids=[str(log_id) for log_id in log_ids],
            )

        yield SimpleNamespace(
            create=create,
            service=create_job_query_service(get_logger=lambda: store),
        )
    finally:
        pg_conn.rollback()
        try:
            with pg_conn.cursor() as cur:
                if owned_log_ids:
                    cur.execute(
                        "DELETE FROM platform_action_log WHERE id = ANY(%s::uuid[])",
                        (owned_log_ids,),
                    )
                if owned_job_ids:
                    cur.execute(
                        "DELETE FROM jobs WHERE id = ANY(%s::uuid[])",
                        (owned_job_ids,),
                    )
                cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
            pg_conn.commit()
        finally:
            if store is not None and store._direct_conn is not None:
                store._direct_conn.close()


def _ids(page: dict) -> list[str]:
    return [str(row["id"]) for row in page["logs"]]


def test_terminal_logs_traverse_backward_without_gaps(persisted_log_case):
    case = persisted_log_case.create("completed")

    newest = persisted_log_case.service.list_logs(case.job_id, limit=2).dump()
    middle = persisted_log_case.service.list_logs(
        case.job_id,
        before_id=newest["next_before_id"],
        limit=2,
    ).dump()
    oldest = persisted_log_case.service.list_logs(
        case.job_id,
        before_id=middle["next_before_id"],
        limit=2,
    ).dump()

    assert _ids(newest) == case.log_ids[3:]
    assert newest["last_id"] == case.log_ids[4]
    assert newest["next_before_id"] == case.log_ids[3]
    assert newest["has_more"] is True
    assert _ids(middle) == case.log_ids[1:3]
    assert middle["last_id"] == case.log_ids[2]
    assert middle["next_before_id"] == case.log_ids[1]
    assert middle["has_more"] is True
    assert _ids(oldest) == case.log_ids[:1]
    assert oldest["last_id"] == case.log_ids[0]
    assert oldest["next_before_id"] == ""
    assert oldest["has_more"] is False


def test_forward_since_cursor_returns_every_newer_log(persisted_log_case):
    case = persisted_log_case.create("in_progress")

    first = persisted_log_case.service.list_logs(
        case.job_id,
        since_id=case.log_ids[1],
        limit=2,
    ).dump()
    second = persisted_log_case.service.list_logs(
        case.job_id,
        since_id=first["last_id"],
        limit=2,
    ).dump()

    assert _ids(first) == case.log_ids[2:4]
    assert first["last_id"] == case.log_ids[3]
    assert first["next_before_id"] == ""
    assert first["has_more"] is True
    assert _ids(second) == case.log_ids[4:]
    assert second["last_id"] == case.log_ids[4]
    assert second["next_before_id"] == ""
    assert second["has_more"] is False


def test_active_job_initial_window_starts_at_oldest_log(persisted_log_case):
    case = persisted_log_case.create("in_progress")

    page = persisted_log_case.service.list_logs(case.job_id, limit=2).dump()

    assert _ids(page) == case.log_ids[:2]
    assert page["last_id"] == case.log_ids[1]
    assert page["next_before_id"] == ""
    assert page["has_more"] is True
