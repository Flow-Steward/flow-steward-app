from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.application.extensions.extension_scan_lifecycle import (
    sync_enabled_extension_with_llm_lifecycle,
    sync_extension_scan_with_llm_lifecycle,
)
from core.infrastructure.db import logger_extensions_accounts_mixin as extensions_mixin
from core.infrastructure.db.logger_policy_projects_mixin import PlatformLoggerPolicyProjectsMixin
from tests.support.platform_test_store import PlatformTestStore as PlatformLogger

PROJECT_A_UUID = "11111111-1111-4111-8111-111111111111"
PROJECT_B_UUID = "22222222-2222-4222-8222-222222222222"


class _QueueCursor:
    def __init__(self, conn: _QueueConn) -> None:
        self._conn = conn
        self.rowcount = 1
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: Any, params: Any = None) -> None:
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


class _QueueConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fetchone_results: list[Any] = []
        self.fetchall_results: list[Any] = []
        self.rowcount_results: list[int] = []

    def cursor(self, *args, **kwargs):
        return _QueueCursor(self)

    def commit(self) -> None:
        return None


def _make_logger(
    *,
    table_exists=None,
    column_exists=None,
) -> tuple[PlatformLogger, _QueueConn]:
    logger = PlatformLogger.__new__(PlatformLogger)
    conn = _QueueConn()
    logger.conn = conn
    logger._column_exists_cache = {}
    logger._lifecycle_starts = {}
    if table_exists is None:
        logger._table_exists = lambda _name: True  # type: ignore[method-assign]
    else:
        logger._table_exists = table_exists  # type: ignore[method-assign]
    if column_exists is None:
        logger._column_exists = lambda _table, _column: True  # type: ignore[method-assign]
    else:
        logger._column_exists = column_exists  # type: ignore[method-assign]
    return logger, conn


def _sync_extension_scan(logger, scanned_extensions):
    return sync_extension_scan_with_llm_lifecycle(logger, scanned_extensions)


def _enable_extension(logger, extension_key, **kwargs):
    return sync_enabled_extension_with_llm_lifecycle(logger, extension_key, **kwargs)


def test_public_progress_helpers_use_strict_repository_not_best_effort_event(monkeypatch) -> None:
    logger, _conn = _make_logger()
    calls: list[tuple[str, Any]] = []

    class _Repository:
        def __init__(self, conn: Any) -> None:
            calls.append(("repository", conn))

    class _Service:
        def __init__(self, repository: Any) -> None:
            calls.append(("service", repository))

        def record_transition(self, **transition: Any) -> str:
            calls.append(("record", transition))
            return "recorded"

        def current_snapshot(self, *, job_id: str) -> str:
            calls.append(("get", job_id))
            return "snapshot"

    monkeypatch.setattr(
        "core.application.jobs.public_progress_repository.PublicProgressRepository",
        _Repository,
    )
    monkeypatch.setattr(
        "core.application.jobs.public_progress_service.PublicProgressService",
        _Service,
    )
    logger.log_event = lambda *args, **kwargs: pytest.fail("best-effort event path used")  # type: ignore[method-assign]

    assert logger.record_public_progress_transition(job_id="job-1") == "recorded"
    assert logger.get_public_progress_snapshot("job-1") == "snapshot"
    assert ("record", {"job_id": "job-1"}) in calls
    assert ("get", "job-1") in calls


def _scheduled_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 1,
        "project_id": 5,
        "account_id": "acc_default",
        "workflow_id": "wf.default",
        "cron_expr": "*/5 * * * *",
        "enabled": True,
        "status": "active",
        "last_job_id": None,
        "last_run_status": "",
        "last_skip_reason": "",
        "last_error": "",
        "created_by": "operator",
        "last_job_status": "pending",
    }
    row.update(overrides)
    return row


def test_lifecycle_and_project_query_helpers_cover_extracted_mixin() -> None:
    logger, conn = _make_logger()
    conn.fetchall_results = [
        [{"id": 1, "task_id": "t1"}],
        [{"id": 2, "task_id": "t2"}],
        [{"id": PROJECT_A_UUID, "name": "Project A", "job_count": 1}],
        [{"id": PROJECT_B_UUID, "name": "Project B", "job_count": 2}],
    ]
    conn.fetchone_results = [
        {"steps": 3, "cycles": 1},
        (7,),
        {"id": PROJECT_A_UUID, "name": "Project A"},
    ]

    lifecycle_id = logger.start_lifecycle()
    assert isinstance(lifecycle_id, str)
    assert lifecycle_id
    elapsed_ms = logger.end_lifecycle(lifecycle_id)
    assert isinstance(elapsed_ms, int)

    with logger.lifecycle() as scoped_id:
        assert scoped_id in logger._lifecycle_starts
    assert scoped_id not in logger._lifecycle_starts

    assert logger.get_action_history(1, limit=5)[0]["task_id"] == "t1"
    assert logger.get_action_history(1, task_id="task-2", limit=5)[0]["task_id"] == "t2"
    assert logger.get_task_statistics(1, "task-2")["steps"] == 3

    projects = logger.get_projects(account_id="acc_default")
    assert projects[0]["name"] == "Project A"
    paginated = logger.list_projects(search="Project", account_id="acc_default")
    assert paginated[0]["name"] == "Project B"
    project_queries = [call[0] for call in conn.calls if "FROM projects p" in call[0]]
    assert len(project_queries) == 2
    for query in project_queries:
        assert (
            "SELECT project_id, COUNT(id) AS job_count FROM jobs "
            "WHERE deleted_at IS NULL GROUP BY project_id"
        ) in " ".join(query.split())
    project_list_sql = "\n".join(project_queries)
    assert "LEFT JOIN workspaces w ON w.id = p.workspace_id" in project_list_sql
    assert "LEFT JOIN project_workflows pw" in project_list_sql
    assert "operator_default_workflow_id" in project_list_sql
    # Both readers list each project's workflows: enabled bindings not removed.
    for query in project_queries:
        flat = " ".join(query.split())
        assert "array_agg(b.workflow_id ORDER BY b.workflow_id)" in flat
        assert "b.enabled = true AND b.deleted_at IS NULL" in flat
        assert "AS workflow_ids" in flat
    assert logger.count_projects(search="Project", account_id="acc_default") == 7
    assert logger.get_project(PROJECT_A_UUID)["id"] == PROJECT_A_UUID


def test_project_list_applies_jobs_filter_and_whitelisted_sort() -> None:
    logger, conn = _make_logger()
    conn.fetchall_results = [[{"id": PROJECT_A_UUID, "name": "Project A", "job_count": 3}]]
    conn.fetchone_results = [(1,)]

    logger.list_projects(
        account_id="acc_default",
        has_jobs="yes",
        sort="job_count",
        direction="desc",
    )
    list_sql, list_params = next(call for call in conn.calls if "FROM projects p" in call[0])

    assert (
        "EXISTS (SELECT 1 FROM jobs fj WHERE fj.project_id = p.id AND fj.deleted_at IS NULL)"
        in list_sql
    )
    # Name stays the tiebreaker so a page boundary never repeats or drops a row.
    assert "ORDER BY COALESCE(j.job_count, 0) DESC, p.name ASC" in list_sql
    assert "acc_default" in list_params

    assert logger.count_projects(account_id="acc_default", has_jobs="no") == 1
    count_sql, count_params = next(
        call for call in conn.calls if "SELECT COUNT(*) FROM projects" in call[0]
    )
    assert (
        "NOT EXISTS (SELECT 1 FROM jobs fj WHERE fj.project_id = projects.id "
        "AND fj.deleted_at IS NULL)"
    ) in count_sql
    assert "acc_default" in count_params


def test_project_list_ignores_unknown_sort_values() -> None:
    logger, conn = _make_logger()
    conn.fetchall_results = [[]]

    logger.list_projects(
        account_id="acc_default",
        sort="p.name; DROP TABLE projects",
        direction="desc",
    )
    list_sql, _ = next(call for call in conn.calls if "FROM projects p" in call[0])

    # An unknown sort key falls back to the default order, so the value never
    # reaches the statement.
    assert "ORDER BY p.name\n" in list_sql
    assert "DROP TABLE" not in list_sql


def test_project_query_helpers_surface_description_from_project_column() -> None:
    logger, conn = _make_logger()
    conn.fetchall_results = [
        [
            {
                "id": PROJECT_A_UUID,
                "name": "Project A",
                "job_count": 1,
                "description": "Stored in column",
            }
        ],
        [
            {
                "id": PROJECT_B_UUID,
                "name": "Project B",
                "job_count": 2,
                "description": "List description",
            }
        ],
    ]
    conn.fetchone_results = [
        {
            "id": PROJECT_A_UUID,
            "name": "Project A",
            "description": "Detail description",
        }
    ]

    assert logger.get_projects(account_id="acc_default")[0]["description"] == "Stored in column"
    assert logger.list_projects(account_id="acc_default")[0]["description"] == "List description"
    assert logger.get_project(PROJECT_A_UUID)["description"] == "Detail description"


def test_get_project_returns_none_for_invalid_uuid() -> None:
    logger, conn = _make_logger()

    assert logger.get_project("9") is None
    assert conn.calls == []


def test_scheduled_jobs_crud_and_runs_paths_cover_extracted_mixin() -> None:
    logger, conn = _make_logger()
    conn.fetchall_results = [
        [_scheduled_row(id=11)],
        [_scheduled_row(id=12)],
        [
            {
                "id": 77,
                "project_id": 5,
                "scheduled_job_id": 12,
                "workflow_id": "wf.default",
                "status": "enqueued",
                "job_status": "pending",
            }
        ],
    ]
    conn.fetchone_results = [
        _scheduled_row(id=11),
        _scheduled_row(id=12),
        _scheduled_row(id=13),
        _scheduled_row(id=14, status="paused"),
        {"id": 77, "project_id": 5, "workflow_id": "wf.default", "status": "enqueued"},
    ]
    conn.rowcount_results = [1]

    assert logger.list_scheduled_jobs(5)[0]["id"] == "11"
    assert logger.get_scheduled_job(5, 11)["id"] == "11"
    due = logger.list_due_scheduled_jobs(now=datetime.now(UTC), limit=10)
    assert due[0]["id"] == "12"

    claimed = logger.claim_due_scheduled_job(
        project_id=5,
        scheduled_job_id=12,
        expected_next_run_at=datetime.now(UTC),
        next_run_at=datetime.now(UTC),
    )
    assert claimed
    assert claimed["id"] == "12"

    created = logger.create_scheduled_job(
        project_id=5,
        workflow_id="wf.default",
        account_id="acc_default",
        cron_expr="*/5 * * * *",
    )
    assert created
    assert created["id"] == "13"

    updated = logger.update_scheduled_job(5, 13, status="paused")
    assert updated
    assert updated["status"] == "paused"
    assert logger.delete_scheduled_job(5, 13) is True

    run_row = logger.create_scheduled_job_run(
        scheduled_job_id=13,
        account_id="acc_default",
        project_id=5,
        workflow_id="wf.default",
        status="enqueued",
    )
    assert run_row
    assert run_row["status"] == "enqueued"
    run_rows = logger.list_scheduled_job_runs(5, scheduled_job_id=13)
    assert run_rows[0]["scheduled_job_id"] == 12


