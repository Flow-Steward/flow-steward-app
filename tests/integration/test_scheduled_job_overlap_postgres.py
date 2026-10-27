"""Automatic schedule overlap safety against a fresh canonical database."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from core.application.jobs.scheduled_job_concurrency import resolve_scheduled_job_concurrency_key
from core.application.jobs.task_scheduling import enqueue_scheduled_job
from core.application.tooling.db.init_db import (
    apply_clean_bootstrap,
    bootstrap_canonical_schema_if_empty,
)
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from core.infrastructure.db import db_pool
from core.infrastructure.db.platform_stores import PlatformControlScheduleStore
from core.infrastructure.security import secret_codec

pytestmark = pytest.mark.integration


@pytest.fixture
def isolated_schedule_dsn():
    """Use only the disposable gate's Postgres, without nested Docker mounts."""
    gate_dsn = os.environ.get("FS_DB_DSN", "")
    connection = conninfo_to_dict(gate_dsn)
    if os.environ.get("FS_AUDIT_ISOLATED") != "1" or any(
        connection.get(key) != value
        for key, value in {
            "host": "pg-audit",
            "dbname": "flow_steward_audit",
            "user": "audit",
        }.items()
    ):
        pytest.fail("Schedule overlap verification requires the isolated fs-audit gate")
    database_name = f"fs_schedule_test_{uuid.uuid4().hex}"
    test_dsn = make_conninfo(gate_dsn, dbname=database_name)
    with psycopg.connect(gate_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        try:
            bootstrap_canonical_schema_if_empty(dsn=test_dsn)
            apply_clean_bootstrap(dsn=test_dsn, author="schedule-overlap-test")
            yield test_dsn
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


def test_concurrent_schedule_launches_persist_one_job_and_keep_other_schedule_independent(
    isolated_schedule_dsn,
    monkeypatch,
) -> None:
    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "schedule-test-key"})
    monkeypatch.setattr(db_pool, "_runtime_env", {"FS_DB_POOL_ENABLED": "0"})
    project_id = str(uuid.uuid4())
    workflow_id = f"schedule-overlap-{uuid.uuid4().hex}"
    schedules = [str(uuid.uuid4()), str(uuid.uuid4())]
    with psycopg.connect(isolated_schedule_dsn) as seed, seed.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, "Schedule overlap", DEFAULT_ACCOUNT_ID),
        )
        cur.execute(
            "INSERT INTO registry_workflows (workflow_key, display_name, settings) VALUES (%s, %s, %s::jsonb)",
            (
                workflow_id,
                "Schedule overlap",
                json.dumps(
                    {
                        "execution_graph": {
                            "schema_version": 1,
                            "entry": {"kind": "step", "id": "done"},
                            "edges": [],
                        }
                    }
                ),
            ),
        )
        cur.execute(
            "INSERT INTO registry_workflow_steps(owner_workflow_id,step_id,step_kind) VALUES (%s,'done','stop_success')",
            (workflow_id,),
        )
        for schedule_id in schedules:
            cur.execute(
                """INSERT INTO scheduled_jobs
                   (id, project_id, account_id, workflow_id, cron_expr)
                   VALUES (%s, %s, %s, %s, '* * * * *')""",
                (schedule_id, project_id, DEFAULT_ACCOUNT_ID, workflow_id),
            )

    barrier = threading.Barrier(2)

    def launch(schedule_id: str, *, concurrent: bool = False) -> dict:
        store = PlatformControlScheduleStore(isolated_schedule_dsn)
        try:
            if concurrent:
                barrier.wait(timeout=20)
            return enqueue_scheduled_job(
                bl=store,
                task_id=f"task-{uuid.uuid4().hex}",
                task_summary="Nightly",
                task_source="schedule",
                project_id=project_id,
                project_name="Schedule overlap",
                project_config={},
                test_mode=False,
                extra_state={"workflow_id": workflow_id},
                meta={},
                enqueue_task_fn=lambda **_kwargs: None,
                build_project_config_snapshot=lambda config: config,
                logger=logging.getLogger(__name__),
                log_prefix="schedule-overlap-test",
                scheduled_job_id=schedule_id,
                account_id=str(DEFAULT_ACCOUNT_ID),
                workflow_id=workflow_id,
                lock_key=resolve_scheduled_job_concurrency_key(
                    account_id=DEFAULT_ACCOUNT_ID,
                    project_id=project_id,
                    scheduled_job_id=schedule_id,
                ),
                run_type="scheduled",
                scheduled_for=None,
                scheduler_instance_id="test",
            )
        finally:
            if store._direct_conn is not None:
                store._direct_conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [executor.submit(launch, schedules[0], concurrent=True) for _ in range(2)]
        results = [attempt.result(timeout=30) for attempt in attempts]
    assert sorted(result["status"] for result in results) == ["conflict", "enqueued"]
    with psycopg.connect(isolated_schedule_dsn) as observer, observer.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE project_id = %s", (project_id,))
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT count(*) FROM job_dispatch_outbox WHERE job_id IN "
            "(SELECT id FROM jobs WHERE project_id = %s)",
            (project_id,),
        )
        assert cur.fetchone()[0] == 1
    assert launch(schedules[1])["status"] == "enqueued"
    with psycopg.connect(isolated_schedule_dsn) as observer, observer.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE project_id = %s", (project_id,))
        assert cur.fetchone()[0] == 2
        cur.execute(
            "SELECT count(*) FROM scheduled_job_execution_locks "
            "WHERE project_id = %s AND released_at IS NULL",
            (project_id,),
        )
        assert cur.fetchone()[0] == 2


