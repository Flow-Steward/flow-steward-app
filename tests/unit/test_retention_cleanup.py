from __future__ import annotations

import json
from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.tooling.ops import retention_cleanup, retention_repository


def test_run_cleanup_fails_closed_when_durable_artifact_sweep_fails(monkeypatch) -> None:
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = MagicMock()
    storage = object()
    sweep = MagicMock(
        return_value={
            "artifacts_expired": 0,
            "datasets_expired": 0,
            "cleanup_failed": 1,
        }
    )
    monkeypatch.setattr(
        retention_cleanup,
        "_connect_psycopg_runtime",
        lambda _dsn: nullcontext(conn),
    )
    monkeypatch.setattr(
        retention_cleanup,
        "resolve_artifact_storage_adapter_from_env",
        lambda **_kwargs: storage,
    )
    monkeypatch.setattr(
        retention_cleanup,
        "sweep_expired_artifact_storage",
        sweep,
        raising=False,
    )
    monkeypatch.setattr(
        retention_cleanup, "_resolve_account_retention_policies", lambda *_a, **_k: []
    )
    monkeypatch.setattr(retention_cleanup, "_delete_or_count", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        retention_cleanup, "_delete_or_count_idempotency_tombstones", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(
        retention_cleanup, "_delete_or_count_registry_changelog", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(retention_cleanup, "_cleanup_trashed_entities", lambda *_a, **_k: (0, 0))
    cfg = retention_cleanup.RetentionConfig(
        jobs_days=30,
        job_logs_days=30,
        action_logs_days=30,
        events_days=30,
        retry_queue_days=14,
        trash_days=30,
        terminal_job_checkpoints_days=30,
        dry_run=False,
        retention_defaults={},
    )

    with pytest.raises(RuntimeError, match="artifact storage cleanup failed"):
        retention_cleanup.run_cleanup("postgres://test", cfg)

    sweep.assert_called_once_with(storage=storage, dry_run=False)
    conn.commit.assert_not_called()


@pytest.mark.parametrize("commit_fails", [False, True])
def test_run_cleanup_finalizes_artifact_marker_only_after_successful_commit(
    monkeypatch, commit_fails: bool
) -> None:
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = MagicMock()
    if commit_fails:
        conn.commit.side_effect = RuntimeError("commit unavailable")
    storage = object()
    job_row = {"id": "job-1", "project_id": "project-1", "meta": {}}
    finalize = MagicMock(return_value=True)

    def _cleanup_trash(*_args, artifact_purges_after_commit, **_kwargs):
        artifact_purges_after_commit.append(job_row)
        return 1, 0

    def _finalize(row, *, storage):
        assert conn.commit.called
        return finalize(row, storage=storage)

    monkeypatch.setattr(
        retention_cleanup,
        "_connect_psycopg_runtime",
        lambda _dsn: nullcontext(conn),
    )
    monkeypatch.setattr(
        retention_cleanup,
        "resolve_artifact_storage_adapter_from_env",
        lambda **_kwargs: storage,
    )
    monkeypatch.setattr(
        retention_cleanup,
        "sweep_expired_artifact_storage",
        lambda **_kwargs: {
            "artifacts_expired": 0,
            "datasets_expired": 0,
            "cleanup_failed": 0,
        },
    )
    monkeypatch.setattr(
        retention_cleanup, "_resolve_account_retention_policies", lambda *_a, **_k: []
    )
    monkeypatch.setattr(retention_cleanup, "_delete_or_count", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        retention_cleanup, "_delete_or_count_idempotency_tombstones", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(
        retention_cleanup, "_delete_or_count_registry_changelog", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(retention_cleanup, "_cleanup_trashed_entities", _cleanup_trash)
    monkeypatch.setattr(retention_cleanup, "finalize_job_artifact_purge", _finalize)
    cfg = retention_cleanup.RetentionConfig(
        jobs_days=30,
        job_logs_days=30,
        action_logs_days=30,
        events_days=30,
        retry_queue_days=14,
        trash_days=30,
        terminal_job_checkpoints_days=30,
        dry_run=False,
        retention_defaults={},
    )

    if commit_fails:
        with pytest.raises(RuntimeError, match="commit unavailable"):
            retention_cleanup.run_cleanup("postgres://test", cfg)
        finalize.assert_not_called()
    else:
        retention_cleanup.run_cleanup("postgres://test", cfg)
        finalize.assert_called_once_with(job_row, storage=storage)


class _Cursor:
    def __init__(self) -> None:
        self.last_sql = ""
        self.last_params = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql, params=None) -> None:
        self.last_sql = str(sql)
        self.last_params = params

    def fetchone(self):
        if "to_regclass" not in self.last_sql:
            return None
        table = (self.last_params or ("",))[0]
        if table in {"accounts", "system_settings"}:
            return (table,)
        return None

    def fetchall(self):
        if "information_schema.columns" in self.last_sql:
            return []
        if "SELECT id FROM accounts" in self.last_sql:
            return [("acc_default",), ("acc_team",)]
        return []


class _Conn:
    def __init__(self) -> None:
        self.cursor_obj = _Cursor()

    def cursor(self, *args, **kwargs):
        del args, kwargs
        return self.cursor_obj


def test_resolve_account_retention_policies_reads_settings_per_account(monkeypatch) -> None:
    calls: list[str] = []
    conn = _Conn()
    cfg = retention_cleanup.RetentionConfig(
        jobs_days=30,
        job_logs_days=30,
        action_logs_days=30,
        events_days=30,
        retry_queue_days=14,
        trash_days=30,
        terminal_job_checkpoints_days=30,
        dry_run=False,
        retention_defaults={
            "action_logs_days": 30,
            "job_logs_days": 30,
            "trash_days": 30,
            "terminal_job_checkpoints_days": 30,
        },
    )

    def _fake_get_retention_settings(*, conn, table_exists, defaults=None, account_id=None):
        del conn, defaults
        assert table_exists("system_settings") is True
        calls.append(str(account_id))
        if account_id == "acc_team":
            return {
                "account_id": "acc_team",
                "action_logs_days": 7,
                "job_logs_days": 14,
                "trash_days": 21,
                "terminal_job_checkpoints_days": 28,
                "revisions_keep": 3,
            }
        return {
            "account_id": DEFAULT_ACCOUNT_ID,
            "action_logs_days": 30,
            "job_logs_days": 30,
            "trash_days": 30,
            "terminal_job_checkpoints_days": 30,
            "revisions_keep": 5,
        }

    monkeypatch.setattr(
        retention_cleanup.system_settings_store,
        "get_retention_settings",
        _fake_get_retention_settings,
    )

    policies = retention_cleanup._resolve_account_retention_policies(conn, cfg=cfg, cache={})

    assert calls == [DEFAULT_ACCOUNT_ID, "acc_default", "acc_team"]
    assert policies == [
        retention_cleanup.AccountRetentionPolicy(
            account_id=DEFAULT_ACCOUNT_ID,
            action_logs_days=30,
            job_logs_days=30,
            trash_days=30,
            terminal_job_checkpoints_days=30,
        ),
        retention_cleanup.AccountRetentionPolicy(
            account_id="acc_default",
            action_logs_days=30,
            job_logs_days=30,
            trash_days=30,
            terminal_job_checkpoints_days=30,
        ),
        retention_cleanup.AccountRetentionPolicy(
            account_id="acc_team",
            action_logs_days=7,
            job_logs_days=14,
            trash_days=21,
            terminal_job_checkpoints_days=28,
            revisions_keep=3,
        ),
    ]


def test_resolve_account_retention_policies_applies_cli_overrides_to_all_accounts(
    monkeypatch,
) -> None:
    conn = _Conn()
    cfg = retention_cleanup.RetentionConfig(
        jobs_days=30,
        job_logs_days=45,
        action_logs_days=60,
        events_days=30,
        retry_queue_days=14,
        trash_days=10,
        terminal_job_checkpoints_days=15,
        dry_run=False,
        retention_defaults={
            "action_logs_days": 30,
            "job_logs_days": 30,
            "trash_days": 30,
            "terminal_job_checkpoints_days": 30,
        },
        job_logs_days_override=45,
        action_logs_days_override=60,
        trash_days_override=10,
        terminal_job_checkpoints_days_override=15,
    )

    monkeypatch.setattr(
        retention_cleanup.system_settings_store,
        "get_retention_settings",
        lambda **_kwargs: {
            "account_id": "ignored",
            "action_logs_days": 1,
            "job_logs_days": 1,
            "trash_days": 1,
            "terminal_job_checkpoints_days": 1,
            "revisions_keep": 5,
        },
    )

    policies = retention_cleanup._resolve_account_retention_policies(conn, cfg=cfg, cache={})

    assert policies == [
        retention_cleanup.AccountRetentionPolicy(
            account_id=DEFAULT_ACCOUNT_ID,
            action_logs_days=60,
            job_logs_days=45,
            trash_days=10,
            terminal_job_checkpoints_days=15,
        ),
        retention_cleanup.AccountRetentionPolicy(
            account_id="acc_default",
            action_logs_days=60,
            job_logs_days=45,
            trash_days=10,
            terminal_job_checkpoints_days=15,
        ),
        retention_cleanup.AccountRetentionPolicy(
            account_id="acc_team",
            action_logs_days=60,
            job_logs_days=45,
            trash_days=10,
            terminal_job_checkpoints_days=15,
        ),
    ]


def test_list_terminal_jobs_qualifies_columns_when_account_is_scoped_by_project(
    monkeypatch,
) -> None:
    class _TerminalJobsCursor:
        def __init__(self) -> None:
            self.sql = ""
            self.params = None

        def execute(self, sql, params=None) -> None:
            self.sql = str(sql)
            self.params = params

        def fetchall(self):
            return [("job-1",)]

    cur = _TerminalJobsCursor()
    monkeypatch.setattr(retention_repository, "_table_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(retention_repository, "_column_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        retention_repository,
        "_project_account_scoping_available",
        lambda *_args, **_kwargs: True,
    )

    job_ids = retention_repository._list_terminal_job_ids(
        cur,
        cache={},
        account_id="account-1",
        days=21,
    )

    assert job_ids == ["job-1"]
    assert "j.finished_at IS NOT NULL" in cur.sql
    assert "j.status = ANY(%s)" in cur.sql
    assert "j.deleted_at IS NULL" in cur.sql
    assert cur.params == (
        "account-1",
        21,
        retention_repository._TERMINAL_JOB_STATUSES,
    )


class _DeleteCursor:
    def __init__(self) -> None:
        self.last_sql = ""
        self.last_params = None
        self.rowcount = 0
        self.deleted_sql: list[str] = []

    def execute(self, sql, params=None) -> None:
        self.last_sql = str(sql)
        self.last_params = params
        self.deleted_sql.append(self.last_sql)
        if self.last_sql.startswith("DELETE FROM jobs "):
            self.rowcount = 1
        else:
            self.rowcount = 0

    def fetchone(self):
        if self.last_sql.startswith("SELECT id, project_id, meta FROM jobs WHERE id = %s"):
            meta = {
                "files": [
                    {
                        "artifact_id": "art_ret_1",
                        "object_key": "artifacts/art_ret_1/blob",
                    }
                ]
            }
            return (77, "project-1", json.dumps(meta))
        return None


class _Storage:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.objects: dict[str, bytes] = {}

    def delete(self, key: str) -> bool:
        self.deleted.append(str(key))
        return True

    def get(self, key: str):
        if key in self.deleted:
            return None
        if key in self.objects:
            return self.objects[key]
        if key == "artifacts/_metadata/art_ret_1.json":
            return (
                b'{"artifact_id":"art_ret_1","object_key":"artifacts/art_ret_1/blob",'
                b'"owner_scope":{"project_id":"project-1","job_id":"77"}}'
            )
        return None

    def put(self, key: str, data: bytes, *, content_type: str):
        self.objects[str(key)] = bytes(data)
        return {"key": str(key), "size_bytes": len(data), "content_type": content_type}

    def list_key_page(self, _prefix: str, **_kwargs):
        return [], None


def test_delete_trashed_job_purges_artifact_storage_before_row_delete(monkeypatch) -> None:
    cur = _DeleteCursor()
    storage = _Storage()
    monkeypatch.setattr(retention_cleanup, "_table_exists", lambda *_args, **_kwargs: True)

    deleted = retention_cleanup._delete_trashed_job_with_artifacts(
        cur,
        cache={},
        job_id=77,
        artifact_storage=storage,
    )

    assert deleted == 1
    assert "artifacts/art_ret_1/blob" in storage.deleted
    assert "artifacts/_metadata/art_ret_1.json" in storage.deleted


def test_delete_terminal_job_uses_job_log_window_and_purges_artifacts_first(
    monkeypatch,
) -> None:
    cur = _DeleteCursor()
    storage = _Storage()
    monkeypatch.setattr(retention_cleanup, "_table_exists", lambda *_args, **_kwargs: True)

    deleted = retention_cleanup._delete_terminal_job_with_artifacts(
        cur,
        cache={},
        job_id=77,
        days=14,
        artifact_storage=storage,
    )

    assert deleted == 1
    assert "artifacts/art_ret_1/blob" in storage.deleted
    delete_job_sql = next(sql for sql in cur.deleted_sql if sql.startswith("DELETE FROM jobs "))
    assert "finished_at < NOW() - (%s * INTERVAL '1 day')" in delete_job_sql
    assert "status = ANY(%s)" in delete_job_sql
    assert cur.last_params == (77, 14, retention_cleanup._TERMINAL_JOB_STATUSES)


def test_delete_terminal_job_keeps_row_when_artifact_delete_is_refused(monkeypatch) -> None:
    class _RefusingStorage:
        def delete(self, _key: str) -> bool:
            return False

        def stat(self, key: str):
            return {"key": key, "size_bytes": 1}

    cur = _DeleteCursor()
    monkeypatch.setattr(retention_cleanup, "_table_exists", lambda *_args, **_kwargs: True)

    _raises_input_472_1 = _RefusingStorage()
    with pytest.raises(RuntimeError, match="Artifact cleanup failed"):
        retention_cleanup._delete_terminal_job_with_artifacts(
            cur, cache={}, job_id=77, days=14, artifact_storage=_raises_input_472_1
        )

    assert not any(sql.startswith("DELETE FROM jobs") for sql in cur.deleted_sql)


def test_delete_trashed_job_keeps_row_when_artifact_delete_is_refused(monkeypatch) -> None:
    class _RefusingStorage:
        def delete(self, _key: str) -> bool:
            return False

        def stat(self, key: str):
            return {"key": key, "size_bytes": 1}

    cur = _DeleteCursor()
    monkeypatch.setattr(retention_cleanup, "_table_exists", lambda *_args, **_kwargs: True)

    _raises_input_495_1 = _RefusingStorage()
    with pytest.raises(RuntimeError, match="Artifact cleanup failed"):
        retention_cleanup._delete_trashed_job_with_artifacts(
            cur, cache={}, job_id=77, artifact_storage=_raises_input_495_1
        )

    assert not any(sql.startswith("DELETE FROM jobs") for sql in cur.deleted_sql)


def test_delete_trashed_job_retries_after_storage_purge_then_database_failure(
    monkeypatch,
) -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    class _FailingDeleteCursor(_DeleteCursor):
        def __init__(self) -> None:
            super().__init__()
            self.fail_job_delete = True

        def execute(self, sql, params=None) -> None:
            if str(sql).startswith("DELETE FROM jobs ") and self.fail_job_delete:
                self.fail_job_delete = False
                raise RuntimeError("database unavailable")
            super().execute(sql, params)

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, _artifact_meta = plane.create_artifact(
        artifact_id="art_ret_1",
        owner_scope={"project_id": "project-1", "job_id": "77"},
        run_id="77",
        step_id="upload",
        filename="blob",
        content_type="application/octet-stream",
        body=b"body",
    )
    assert artifact.artifact_id == "art_ret_1"
    cur = _FailingDeleteCursor()
    monkeypatch.setattr(retention_cleanup, "_table_exists", lambda *_args, **_kwargs: True)

    with pytest.raises(RuntimeError, match="database unavailable"):
        retention_cleanup._delete_trashed_job_with_artifacts(
            cur,
            cache={},
            job_id=77,
            artifact_storage=storage,
        )

    deleted = retention_cleanup._delete_trashed_job_with_artifacts(
        cur,
        cache={},
        job_id=77,
        artifact_storage=storage,
    )

    assert deleted == 1


class _CheckpointCleanupCursor:
    def __init__(self, *, count: int = 2) -> None:
        self.last_sql = ""
        self.last_params = None
        self.rowcount = count
        self.count = count
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=None) -> None:
        self.last_sql = str(sql)
        self.last_params = params
        self.calls.append((self.last_sql, params))

    def fetchone(self):
        if "to_regclass" in self.last_sql:
            return ((self.last_params or ("",))[0],)
        if "information_schema.columns" in self.last_sql:
            return (1,)
        if "COUNT(*)" in self.last_sql:
            return (self.count,)
        return None


def test_terminal_job_checkpoint_cleanup_is_terminal_account_scoped_dry_run() -> None:
    cur = _CheckpointCleanupCursor(count=3)

    rows = retention_repository._delete_or_count_terminal_job_checkpoints(
        cur,
        cache={},
        account_id="acc_team",
        days=17,
        dry_run=True,
    )

    assert rows == 9
    sql = "\n".join(call[0] for call in cur.calls)
    assert "SELECT COUNT(*) FROM checkpoint_writes" in sql
    assert "SELECT COUNT(*) FROM checkpoint_blobs" in sql
    assert "SELECT COUNT(*) FROM checkpoints" in sql
    assert "checkpoint_migrations" not in sql
    assert "j.status = ANY(%s)" in sql
    assert "p.account_id = %s" in sql
    assert "j.finished_at < NOW() - (%s * INTERVAL '1 day')" in sql
    assert all("DELETE FROM" not in call[0] for call in cur.calls)
    assert any(
        params == (17, retention_repository._TERMINAL_JOB_STATUSES, "acc_team")
        for _, params in cur.calls
    )


def test_delete_checkpoint_rows_for_thread_skips_blank_thread_and_migrations() -> None:
    cur = _CheckpointCleanupCursor(count=2)

    assert retention_repository._delete_checkpoint_rows_for_thread(cur, cache={}, thread_id="") == 0
    assert (
        retention_repository._delete_checkpoint_rows_for_thread(
            cur,
            cache={},
            thread_id="thread-1",
        )
        == 6
    )

    sql = "\n".join(call[0] for call in cur.calls)
    assert "DELETE FROM checkpoint_writes WHERE thread_id = %s" in sql
    assert "DELETE FROM checkpoint_blobs WHERE thread_id = %s" in sql
    assert "DELETE FROM checkpoints WHERE thread_id = %s" in sql
    assert "checkpoint_migrations" not in sql


def test_idempotency_cleanup_keeps_results_until_tombstone_expiry():
    calls: list[tuple[str, tuple]] = []

    class _Cursor:
        rowcount = 3

        def execute(self, sql, params=()):
            calls.append((str(sql), tuple(params or ())))

    deleted = retention_repository._delete_or_count_idempotency_tombstones(
        _Cursor(),
        cache={
            "table:request_idempotency": True,
            "column:request_idempotency.tombstone_expires_at": True,
        },
        dry_run=False,
    )

    assert deleted == 3
    assert "tombstone_expires_at <= NOW()" in calls[-1][0]
    assert "NOT EXISTS" in calls[-1][0]
    assert "FROM jobs" in calls[-1][0]
    assert "result_expires_at" not in calls[-1][0]


def test_event_retention_preserves_latest_public_state_marker():
    assert "event_type = 'workflow_run.state'" in retention_cleanup.EVENT_RETENTION_WHERE
    assert "MAX(marker.id)" not in retention_cleanup.EVENT_RETENTION_WHERE
    assert "marker.job_id = platform_events.job_id" in retention_cleanup.EVENT_RETENTION_WHERE
    assert "ORDER BY COALESCE((marker.detail ->> 'revision')::bigint, 0) DESC" in (
        retention_cleanup.EVENT_RETENTION_WHERE
    )
    assert "LIMIT 1" in retention_cleanup.EVENT_RETENTION_WHERE


def test_revision_retention_counts_content_revisions_per_entity_in_one_account():
    cur = _CheckpointCleanupCursor(count=7)

    deleted = retention_repository._delete_or_count_registry_changelog(
        cur,
        cache={},
        account_id=DEFAULT_ACCOUNT_ID,
        keep=5,
        dry_run=False,
    )

    assert deleted == 7
    assert cur.last_sql.lstrip().startswith("DELETE FROM registry_changelog WHERE id IN")
    assert "PARTITION BY candidate.entity_type, candidate.entity_id" in cur.last_sql
    assert "ORDER BY candidate.created_at DESC, candidate.id DESC" in cur.last_sql
    assert "INTERVAL" not in cur.last_sql
    assert cur.last_params == (DEFAULT_ACCOUNT_ID, DEFAULT_ACCOUNT_ID, 5)


def test_scheduled_retention_runs_with_every_accounts_own_settings(monkeypatch):
    seen = {}

    def fake_run(dsn, cfg):
        seen["dsn"], seen["cfg"] = dsn, cfg
        return {"trash": 2, "registry_changelog": 0}

    monkeypatch.setattr(retention_cleanup, "resolve_observability_dsn", lambda value: "dsn://x")
    monkeypatch.setattr(
        retention_cleanup,
        "_load_db_retention_settings",
        lambda *_a, **_k: {
            "action_logs_days": 30,
            "job_logs_days": 30,
            "trash_days": 30,
            "terminal_job_checkpoints_days": 30,
            "revisions_keep": 5,
        },
    )
    monkeypatch.setattr(retention_cleanup, "run_cleanup", fake_run)

    assert retention_cleanup.run_scheduled_retention() == {"trash": 2, "registry_changelog": 0}
    assert seen["dsn"] == "dsn://x"
    cfg = seen["cfg"]
    assert cfg.dry_run is False
    assert cfg.revisions_keep_override is None
    assert cfg.trash_days_override is None
    assert cfg.retention_defaults["revisions_keep"] == 5