def test_schedule_update_without_changes_reads_the_account_scoped_row() -> None:
    logger, conn = _make_logger()
    conn.fetchone_results = [_scheduled_row(id=11)]

    result = logger.update_scheduled_job(5, 11, account_id="acc_default")

    assert result is not None
    assert result["id"] == "11"
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert "SELECT" in sql
    assert params == ("5", "11", "acc_default", "acc_default")


def test_provider_and_secret_methods_cover_scheduled_provider_mixin() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name
            in {
                "provider_types",
                "providers",
                "provider_health_events",
                "provider_secrets",
                "project_secrets",
            }
        )
    )
    logger._resolve_provider_id_cur = lambda cur, provider_key: 21  # type: ignore[method-assign]
    logger._resolve_provider_type_id_cur = lambda cur, provider_type: 2  # type: ignore[method-assign]

    conn.fetchall_results = [
        [{"id": 2, "key": "task_provider", "enabled": True}],
        [
            {
                "provider_id": 21,
                "provider_key": "sample_tasks",
                "provider_type_id": 2,
                "provider_type_key": "task_provider",
                "provider_type_label": "Task Provider",
                "display_name": "Sample Tasks",
                "description": "",
                "implementation_kind": "builtin",
                "integration_key": "sample_tasks",
                "config": {},
                "skills": ["task_ingest"],
                "enabled": True,
                "created_at": None,
                "updated_at": None,
            }
        ],
        [
            {
                "provider_key": "sample_tasks",
                "project_id": None,
                "status": "ok",
                "summary": "healthy",
                "payload_jsonb": {"uptime": "1d"},
                "created_at": datetime.now(UTC),
            }
        ],
    ]
    conn.fetchone_results = [
        {
            "provider_id": 21,
            "provider_key": "sample_tasks",
            "provider_type_id": 2,
            "provider_type_key": "task_provider",
            "provider_type_label": "Task Provider",
            "display_name": "Sample Tasks",
            "description": "",
            "implementation_kind": "builtin",
            "integration_key": "sample_tasks",
            "config": {"a": 1},
            "skills": ["task_ingest"],
            "enabled": True,
            "created_at": None,
            "updated_at": None,
        },
        {
            "secret_value_ciphertext": "project-secret",
            "secret_value_nonce": None,
            "key_version": "plain:v1",
        },
        {
            "secret_value_ciphertext": "db-secret",
            "secret_value_nonce": None,
            "key_version": "plain:v1",
        },
    ]

    provider_types = logger.list_provider_types(enabled_only=True)
    assert provider_types[0]["key"] == "task_provider"
    provider = logger.get_provider("sample_tasks")
    assert provider
    assert provider["provider_key"] == "sample_tasks"
    providers = logger.list_providers(enabled_only=True)
    assert providers
    assert providers[0]["provider_key"] == "sample_tasks"
    health = logger.list_latest_provider_health(project_id=5)
    assert health["sample_tasks"]["status"] == "ok"

    assert logger.save_provider_secret(
        provider_key="sample_tasks",
        secret_key="webhook_secret",
        secret_value="top-secret",
        project_id=5,
    )
    assert (
        logger.get_provider_secret(
            provider_key="sample_tasks",
            secret_key="webhook_secret",
            project_id=5,
            include_global=False,
        )
        == "project-secret"
    )

    assert logger.save_project_secret(project_id=5, secret_key="db_dsn", secret_value="postgres://")
    assert logger.get_project_secret(project_id=5, secret_key="db_dsn") == "db-secret"

    logger.save_provider(
        {
            "provider_key": "sample_tasks",
            "provider_type_key": "task_provider",
            "display_name": "Sample Tasks",
            "implementation_kind": "builtin",
            "integration_key": "sample_tasks",
            "config": {},
            "skills": ["task_ingest"],
            "enabled": True,
        }
    )
    normalized = logger._normalize_project_provider_binding_row(
        {
            "provider_config": {"a": 1},
            "binding_config": {"b": 2},
            "skills": ["x"],
            "enabled": 1,
            "provider_enabled": 0,
        }
    )
    assert normalized["enabled"] is True
    assert normalized["provider_enabled"] is False


def test_provider_lock_uses_global_provider_row_for_update() -> None:
    logger, conn = _make_logger()
    conn.fetchone_results = [
        {
            "provider_id": "provider-1",
            "provider_key": "fixture_oauth",
            "provider_type_key": "tool_provider",
            "config": {"oauth_client_id": "client-1"},
            "features": ["oauth2", "messages"],
            "enabled": True,
        }
    ]

    provider = logger.lock_provider_for_update("fixture_oauth")

    assert provider
    assert provider["provider_key"] == "fixture_oauth"
    sql, params = conn.calls[-1]
    assert "FOR UPDATE OF p" in sql
    assert params == ("fixture_oauth",)


def test_resolve_provider_type_id_cur_branches() -> None:
    logger, conn = _make_logger()
    cur = conn.cursor()

    conn.fetchone_results = [(12,)]
    assert logger._resolve_provider_type_id_cur(cur, 12) == "12"

    conn.fetchone_results = [(4,)]
    assert logger._resolve_provider_type_id_cur(cur, "task_provider") == "4"


def test_policy_static_helpers_and_fallback_derivations() -> None:
    normalized = PlatformLoggerPolicyProjectsMixin._normalize_project_policy(
        {
            "autonomy_mode": "assist",
            "autonomy_confidence_threshold": 1.5,
            "retry_strategy": "flat",
            "retry_max_attempts": "3",
            "concurrency_cap": 0,
            "tool_policy_mode": "allow_all",
        }
    )
    assert normalized["autonomy_mode"] == "supervised"
    assert normalized["autonomy_confidence_threshold"] == 1.0
    assert normalized["retry_strategy"] == "flat"
    assert normalized["concurrency_cap"] == 1
    assert normalized["tool_policy_mode"] == "permissive"

    derived = PlatformLoggerPolicyProjectsMixin._derive_project_policy_from_row(
        {
            "agents_config": {
                "orchestrator": {"autonomy": {"default_mode": "manual", "min_confidence": 0.3}},
            },
            "config": {"orchestrator": {"concurrency_cap": 6}},
        }
    )
    assert derived["autonomy_mode"] == "manual"
    assert derived["concurrency_cap"] == 6


def test_policy_db_paths_and_run_config() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: name in {"project_policies", "project_workflows"}
    )
    policy_row = {
        "project_id": 5,
        "autonomy_mode": "supervised",
        "autonomy_confidence_threshold": 0.8,
        "retry_strategy": "bounded",
        "retry_max_attempts": 2,
        "retry_backoff_initial_seconds": 2,
        "retry_backoff_max_seconds": 120,
        "concurrency_cap": 2,
        "tool_policy_mode": "registry_only",
    }
    conn.fetchall_results = [
        [
            {
                "project_id": 5,
                "workflow_id": "wf.default",
                "workflow_display_name": "Default",
                "is_default": True,
                "priority": 10,
                "enabled": True,
                "deleted_at": None,
            }
        ],
    ]
    conn.fetchone_results = [policy_row, policy_row]
    logger.get_project = lambda project_id: {  # type: ignore[method-assign]
        "id": int(project_id),
        "name": "Demo",
        "agents_config": {"agents": {}},
        "config": {},
        "project_rules": "",
    }
    logger.list_project_connections = lambda project_id, enabled_only=True: []  # type: ignore[method-assign]
    logger.get_project_provider_bindings = (  # type: ignore[method-assign]
        lambda project_id, project_row=None, enabled_only=True: []
    )

    assert logger.get_project_policy(5)["concurrency_cap"] == 2
    run_cfg = logger.get_project_run_config(5)
    assert run_cfg["project_name"] == "Demo"
    assert run_cfg["operator_default_workflow_id"] == "wf.default"
    assert run_cfg["project_policy"]["concurrency_cap"] == 2
    assert run_cfg["assigned_data_connections"] == []
    assert "db_type" not in run_cfg
    assert "db_schema" not in run_cfg

    logger.save_project_policy(5, {"autonomy_mode": "manual"})
    logger.save_project_workflows(5, ["wf.default"], operator_default_workflow_id="wf.default")
    logger.update_project(
        project_id=5,
        name="Demo",
        project_policy={"autonomy_mode": "manual"},
    )
    logger.update_project_agents_config(5, {"agents": {}})
    logger.delete_project(5)

    sql_text = "\n".join(sql for sql, _params in conn.calls)
    assert "UPDATE projects SET" in sql_text
    assert "INSERT INTO project_policies" in sql_text
    assert "DELETE FROM projects WHERE id = %s" in sql_text
    assert "project_workflows" in sql_text


def test_policy_save_helpers_cover_cursor_paths() -> None:
    class _PolicyCursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        def execute(self, sql: Any, params: Any = None) -> None:
            self.calls.append((str(sql), params))

    cursor = _PolicyCursor()
    PlatformLoggerPolicyProjectsMixin._save_project_policy_cur(
        cursor, project_id=5, policy={"autonomy_mode": "manual", "concurrency_cap": 4}
    )
    combined_sql = "\n".join(sql for sql, _ in cursor.calls)
    assert "INSERT INTO project_policies" in combined_sql
    assert "ON CONFLICT (project_id) DO UPDATE SET" in combined_sql
    params = cursor.calls[0][1]
    assert params[0] == 5
    assert params[1] == "manual"
    assert params[-2] == 4


