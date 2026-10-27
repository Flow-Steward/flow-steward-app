from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.infrastructure.db import job_queries_store


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self.rowcount = 1

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql: Any, params: Any = None) -> None:
        if self._conn.raise_on_execute:
            raise RuntimeError("boom")
        self._conn.calls.append((str(sql), params))
        if self._conn.rowcount_results:
            self.rowcount = int(self._conn.rowcount_results.pop(0))

    def fetchone(self):
        if self._conn.fetchone_results:
            return self._conn.fetchone_results.pop(0)
        return None

    def fetchall(self):
        if self._conn.fetchall_results:
            return self._conn.fetchall_results.pop(0)
        return []


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fetchone_results: list[Any] = []
        self.fetchall_results: list[Any] = []
        self.rowcount_results: list[int] = []
        self.raise_on_execute = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, *args, **kwargs) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_get_jobs_respects_deleted_filter_and_pagination_bounds() -> None:
    conn = _Conn()
    conn.fetchall_results = [[{"id": 11}]]

    rows = job_queries_store.get_jobs(
        conn,
        column_exists=lambda table, column: table == "jobs" and column == "deleted_at",
        project_id=7,
        limit=25,
        offset=-9,
        include_trashed=False,
    )

    assert rows == [{"id": 11}]
    sql, params = conn.calls[0]
    assert "FROM jobs WHERE project_id = %s AND deleted_at IS NULL" in sql
    assert params == (7, 25, 0)


def test_count_jobs_omits_deleted_filter_when_trashed_included() -> None:
    conn = _Conn()
    conn.fetchone_results = [(9,)]

    count = job_queries_store.count_jobs(
        conn,
        column_exists=lambda _table, _column: True,
        project_id=5,
        include_trashed=True,
    )

    assert count == 9
    sql, params = conn.calls[0]
    assert "deleted_at IS NULL" not in sql
    assert "WHERE project_id = %s" in sql
    assert params == (5,)


def test_list_jobs_for_attention_builds_acknowledged_filters_and_bounds() -> None:
    conn = _Conn()
    conn.fetchall_results = [[{"id": 23}]]

    rows = job_queries_store.list_jobs_for_attention(
        conn,
        column_exists=lambda _table, _column: True,
        project_ids=[3, 1, 3],
        status="acknowledged",
        account_id="acc_default",
        finished_after="2026-03-30T00:00:00Z",
        finished_before=datetime(2026, 3, 31, 12, 0, 0),
        limit=5000,
        offset=-5,
        include_trashed=False,
    )

    assert rows == [{"id": 23}]
    sql, params = conn.calls[0]
    assert "j.deleted_at IS NULL" in sql
    assert "p.deleted_at IS NULL" in sql
    assert "p.account_id = %s" in sql
    assert "LEFT JOIN registry_workflows rw ON rw.workflow_id = j.selected_workflow" in sql
    assert "rw.display_name AS selected_workflow_display_name" in sql
    assert "j.meta ->> 'task_summary_key' AS task_summary_key" in sql
    assert "j.meta -> 'task_summary_params' AS task_summary_params" in sql
    assert "j.project_id = ANY(%s)" in sql
    assert "j.finished_at >= %s" in sql
    assert "j.finished_at < %s" in sql
    assert "j.acknowledged_at IS NOT NULL" in sql
    assert "LOWER(COALESCE(j.status, '')) IN ('failed', 'max_iterations', 'triage_needed')" in sql

    assert params[0] == "acc_default"
    assert params[1] == ["1", "3"]
    assert isinstance(params[2], datetime)
    assert params[2].tzinfo == UTC
    assert isinstance(params[3], datetime)
    assert params[3].tzinfo == UTC
    assert params[-2] == 2000
    assert params[-1] == 0


