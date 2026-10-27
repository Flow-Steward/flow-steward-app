"""Native report storage roundtrip after management-chat retirement."""

from types import SimpleNamespace

from core.application.auth.config import DEFAULT_TENANT_ID
from core.application.management.scope import ScopeContext
from core.application.reports.reporting_service import ReportingService
from core.infrastructure.db import report_run_store
from core.infrastructure.db.psycopg_runtime._connection import ConnectionAdapter


def test_generate_persist_and_retrieve_report_without_management_chat(pg_conn, default_account):
    assert pg_conn.info.host == "pg-audit"
    assert pg_conn.info.dbname == "flow_steward_audit"
    db = ConnectionAdapter(pg_conn)
    service = ReportingService(
        get_logger_only=lambda: SimpleNamespace(conn=db),
        job_query_service=lambda: None,
        analytics_service=SimpleNamespace(
            analytics_query=lambda *_args, **_kwargs: {
                "window_hours": 24,
                "metrics": {"stuck_jobs": 0},
            }
        ),
    )
    scope = ScopeContext(
        account_id=default_account, tenant_id=DEFAULT_TENANT_ID, actor_id="retirement-report-test"
    )
    report_id = None
    try:
        generated = service.report_generate(
            "core_health_summary", {"window_hours": 24}, scope=scope
        )
        report_id = generated["id"]
        assert report_id
        assert "session_id" not in generated
        stored = service.report_get(report_id, scope=scope)
        assert stored["payload"] == generated["payload"]
        assert stored["summary_text"] == generated["summary_text"]
        assert "session_id" not in stored
        rows = report_run_store.list_report_runs(
            conn=db,
            account_id=default_account,
            tenant_id=DEFAULT_TENANT_ID,
            report_type="core_health_summary",
        )
        assert any(str(row["id"]) == report_id for row in rows)
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.chat_sessions'), to_regclass('public.chat_messages'), to_regclass('public.chat_session_summaries')"
            )
            assert cur.fetchone() == (None, None, None)
    finally:
        if report_id:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM report_runs WHERE id=%s AND account_id=%s",
                    (report_id, default_account),
                )
            pg_conn.commit()
