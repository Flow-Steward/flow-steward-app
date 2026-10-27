from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.tooling.db import init_db


def _write_bootstrap_layout(root: Path) -> Path:
    schema_dir = root / "db" / "schema"
    seeds_dir = root / "db" / "seeds"
    schema_dir.mkdir(parents=True, exist_ok=True)
    seeds_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "001_schema_part_01.sql").write_text("-- schema", encoding="utf-8")
    (seeds_dir / "lookup_seed.json").write_text("{}", encoding="utf-8")
    (seeds_dir / "registry_seed.json").write_text("{}", encoding="utf-8")
    return schema_dir


def test_bootstrap_dir_points_to_clean_bootstrap_root(tmp_path: Path) -> None:
    assert init_db.bootstrap_dir(tmp_path) == tmp_path / "db" / "schema"
    assert init_db.seeds_dir(tmp_path) == tmp_path / "db" / "seeds"


def test_validate_bootstrap_layout_requires_schema_and_seed_files(tmp_path: Path) -> None:
    schema_dir = tmp_path / "db" / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "001_schema_part_01.sql").write_text("-- schema", encoding="utf-8")

    with pytest.raises(FileNotFoundError) as exc:
        init_db.validate_bootstrap_layout(tmp_path)

    message = str(exc.value)
    assert "lookup_seed.json" in message
    assert "registry_seed.json" in message


def test_validate_bootstrap_layout_requires_schema_files(tmp_path: Path) -> None:
    seeds_dir = tmp_path / "db" / "seeds"
    seeds_dir.mkdir(parents=True, exist_ok=True)
    (seeds_dir / "lookup_seed.json").write_text("{}", encoding="utf-8")
    (seeds_dir / "registry_seed.json").write_text("{}", encoding="utf-8")

    with pytest.raises(FileNotFoundError) as exc:
        init_db.validate_bootstrap_layout(tmp_path)

    assert "db/schema/*.sql" in str(exc.value)


def test_validate_bootstrap_layout_rejects_legacy_migrations_dir(tmp_path: Path) -> None:
    _write_bootstrap_layout(tmp_path)
    (tmp_path / "migrations").mkdir()

    with pytest.raises(RuntimeError, match="migrations"):
        init_db.validate_bootstrap_layout(tmp_path)


def test_bootstrap_canonical_schema_installs_only_into_empty_public_schema(
    tmp_path: Path, monkeypatch
) -> None:
    _write_bootstrap_layout(tmp_path)
    responses = iter(((0,), (None,)))

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def execute(self, _sql: str) -> None:
            return None

        def fetchone(self):
            return next(responses)

    connection = MagicMock()
    connection.cursor.return_value = _Cursor()
    monkeypatch.setattr(init_db.psycopg_runtime, "connect", lambda _dsn: connection)
    reconcile = MagicMock(return_value={"files": 1, "statements": 2})
    monkeypatch.setattr(init_db, "reconcile_canonical_schema", reconcile)

    result = init_db.bootstrap_canonical_schema_if_empty(dsn="postgresql://example", root=tmp_path)

    assert result == {"status": "installed", "files": 1, "statements": 2}
    reconcile.assert_called_once()
    connection.close.assert_called_once()


def test_bootstrap_canonical_schema_rejects_partial_external_database(
    tmp_path: Path, monkeypatch
) -> None:
    _write_bootstrap_layout(tmp_path)
    responses = iter(((3,), (None,)))

    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.side_effect = lambda: next(responses)
    connection = MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(init_db.psycopg_runtime, "connect", lambda _dsn: connection)

    with pytest.raises(RuntimeError, match="partial or foreign"):
        init_db.bootstrap_canonical_schema_if_empty(dsn="postgresql://example", root=tmp_path)


def test_apply_clean_bootstrap_validates_layout_before_seeding(tmp_path: Path, monkeypatch) -> None:
    _write_bootstrap_layout(tmp_path)
    from core.application.tooling.db import bootstrap_seed

    captured: dict[str, str] = {}

    def _fake_apply_bootstrap_seed(*, dsn: str | None = None, author: str = "system"):
        captured["dsn"] = str(dsn)
        captured["author"] = author
        return {"lookup": {"accounts": 1}, "registry": {"agents": 2}}

    monkeypatch.setattr(bootstrap_seed, "apply_bootstrap_seed", _fake_apply_bootstrap_seed)

    summary = init_db.apply_clean_bootstrap(
        dsn="postgresql://unused",
        author="tester",
        root=tmp_path,
    )

    assert captured == {"dsn": "postgresql://unused", "author": "tester"}
    assert summary == {"lookup": {"accounts": 1}, "registry": {"agents": 2}}