def test_count_jobs_for_attention_builds_status_equality_filter() -> None:
    conn = _Conn()
    conn.fetchone_results = [(4,)]

    count = job_queries_store.count_jobs_for_attention(
        conn,
        column_exists=lambda _table, _column: True,
        project_id=11,
        status="failed",
        include_trashed=True,
    )

    assert count == 4
    sql, params = conn.calls[0]
    assert "JOIN projects p ON p.id = j.project_id" in sql
    assert "j.deleted_at IS NULL" not in sql
    assert "LOWER(COALESCE(j.status, '')) = ANY(%s)" in sql
    assert params == ("11", ["failed"])


def test_attention_status_filter_matches_any_of_several_statuses() -> None:
    conn = _Conn()
    conn.fetchone_results = [(7,)]

    job_queries_store.count_jobs_for_attention(
        conn,
        column_exists=lambda _table, _column: True,
        project_id=11,
        status=("Failed", "stuck", "acknowledged", "failed"),
    )

    sql, params = conn.calls[0]
    assert (
        "(LOWER(COALESCE(j.status, '')) = ANY(%s) OR "
        "(LOWER(COALESCE(j.status, '')) IN ('failed', 'max_iterations', 'triage_needed') "
        "AND j.acknowledged_at IS NOT NULL))"
    ) in sql
    assert params == ("11", ["failed", "stuck"])


def test_hide_acknowledged_leaves_triaged_runs_out_of_rows_and_total() -> None:
    conn = _Conn()
    conn.fetchone_results = [(2,)]

    job_queries_store.list_jobs_for_attention(
        conn,
        column_exists=lambda _table, _column: True,
        status=("failed", "stuck"),
        hide_acknowledged=True,
    )
    job_queries_store.count_jobs_for_attention(
        conn,
        column_exists=lambda _table, _column: True,
        status=("failed", "stuck"),
        hide_acknowledged=True,
    )

    for sql, _params in conn.calls:
        assert "j.acknowledged_at IS NULL" in sql
    # Off by default: acknowledged runs stay listed.
    conn.calls.clear()
    job_queries_store.count_jobs_for_attention(
        conn, column_exists=lambda _table, _column: True, status="failed"
    )
    assert "j.acknowledged_at IS NULL" not in conn.calls[0][0]


def test_attention_queries_apply_workflow_and_text_search_to_rows_and_total() -> None:
    conn = _Conn()
    conn.fetchall_results = [[]]
    conn.fetchone_results = [(0,)]

    job_queries_store.list_jobs_for_attention(
        conn,
        column_exists=lambda _table, _column: True,
        workflow_id="inventory-import",
        search="supplier feed",
        limit=25,
    )
    job_queries_store.count_jobs_for_attention(
        conn,
        column_exists=lambda _table, _column: True,
        workflow_id="inventory-import",
        search="supplier feed",
    )

    list_sql, list_params = conn.calls[0]
    count_sql, count_params = conn.calls[1]
    for sql in (list_sql, count_sql):
        assert "LEFT JOIN registry_workflows rw ON rw.workflow_id = j.selected_workflow" in sql
        assert "j.selected_workflow = %s" in sql
        assert "COALESCE(j.task_summary, '') ILIKE %s" in sql
        assert "COALESCE(rw.display_name, '') ILIKE %s" in sql
    assert list_params[-2:] == (25, 0)
    assert list_params[:-2] == count_params
    assert count_params[0] == "inventory-import"
    assert all(param == "%supplier feed%" for param in count_params[1:])


def test_acknowledge_job_commits_on_success() -> None:
    conn = _Conn()
    conn.rowcount_results = [1]

    ok = job_queries_store.acknowledge_job(conn, job_id=42, username="alice")

    assert ok is True
    assert conn.commits == 1
    assert conn.rollbacks == 0
    sql, params = conn.calls[0]
    assert "UPDATE jobs SET acknowledged_at = NOW(), acknowledged_by = %s" in sql
    assert params == ("alice", "42")


