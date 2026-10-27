from __future__ import annotations

from unittest.mock import MagicMock

from tests.support.platform_test_store import PlatformTestStore as PlatformLogger


def _make_logger() -> PlatformLogger:
    logger = PlatformLogger.__new__(PlatformLogger)
    logger.conn = object()
    logger._column_exists = lambda _table, _column: True  # type: ignore[method-assign]
    return logger


def test_job_query_methods_delegate_to_store(monkeypatch) -> None:
    logger = _make_logger()

    get_jobs = MagicMock(return_value=[{"id": 1}])
    count_jobs = MagicMock(return_value=3)
    list_attention = MagicMock(return_value=[{"id": 9}])
    count_attention = MagicMock(return_value=4)

    monkeypatch.setattr("core.infrastructure.db.job_queries_store.get_jobs", get_jobs)
    monkeypatch.setattr("core.infrastructure.db.job_queries_store.count_jobs", count_jobs)
    monkeypatch.setattr(
        "core.infrastructure.db.job_queries_store.list_jobs_for_attention", list_attention
    )
    monkeypatch.setattr(
        "core.infrastructure.db.job_queries_store.count_jobs_for_attention", count_attention
    )

    assert logger.get_jobs(7, limit=25, offset=2, include_trashed=True) == [{"id": 1}]
    assert logger.count_jobs(7, include_trashed=True) == 3
    assert logger.list_jobs_for_attention(
        project_ids=[7, 8],
        limit=100,
        offset=1,
        status="failed",
        account_id="acc",
        include_trashed=True,
    ) == [{"id": 9}]
    assert (
        logger.count_jobs_for_attention(
            project_ids=[7, 8],
            status="failed",
            account_id="acc",
            include_trashed=True,
        )
        == 4
    )

    get_jobs.assert_called_once()
    count_jobs.assert_called_once()
    list_attention.assert_called_once()
    count_attention.assert_called_once()


def test_acknowledge_methods_delegate_to_store(monkeypatch) -> None:
    logger = _make_logger()

    acknowledge = MagicMock(return_value=True)
    unacknowledge = MagicMock(return_value=False)
    monkeypatch.setattr("core.infrastructure.db.job_queries_store.acknowledge_job", acknowledge)
    monkeypatch.setattr("core.infrastructure.db.job_queries_store.unacknowledge_job", unacknowledge)

    assert logger.acknowledge_job(42, "alice") is True
    assert logger.unacknowledge_job(42) is False

    acknowledge.assert_called_once()
    unacknowledge.assert_called_once()


def test_history_source_and_scope_reach_existing_list_and_count_adapters(monkeypatch):
    from core.application.jobs.query_scope import job_scope_conditions
    from core.application.management.scope import ScopeContext

    logger = _make_logger()
    rows = MagicMock(return_value=[])
    count = MagicMock(return_value=0)
    monkeypatch.setattr("core.infrastructure.db.job_queries_store.list_jobs_for_attention", rows)
    monkeypatch.setattr("core.infrastructure.db.job_queries_store.count_jobs_for_attention", count)
    scope = ScopeContext(
        account_id="account", tenant_id="tenant", project_id="project", workspace_id="workspace"
    )
    filters = {
        "project_id": "project",
        "workflow_id": "workflow",
        "task_source": "workflow_test_run",
        "scope": scope,
    }
    assert logger.list_jobs_for_attention(**filters, limit=8) == []
    assert logger.count_jobs_for_attention(**filters) == 0
    for call in (rows.call_args, count.call_args):
        assert call.kwargs["task_source"] == "workflow_test_run"
        assert call.kwargs["workflow_id"] == "workflow"
        assert call.kwargs["project_id"] == "project"
        assert call.kwargs["scope_conditions"] == job_scope_conditions(scope)