def test_cleanup_retired_project_schema_removes_db_type(
    monkeypatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class _FakeCursor:
        rowcount = 3

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def execute(self, sql: str, params=None) -> None:
            calls.append((str(sql), params))

    class _FakeConnection:
        autocommit = False

        def cursor(self):
            return _FakeCursor()

        def close(self) -> None:
            calls.append(("close", None))

    monkeypatch.setattr(init_db.psycopg_runtime, "connect", lambda _dsn: _FakeConnection())

    summary = init_db.cleanup_retired_project_schema(dsn="postgresql://example")

    executed_sql = "\n".join(sql for sql, _params in calls)
    assert "DROP COLUMN IF EXISTS db_type" in executed_sql
    assert "ADD COLUMN runtime_status text DEFAULT 'unknown'::text NOT NULL" in executed_sql
    assert "ADD COLUMN supported_role_types text[] DEFAULT '{}'::text[] NOT NULL" in executed_sql
    assert "project_type" not in executed_sql
    assert calls[-1] == ("close", None)
    assert summary == {
        "dropped_db_type": 1,
        "ensured_extension_install_runtime_status": 1,
        "ensured_project_connection_runtime_status": 1,
        "ensured_skill_supported_role_types": 1,
    }


def test_init_extension_catalog_recovers_publication_and_scans_before_safe_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from core.application.extensions import install as extension_install
    from core.infrastructure.db import platform_stores
    from core.infrastructure.extension_runtime import (
        bundle_cleanup,
        extension_runtime,
        lifecycle_lock,
        publication_recovery,
    )

    root = tmp_path / "extensions"
    events: list[str] = []
    extension_rows = [
        {
            "extension_key": "acme.demo",
            "bundle_root": str(root / ".releases" / "acme_demo" / "2.0.0"),
        }
    ]

    class _InstallStore:
        def __init__(self, conn) -> None:
            assert conn == "db-connection"

        def get(self, *, extension_id: str, account_id: str):
            events.append(f"installed:{extension_id}:{account_id}")
            return object()

    class _Logger:
        conn = "db-connection"

        @contextmanager
        def extension_lifecycle_transaction(self):
            yield self.conn

        def list_extensions(self, *, enabled_only: bool):
            assert enabled_only is False
            events.append("list")
            return extension_rows

        def sync_extension_scan_records(self, transaction_conn, scanned):
            assert transaction_conn == self.conn
            events.append("sync")
            return scanned, []

    def reconcile(
        *,
        extensions_root,
        is_installed,
        intent_path=None,
        discard_installed_intents,
        discovered_extension_ids,
    ):
        assert extensions_root == root
        assert intent_path is None
        assert discard_installed_intents is True
        assert discovered_extension_ids == {"acme.demo"}
        assert is_installed("acme.demo", "account-default") is True
        events.append("cleanup")
        return SimpleNamespace(pending=0, completed=1, discarded=0, errors=())

    def recover(*, extensions_root, extensions):
        assert extensions_root == root
        assert list(extensions) == extension_rows
        events.append("publication")
        return 1

    def scan(scan_root):
        assert scan_root == str(root)
        events.append("scan")
        return [{"extension_key": "acme.demo"}]

    @contextmanager
    def lock(*, connection) -> Iterator[bool]:
        assert connection == "db-connection"
        events.append("lock")
        try:
            yield True
        finally:
            events.append("unlock")

    monkeypatch.setattr(init_db, "_resolve_sql_dsn", lambda: "postgresql://example")
    monkeypatch.setattr(extension_runtime, "default_extensions_root", lambda: root)
    monkeypatch.setattr(extension_runtime, "scan_extensions", scan)
    monkeypatch.setattr(platform_stores, "PlatformIdentityStore", lambda _dsn: _Logger())
    monkeypatch.setattr(extension_install, "DBExtensionInstallStore", _InstallStore)
    monkeypatch.setattr(bundle_cleanup, "reconcile_cleanup_journal", reconcile)
    monkeypatch.setattr(lifecycle_lock, "extension_lifecycle_lock", lock)
    monkeypatch.setattr(
        publication_recovery,
        "recover_managed_release_publications",
        recover,
    )

    init_db.init_extension_catalog()

    assert events == [
        "lock",
        "list",
        "publication",
        "scan",
        "sync",
        "installed:acme.demo:account-default",
        "cleanup",
        "unlock",
    ]


def test_init_extension_catalog_fails_closed_when_cleanup_is_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from core.application.extensions import install as extension_install
    from core.infrastructure.db import platform_stores
    from core.infrastructure.extension_runtime import (
        bundle_cleanup,
        extension_runtime,
        lifecycle_lock,
        publication_recovery,
    )

    root = tmp_path / "extensions"

    class _Logger:
        conn = "db-connection"
        list_extensions = MagicMock()
        sync_extension_scan_records = MagicMock(return_value=([], []))

        @contextmanager
        def extension_lifecycle_transaction(self):
            yield self.conn

    logger = _Logger()
    raw_scan = MagicMock()
    recover = MagicMock()

    @contextmanager
    def lock(*, connection) -> Iterator[bool]:
        assert connection == "db-connection"
        yield True

    monkeypatch.setattr(init_db, "_resolve_sql_dsn", lambda: "postgresql://example")
    monkeypatch.setattr(extension_runtime, "default_extensions_root", lambda: root)
    monkeypatch.setattr(extension_runtime, "scan_extensions", raw_scan)
    monkeypatch.setattr(platform_stores, "PlatformIdentityStore", lambda _dsn: logger)
    monkeypatch.setattr(extension_install, "DBExtensionInstallStore", lambda _conn: MagicMock())
    monkeypatch.setattr(
        lifecycle_lock,
        "extension_lifecycle_lock",
        lock,
    )
    monkeypatch.setattr(
        bundle_cleanup,
        "reconcile_cleanup_journal",
        lambda **_kwargs: SimpleNamespace(
            pending=1,
            completed=0,
            discarded=0,
            errors=("cleanup journal: disk unavailable",),
        ),
    )
    monkeypatch.setattr(
        publication_recovery,
        "recover_managed_release_publications",
        recover,
    )

    with pytest.raises(RuntimeError, match=r"cleanup reconciliation.*pending"):
        init_db.init_extension_catalog()

    logger.list_extensions.assert_called_once_with(enabled_only=False)
    recover.assert_called_once()
    raw_scan.assert_called_once_with(str(root))
    logger.sync_extension_scan_records.assert_called_once_with(
        "db-connection", raw_scan.return_value
    )


def test_initialize_database_runs_clean_bootstrap_sequence(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        init_db,
        "validate_bootstrap_layout",
        lambda root=None: calls.append("validate") or [],
    )
    monkeypatch.setattr(
        init_db,
        "init_platform_observability_schema",
        lambda: calls.append("platform"),
    )
    monkeypatch.setattr(
        init_db,
        "apply_clean_bootstrap",
        lambda *, dsn=None, author="system", root=None: (
            calls.append("seed") or {"lookup": {"accounts": 1}, "registry": {"agents": 2}}
        ),
    )
    monkeypatch.setattr(
        init_db,
        "cleanup_retired_project_schema",
        lambda *, dsn=None: (
            calls.append("cleanup")
            or {
                "dropped_db_type": 1,
                "ensured_extension_install_runtime_status": 1,
                "ensured_project_connection_runtime_status": 1,
                "ensured_skill_supported_role_types": 1,
            }
        ),
    )
    monkeypatch.setattr(
        init_db,
        "init_extension_catalog",
        lambda: calls.append("extensions"),
    )
    monkeypatch.setattr(
        init_db,
        "init_workflow_knowledge_graph",
        lambda *, dsn=None: (
            calls.append("workflow-knowledge")
            or {
                "graph_name": "platform_workflow_graph",
                "available": True,
                "labels_ready": True,
            }
        ),
    )
    monkeypatch.setattr(init_db, "init_langgraph_schema", lambda: calls.append("langgraph"))
    monkeypatch.setattr(init_db, "_resolve_sql_dsn", lambda: "postgresql://example")
    monkeypatch.setattr(
        init_db.migration_runner,
        "run_pending_migrations",
        lambda *, dsn=None, root=None: (
            calls.append("pending-migrations")
            or {"total": 1, "applied": ["0002.sql"], "skipped": []}
        ),
    )

    class _FakeConnection:
        autocommit = False

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(init_db.psycopg_runtime, "connect", lambda _dsn: _FakeConnection())
    monkeypatch.setattr(
        init_db,
        "record_canonical_schema_state",
        lambda conn, *, applied_by="system", metadata=None: calls.append("schema-state"),
    )

    summary = init_db.initialize_database()

    assert calls == [
        "validate",
        "platform",
        "pending-migrations",
        "seed",
        "cleanup",
        "workflow-knowledge",
        "extensions",
        "langgraph",
        "schema-state",
        "close",
    ]
    assert summary == {
        "post_baseline_migrations": {
            "total": 1,
            "applied": ["0002.sql"],
            "skipped": [],
        },
        "lookup": {"accounts": 1},
        "registry": {"agents": 2},
        "schema_cleanup": {
            "dropped_db_type": 1,
            "ensured_extension_install_runtime_status": 1,
            "ensured_project_connection_runtime_status": 1,
            "ensured_skill_supported_role_types": 1,
        },
        "workflow_knowledge_graph": {
            "graph_name": "platform_workflow_graph",
            "available": True,
            "labels_ready": True,
        },
        "schema_state": {"updated": 1},
    }


def test_workflow_knowledge_graph_bootstrap_checks_graph_without_masking_vector_status(
    monkeypatch,
) -> None:
    from core.infrastructure.db import workflow_knowledge_store

    class _Store:
        graph_name = "platform_workflow_graph"

        def __init__(self, *, dsn: str) -> None:
            assert dsn == "postgresql://example"

        def create_graph_indexes(self) -> None:
            return None

        def runtime_status(self) -> dict:
            return {
                "ok": False,
                "graph": {
                    "available": True,
                    "extension_available": True,
                    "extension_installed": True,
                    "labels_ready": True,
                    "error": "",
                },
                "vector": {
                    "available": False,
                    "error": "separate vector failure",
                },
            }

    monkeypatch.setattr(workflow_knowledge_store, "PostgresAgeWorkflowKnowledgeStore", _Store)

    assert init_db.init_workflow_knowledge_graph(dsn="postgresql://example") == {
        "graph_name": "platform_workflow_graph",
        "available": True,
        "labels_ready": True,
    }