def test_events_reaction_and_outbox_paths_cover_dispatch_mixin() -> None:
    logger, conn = _make_logger(
        table_exists=lambda _name: True,
        column_exists=lambda _table, _column: True,
    )
    conn.fetchall_results = [
        [
            {
                "id": 1,
                "job_id": 42,
                "workflow_id": "wf.default",
                "event_type": "external.comment.received",
                "source": "sample_tasks",
                "source_id": "cm-1",
                "event_key": "comment.created",
                "payload_jsonb": {"text": "hello"},
                "dedupe_key": "dup-1",
                "processed": False,
                "created_at": None,
            }
        ],
        [
            {
                "id": 11,
                "event_id": "evt-1",
                "event_type": "domain.event",
                "aggregate_type": "job",
                "aggregate_id": "job-42",
                "payload_jsonb": {"a": 1},
                "actor_jsonb": {"id": "u1"},
                "event_envelope_jsonb": {"event_id": "evt-1"},
                "status": "pending",
                "account_id": "acc_default",
                "claim_owner": "worker-a",
                "claim_token": "claim-a",
                "claim_version": 1,
                "claim_expires_at": "2026-08-29T00:00:15+00:00",
            }
        ],
        [
            {
                "id": 12,
                "event_id": "evt-2",
                "event_type": "domain.event",
                "aggregate_type": "job",
                "aggregate_id": "job-43",
                "payload_jsonb": {"a": 2},
                "actor_jsonb": {"id": "u2"},
                "event_envelope_jsonb": {"event_id": "evt-2"},
                "status": "pending",
                "account_id": "acc_default",
            }
        ],
    ]
    conn.fetchone_results = [
        {
            "id": 1,
            "job_id": 42,
            "workflow_id": "wf.default",
            "event_type": "external.comment.received",
            "source": "sample_tasks",
            "source_id": "cm-1",
            "event_key": "comment.created",
            "payload_jsonb": {"text": "hello"},
            "dedupe_key": "dup-1",
            "processed": True,
            "created_at": None,
        },
        {
            "id": 11,
            "event_id": "evt-1",
            "event_type": "domain.event",
            "aggregate_type": "job",
            "aggregate_id": "job-42",
            "payload_jsonb": {"a": 1},
            "actor_jsonb": {"id": "u1"},
            "event_envelope_jsonb": {"event_id": "evt-1"},
            "status": "pending",
            "account_id": "acc_default",
            "claim_owner": "worker-a",
            "claim_token": "claim-a",
            "claim_version": 1,
            "claim_expires_at": "2026-08-29T00:00:30+00:00",
        },
        {
            "id": 11,
            "event_id": "evt-1",
            "event_type": "domain.event",
            "aggregate_type": "job",
            "aggregate_id": "job-42",
            "payload_jsonb": {"a": 1},
            "actor_jsonb": {"id": "u1"},
            "event_envelope_jsonb": {"event_id": "evt-1"},
            "status": "pending",
            "account_id": "acc_default",
            "claim_owner": "worker-a",
            "claim_token": "claim-a",
            "claim_version": 1,
            "claim_expires_at": "2026-08-29T00:05:00+00:00",
        },
        {
            "id": 11,
            "event_id": "evt-1",
            "event_type": "domain.event",
            "aggregate_type": "job",
            "aggregate_id": "job-42",
            "payload_jsonb": {"a": 1},
            "actor_jsonb": {"id": "u1"},
            "event_envelope_jsonb": {"event_id": "evt-1"},
            "status": "published",
            "account_id": "acc_default",
            "claim_owner": None,
            "claim_token": None,
            "claim_version": 1,
            "claim_expires_at": None,
        },
        None,
    ]
    conn.rowcount_results = [2]

    events = logger.list_job_reaction_events(42, limit=10)
    assert events[0]["event_type"] == "external.comment.received"

    processed = logger.mark_job_reaction_event_processed(1, processed=True)
    assert processed
    assert processed["processed"] is True

    logger.enqueue_domain_event(
        event_type="domain.event",
        event_id="evt-1",
        account_id="acc_default",
        project_id=5,
        workflow_id="wf.default",
        job_id=42,
        payload={"hello": "world"},
    )

    claimed = logger.claim_pending_domain_event_outbox(
        claim_owner="worker-a",
        limit=5,
        lease_seconds=15,
    )
    assert claimed
    assert claimed[0]["event_id"] == "evt-1"
    claim_sql = next(
        sql
        for sql, _params in conn.calls
        if "WITH claimed AS" in sql and "UPDATE domain_event_outbox o" in sql
    )
    returning_sql = claim_sql.split("RETURNING", maxsplit=1)[1]
    assert "o.id" in returning_sql
    assert "o.event_type" in returning_sql
    assert "claim_owner = %s" in claim_sql
    assert "claim_token = gen_random_uuid()::text" in claim_sql
    assert "claim_version = o.claim_version + 1" in claim_sql
    assert "claim_expires_at = now() + (%s * interval '1 second')" in claim_sql
    assert "claim_expires_at IS NULL OR claim_expires_at <= now()" in claim_sql

    pending = logger.list_pending_domain_event_outbox(limit=5)
    assert pending
    assert pending[0]["event_id"] == "evt-2"

    with logger.claimed_domain_event_effect(
        "11",
        claim_owner="worker-a",
        claim_token="claim-a",
        claim_version=1,
        lease_seconds=300,
    ) as locked:
        assert locked
        assert locked["claim_token"] == "claim-a"
    effect_sql = next(
        sql
        for sql, _params in conn.calls
        if "UPDATE domain_event_outbox" in sql
        and "claim_expires_at = now()" in sql
        and "WITH claimed AS" not in sql
    )
    assert "claim_owner = %s" in effect_sql
    assert "claim_token = %s" in effect_sql
    assert "claim_version = %s" in effect_sql
    assert "claim_expires_at > now()" in effect_sql

    renewed = logger.renew_domain_event_outbox_claim(
        "11",
        claim_owner="worker-a",
        claim_token="claim-a",
        claim_version=1,
        lease_seconds=300,
    )
    assert renewed
    assert renewed["claim_token"] == "claim-a"
    renew_sql = [
        sql
        for sql, _params in conn.calls
        if "UPDATE domain_event_outbox" in sql
        and "claim_expires_at = now()" in sql
        and "WITH claimed AS" not in sql
        and "RETURNING *" in sql
    ][-1]
    assert "claim_owner = %s" in renew_sql
    assert "claim_token = %s" in renew_sql
    assert "claim_version = %s" in renew_sql
    assert "claim_expires_at > now()" in renew_sql

    updated = logger.mark_domain_event_outbox_status(
        11,
        status="published",
        claim_owner="worker-a",
        claim_token="claim-a",
        claim_version=1,
    )
    assert updated
    assert updated["status"] == "published"
    finalize_sql = next(
        sql
        for sql, _params in conn.calls
        if "UPDATE domain_event_outbox" in sql
        and "claim_owner = NULL" in sql
        and "RETURNING *" in sql
    )
    assert "claim_owner = %s" in finalize_sql
    assert "claim_token = %s" in finalize_sql
    assert "claim_version = %s" in finalize_sql
    assert "claim_expires_at > now()" in finalize_sql

    stale = logger.mark_domain_event_outbox_status(
        11,
        status="published",
        claim_owner="worker-a",
        claim_token="claim-a",
        claim_version=1,
    )
    assert stale is None

    assert logger.mark_domain_event_outbox_published_for_event("evt-1") >= 1


def test_domain_event_outbox_claim_status_is_exact_and_read_only() -> None:
    logger, conn = _make_logger()
    conn.fetchone_results = [
        {
            "active": True,
            "claim_expires_at": "2026-09-07T04:50:00+00:00",
        }
    ]

    status = logger.get_domain_event_outbox_claim_status(
        outbox_id="outbox-active",
        claim_owner="worker-1",
        claim_version=7,
    )

    assert status == {
        "active": True,
        "claim_expires_at": "2026-09-07T04:50:00+00:00",
    }
    sql, params = conn.calls[-1]
    assert sql.lstrip().startswith("SELECT")
    assert "claim_expires_at > now() AS active" in sql
    assert "status = 'pending'" in sql
    assert params == ("outbox-active", "worker-1", 7)


def test_events_domain_ledger_dispatch_and_delivery_paths() -> None:
    logger, conn = _make_logger(
        table_exists=lambda _name: True,
        column_exists=lambda _table, _column: True,
    )
    logger._refresh_domain_event_status = lambda _event_id: None  # type: ignore[method-assign]

    conn.fetchall_results = [
        [
            {
                "id": 10,
                "event_id": "evt-1",
                "event_type": "domain.event",
                "account_id": "acc_default",
                "project_id": 5,
                "workflow_id": "wf.default",
                "job_id": 42,
                "severity": "info",
                "summary": "ok",
                "payload_jsonb": {"x": 1},
                "actor_jsonb": {"id": "u1"},
                "urls_jsonb": {},
                "envelope_jsonb": {"event_id": "evt-1"},
                "source": "runtime",
                "aggregate_type": "job",
                "aggregate_id": "job-42",
                "status": "recorded",
                "dispatch_count": 0,
            }
        ],
        [
            {
                "id": 21,
                "dispatch_id": "dsp-1",
                "event_id": "evt-1",
                "account_id": "acc_default",
                "target_type": "provider",
                "target_id": "sample_tasks",
                "status": "dispatching",
                "scope_jsonb": {},
                "metadata_jsonb": {},
                "attempt_count": 1,
                "max_attempts": 3,
            }
        ],
        [
            {
                "id": 99,
                "event_id": "evt-1",
                "dispatch_id": "dsp-1",
                "attempt_no": 1,
                "status": "failed",
                "request_jsonb": {},
                "response_jsonb": {},
                "retry_in_seconds": 30,
            }
        ],
        [
            {
                "id": 501,
                "command_id": "cmd-1",
                "event_id": "evt-1",
                "dispatch_id": "dsp-1",
                "command_type": "dispatch",
                "account_id": "acc_default",
                "target_jsonb": {"provider": "sample_tasks"},
                "payload_jsonb": {"hello": "world"},
                "result_jsonb": {"ok": True},
                "status": "succeeded",
            }
        ],
    ]
    conn.fetchone_results = [
        {
            "id": 10,
            "event_id": "evt-1",
            "event_type": "domain.event",
            "account_id": "acc_default",
            "project_id": 5,
            "workflow_id": "wf.default",
            "job_id": 42,
            "severity": "info",
            "summary": "ok",
            "payload_jsonb": {"x": 1},
            "actor_jsonb": {"id": "u1"},
            "urls_jsonb": {},
            "envelope_jsonb": {"event_id": "evt-1"},
            "source": "runtime",
            "aggregate_type": "job",
            "aggregate_id": "job-42",
            "status": "recorded",
            "dispatch_count": 0,
        },
        (3,),
        {
            "id": 10,
            "event_id": "evt-1",
            "event_type": "domain.event",
            "account_id": "acc_default",
            "project_id": 5,
            "workflow_id": "wf.default",
            "job_id": 42,
            "severity": "info",
            "summary": "ok",
            "payload_jsonb": {"x": 1},
            "actor_jsonb": {"id": "u1"},
            "urls_jsonb": {},
            "envelope_jsonb": {"event_id": "evt-1"},
            "source": "runtime",
            "aggregate_type": "job",
            "aggregate_id": "job-42",
            "status": "recorded",
            "dispatch_count": 0,
        },
        {
            "id": 21,
            "dispatch_id": "dsp-1",
            "event_id": "evt-1",
            "account_id": "acc_default",
            "target_type": "provider",
            "target_id": "sample_tasks",
            "status": "matched",
            "scope_jsonb": {},
            "metadata_jsonb": {},
            "attempt_count": 0,
            "max_attempts": 3,
        },
        {
            "id": 21,
            "dispatch_id": "dsp-1",
            "event_id": "evt-1",
            "account_id": "acc_default",
            "target_type": "provider",
            "target_id": "sample_tasks",
            "status": "matched",
            "scope_jsonb": {},
            "metadata_jsonb": {},
            "attempt_count": 0,
            "max_attempts": 3,
        },
        {
            "status": "matched",
            "event_id": "evt-1",
        },
        {
            "id": 21,
            "dispatch_id": "dsp-1",
            "event_id": "evt-1",
            "account_id": "acc_default",
            "target_type": "provider",
            "target_id": "sample_tasks",
            "status": "dispatching",
            "scope_jsonb": {},
            "metadata_jsonb": {},
            "attempt_count": 1,
            "max_attempts": 3,
        },
        {
            "status": "queued",
            "attempt_count": 0,
            "max_attempts": 3,
        },
        {"next_attempt_no": 1},
        {
            "id": 99,
            "event_id": "evt-1",
            "dispatch_id": "dsp-1",
            "attempt_no": 1,
            "status": "failed",
            "request_jsonb": {"body": {}},
            "response_jsonb": {},
            "retry_in_seconds": 30,
        },
    ]
    # Keep dispatch update path deterministic.
    logger.update_event_dispatch_status = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: {"dispatch_id": "dsp-1", "status": "failed"}
    )

    event_row = logger.record_domain_event(
        event_id="evt-1",
        event_type="domain.event",
        account_id="acc_default",
        project_id=5,
        workflow_id="wf.default",
        job_id=42,
        group_id="per_item",
        step_id="send",
        step_execution_id="6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
        payload={"x": 1},
    )
    assert event_row
    assert event_row["event_id"] == "evt-1"
    ledger_sql, ledger_params = next(
        (sql, params) for sql, params in conn.calls if "INSERT INTO domain_event_ledger" in sql
    )
    assert "phase_id" not in ledger_sql
    ledger_columns = [
        column.strip() for column in ledger_sql.split("(", 1)[1].split(")", 1)[0].split(",")
    ]
    ledger_row = dict(zip(ledger_columns, ledger_params, strict=False))
    assert ledger_row["group_id"] == "per_item"
    assert ledger_row["step_id"] == "send"
    assert ledger_row["step_execution_id"] == "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42"

    events = logger.list_domain_events(account_id="acc_default", limit=10, offset=0)
    assert events
    assert events[0]["event_id"] == "evt-1"
    assert logger.count_domain_events(account_id="acc_default") == 3
    assert logger.get_domain_event("evt-1", account_id="acc_default")["event_id"] == "evt-1"

    dispatch = logger.record_event_dispatch(
        event_id="evt-1",
        target_type="provider",
        target_id="sample_tasks",
        account_id="acc_default",
        target_label="Sample Tasks",
    )
    assert dispatch
    assert dispatch["dispatch_id"] == "dsp-1"

    updated_dispatch = logger.update_event_dispatch_status("dsp-1", status="dispatching")
    assert updated_dispatch
    assert updated_dispatch["dispatch_id"] == "dsp-1"
    listed = logger.list_event_dispatches(event_id="evt-1", limit=25)
    assert listed
    assert listed[0]["dispatch_id"] == "dsp-1"

    attempt = logger.record_event_delivery_attempt(
        event_id="evt-1",
        dispatch_id="dsp-1",
        status="failed",
        retry_in_seconds=30,
    )
    assert attempt
    assert attempt["dispatch_id"] == "dsp-1"

    attempts = logger.list_event_delivery_attempts(dispatch_id="dsp-1", limit=25)
    assert attempts
    assert attempts[0]["dispatch_id"] == "dsp-1"
    executions = logger.list_command_executions(event_id="evt-1", dispatch_id="dsp-1", limit=25)
    assert executions
    assert executions[0]["command_id"] == "cmd-1"


