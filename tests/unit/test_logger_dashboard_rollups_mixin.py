from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.infrastructure.db import dashboard_rollups_store
from core.infrastructure.db.logger_dashboard_rollups_mixin import LoggerDashboardRollupsMixin


class _DummyDashboardLogger(LoggerDashboardRollupsMixin):
    def __init__(
        self,
        *,
        table_exists: bool = False,
        dashboard_job_facts_exists: bool | None = None,
        dashboard_daily_rollups_exists: bool | None = None,
    ) -> None:
        self.conn = None
        self._table_exists_default = table_exists
        self._dashboard_job_facts_exists = dashboard_job_facts_exists
        self._dashboard_daily_rollups_exists = dashboard_daily_rollups_exists

    def _table_exists(self, table_name: str) -> bool:
        if table_name == "dashboard_job_facts" and self._dashboard_job_facts_exists is not None:
            return self._dashboard_job_facts_exists
        if (
            table_name == "dashboard_daily_rollups"
            and self._dashboard_daily_rollups_exists is not None
        ):
            return self._dashboard_daily_rollups_exists
        return self._table_exists_default

    def _column_exists(self, _table_name: str, _column_name: str) -> bool:
        return False


def test_dashboard_parse_dt_handles_valid_and_invalid_values() -> None:
    now = datetime(2026, 3, 30, 12, 0, tzinfo=UTC)
    parsed_direct = LoggerDashboardRollupsMixin._dashboard_parse_dt(now)
    assert parsed_direct is not None
    assert parsed_direct.tzinfo is not None

    parsed_iso_z = LoggerDashboardRollupsMixin._dashboard_parse_dt("2026-03-30T12:00:00Z")
    assert parsed_iso_z == datetime(2026, 3, 30, 12, 0, tzinfo=UTC)

    assert LoggerDashboardRollupsMixin._dashboard_parse_dt("not-a-datetime") is None
    assert LoggerDashboardRollupsMixin._dashboard_parse_dt(None) is None


def test_rollup_helpers_return_safe_defaults_when_tables_are_missing() -> None:
    logger = _DummyDashboardLogger(table_exists=False)
    assert logger._list_dashboard_rollup_accounts() == [DEFAULT_ACCOUNT_ID]
    assert logger.get_dashboard_rollup_state() == {}
    assert logger.list_dashboard_job_facts() == []
    assert logger.list_dashboard_daily_rollups() == []


def test_core_slo_metrics_returns_default_payload_without_source_tables() -> None:
    logger = _DummyDashboardLogger(table_exists=False)
    payload = logger.get_core_slo_metrics(window_hours=48)
    assert payload["window_hours"] == 48
    assert payload["samples"]["latency_jobs"] == 0
    assert payload["samples"]["dispatches"] == 0
    assert payload["retry_rate"] == 0.0
    assert payload["dead_letter_rate"] == 0.0
    assert payload["stuck_jobs"] == 0


def test_refresh_dashboard_job_facts_window_short_circuits_without_table() -> None:
    logger = _DummyDashboardLogger(
        table_exists=False,
        dashboard_job_facts_exists=False,
    )
    result = logger._refresh_dashboard_job_facts_window(
        account_id="acc_default",
        window_start=datetime(2026, 3, 29, 0, 0, tzinfo=UTC),
        window_end=datetime(2026, 3, 30, 0, 0, tzinfo=UTC),
    )
    assert result == {"deleted": 0, "inserted": 0}


def test_refresh_dashboard_job_facts_window_short_circuits_for_empty_window() -> None:
    logger = _DummyDashboardLogger(
        table_exists=True,
        dashboard_job_facts_exists=True,
    )
    start = datetime(2026, 3, 30, 0, 0, tzinfo=UTC)
    result = logger._refresh_dashboard_job_facts_window(
        account_id="acc_default",
        window_start=start,
        window_end=start,
    )
    assert result == {"deleted": 0, "inserted": 0}


def test_refresh_dashboard_daily_rollups_window_short_circuits_without_table() -> None:
    logger = _DummyDashboardLogger(
        table_exists=False,
        dashboard_daily_rollups_exists=False,
    )
    result = logger._refresh_dashboard_daily_rollups_window(
        account_id="acc_default",
        start_date=date(2026, 3, 29),
        end_date=date(2026, 3, 30),
    )
    assert result == {"deleted": 0, "inserted": 0}


def test_refresh_dashboard_daily_rollups_window_short_circuits_for_bad_date_range() -> None:
    logger = _DummyDashboardLogger(
        table_exists=True,
        dashboard_daily_rollups_exists=True,
    )
    result = logger._refresh_dashboard_daily_rollups_window(
        account_id="acc_default",
        start_date=date(2026, 3, 30),
        end_date=date(2026, 3, 29),
    )
    assert result == {"deleted": 0, "inserted": 0}


