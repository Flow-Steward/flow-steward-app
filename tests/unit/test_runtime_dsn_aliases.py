from __future__ import annotations

from core.application.jobs.ops_health_snapshot_runtime import resolve_ops_health_dsn
from core.application.registry.registry_manager import RegistryManager
from core.infrastructure.db.workflow_knowledge_store import _resolve_dsn as resolve_workflow_dsn


def test_workflow_knowledge_store_accepts_fs_db_dsn(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FS_DB_DSN", "postgres://flow-steward-db")

    assert resolve_workflow_dsn() == "postgres://flow-steward-db"


def test_registry_manager_accepts_fs_db_dsn(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FS_DB_DSN", "postgres://flow-steward-db")

    assert RegistryManager._resolve_db_dsn() == "postgres://flow-steward-db"


def test_ops_health_dsn_prefers_fs_observability_then_fs_db(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FS_OBSERVABILITY_DSN", "postgres://flow-steward-observability")
    monkeypatch.setenv("FS_DB_DSN", "postgres://flow-steward-db")

    assert resolve_ops_health_dsn("") == "postgres://flow-steward-observability"

    monkeypatch.delenv("FS_OBSERVABILITY_DSN", raising=False)

    assert resolve_ops_health_dsn("") == "postgres://flow-steward-db"