def test_event_delivery_attempts_batch_lookup_groups_by_dispatch_id() -> None:
    logger, conn = _make_logger()
    now = datetime.now(UTC)
    conn.fetchall_results = [
        [
            {
                "id": 2,
                "event_id": "evt-1",
                "dispatch_id": "dsp-1",
                "attempt_no": 1,
                "status": "failed",
                "worker_id": "worker-a",
                "handler": "fixture_notifier",
                "started_at": now,
                "finished_at": now,
                "duration_ms": 12,
                "request_jsonb": {},
                "response_jsonb": {},
                "error_text": "timeout",
                "retry_in_seconds": 30,
                "created_at": now,
            },
            {
                "id": 3,
                "event_id": "evt-1",
                "dispatch_id": "dsp-2",
                "attempt_no": 1,
                "status": "succeeded",
                "worker_id": "worker-b",
                "handler": "slack",
                "started_at": now,
                "finished_at": now,
                "duration_ms": 8,
                "request_jsonb": {},
                "response_jsonb": {},
                "error_text": None,
                "retry_in_seconds": None,
                "created_at": now,
            },
        ]
    ]

    grouped = logger.list_event_delivery_attempts_by_dispatch_ids(
        dispatch_ids=["dsp-1", "dsp-2"],
        limit_per_dispatch=50,
    )

    assert list(grouped) == ["dsp-1", "dsp-2"]
    assert grouped["dsp-1"][0]["handler"] == "fixture_notifier"
    assert grouped["dsp-2"][0]["status"] == "succeeded"


def test_extension_accounts_machine_tokens_and_pat_paths() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name in {"accounts", "service_accounts", "machine_tokens", "personal_access_tokens"}
        ),
        column_exists=lambda _table, _column: True,
    )

    now = datetime.now(UTC)
    conn.fetchall_results = [
        [{"account_id": "acc_default", "display_name": "Default", "is_active": True}],
        [
            {
                "id": 1,
                "account_id": "acc_default",
                "service_account_key": "svc_ops",
                "display_name": "Svc Ops",
                "description": "",
                "scopes_jsonb": ["read", "write"],
                "allowed_project_ids_jsonb": [5],
                "allowed_workflow_ids_jsonb": ["wf.default"],
                "is_active": True,
                "created_by": "system",
            }
        ],
        [
            {
                "id": 10,
                "service_account_id": 1,
                "account_id": "acc_default",
                "label": "token-a",
                "scopes_jsonb": ["read"],
                "allowed_project_ids_jsonb": [5],
                "allowed_workflow_ids_jsonb": ["wf.default"],
                "expires_at": now + timedelta(days=3),
                "last_used_at": None,
                "revoked": False,
                "created_by": "system",
            }
        ],
        [
            {
                "id": 41,
                "label": "pat-a",
                "scopes_jsonb": ["read"],
                "allowed_project_ids_jsonb": ["00000000-0000-0000-0000-000000000105"],
                "allowed_workflow_ids_jsonb": ["wf.default"],
                "expires_at": now + timedelta(days=2),
                "last_used_at": None,
                "revoked": False,
                "created_at": now,
                "updated_at": now,
            }
        ],
        [
            {
                "id": 42,
                "label": "pat-b",
                "scopes_jsonb": ["write"],
                "allowed_project_ids_jsonb": [6],
                "allowed_workflow_ids_jsonb": ["wf.ops"],
                "expires_at": now + timedelta(days=5),
                "last_used_at": None,
                "revoked": False,
                "created_at": now,
                "updated_at": now,
            }
        ],
        [{"key": "admin"}, {"key": "operator"}],
    ]
    conn.fetchone_results = [
        {"is_active": True},
        {"account_id": "acc_ops", "display_name": "Ops", "is_active": True},
        {"account_id": "acc_ops", "display_name": "Ops", "is_active": False},
        {
            "id": 1,
            "account_id": "acc_default",
            "service_account_key": "svc_ops",
            "display_name": "Svc Ops",
            "description": "",
            "scopes_jsonb": ["read", "write"],
            "allowed_project_ids_jsonb": [5],
            "allowed_workflow_ids_jsonb": ["wf.default"],
            "is_active": True,
            "created_by": "system",
        },
        {
            "id": 1,
            "account_id": "acc_default",
            "service_account_key": "svc_ops",
            "display_name": "Svc Ops Disabled",
            "description": "paused",
            "scopes_jsonb": ["read"],
            "allowed_project_ids_jsonb": [5],
            "allowed_workflow_ids_jsonb": ["wf.default"],
            "is_active": False,
            "created_by": "system",
        },
        {
            "id": 1,
            "account_id": "acc_default",
            "scopes_jsonb": ["read", "write"],
            "allowed_project_ids_jsonb": [5],
            "allowed_workflow_ids_jsonb": ["wf.default"],
            "is_active": True,
        },
        {
            "id": 10,
            "service_account_id": 1,
            "account_id": "acc_default",
            "label": "token-a",
            "scopes_jsonb": ["read", "write"],
            "allowed_project_ids_jsonb": [5],
            "allowed_workflow_ids_jsonb": ["wf.default"],
            "expires_at": now + timedelta(days=7),
            "last_used_at": None,
            "revoked": False,
            "created_by": "system",
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": 10,
            "revoked": False,
            "service_account_key": "svc_ops",
            "token_scopes_jsonb": ["read"],
            "token_allowed_project_ids_jsonb": ["5"],
            "token_allowed_workflow_ids_jsonb": ["wf.default"],
            "service_scopes_jsonb": ["read", "write"],
            "service_allowed_project_ids_jsonb": [5],
            "service_allowed_workflow_ids_jsonb": ["wf.default"],
            "is_active": True,
        },
        {
            "id": 10,
            "service_account_id": 1,
            "account_id": "acc_default",
            "label": "token-b",
            "scopes_jsonb": ["read"],
            "allowed_project_ids_jsonb": [5],
            "allowed_workflow_ids_jsonb": ["wf.default"],
            "expires_at": now + timedelta(days=7),
            "last_used_at": None,
            "revoked": False,
            "created_by": "system",
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": 10,
            "service_account_id": 1,
            "account_id": "acc_default",
            "label": "token-a",
            "scopes_jsonb": ["read", "write"],
            "allowed_project_ids_jsonb": [5],
            "allowed_workflow_ids_jsonb": ["wf.default"],
            "expires_at": now + timedelta(days=7),
            "last_used_at": None,
            "revoked": True,
            "created_by": "system",
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": 10,
            "service_account_id": 1,
            "account_id": "acc_default",
            "label": "token-a",
            "scopes_jsonb": ["read", "write"],
            "allowed_project_ids_jsonb": [5],
            "allowed_workflow_ids_jsonb": ["wf.default"],
            "expires_at": now + timedelta(days=7),
            "last_used_at": None,
            "revoked": True,
            "created_by": "system",
            "created_at": now,
            "updated_at": now,
        },
        {
            "token_id": 10,
            "token_account_id": "acc_default",
            "token_label": "token-a",
            "token_scopes_jsonb": ["read"],
            "token_allowed_project_ids_jsonb": [5],
            "token_allowed_workflow_ids_jsonb": ["wf.default"],
            "expires_at": now + timedelta(days=1),
            "revoked": False,
            "service_account_id": 1,
            "service_account_account_id": "acc_default",
            "service_account_key": "svc_ops",
            "display_name": "Svc Ops",
            "description": "",
            "service_scopes_jsonb": ["read", "write"],
            "service_allowed_project_ids_jsonb": [5],
            "service_allowed_workflow_ids_jsonb": ["wf.default"],
            "is_active": True,
        },
        {"is_active": True},
        {
            "id": 41,
            "label": "pat-a",
            "scopes_jsonb": ["read"],
            "allowed_project_ids_jsonb": ["00000000-0000-0000-0000-000000000105"],
            "allowed_workflow_ids_jsonb": ["wf.default"],
            "expires_at": now + timedelta(days=3),
            "created_at": now,
            "account_id": "acc_default",
        },
        (2,),
        {
            "pat_id": 41,
            "user_id": 7,
            "pat_account_id": "acc_default",
            "pat_label": "pat-a",
            "pat_scopes_jsonb": ["read"],
            "pat_allowed_project_ids_jsonb": [5],
            "pat_allowed_workflow_ids_jsonb": ["wf.default"],
            "expires_at": now + timedelta(days=2),
            "revoked": False,
            "user_id_check": 7,
            "username": "alice",
            "display_name": "Alice",
            "is_active": True,
        },
        {"id": 41, "label": "pat-a", "revoked": True},
        {
            "id": 1,
            "account_id": "acc_default",
            "service_account_key": "svc_ops",
            "display_name": "Svc Ops Disabled",
            "description": "paused",
            "scopes_jsonb": ["read"],
            "allowed_project_ids_jsonb": [5],
            "allowed_workflow_ids_jsonb": ["wf.default"],
            "is_active": False,
            "created_by": "system",
        },
    ]

    accounts = logger.list_accounts(include_inactive=False, search="acc", limit=20, offset=0)
    assert accounts
    assert accounts[0]["account_id"] == "acc_default"
    assert logger.is_account_active("acc_default") is True

    created_account = logger.create_account(account_id="acc_ops", display_name="Ops")
    assert created_account
    assert created_account["account_id"] == "acc_ops"
    toggled = logger.set_account_active("acc_ops", is_active=False)
    assert toggled
    assert toggled["is_active"] is False

    service_accounts = logger.list_service_accounts(
        account_id="acc_default", include_inactive=False
    )
    assert service_accounts
    assert service_accounts[0]["service_account_key"] == "svc_ops"
    created_service = logger.create_service_account(
        service_account_key="svc_ops",
        display_name="Svc Ops",
        account_id="acc_default",
        scopes=["read", "write"],
        allowed_project_ids=[5],
        allowed_workflow_ids=["wf.default"],
    )
    assert created_service
    assert created_service["service_account_key"] == "svc_ops"
    updated_service = logger.update_service_account(
        "svc_ops",
        display_name="Svc Ops Disabled",
        description="paused",
        account_id="acc_default",
        scopes=["read"],
        allowed_project_ids=[5],
        allowed_workflow_ids=["wf.default"],
        is_active=False,
    )
    assert updated_service
    assert updated_service["is_active"] is False

    service_tokens = logger.list_service_account_tokens("svc_ops")
    assert service_tokens
    assert service_tokens[0]["label"] == "token-a"
    machine = logger.create_machine_token(
        service_account_key="svc_ops",
        label="token-a",
        scopes=["read"],
        allowed_project_ids=[5],
        allowed_workflow_ids=["wf.default"],
        expires_in_days=7,
    )
    assert machine["token"].startswith("fs_svc_")
    machine_insert = next(
        (params for sql, params in conn.calls if "INSERT INTO machine_tokens" in sql),
        (),
    )
    assert machine_insert[7] == 7
    assert machine_insert[8] is False

    updated_machine = logger.update_machine_token(
        "10",
        label="token-b",
        scopes=["read"],
        allowed_project_ids=[5],
        allowed_workflow_ids=["wf.default"],
        account_id="acc_default",
    )
    assert updated_machine
    assert updated_machine["label"] == "token-b"
    machine_update = next(
        (
            params
            for sql, params in conn.calls
            if "UPDATE machine_tokens mt" in sql and "label = %s" in sql
        ),
        (),
    )
    assert machine_update[0] == "token-b"
    assert json.loads(machine_update[1]) == ["read"]
    assert machine_update[4] == "10"
    assert machine_update[5] == "acc_default"

    revoked_machine = logger.revoke_machine_token(10)
    assert revoked_machine
    assert revoked_machine["revoked"] is True
    deleted_machine = logger.delete_machine_token(10)
    assert deleted_machine
    assert deleted_machine["id"] == "10"

    resolved_machine = logger.resolve_machine_token(machine["token"])
    assert resolved_machine
    assert resolved_machine["principal_type"] == "service_account"

    project_uuid = "00000000-0000-0000-0000-000000000105"
    pat = logger.create_pat(
        user_id=7,
        label="pat-a",
        scopes=["read"],
        allowed_project_ids=[project_uuid],
        allowed_workflow_ids=["wf.default"],
        expires_in_days=3,
        account_id="acc_default",
    )
    assert pat["raw_token"].startswith("fs_pat_")
    assert pat["allowed_project_ids"] == [project_uuid]

    listed = logger.list_user_pats(7)
    assert listed
    assert listed[0]["label"] == "pat-a"
    paginated = logger.list_user_pats_paginated(7, include_revoked=True, limit=1, offset=0)
    assert paginated
    assert paginated[0]["label"] == "pat-b"
    assert logger.count_user_pats(7, include_revoked=True) == 2

    resolved_pat = logger.resolve_pat(pat["raw_token"])
    assert resolved_pat
    assert resolved_pat["principal_type"] == "user"
    revoked_pat = logger.revoke_pat(41, 7)
    assert revoked_pat
    assert revoked_pat["revoked"] is True
    deleted_service = logger.delete_service_account("svc_ops", account_id="acc_default")
    assert deleted_service
    assert deleted_service["service_account_key"] == "svc_ops"