def test_unacknowledge_job_rolls_back_on_failure() -> None:
    conn = _Conn()
    conn.raise_on_execute = True

    ok = job_queries_store.unacknowledge_job(conn, job_id=42)

    assert ok is False
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_scoped_job_rows_and_count_use_identical_parameterized_predicates():
    from core.application.management.scope import ScopeContext
    from core.infrastructure.db.platform_stores import PlatformJobStore

    scope = ScopeContext(
        account_id="account",
        tenant_id="tenant",
        allowed_project_ids=frozenset({"p"}),
        allowed_workspace_ids=frozenset({"w"}),
        allowed_workflow_ids=frozenset({"release"}),
    )
    conn = _Conn()
    conn.fetchone_results = [(0,)]
    store = PlatformJobStore.__new__(PlatformJobStore)
    store.conn = conn
    store._column_exists = lambda *_: True
    store.list_jobs_for_attention(scope=scope, workflow_id="release")
    store.count_jobs_for_attention(scope=scope, workflow_id="release")
    rows_sql, rows_params = conn.calls[0]
    count_sql, count_params = conn.calls[1]
    assert rows_params[:-2] == count_params
    for predicate in (
        "p.tenant_id = %s",
        "p.account_id = %s",
        "j.project_id = ANY(%s)",
        "j.selected_workflow = ANY(%s)",
        "p.workspace_id = ANY(%s)",
    ):
        assert predicate in rows_sql
        assert predicate in count_sql
    assert ["release"] in count_params


def test_workflow_test_history_source_is_bound_in_both_scoped_queries_before_pagination():
    conn = _Conn()
    conn.fetchall_results = [[{"id": "run", "task_source": "workflow_test_run", "duration_ms": 0}]]
    conn.fetchone_results = [(12,)]
    scope_conditions = (
        [
            "p.account_id = %s",
            "p.tenant_id = %s",
            "j.account_id = p.account_id",
            "j.tenant_id = p.tenant_id",
            "j.workspace_id = p.workspace_id",
            "p.workspace_id = %s",
        ],
        ["account", "tenant", "workspace"],
    )
    filters = {
        "column_exists": lambda _t, _c: True,
        "project_id": "project",
        "workflow_id": "workflow",
        "task_source": "workflow_test_run",
        "scope_conditions": scope_conditions,
    }
    rows = job_queries_store.list_jobs_for_attention(conn, **filters, limit=8, offset=8)
    total = job_queries_store.count_jobs_for_attention(conn, **filters)
    assert rows == [{"id": "run", "task_source": "workflow_test_run", "duration_ms": 0}]
    assert total == 12
    rows_sql, rows_params = conn.calls[0]
    count_sql, count_params = conn.calls[1]
    for predicate in [
        *scope_conditions[0],
        "j.project_id = %s",
        "j.selected_workflow = %s",
        "j.task_source = %s",
    ]:
        assert predicate in rows_sql
        assert predicate in count_sql
    assert "j.task_source" in rows_sql.split("FROM jobs")[0]
    assert "j.duration_ms" in rows_sql.split("FROM jobs")[0]
    assert "ORDER BY COALESCE(j.started_at, j.queued_at) DESC NULLS LAST, j.id DESC" in rows_sql
    assert rows_params[-2:] == (8, 8)
    assert tuple(rows_params[:-2]) == tuple(count_params)
    assert "workflow_test_run" in count_params
    assert "workflow_test_run" not in rows_sql


def test_attention_list_joins_the_hook_owner_so_a_hook_run_can_be_labelled() -> None:
    conn = _Conn()
    conn.fetchall_results = [[]]

    job_queries_store.list_jobs_for_attention(
        conn,
        column_exists=lambda _table, _column: True,
        account_id="acc_default",
    )

    sql = conn.calls[0][0]
    assert "COALESCE(rw.settings ? 'hook_storage', false) AS hook_storage" in sql
    assert "aw.display_name AS hook_author_workflow_display_name" in sql
    assert "rw.settings -> 'hook_storage' ->> 'author_workflow_id'" in sql