def test_refresh_dashboard_rollups_returns_unavailable_when_required_tables_missing() -> None:
    logger = _DummyDashboardLogger(table_exists=False)
    payload = logger.refresh_dashboard_rollups()
    assert payload == {"unavailable": True, "refreshed_accounts": 0, "details": []}


def test_refresh_dashboard_rollups_normalizes_now_when_window_has_no_time_room() -> None:
    logger = _DummyDashboardLogger(table_exists=False)
    payload = logger.refresh_dashboard_rollups(
        now=datetime.now(UTC) - timedelta(days=1),
        lookback_days=1,
        backfill_days=1,
    )
    assert payload["unavailable"] is True


class _SqlCaptureCursor:
    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.rows = list(rows or [])
        self.rowcount = 0
        self.calls: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None) -> None:
        self.calls.append((str(sql), params))

    def fetchall(self):
        return list(self.rows)


class _SqlCaptureConn:
    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.cursor_obj = _SqlCaptureCursor(rows)

    def cursor(self, *args, **kwargs):
        del args, kwargs
        return self.cursor_obj


def test_list_dashboard_rollup_accounts_does_not_trim_uuid_account_id() -> None:
    conn = _SqlCaptureConn(rows=[("11111111-1111-4111-8111-111111111111",)])

    accounts = dashboard_rollups_store.list_dashboard_rollup_accounts(
        conn,
        table_exists=lambda table: table == "projects",
        column_exists=lambda table, column: (
            table == "projects" and column in {"account_id", "deleted_at"}
        ),
        default_account_id=DEFAULT_ACCOUNT_ID,
    )

    sql = "\n".join(call[0] for call in conn.cursor_obj.calls)
    assert accounts == ["11111111-1111-4111-8111-111111111111"]
    assert "account_id IS NOT NULL" in sql
    assert "BTRIM(account_id)" not in sql
    assert "BTRIM(p.account_id)" not in sql


def test_refresh_dashboard_job_facts_window_uses_uuid_safe_account_filter() -> None:
    conn = _SqlCaptureConn()
    start = datetime(2026, 3, 30, 0, 0, tzinfo=UTC)
    end = datetime(2026, 3, 31, 0, 0, tzinfo=UTC)

    result = dashboard_rollups_store.refresh_dashboard_job_facts_window(
        conn,
        table_exists=lambda table: table == "dashboard_job_facts",
        column_exists=lambda table, column: (
            (table, column)
            in {
                ("projects", "account_id"),
                ("projects", "deleted_at"),
                ("jobs", "deleted_at"),
            }
        ),
        account_id="11111111-1111-4111-8111-111111111111",
        default_account_id=DEFAULT_ACCOUNT_ID,
        window_start=start,
        window_end=end,
    )

    sql = "\n".join(call[0] for call in conn.cursor_obj.calls)
    assert result == {"deleted": 0, "inserted": 0}
    assert "COALESCE(p.account_id, %s::uuid)" in sql
    assert "COALESCE(p.account_id, %s::uuid) = %s::uuid" in sql
    assert "BTRIM(account_id)" not in sql
    assert "BTRIM(p.account_id)" not in sql
    insert_params = conn.cursor_obj.calls[-1][1]
    assert insert_params[-2:] == (
        DEFAULT_ACCOUNT_ID,
        "11111111-1111-4111-8111-111111111111",
    )


def test_reporting_store_composes_scoped_activity_repository_sql() -> None:
    from core.application.management.scope import ScopeContext
    from core.infrastructure.db.platform_stores import PlatformReportingStore

    conn = _SqlCaptureConn()
    store = PlatformReportingStore.__new__(PlatformReportingStore)
    store.conn = conn
    scope = ScopeContext(
        account_id="account",
        tenant_id="tenant",
        allowed_project_ids=frozenset({"project"}),
        allowed_workspace_ids=frozenset({"workspace"}),
        allowed_workflow_ids=frozenset({"release"}),
    )
    start = datetime(2026, 3, 30, tzinfo=UTC)
    end = datetime(2026, 3, 31, tzinfo=UTC)

    assert (
        store.list_report_activity_facts(
            scope=scope,
            project_id="project",
            workflow_id="release",
            window_start=start,
            window_end=end,
        )
        == []
    )

    sql, params = conn.cursor_obj.calls[0]
    for predicate in (
        "p.account_id = %s",
        "p.tenant_id = %s",
        "j.account_id = p.account_id",
        "j.tenant_id = p.tenant_id",
        "j.workspace_id = p.workspace_id",
        "j.project_id = ANY(%s)",
        "j.selected_workflow = ANY(%s)",
        "p.workspace_id = ANY(%s)",
        "f.finished_at >= %s",
        "f.finished_at < %s",
    ):
        assert predicate in sql
    assert params == (
        "account",
        "tenant",
        ["project"],
        ["release"],
        ["workspace"],
        "account",
        start,
        end,
        "project",
        "release",
    )
    assert "LIMIT" not in sql