def test_machine_token_label_only_update_preserves_token_authority() -> None:
    logger, conn = _make_logger()
    now = datetime.now(UTC)
    conn.fetchone_results = [
        {
            "id": 10,
            "revoked": False,
            "service_account_key": "svc_ops",
            "token_scopes_jsonb": ["jobs.read"],
            "token_allowed_project_ids_jsonb": [5],
            "token_allowed_workflow_ids_jsonb": ["wf.narrow"],
            "service_scopes_jsonb": ["jobs.read", "jobs.write"],
            "service_allowed_project_ids_jsonb": ["5", "6"],
            "service_allowed_workflow_ids_jsonb": ["wf.narrow", "wf.broad"],
            "is_active": True,
        },
        {
            "id": 10,
            "service_account_id": 1,
            "account_id": "acc_default",
            "label": "renamed",
            "scopes_jsonb": ["jobs.read"],
            "allowed_project_ids_jsonb": ["5"],
            "allowed_workflow_ids_jsonb": ["wf.narrow"],
            "expires_at": None,
            "last_used_at": None,
            "revoked": False,
            "created_by": "system",
            "created_at": now,
            "updated_at": now,
        },
    ]

    updated = logger.update_machine_token("10", label="renamed")

    assert updated
    assert updated["label"] == "renamed"
    update_params = next(
        params
        for sql, params in conn.calls
        if "UPDATE machine_tokens mt" in sql and "label = %s" in sql
    )
    assert update_params[1:4] == (None, None, None)
    update_sql = next(
        sql
        for sql, _params in conn.calls
        if "UPDATE machine_tokens mt" in sql and "label = %s" in sql
    )
    assert update_sql.count("COALESCE(") == 3


def test_machine_token_create_persists_explicit_empty_scopes() -> None:
    logger, conn = _make_logger()
    now = datetime.now(UTC)
    conn.fetchone_results = [
        {
            "id": 1,
            "account_id": "acc_default",
            "scopes_jsonb": ["jobs:*"],
            "allowed_project_ids_jsonb": [],
            "allowed_workflow_ids_jsonb": [],
            "is_active": True,
        },
        {
            "id": 10,
            "service_account_id": 1,
            "account_id": "acc_default",
            "label": "no-authority",
            "scopes_jsonb": [],
            "allowed_project_ids_jsonb": [],
            "allowed_workflow_ids_jsonb": [],
            "expires_at": None,
            "last_used_at": None,
            "revoked": False,
            "created_by": "system",
            "created_at": now,
            "updated_at": now,
        },
    ]

    created = logger.create_machine_token(
        service_account_key="ci",
        label="no-authority",
        scopes=[],
    )

    assert created["scopes"] == []
    insert_params = next(
        params for sql, params in conn.calls if "INSERT INTO machine_tokens" in sql
    )
    assert json.loads(insert_params[4]) == []


def test_pat_resolution_fails_after_account_membership_is_removed() -> None:
    logger, conn = _make_logger()
    conn.fetchone_results = [
        {
            "pat_id": 41,
            "user_id": 7,
            "pat_account_id": "acc_default",
            "pat_label": "pat-a",
            "pat_scopes_jsonb": ["read:*"],
            "pat_allowed_project_ids_jsonb": [],
            "pat_allowed_workflow_ids_jsonb": [],
            "expires_at": None,
            "revoked": False,
            "user_id_check": 7,
            "username": "alice",
            "display_name": "Alice",
            "is_active": True,
        },
        {"is_active": True},
        None,
    ]
    conn.fetchall_results = [[]]

    assert logger.resolve_pat("fs_pat_stale") is None

    combined_sql = "\n".join(sql for sql, _params in conn.calls)
    assert "FROM user_account_memberships" in combined_sql


def test_machine_token_resolution_fails_when_account_is_disabled() -> None:
    logger, conn = _make_logger()
    conn.fetchone_results = [
        {
            "token_id": 10,
            "token_account_id": "acc_disabled",
            "token_label": "ci",
            "token_scopes_jsonb": ["command:*"],
            "token_allowed_project_ids_jsonb": [],
            "token_allowed_workflow_ids_jsonb": [],
            "expires_at": None,
            "revoked": False,
            "service_account_id": 9,
            "service_account_account_id": "acc_disabled",
            "service_account_key": "ci",
            "display_name": "CI",
            "description": "",
            "service_scopes_jsonb": ["command:*"],
            "service_allowed_project_ids_jsonb": [],
            "service_allowed_workflow_ids_jsonb": [],
            "is_active": True,
        },
        {"is_active": False},
    ]

    assert logger.resolve_machine_token("fs_svc_disabled") is None


def test_machine_token_resolution_narrows_to_current_service_account_grants() -> None:
    logger, conn = _make_logger()
    conn.fetchone_results = [
        {
            "token_id": 10,
            "token_account_id": "acc_default",
            "token_label": "ci",
            "token_scopes_jsonb": ["command:*", "notify:dispatch"],
            "token_allowed_project_ids_jsonb": [5, 6],
            "token_allowed_workflow_ids_jsonb": ["wf.old", "wf.current"],
            "expires_at": None,
            "revoked": False,
            "service_account_id": 9,
            "service_account_account_id": "acc_default",
            "service_account_key": "ci",
            "display_name": "CI",
            "description": "",
            "service_scopes_jsonb": ["notify:dispatch"],
            "service_allowed_project_ids_jsonb": [6],
            "service_allowed_workflow_ids_jsonb": ["wf.current"],
            "is_active": True,
        },
        {"is_active": True},
    ]

    principal = logger.resolve_machine_token("fs_svc_narrowed")

    assert principal is not None
    assert principal["scopes"] == ["notify:dispatch"]
    assert principal["allowed_project_ids"] == ["6"]
    assert principal["allowed_workflow_ids"] == ["wf.current"]


def test_machine_token_resolution_keeps_explicit_empty_scopes_empty() -> None:
    from core.entrypoints.mcp.server_discovery import visible_mcp_tool_names

    logger, conn = _make_logger()
    conn.fetchone_results = [
        {
            "token_id": 10,
            "token_account_id": "acc_default",
            "token_label": "no-authority",
            "token_scopes_jsonb": [],
            "token_allowed_project_ids_jsonb": [],
            "token_allowed_workflow_ids_jsonb": [],
            "expires_at": None,
            "revoked": False,
            "service_account_id": 9,
            "service_account_account_id": "acc_default",
            "service_account_key": "ci",
            "display_name": "CI",
            "description": "",
            "service_scopes_jsonb": ["jobs:*", "notify:dispatch"],
            "service_allowed_project_ids_jsonb": [],
            "service_allowed_workflow_ids_jsonb": [],
            "is_active": True,
        },
        {"is_active": True},
    ]

    principal = logger.resolve_machine_token("fs_svc_empty")

    assert principal is not None
    assert principal["scopes"] == []
    assert visible_mcp_tool_names(principal["scopes"], auth_enabled=True) == set()


def test_accounts_mixin_supports_canonical_accounts_id_schema() -> None:
    account_id = "00000000-0000-0000-0000-000000000002"
    logger, conn = _make_logger(
        table_exists=lambda name: name == "accounts",
        column_exists=lambda table, column: table == "accounts" and column in {"id", "account_key"},
    )
    conn.fetchone_results = [
        {"is_active": True},
        {"account_id": account_id, "display_name": "Default", "is_active": True},
        {"account_id": account_id, "display_name": "Default", "is_active": True},
    ]

    assert logger.is_account_active(account_id) is True
    sql, params = conn.calls[-1]
    assert "WHERE id = %s" in sql
    assert params == (account_id,)

    account = logger.get_account("default")
    assert account
    assert account["account_id"] == account_id
    sql, params = conn.calls[-1]
    assert "WHERE account_key = %s" in sql
    assert params == ("default",)

    created = logger.create_account(account_id=account_id, display_name="Default")
    assert created
    assert created["account_id"] == account_id
    sql, params = conn.calls[-1]
    assert "INSERT INTO accounts (id, account_key, display_name, is_active)" in sql
    assert params[0] == account_id


