"""Real SQL parity for report facts, job pagination and restricted attention counts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from core.application.auth.config import DEFAULT_TENANT_ID
from core.application.management.scope import ScopeContext
from core.application.reports.activity_repository import list_activity_facts
from core.application.reports.reporting_service import ReportingService
from core.infrastructure.db.platform_stores import PlatformJobStore, PlatformReportingStore
from core.infrastructure.db.psycopg_runtime._connection import ConnectionAdapter


def test_operational_report_sql_filters_workflow_and_all_scope_dimensions(pg_conn):
    # Assert before any write: temporary fixture tables must only exist in fs-audit.
    assert pg_conn.info.host == "pg-audit"
    assert pg_conn.info.dbname == "flow_steward_audit"
    db = ConnectionAdapter(pg_conn)
    job_store = PlatformJobStore.__new__(PlatformJobStore)
    job_store.conn = db
    job_store._column_exists = lambda *_: False
    reporting_store = PlatformReportingStore.__new__(PlatformReportingStore)
    reporting_store.conn = db
    try:
        stamp = datetime(2026, 4, 1, 12, tzinfo=UTC)
        with pg_conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE projects (id text, name text, account_id text, tenant_id text, workspace_id text, deleted_at timestamptz)"
            )
            cur.execute(
                "CREATE TEMP TABLE jobs (id text, project_id text, selected_workflow text, tenant_id text, account_id text, workspace_id text, status text, started_at timestamptz, finished_at timestamptz, deleted_at timestamptz)"
            )
            cur.execute(
                "CREATE TEMP TABLE dashboard_job_facts (job_id text, project_id text, account_id text, started_at timestamptz, finished_at timestamptz, result text, duration_seconds float, agent text)"
            )
            # The rollup covers the whole window: every fact below is read from it.
            cur.execute(
                "CREATE TEMP TABLE dashboard_rollup_state (account_id text, facts_covered_until timestamptz)"
            )
            cur.execute(
                "INSERT INTO dashboard_rollup_state VALUES ('account', %s)",
                (datetime(2026, 4, 3, tzinfo=UTC),),
            )
            for project, tenant, account, workspace in [
                ("p", DEFAULT_TENANT_ID, "account", "w"),
                ("foreign-p", DEFAULT_TENANT_ID, "account", "w"),
                ("foreign-t", "other", "account", "w"),
                ("foreign-a", DEFAULT_TENANT_ID, "other", "w"),
                ("foreign-w", DEFAULT_TENANT_ID, "account", "other"),
            ]:
                cur.execute(
                    "INSERT INTO projects VALUES (%s,%s,%s,%s,%s,NULL)",
                    (project, project, account, tenant, workspace),
                )
                for workflow in ("release", "other"):
                    job = project + workflow
                    cur.execute(
                        "INSERT INTO jobs VALUES (%s,%s,%s,%s,%s,%s,'completed',NULL,%s,NULL)",
                        (job, project, workflow, tenant, account, workspace, stamp),
                    )
                    cur.execute(
                        "INSERT INTO dashboard_job_facts VALUES (%s,%s,%s,NULL,%s,'completed',5,'worker')",
                        (job, project, account, stamp),
                    )
            for job, tenant, account in [
                ("inconsistent-tenant", "other", "account"),
                ("inconsistent-account", DEFAULT_TENANT_ID, "other"),
            ]:
                cur.execute(
                    "INSERT INTO jobs VALUES (%s,'p','release',%s,%s,'w','completed',NULL,%s,NULL)",
                    (job, tenant, account, stamp),
                )
                cur.execute(
                    "INSERT INTO dashboard_job_facts VALUES (%s,'p','account',NULL,%s,'completed',5,'worker')",
                    (job, stamp),
                )
        scope = ScopeContext(
            account_id="account",
            tenant_id=DEFAULT_TENANT_ID,
            allowed_project_ids=frozenset({"p"}),
            allowed_workspace_ids=frozenset({"w"}),
            allowed_workflow_ids=frozenset({"release"}),
        )
        kwargs = {
            "scope": scope,
            "project_id": None,
            "workflow_id": None,
            "window_start": datetime(2026, 4, 1, tzinfo=UTC),
            "window_end": datetime(2026, 4, 2, tzinfo=UTC),
        }
        assert [str(row["job_id"]) for row in list_activity_facts(db, **kwargs)] == ["prelease"]
        assert list_activity_facts(db, **{**kwargs, "workflow_id": "missing"}) == []
        assert job_store.count_jobs_for_attention(scope=scope) == 1
        logger = SimpleNamespace(
            list_report_activity_facts=reporting_store.list_report_activity_facts,
            list_dashboard_job_facts=lambda **kw: [],
            list_dashboard_daily_rollups=lambda **kw: [
                {"rollup_date": "2026-04-01", "completed_count": 999}
            ],
            conn=pg_conn,
        )
        service = ReportingService(get_logger_only=lambda: logger, job_query_service=lambda: None)
        for report_type in ("activity_daily", "project_summary", "project_stats"):
            result = service.report_generate(
                report_type,
                {"workflow_id": "release", "start_date": "2026-04-01", "end_date": "2026-04-01"},
                scope=scope,
                persist=False,
            )
            assert result["payload"]["kpis"]["total_jobs"] == 1
        default_result = service.report_generate(
            "activity_daily",
            {"start_date": "2026-04-01", "end_date": "2026-04-01"},
            scope=ScopeContext(account_id="account", tenant_id=DEFAULT_TENANT_ID),
            persist=False,
        )
        assert default_result["payload"]["kpis"]["total_jobs"] == 6
    finally:
        pg_conn.rollback()


def test_scoped_fact_and_job_queries_use_canonical_uuid_columns(pg_conn, default_account):
    from uuid import uuid4

    from core.application.auth.config import DEFAULT_WORKSPACE_ID
    from core.application.jobs.query_scope import job_row_in_scope
    from core.infrastructure.db import job_admin_store

    assert pg_conn.info.host == "pg-audit"
    assert pg_conn.info.dbname == "flow_steward_audit"
    project_id, job_id = uuid4(), uuid4()
    stamp = datetime(2026, 4, 1, 12, tzinfo=UTC)
    db = ConnectionAdapter(pg_conn)
    job_store = PlatformJobStore.__new__(PlatformJobStore)
    job_store.conn = db
    job_store._column_exists = lambda *_: True
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, 'scope fixture', %s)",
                (project_id, default_account),
            )
            cur.execute(
                "INSERT INTO jobs (id, project_id, account_id, task_id, selected_workflow, status, finished_at) VALUES (%s, %s, %s, 'scope-fixture', 'release', 'completed', %s)",
                (job_id, project_id, default_account, stamp),
            )
            cur.execute(
                "INSERT INTO dashboard_job_facts (job_id, account_id, project_id, project_name, workflow, agent, status, result, finished_at, finished_date) VALUES (%s,%s,%s,'scope fixture','release','worker','completed','completed',%s,%s)",
                (job_id, default_account, project_id, stamp, stamp.date()),
            )
        scope = ScopeContext(
            account_id=str(default_account),
            tenant_id=DEFAULT_TENANT_ID,
            allowed_project_ids=frozenset({str(project_id)}),
            allowed_workspace_ids=frozenset({DEFAULT_WORKSPACE_ID}),
            allowed_workflow_ids=frozenset({"release"}),
        )
        facts = list_activity_facts(
            db,
            scope=scope,
            project_id=None,
            workflow_id="release",
            window_start=datetime(2026, 4, 1, tzinfo=UTC),
            window_end=datetime(2026, 4, 2, tzinfo=UTC),
        )
        assert [str(row["job_id"]) for row in facts] == [str(job_id)]
        rows = job_store.list_jobs_for_attention(scope=scope)
        assert [str(row["id"]) for row in rows] == [str(job_id)]
        assert job_store.count_jobs_for_attention(scope=scope) == 1
        denied = scope.model_copy(update={"allowed_workflow_ids": frozenset({"other"})})
        assert job_store.count_jobs_for_attention(scope=denied) == 0
        with pg_conn.cursor() as cur:
            cur.execute("UPDATE jobs SET status='awaiting_approval' WHERE id=%s", (job_id,))
        pending = job_admin_store.get_awaiting_approval_jobs(
            db,
            account_id=str(default_account),
            sql_project_account_id_eq="p.account_id = %s",
            sql_and=" AND ",
        )
        row = next(row for row in pending if str(row["id"]) == str(job_id))
        assert job_row_in_scope(scope, row)
        assert not job_row_in_scope(denied, row)
    finally:
        pg_conn.rollback()


def test_a_report_counts_runs_the_rollup_has_not_refreshed_yet(pg_conn, default_account):
    """Right after runs, a report must not miss them while its window says "now".

    The rollup was last refreshed at 12:00. A completed run at 13:00 is not in
    it yet and a cancelled run never is: both are read live. A run already in
    the rollup is counted once.
    """
    from uuid import uuid4

    from core.application.auth.config import DEFAULT_WORKSPACE_ID

    assert pg_conn.info.host == "pg-audit"
    assert pg_conn.info.dbname == "flow_steward_audit"
    db = ConnectionAdapter(pg_conn)
    project_id = uuid4()
    day = datetime(2026, 4, 1, tzinfo=UTC)
    refreshed = day.replace(hour=12)
    runs = {
        # name: (status, finished at, in the rollup)
        "rolled_completed": ("completed", day.replace(hour=10), True),
        "rolled_failed": ("failed", day.replace(hour=11), True),
        "after_refresh": ("completed", day.replace(hour=13), False),
        "cancelled": ("cancelled", day.replace(hour=9), False),
        # In the rollup, but inside its safety margin: read live, counted once.
        "near_refresh": ("completed", refreshed - timedelta(minutes=1), True),
    }
    ids = {name: uuid4() for name in runs}
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, 'live report', %s)",
                (project_id, default_account),
            )
            for name, (status, finished, rolled) in runs.items():
                started = finished - timedelta(seconds=30)
                cur.execute(
                    "INSERT INTO jobs (id, project_id, account_id, task_id, selected_workflow,"
                    " status, started_at, finished_at)"
                    " VALUES (%s, %s, %s, %s, 'release', %s, %s, %s)",
                    (ids[name], project_id, default_account, name, status, started, finished),
                )
                if rolled:
                    cur.execute(
                        "INSERT INTO dashboard_job_facts (job_id, account_id, project_id,"
                        " project_name, workflow, agent, status, result, started_at, finished_at,"
                        " finished_date, duration_seconds)"
                        " VALUES (%s,%s,%s,'live report','release','worker',%s,%s,%s,%s,%s,30)",
                        (
                            ids[name],
                            default_account,
                            project_id,
                            status,
                            status,
                            started,
                            finished,
                            finished.date(),
                        ),
                    )
            cur.execute(
                "INSERT INTO dashboard_rollup_state (account_id, facts_covered_until)"
                " VALUES (%s, %s) ON CONFLICT (account_id)"
                " DO UPDATE SET facts_covered_until = EXCLUDED.facts_covered_until",
                (default_account, refreshed),
            )
        scope = ScopeContext(
            account_id=str(default_account),
            tenant_id=DEFAULT_TENANT_ID,
            allowed_project_ids=frozenset({str(project_id)}),
            allowed_workspace_ids=frozenset({DEFAULT_WORKSPACE_ID}),
        )
        facts = list_activity_facts(
            db,
            scope=scope,
            project_id=None,
            workflow_id=None,
            window_start=day,
            window_end=day + timedelta(days=1),
        )
        by_job = {str(row["job_id"]): (row["result"], row["source"]) for row in facts}
        assert len(facts) == len(by_job) == 5
        assert by_job == {
            str(ids["rolled_completed"]): ("completed", "rollup"),
            str(ids["rolled_failed"]): ("failed", "rollup"),
            str(ids["after_refresh"]): ("completed", "live"),
            str(ids["cancelled"]): ("cancelled", "live"),
            str(ids["near_refresh"]): ("completed", "live"),
        }

        reporting_store = PlatformReportingStore.__new__(PlatformReportingStore)
        reporting_store.conn = db
        logger = SimpleNamespace(
            list_report_activity_facts=reporting_store.list_report_activity_facts
        )
        service = ReportingService(get_logger_only=lambda: logger, job_query_service=lambda: None)
        report = service.report_generate(
            "activity_daily",
            {"start_date": "2026-04-01", "end_date": "2026-04-01"},
            scope=scope,
            persist=False,
        )
        kpis = report["payload"]["kpis"]
        assert (kpis["completed_count"], kpis["failed_count"], kpis["cancelled_count"]) == (
            3,
            1,
            1,
        )
        assert report["payload"]["data_as_of"]
    finally:
        pg_conn.rollback()
