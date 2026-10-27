from __future__ import annotations

import pytest

from core.application.auth.config import DEFAULT_TENANT_ID, DEFAULT_WORKSPACE_ID
from core.infrastructure.db import psycopg_runtime as _psycopg_runtime  # noqa: F401
from tests.support.platform_test_store import PlatformTestStore as PlatformLogger


class _RecordingCursor:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self._calls = calls
        self._fetchone_results: list[object] = []
        self._fetchall_results: list[list[object]] = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self._calls.append((str(sql), params))

    def fetchone(self):
        if self._fetchone_results:
            return self._fetchone_results.pop(0)
        return (321,)

    def fetchall(self):
        if self._fetchall_results:
            return self._fetchall_results.pop(0)
        return []


class _RecordingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fetchone_results: list[object] = []
        self.fetchall_results: list[list[object]] = []
        self.autocommit = True
        self.transaction_events: list[str] = []

    def cursor(self, *args, **kwargs):
        cursor = _RecordingCursor(self.calls)
        cursor._fetchone_results = self.fetchone_results
        cursor._fetchall_results = self.fetchall_results
        return cursor

    def transaction(self):
        events = self.transaction_events

        class _Transaction:
            def __enter__(self):
                events.append("begin")

            def __exit__(self, exc_type, exc, tb):
                events.append("rollback" if exc_type else "commit")
                return False

        return _Transaction()


def _make_logger() -> PlatformLogger:
    logger = PlatformLogger.__new__(PlatformLogger)
    logger.conn = _RecordingConn()
    logger._column_exists_cache = {}
    logger._column_exists = lambda table_name, column_name: bool(  # type: ignore[method-assign]
        table_name == "projects" and column_name in {"tenant_id", "workspace_id"}
    )
    logger._table_exists = lambda table_name: (
        table_name
        in {  # type: ignore[method-assign]
            "project_workflows",
            "project_policies",
        }
    )
    return logger


def test_add_project_requires_explicit_workspace_when_column_exists() -> None:
    logger = _make_logger()

    with pytest.raises(ValueError, match="workspace_id is required"):
        logger.add_project(name="floating")


def test_add_project_syncs_workflows_and_policy_when_tables_exist() -> None:
    logger = _make_logger()
    workflow_calls: list[tuple[int, list[str], str]] = []
    policy_calls: list[tuple[int, dict]] = []

    logger._sync_project_workflows_cur = (
        lambda cur, project_id, project_workflow_ids, operator_default_workflow_id: (
            workflow_calls.append(  # type: ignore[method-assign]
                (project_id, list(project_workflow_ids), operator_default_workflow_id)
            )
        )
    )
    logger._save_project_policy_cur = lambda cur, project_id, policy: policy_calls.append(  # type: ignore[method-assign]
        (project_id, dict(policy))
    )

    project_id = logger.add_project(
        name="demo",
        project_workflow_ids=["wf-1", "wf-2"],
        operator_default_workflow_id="wf-2",
        project_policy={"autonomy_mode": "supervised"},
        workspace_id=DEFAULT_WORKSPACE_ID,
    )

    assert project_id == 321
    assert workflow_calls == [(321, ["wf-1", "wf-2"], "wf-2")]
    assert policy_calls == [(321, {"autonomy_mode": "supervised"})]
    executed_sql = "\n".join(sql for sql, _params in logger.conn.calls)
    assert "db_type" not in executed_sql
    project_insert = next(
        params for sql, params in logger.conn.calls if "INSERT INTO projects" in sql
    )
    assert isinstance(project_insert, tuple)
    assert DEFAULT_TENANT_ID in project_insert


def test_add_project_create_only_never_updates_an_existing_project() -> None:
    logger = _make_logger()

    logger.add_project(
        name="demo",
        workspace_id=DEFAULT_WORKSPACE_ID,
        create_only=True,
    )

    executed_sql = next(sql for sql, _params in logger.conn.calls if "INSERT INTO projects" in sql)
    assert "ON CONFLICT(tenant_id, account_id, workspace_id, name) DO NOTHING" in executed_sql
    assert "DO UPDATE" not in executed_sql


def test_add_project_rolls_back_insert_when_workflow_linking_fails() -> None:
    logger = _make_logger()
    logger._sync_project_workflows_cur = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("workflow FK failed"))
    )
    logger._save_project_policy_cur = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="workflow FK failed"):
        logger.add_project(
            name="atomic-project",
            workspace_id=DEFAULT_WORKSPACE_ID,
            project_workflow_ids=["missing-workflow"],
            create_only=True,
        )

    assert logger.conn.transaction_events == ["begin", "rollback"]