def test_extension_account_validation_errors() -> None:
    logger, _conn = _make_logger(
        table_exists=lambda name: name in {"accounts", "machine_tokens"},
        column_exists=lambda _table, _column: True,
    )
    with pytest.raises(ValueError):
        logger.create_account(account_id="!", display_name="bad")

    with pytest.raises(ValueError):
        logger.create_machine_token(service_account_key="", label="x")


def test_extension_scan_list_and_get_paths_cover_mixin_branches() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name
            in {
                "accounts",
                "extension_bundles",
                "extension_versions",
                "enabled_extensions",
                "extension_installs",
            }
        ),
        column_exists=lambda _table, _column: True,
    )
    conn.fetchone_results = [
        {"account_id": "acc_default", "display_name": "Main account", "is_active": True},
        {"id": 501},
        {"id": 502},
        {"id": 503},
    ]
    scanned = [
        None,
        {"extension_key": "", "kind": "api"},
        {
            "extension_key": "ext.alpha",
            "kind": "api",
            "status": "unexpected",
            "display_name": "Alpha",
            "description": "desc",
            "bundle_root": "/tmp/ext-alpha",
            "manifest_path": "/tmp/ext-alpha/manifest.yaml",
            "version": "1.2.3",
            "scan_error": "",
            "manifest_version": 0,
            "compile_errors": ["compile failed"],
            "compile_warnings": ["warn"],
            "validation_errors": [],
            "validation_warnings": ["vw"],
            "lifecycle": {"validate": "warn"},
            "account_id": "",
            "manifest": {"extension_id": "ext.alpha"},
            "skills": ["query"],
            "required_scopes": ["read"],
            "supported_events": ["job.finished"],
            "schemas": {"in": {"type": "object"}},
            "healthcheck": {"path": "/health"},
            "entrypoint": {"type": "http"},
            "ui_manifest": {"panels": []},
            "query_definitions": [{"id": "q1"}],
            "action_definitions": [{"id": "a1"}],
            "compiled_manifest": {"compiled": True},
            "machine_auth": {"kind": "service_account"},
            "account_scope": {"mode": "all"},
            "checksum": "abc123",
        },
        {
            "extension_key": "ext.beta",
            "kind": "webhook",
            "status": "valid",
            "display_name": "Beta",
            "version": "",
            "manifest": {"extension_id": "ext.beta"},
        },
    ]

    synced = _sync_extension_scan(logger, scanned)
    assert [row["extension_key"] for row in synced] == ["ext.alpha", "ext.beta"]

    calls_sql = "\n".join(sql for sql, _ in conn.calls)
    assert "INSERT INTO extension_bundles" in calls_sql
    assert "INSERT INTO extension_versions" in calls_sql
    assert "latest_compile_status" in calls_sql
    assert "required_scopes_jsonb" in calls_sql
    assert "validation_warnings_jsonb" in calls_sql

    extension_row = {
        "bundle_id": 501,
        "extension_key": "ext.alpha",
        "kind": "api",
        "display_name": "Alpha",
        "description": "desc",
        "bundle_root": "/tmp/ext-alpha",
        "manifest_path": "/tmp/ext-alpha/manifest.yaml",
        "latest_version": "1.2.3",
        "status": "valid",
        "last_scan_error": None,
        "version_id": 601,
        "manifest_jsonb": {"extension_id": "ext.alpha"},
        "skills_jsonb": ["query"],
        "schemas_jsonb": {"in": {"type": "object"}},
        "healthcheck_jsonb": {"path": "/health"},
        "entrypoint_jsonb": {"type": "http"},
        "checksum": "abc123",
        "scan_error": None,
        "enabled": True,
        "enabled_at": None,
        "updated_at": None,
        "config_jsonb": {"account_id": "acc_default"},
        "manifest_version": 1,
        "required_scopes_jsonb": ["read"],
        "supported_events_jsonb": ["job.finished"],
        "ui_manifest_jsonb": {"panels": []},
        "query_definitions_jsonb": [{"id": "q1"}],
        "action_definitions_jsonb": [{"id": "a1"}],
        "compiled_manifest_jsonb": {"compiled": True},
        "compile_errors_jsonb": [],
        "compile_warnings_jsonb": ["warn"],
        "validation_errors_jsonb": [],
        "validation_warnings_jsonb": ["vw"],
        "machine_auth_jsonb": {"kind": "service_account"},
        "account_scope_jsonb": {"mode": "all"},
        "account_id": "acc_default",
    }
    conn.fetchall_results = [[extension_row], [extension_row]]

    listed = logger.list_extensions(enabled_only=True)
    assert listed
    assert listed[0]["extension_id"] == "ext.alpha"
    assert logger.get_extension("") is None
    assert logger.get_extension("ext.alpha")["extension_key"] == "ext.alpha"

    logger_no_tables, _ = _make_logger(table_exists=lambda _name: False)
    assert logger_no_tables.list_extensions(enabled_only=False) == []


def test_extension_scan_disables_missing_bundle_paths() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name
            in {
                "accounts",
                "extension_bundles",
                "extension_versions",
                "enabled_extensions",
                "extension_installs",
            }
        ),
        column_exists=lambda _table, _column: True,
    )
    conn.fetchone_results = [
        {"account_id": "acc_default", "display_name": "Main account", "is_active": True},
        {"id": 777},
    ]
    present_root = str(Path(__file__).resolve().parent)
    present_manifest = str(Path(__file__).resolve())
    conn.fetchall_results = [
        [
            {
                "extension_key": "ext.present",
                "bundle_root": present_root,
                "manifest_path": present_manifest,
            },
            {
                "extension_key": "ext.missing",
                "bundle_root": "/tmp/flow-steward-ext-missing",
                "manifest_path": "/tmp/flow-steward-ext-missing/extension.yaml",
            },
        ]
    ]

    synced = _sync_extension_scan(
        logger,
        [
            {
                "extension_key": "ext.present",
                "kind": "notification_provider",
                "status": "valid",
                "display_name": "Present",
                "version": "1.0.0",
                "manifest": {"extension_id": "ext.present"},
            }
        ],
    )
    assert [row["extension_key"] for row in synced] == ["ext.present"]

    calls_sql = "\n".join(sql for sql, _ in conn.calls)
    assert "UPDATE extension_bundles" in calls_sql
    assert "UPDATE enabled_extensions" in calls_sql
    assert "descriptor_source = 'file'" not in calls_sql
    assert "descriptor_source = 'extension_scan'" in calls_sql
    stale_updates = [
        params
        for sql, params in conn.calls
        if "UPDATE extension_bundles" in sql and isinstance(params, tuple) and params
    ]
    assert stale_updates
    assert stale_updates[-1][-1] == "ext.missing"
    canonical_invalidations = [
        params for sql, params in conn.calls if "extension_invalid_or_missing" in str(params)
    ]
    assert canonical_invalidations == [("ext.missing", "extension_invalid_or_missing")]


def test_extension_scan_uses_one_explicit_transaction(monkeypatch) -> None:
    from core.infrastructure.db import db_pool

    logger, conn = _make_logger(
        table_exists=lambda name: name in {"accounts", "extension_bundles", "extension_versions"},
        column_exists=lambda _table, _column: True,
    )
    conn.fetchone_results = [
        {"account_id": "acc_default", "display_name": "Main account", "is_active": True}
    ]
    conn.fetchall_results = [[]]
    entered: list[object] = []

    @contextmanager
    def _transaction(target):
        entered.append(target)
        yield target

    monkeypatch.setattr(db_pool, "transaction_connection", _transaction)

    assert _sync_extension_scan(logger, []) == []
    assert entered == [conn]


def test_invalid_extension_scan_deactivates_canonical_routes() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name in {"accounts", "extension_bundles", "extension_versions", "enabled_extensions"}
        ),
        column_exists=lambda _table, _column: True,
    )
    conn.fetchone_results = [
        {"account_id": "acc_default", "display_name": "Main account", "is_active": True},
        {"id": 901},
        {"id": 902},
    ]
    conn.fetchall_results = [[]]

    _sync_extension_scan(
        logger,
        [
            {
                "extension_key": "ext.invalid",
                "kind": "tool_provider",
                "status": "invalid",
                "display_name": "Invalid extension",
                "version": "1.0.0",
                "manifest": {"extension_id": "ext.invalid"},
            }
        ],
    )

    canonical_invalidations = [
        params for sql, params in conn.calls if "extension_invalid_or_missing" in str(params)
    ]
    assert canonical_invalidations == [("ext.invalid", "extension_invalid_or_missing")]


def test_invalid_extension_without_kind_still_deactivates_canonical_routes() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: name in {"accounts", "extension_bundles", "extension_versions"},
        column_exists=lambda _table, _column: True,
    )
    conn.fetchone_results = [
        {"account_id": "acc_default", "display_name": "Main account", "is_active": True}
    ]

    assert (
        _sync_extension_scan(
            logger, [{"extension_key": "ext.broken", "kind": "", "status": "invalid"}]
        )
        == []
    )

    canonical_invalidations = [
        params for _sql, params in conn.calls if "extension_invalid_or_missing" in str(params)
    ]
    assert canonical_invalidations == [("ext.broken", "extension_invalid_or_missing")]


def test_extension_scan_projects_new_valid_bundles_disabled_by_default() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name
            in {
                "accounts",
                "extension_bundles",
                "extension_versions",
                "enabled_extensions",
                "extension_releases",
                "extension_installs",
            }
        ),
        column_exists=lambda _table, _column: True,
    )
    conn.fetchone_results = [
        {"account_id": "acc_default", "display_name": "Main account", "is_active": True},
        {"id": 901},
        {"id": 902},
        {
            "manifest_jsonb": {"extension_id": "ext.startup", "kind": "local"},
            "compiled_manifest_jsonb": {"extension_id": "ext.startup"},
        },
        {},
        None,
    ]

    synced = _sync_extension_scan(
        logger,
        [
            {
                "extension_key": "ext.startup",
                "kind": "local",
                "status": "valid",
                "display_name": "Startup",
                "version": "1.0.0",
                "manifest": {"extension_id": "ext.startup", "kind": "local"},
                "compiled_manifest": {"extension_id": "ext.startup"},
            }
        ],
    )

    assert [row["extension_key"] for row in synced] == ["ext.startup"]
    calls_sql = "\n".join(sql for sql, _ in conn.calls)
    assert "INSERT INTO extension_releases" in calls_sql
    assert "INSERT INTO extension_installs" in calls_sql
    assert "INSERT INTO enabled_extensions" in calls_sql
    assert "state = extension_installs.state" in calls_sql
    assert "EXCLUDED.config_jsonb || extension_installs.config_jsonb" in calls_sql
    assert "state = state =" not in calls_sql
    assert "installed_at = installed_at =" not in calls_sql
    enabled_insert_params = [
        params
        for sql, params in conn.calls
        if "INSERT INTO enabled_extensions" in sql and isinstance(params, tuple)
    ]
    assert enabled_insert_params
    assert False in enabled_insert_params[-1]