def test_schedule_store_create_edit_and_pause_use_only_current_schema(
    isolated_schedule_dsn,
    monkeypatch,
) -> None:
    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "schedule-test-key"})
    monkeypatch.setattr(db_pool, "_runtime_env", {"FS_DB_POOL_ENABLED": "0"})
    project_id = str(uuid.uuid4())
    workflow_id = f"schedule-store-{uuid.uuid4().hex}"
    with psycopg.connect(isolated_schedule_dsn) as seed, seed.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, "Schedule store", DEFAULT_ACCOUNT_ID),
        )
        cur.execute(
            "INSERT INTO registry_workflows (workflow_key, display_name) VALUES (%s, %s)",
            (workflow_id, "Schedule store"),
        )
    store = PlatformControlScheduleStore(isolated_schedule_dsn)
    try:
        created = store.create_scheduled_job(
            project_id=project_id,
            workflow_id=workflow_id,
            account_id=DEFAULT_ACCOUNT_ID,
            cron_expr="0 9 * * *",
            schedule_timezone="Europe/Vilnius",
            inputs={"count": 12, "enabled": False, "record": {"region": "lt"}},
            enabled=False,
        )
        assert created is not None
        assert created["status"] == "paused"
        assert created["inputs"] == {"count": 12, "enabled": False, "record": {"region": "lt"}}
        assert not {
            "instructions",
            "concurrency_scope",
            "concurrency_key",
            "catchup_policy",
            "overlap_policy",
            "task_summary",
        }.intersection(created)
        updated = store.update_scheduled_job(
            project_id,
            created["id"],
            account_id=DEFAULT_ACCOUNT_ID,
            cron_expr="0 9 * * 1",
            enabled=True,
            status="active",
        )
        assert updated["enabled"] is True
        assert updated["status"] == "active"
        paused = store.update_scheduled_job(
            project_id, created["id"], account_id=DEFAULT_ACCOUNT_ID, enabled=False, status="paused"
        )
        assert paused["status"] == "paused"
        assert paused["enabled"] is False
        reopened = store.get_scheduled_job(project_id, created["id"], account_id=DEFAULT_ACCOUNT_ID)
        assert "task_summary" not in reopened
        assert reopened["cron_expr"] == "0 9 * * 1"
        assert reopened["schedule_timezone"] == "Europe/Vilnius"
        assert reopened["inputs"] == created["inputs"]
    finally:
        if store._direct_conn is not None:
            store._direct_conn.close()
