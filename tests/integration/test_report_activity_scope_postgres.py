"""Real SQL parity for report facts, job pagination and restricted attention counts."""

from datetime import UTC, datetime
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
                "CREATE TEMP TABLE projects (id text, name text, account_id text, tenant_id text, workspace_id text)"
            )
            cur.execute(
                "CREATE TEMP TABLE jobs (id text, project_id text, selected_workflow text, tenant_id text, account_id text, workspace_id text)"
            )
            cur.execute(
                "CREATE TEMP TABLE dashboard_job_facts (job_id text, project_id text, account_id text, finished_at timestamptz, result text, cost_usd float, duration_seconds float, agent text)"
            )
            for project, tenant, account, workspace in [
                ("p", DEFAULT_TENANT_ID, "account", "w"),
                ("foreign-p", DEFAULT_TENANT_ID, "account", "w"),
                ("foreign-t", "other", "account", "w"),
                ("foreign-a", DEFAULT_TENANT_ID, "other", "w"),
                ("foreign-w", DEFAULT_TENANT_ID, "account", "other"),
            ]:
                cur.execute(
                    "INSERT INTO projects VALUES (%s,%s,%s,%s,%s)",
                    (project, project, account, tenant, workspace),
                )
                for workflow in ("release", "other"):
                    job = project + workflow
                    cur.execute(
                        "INSERT INTO jobs VALUES (%s,%s,%s,%s,%s,%s)",
                        (job, project, workflow, tenant, account, workspace),
                    )
                    cur.execute(
                        "INSERT INTO dashboard_job_facts VALUES (%s,%s,%s,%s,'completed',0,5,'worker')",
                        (job, project, account, stamp),
                    )
            for job, tenant, account in [
                ("inconsistent-tenant", "other", "account"),
                ("inconsistent-account", DEFAULT_TENANT_ID, "other"),
            ]:
                cur.execute(
                    "INSERT INTO jobs VALUES (%s,'p','release',%s,%s,'w')", (job, tenant, account)
                )
                cur.execute(
                    "INSERT INTO dashboard_job_facts VALUES (%s,'p','account',%s,'completed',0,5,'worker')",
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
            assert sum(row["job_count"] for row in result["payload"]["spend_by_agent"]) == 1
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
                "INSERT INTO jobs (id, project_id, account_id, task_id, selected_workflow, status) VALUES (%s, %s, %s, 'scope-fixture', 'release', 'completed')",
                (job_id, project_id, default_account),
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