def test_extension_scan_preserves_disabled_install_runtime_enablement() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name
            in {
                "accounts",
                "extension_bundles",
                "extension_versions",
                "enabled_extensions",
                "extension_releases",
                "extension_installs",
            }
        ),
        column_exists=lambda _table, _column: True,
    )
    conn.fetchone_results = [
        {"account_id": "acc_default", "display_name": "Main account", "is_active": True},
        {"id": 901},
        {"id": 902},
        {
            "manifest_jsonb": {"extension_id": "ext.disabled", "kind": "local"},
            "compiled_manifest_jsonb": {"extension_id": "ext.disabled"},
        },
        {"install_id": "existing-install", "state": "disabled"},
    ]

    synced = _sync_extension_scan(
        logger,
        [
            {
                "extension_key": "ext.disabled",
                "kind": "local",
                "status": "valid",
                "display_name": "Disabled",
                "version": "1.0.0",
                "manifest": {"extension_id": "ext.disabled", "kind": "local"},
                "compiled_manifest": {"extension_id": "ext.disabled"},
            }
        ],
    )

    assert [row["extension_key"] for row in synced] == ["ext.disabled"]
    enabled_insert_params = [
        params
        for sql, params in conn.calls
        if "INSERT INTO enabled_extensions" in sql and isinstance(params, tuple)
    ]
    assert enabled_insert_params
    assert False in enabled_insert_params[-1]
    enabled_upsert_sql = "\n".join(
        sql for sql, _ in conn.calls if "INSERT INTO enabled_extensions" in sql
    )
    assert "enabled = EXCLUDED.enabled" in enabled_upsert_sql
    assert "config_jsonb = EXCLUDED.config_jsonb" not in enabled_upsert_sql
    install_upsert_sql = "\n".join(
        sql for sql, _ in conn.calls if "INSERT INTO extension_installs" in sql
    )
    assert (
        "config_jsonb = EXCLUDED.config_jsonb || extension_installs.config_jsonb"
        in install_upsert_sql
    )


def test_extension_scan_keeps_unconfigured_remote_runtime_disabled() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name
            in {
                "accounts",
                "extension_bundles",
                "extension_versions",
                "enabled_extensions",
                "extension_releases",
                "extension_installs",
            }
        ),
        column_exists=lambda _table, _column: True,
    )
    manifest = {
        "extension_id": "acme.llm",
        "kind": "tool_provider",
        "entrypoint": {"mode": "remote_https"},
        "runtime": {"contract_version": "extension_host_v2"},
    }
    conn.fetchone_results = [
        {"account_id": "acc_default", "display_name": "Main account", "is_active": True},
        {"id": 901},
        {"id": 902},
        {"manifest_jsonb": manifest, "compiled_manifest_jsonb": manifest},
        {},
        {"enabled": True},
    ]

    _sync_extension_scan(
        logger,
        [
            {
                "extension_key": "acme.llm",
                "kind": "tool_provider",
                "status": "valid",
                "display_name": "Acme LLM",
                "version": "1.0.0",
                "manifest": manifest,
                "compiled_manifest": manifest,
            }
        ],
    )

    install_params = [
        params
        for sql, params in conn.calls
        if "INSERT INTO extension_installs" in sql and isinstance(params, tuple)
    ]
    assert install_params
    assert "remote_https" in install_params[-1]
    assert "tool_provider" not in install_params[-1]
    assert install_params[-1][9] == "disabled"
    enabled_insert_params = [
        params
        for sql, params in conn.calls
        if "INSERT INTO enabled_extensions" in sql and isinstance(params, tuple)
    ]
    assert enabled_insert_params
    assert False in enabled_insert_params[-1]
    release_params = [
        params
        for sql, params in conn.calls
        if "INSERT INTO extension_releases" in sql and isinstance(params, tuple)
    ]
    assert release_params
    assert '["remote_https"]' in release_params[-1]
    assert "extension_host_v2" in release_params[-1]


def test_extension_scan_preserves_active_remote_runtime_url() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name
            in {
                "accounts",
                "extension_bundles",
                "extension_versions",
                "enabled_extensions",
                "extension_releases",
                "extension_installs",
            }
        ),
        column_exists=lambda _table, _column: True,
    )
    manifest = {
        "extension_id": "acme.llm",
        "kind": "tool_provider",
        "entrypoint": {"mode": "remote_https"},
        "runtime": {"contract_version": "extension_host_v2"},
    }
    conn.fetchone_results = [
        {"account_id": "acc_default", "display_name": "Main account", "is_active": True},
        {"id": 901},
        {"id": 902},
        {"manifest_jsonb": manifest, "compiled_manifest_jsonb": manifest},
        {
            "install_id": "existing-install",
            "state": "active",
            "remote_base_url": "https://runtime.example.com",
            "release_id": "902",
            "release_descriptor_sha256": hashlib.sha256(
                json.dumps(manifest).encode("utf-8")
            ).hexdigest(),
        },
    ]

    _sync_extension_scan(
        logger,
        [
            {
                "extension_key": "acme.llm",
                "kind": "tool_provider",
                "status": "valid",
                "display_name": "Acme LLM",
                "version": "1.0.0",
                "manifest": manifest,
                "compiled_manifest": manifest,
            }
        ],
    )

    install_calls = [
        (sql, params)
        for sql, params in conn.calls
        if "INSERT INTO extension_installs" in sql and isinstance(params, tuple)
    ]
    assert install_calls
    sql, params = install_calls[-1]
    assert "remote_base_url" in sql
    assert params[8] == "https://runtime.example.com"
    assert params[9] == "active"
    assert "remote_base_url = extension_installs.remote_base_url" in sql
    assert not any(
        "extension_release_or_descriptor_changed" in statement for statement, _params in conn.calls
    )


@pytest.mark.parametrize("change_kind", ["release", "descriptor"])
def test_extension_scan_invalidates_models_when_release_or_descriptor_changes(
    change_kind: str,
) -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name
            in {
                "accounts",
                "extension_bundles",
                "extension_versions",
                "enabled_extensions",
                "extension_releases",
                "extension_installs",
            }
        ),
        column_exists=lambda _table, _column: True,
    )
    manifest = {
        "extension_id": "acme.llm",
        "kind": "tool_provider",
        "entrypoint": {"mode": "remote_https"},
        "runtime": {"contract_version": "extension_host_v2"},
    }
    current_descriptor_sha = hashlib.sha256(json.dumps(manifest).encode("utf-8")).hexdigest()
    conn.fetchone_results = [
        {"account_id": "acc_default", "display_name": "Main account", "is_active": True},
        {"id": 901},
        {"id": 902},
        {"manifest_jsonb": manifest, "compiled_manifest_jsonb": manifest},
        {
            "install_id": "existing-install",
            "state": "active",
            "remote_base_url": "https://runtime.example.com",
            "release_id": "old-release" if change_kind == "release" else "902",
            "release_descriptor_sha256": (
                "old-descriptor-sha" if change_kind == "descriptor" else current_descriptor_sha
            ),
        },
    ]

    _sync_extension_scan(
        logger,
        [
            {
                "extension_key": "acme.llm",
                "kind": "tool_provider",
                "status": "valid",
                "display_name": "Acme LLM",
                "version": "2.0.0",
                "account_id": "acc_default",
                "manifest": manifest,
                "compiled_manifest": manifest,
            }
        ],
    )

    invalidation_calls = [
        (sql, params)
        for sql, params in conn.calls
        if "extension_release_or_descriptor_changed" in str(params)
    ]
    assert len(invalidation_calls) == 1
    sql, params = invalidation_calls[0]
    assert "verification_status = 'stale'" in sql
    assert "verification_generation = NULL" in sql
    assert "enabled = false" in sql
    assert params == (
        "902" if change_kind == "descriptor" else "",
        "acc_default",
        "existing-install",
        "extension_release_or_descriptor_changed",
    )
    affected_install_lock_indexes = [
        index
        for index, (statement, _params) in enumerate(conn.calls)
        if "FROM extension_installs" in statement
        and "ORDER BY account_id, install_id" in statement
        and "FOR UPDATE" in statement
    ]
    release_upsert_index = next(
        index
        for index, (statement, _params) in enumerate(conn.calls)
        if "INSERT INTO extension_releases" in statement
    )
    membership_advisory_lock_index = next(
        index
        for index, (statement, params) in enumerate(conn.calls)
        if "pg_advisory_xact_lock" in statement
        and params == ("fs.extension_release_membership", "902")
    )
    assert affected_install_lock_indexes
    assert membership_advisory_lock_index < affected_install_lock_indexes[-1]
    assert affected_install_lock_indexes[-1] < release_upsert_index


def test_extension_scan_projects_existing_disabled_runtime_state_to_install() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name
            in {
                "accounts",
                "extension_bundles",
                "extension_versions",
                "enabled_extensions",
                "extension_releases",
                "extension_installs",
            }
        ),
        column_exists=lambda _table, _column: True,
    )
    conn.fetchone_results = [
        {"account_id": "acc_default", "display_name": "Main account", "is_active": True},
        {"id": 901},
        {"id": 902},
        {
            "manifest_jsonb": {"extension_id": "ext.runtime_disabled", "kind": "local"},
            "compiled_manifest_jsonb": {"extension_id": "ext.runtime_disabled"},
        },
        {},
        {"enabled": False},
    ]

    synced = _sync_extension_scan(
        logger,
        [
            {
                "extension_key": "ext.runtime_disabled",
                "kind": "local",
                "status": "valid",
                "display_name": "Runtime Disabled",
                "version": "1.0.0",
                "manifest": {"extension_id": "ext.runtime_disabled", "kind": "local"},
                "compiled_manifest": {"extension_id": "ext.runtime_disabled"},
            }
        ],
    )

    assert [row["extension_key"] for row in synced] == ["ext.runtime_disabled"]
    install_params = [
        params
        for sql, params in conn.calls
        if "INSERT INTO extension_installs" in sql and isinstance(params, tuple)
    ]
    assert install_params
    assert "disabled" in install_params[-1]
    enabled_insert_params = [
        params
        for sql, params in conn.calls
        if "INSERT INTO enabled_extensions" in sql and isinstance(params, tuple)
    ]
    assert enabled_insert_params
    assert False in enabled_insert_params[-1]


def test_extension_scan_skips_until_default_account_exists() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: name in {"extension_bundles", "extension_versions", "accounts"},
        column_exists=lambda _table, _column: True,
    )
    conn.fetchone_results = [None]

    synced = _sync_extension_scan(
        logger,
        [
            {
                "extension_key": "ext.pending",
                "kind": "notification_provider",
                "status": "valid",
                "display_name": "Pending",
                "version": "1.0.0",
                "manifest": {"extension_id": "ext.pending"},
            }
        ],
    )

    assert synced == []
    executed_sql = "\n".join(sql for sql, _ in conn.calls)
    assert "SELECT account_id::text AS account_id, display_name, is_active" in executed_sql
    assert "INSERT INTO extension_bundles" not in executed_sql


def test_enable_extension_and_private_resolution_quality_branches() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name
            in {
                "enabled_extensions",
                "extension_bundles",
                "extension_versions",
            }
        ),
        column_exists=lambda _table, _column: True,
    )
    cur = conn.cursor()

    conn.fetchone_results = [None]
    with pytest.raises(ValueError, match="Unknown extension"):
        logger._resolve_extension_bundle_version(cur, normalized_key="missing", version=None)

    conn.fetchone_results = [{"id": 1, "latest_version": ""}]
    with pytest.raises(ValueError, match="no scanned version"):
        logger._resolve_extension_bundle_version(cur, normalized_key="ext.alpha", version=None)

    conn.fetchone_results = [{"id": 1, "latest_version": "1.0.0"}, None]
    with pytest.raises(ValueError, match="is not available"):
        logger._resolve_extension_bundle_version(cur, normalized_key="ext.alpha", version=None)

    conn.fetchone_results = [{"id": 1, "latest_version": "1.0.0"}, {"id": 2}]
    resolved = logger._resolve_extension_bundle_version(
        cur, normalized_key="ext.alpha", version=None
    )
    assert resolved == ("1", "2", "1.0.0")

    conn.fetchone_results = [{"compile_errors_jsonb": ["boom"], "validation_errors_jsonb": []}]
    with pytest.raises(ValueError, match="compile/validation errors"):
        logger._assert_extension_version_quality(
            cur,
            version_id=2,
            normalized_key="ext.alpha",
            version_value="1.0.0",
        )

    conn.fetchone_results = [{"compile_errors_jsonb": [], "validation_errors_jsonb": []}]
    logger._assert_extension_version_quality(
        cur,
        version_id=2,
        normalized_key="ext.alpha",
        version_value="1.0.0",
    )

    logger_without_tables, _ = _make_logger(table_exists=lambda _name: False)
    assert _enable_extension(logger_without_tables, "ext.alpha") is None
    with pytest.raises(ValueError, match="extension_key is required"):
        _enable_extension(logger, "")

    logger.get_extension = lambda extension_key: {"extension_key": extension_key}  # type: ignore[method-assign]
    conn.fetchone_results = [
        {"id": 1, "latest_version": "1.0.0"},
        {"id": 2},
        {"compile_errors_jsonb": [], "validation_errors_jsonb": []},
    ]
    enabled = _enable_extension(
        logger,
        "ext.alpha",
        enabled=True,
        config={"account_id": "acc_ops", "required_scopes": ["read"]},
    )
    assert enabled
    assert enabled["extension_key"] == "ext.alpha"


def test_log_and_list_extension_invocations_cover_context_and_early_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: name == "extension_invocation_logs",
        column_exists=lambda _table, _column: True,
    )
    monkeypatch.setattr(
        extensions_mixin,
        "_trace_id_for_job",
        lambda _job_id: "trace-job-42",
    )

    logger.get_extension = lambda extension_key: (  # type: ignore[method-assign]
        {"bundle_id": 7, "version_id": 9, "account_id": "acc_default"}
        if extension_key == "ext.alpha"
        else None
    )
    logger.log_extension_invocation(
        extension_key="ext.alpha",
        invocation_type="",
        status="",
        request={
            "token": "secret",
            "callback_url": "https://example.test/webhook/extensions/fixture.callback-provider/updates/raw-token",
            "callback_secret": "callback-secret",
        },
        response={"ok": True},
        error_text="",
        account_id="",
        actor_type="",
        actor_id="actor-1",
        request_context={"source": "unit"},
        query_id="q1",
        action_id="a1",
        command_id="cmd-1",
        job_id=42,
        workflow_id="wf.default",
        group_id="group-a",
        step_id="step-b",
        step_execution_id="6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
    )
    insert_sql, insert_params = next(
        (sql, params)
        for sql, params in conn.calls
        if "INSERT INTO extension_invocation_logs" in sql
    )
    columns_block = insert_sql.split("(", 1)[1].split(")", 1)[0]
    insert_columns = [column.strip() for column in columns_block.split(",")]
    insert_payload = dict(zip(insert_columns, insert_params, strict=False))
    request_context_payload = json.loads(insert_payload["request_context_jsonb"])
    request_payload = json.loads(insert_payload["request_jsonb"])
    assert insert_payload["job_id"] == "42"
    assert insert_payload["workflow_id"] == "wf.default"
    assert insert_payload["group_id"] == "group-a"
    assert insert_payload["step_id"] == "step-b"
    assert insert_payload["step_execution_id"] == "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42"
    assert "phase_id" not in insert_payload
    assert request_context_payload == {
        "source": "unit",
        "trace_id": "trace-job-42",
    }
    assert request_payload["callback_url"] == "[REDACTED]"
    assert request_payload["callback_secret"] == "[REDACTED]"

    before_calls = len(conn.calls)
    logger.log_extension_invocation(
        extension_key="missing",
        invocation_type="run",
        status="ok",
    )
    assert len(conn.calls) == before_calls

    conn.fetchall_results = [
        [
            {
                "id": 1,
                "request_jsonb": '{"input": 1}',
                "response_jsonb": '{"ok": true}',
                "request_context_jsonb": "{}",
                "job_id": 42,
                "created_at": None,
            }
        ]
    ]
    invocations = logger.list_extension_invocations(job_id=42, account_id="acc_default", limit=5000)
    assert invocations
    assert invocations[0]["request_context_jsonb"]["trace_id"] == "trace-job-42"
    select_sql = conn.calls[-1][0]
    assert "WHERE job_id = %s AND account_id = %s" in select_sql
    assert "request_context_jsonb" not in select_sql

    logger_without_tables, _ = _make_logger(table_exists=lambda _name: False)
    logger_without_tables.log_extension_invocation(
        extension_key="ext.alpha",
        invocation_type="run",
        status="ok",
    )
    assert logger_without_tables.list_extension_invocations() == []


def test_list_extension_invocations_fails_closed_without_structured_job_column() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: name == "extension_invocation_logs",
        column_exists=lambda _table, column: column != "job_id",
    )
    conn.fetchall_results = [
        [
            {
                "id": "foreign-invocation",
                "request_context_jsonb": {"job_id": "job-foreign"},
            }
        ]
    ]

    assert (
        logger.list_extension_invocations(
            job_id="job-current",
            account_id="acc-default",
        )
        == []
    )
    assert not any("SELECT *" in sql for sql, _params in conn.calls)


def test_account_machine_and_pat_edge_paths_cover_remaining_branches() -> None:
    logger, conn = _make_logger(
        table_exists=lambda name: (
            name
            in {
                "accounts",
                "machine_tokens",
                "personal_access_tokens",
            }
        ),
        column_exists=lambda _table, _column: True,
    )

    assert logger.is_account_active("") is False

    logger_no_accounts, no_accounts_conn = _make_logger(table_exists=lambda _name: False)
    no_accounts_conn.fetchone_results = [None]
    assert logger_no_accounts.is_account_active("acc_default") is True

    with pytest.raises(ValueError, match="account_id is required"):
        logger.create_account(account_id="", display_name="x")
    with pytest.raises(ValueError, match="account_id is required"):
        logger.set_account_active("", is_active=True)
    with pytest.raises(ValueError, match="Default account cannot be disabled"):
        logger.set_account_active("acc_default", is_active=False)

    conn.fetchone_results = [None]
    with pytest.raises(ValueError, match="already exists"):
        logger.create_account(account_id="acc_ops", display_name="Ops")

    conn.fetchone_results = [None]
    assert logger.revoke_pat(999, 7) is None

    conn.fetchone_results = [(1,)]
    assert logger.count_user_pats(7, include_revoked=False) == 1

    conn.fetchone_results = [{"token_id": 10, "revoked": True}]
    assert logger.resolve_machine_token("fs_svc_xxx") is None
    conn.fetchone_results = [
        {
            "token_id": 10,
            "revoked": False,
            "expires_at": datetime.now(UTC) - timedelta(seconds=1),
            "is_active": True,
        }
    ]
    assert logger.resolve_machine_token("fs_svc_yyy") is None
    conn.fetchone_results = [
        {
            "token_id": 10,
            "revoked": False,
            "expires_at": None,
            "is_active": False,
        }
    ]
    assert logger.resolve_machine_token("fs_svc_zzz") is None

    conn.fetchone_results = [{"pat_id": 1, "revoked": True}]
    assert logger.resolve_pat("fs_pat_xxx") is None
    conn.fetchone_results = [
        {
            "pat_id": 1,
            "revoked": False,
            "expires_at": datetime.now(UTC) - timedelta(seconds=1),
            "is_active": True,
        }
    ]
    assert logger.resolve_pat("fs_pat_yyy") is None
    conn.fetchone_results = [
        {
            "pat_id": 1,
            "revoked": False,
            "expires_at": None,
            "is_active": False,
        }
    ]
    assert logger.resolve_pat("fs_pat_zzz") is None


def test_authoritative_workflow_update_tombstones_stale_and_adds_new_in_one_cursor() -> None:
    """Removal must tombstone, not merely disable.

    Disabling alone left the row listed for good: an operator who unbound a
    workflow still saw it, with no way to get rid of it. The insert path clears
    deleted_at on re-add, so the round trip stays intact.
    """
    logger, conn = _make_logger()

    logger.save_project_workflows(5, ["wf.new"], operator_default_workflow_id="")

    removals = [
        (sql, params)
        for sql, params in conn.calls
        if "UPDATE project_workflows" in sql and "enabled = false" in sql
    ]
    assert removals, "removing siblings must issue an update"
    for sql, params in removals:
        assert "deleted_at = NOW()" in sql, sql
        assert "deleted_at IS NULL" in sql, "already-removed rows must not be re-stamped"
        assert params == (5, ["wf.new"])

    inserts = [sql for sql, _params in conn.calls if "INSERT INTO project_workflows" in sql]
    assert inserts, "kept bindings must be upserted"
    assert any(params[1] == "wf.new" for _sql, params in conn.calls if len(params) > 1)
    assert any("deleted_at = NULL" in sql for sql in inserts), (
        "re-adding a removed binding has to clear the tombstone"
    )


def test_removing_every_project_workflow_binding_tombstones_them() -> None:
    """The clear-all branch has to behave the same as the partial one."""
    logger, conn = _make_logger()

    logger.save_project_workflows(5, [], operator_default_workflow_id="")

    updates = [sql for sql, _params in conn.calls if "UPDATE project_workflows" in sql]
    assert updates, "clearing bindings must issue an update"
    assert all("deleted_at = NOW()" in sql for sql in updates), updates


def test_binding_list_can_exclude_removed_bindings() -> None:
    """The operator-facing list filters tombstones; history keeps them."""
    logger, conn = _make_logger()

    logger.get_project_workflows(5, enabled_only=False, include_deleted=False)
    filtered = [sql for sql, _params in conn.calls if "FROM project_workflows" in sql]
    assert filtered
    assert "pw.deleted_at IS NULL" in filtered[-1]
    assert "rw.deleted_at IS NULL" in filtered[-1]

    logger, conn = _make_logger()
    logger.get_project_workflows(5, enabled_only=False)
    unfiltered = [sql for sql, _params in conn.calls if "FROM project_workflows" in sql]
    assert unfiltered, "the job display-name lookup still needs rows for bindings since removed"
    assert "pw.deleted_at IS NULL" not in unfiltered[-1], (
        "the job display-name lookup still needs rows for bindings since removed"
    )
    assert "rw.deleted_at AS workflow_deleted_at" in unfiltered[-1]
    assert "rw.deleted_at IS NULL" not in unfiltered[-1]


def test_enabled_binding_list_excludes_soft_deleted_registry_workflows() -> None:
    """A live link cannot make a deleted workflow current again."""
    logger, conn = _make_logger()

    logger.get_project_workflows(5, enabled_only=True)

    queries = [sql for sql, _params in conn.calls if "FROM project_workflows" in sql]
    assert queries
    assert "pw.enabled = true" in queries[-1]
    assert "pw.deleted_at IS NULL" in queries[-1]
    assert "rw.deleted_at IS NULL" in queries[-1]
