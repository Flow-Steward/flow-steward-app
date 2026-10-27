from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from core.application.extensions.projected_dataset_bridge import projection_bridge_id


class _Registry:
    def __init__(self, db) -> None:
        self.db_store = db


class _ProjectedDatasetFixtureDB:
    def __init__(self) -> None:
        self.install_id = "install-fixture"
        self.release_id = "release-fixture"
        self.query_artifact_id = "open_records_query"
        self.dataset_artifact_id = "projected_records"
        self.query_bridge_id = projection_bridge_id(self.install_id, self.query_artifact_id)
        self.dataset_bridge_id = projection_bridge_id(self.install_id, self.dataset_artifact_id)

    def get_job(self, job_id: str):
        if job_id != "job-1":
            return None
        return {
            "id": job_id,
            "project_id": "project-1",
            "account_id": "account-1",
            "tenant_id": "tenant-1",
            "workspace_id": "workspace-1",
        }

    def get_project(self, project_id: str):
        if project_id != "project-1":
            return None
        return {
            "id": project_id,
            "account_id": "account-1",
            "tenant_id": "tenant-1",
            "workspace_id": "workspace-1",
        }

    def get_workflow(self, workflow_id: str):
        if workflow_id == "fixture_projected_dataset_workflow":
            return {"source_install_id": self.install_id}
        return None

    def get_dataset_query_template(self, query_template_id: str):
        if query_template_id != self.query_bridge_id:
            return None
        return {
            "query_template_id": self.query_bridge_id,
            "dataset_id": self.dataset_bridge_id,
            "display_name": "Projected records query",
            "query_language": "sql",
            "query_definition_jsonb": {"sql": "SELECT record_id FROM records LIMIT 10"},
            "enabled": True,
        }

    def get_dataset(self, dataset_id: str):
        if dataset_id != self.dataset_bridge_id:
            return None
        return {
            "dataset_id": self.dataset_bridge_id,
            "display_name": "Projected records",
            "connector_id": "fixture_connector",
            "project_ids": ["project-1"],
            "metadata_jsonb": {
                "connection_resolution": "project_scope",
                "projection_bridge": {
                    "immutable": True,
                    "install_id": self.install_id,
                    "release_id": self.release_id,
                    "artifact_kind": "dataset",
                    "artifact_id": self.dataset_artifact_id,
                },
            },
            "resource_scope_jsonb": {
                "version": 1,
                "resources": [{"resource_id": "records", "columns": ["record_id"]}],
            },
        }

    def get_extension_registry_projection(self, **kwargs):
        expected = {
            "saved_query": self.query_artifact_id,
            "dataset": self.dataset_artifact_id,
        }
        kind = kwargs["artifact_kind"]
        artifact_id = kwargs["artifact_id"]
        if kwargs["install_id"] != self.install_id or expected.get(kind) != artifact_id:
            return None
        projection = (
            {
                "query_template_id": artifact_id,
                "dataset_id": self.dataset_artifact_id,
            }
            if kind == "saved_query"
            else {}
        )
        return {
            "account_id": "account-1",
            "install_id": self.install_id,
            "release_id": self.release_id,
            "artifact_kind": kind,
            "artifact_id": artifact_id,
            "projection": projection,
            "is_active": True,
            "availability_status": "available",
            "new_runs_blocked": False,
        }

    def list_workspace_data_connections(self, *, workspace_id: str | None = None):
        if workspace_id and workspace_id != "workspace-1":
            return []
        return [
            {
                "connection_id": "fixture-connection",
                "account_id": "account-1",
                "tenant_id": "tenant-1",
                "workspace_id": "workspace-1",
                "enabled": True,
                "connection_config_jsonb": {"mode": "fixture"},
                "secret_refs_jsonb": {},
                "connector_ref_jsonb": {
                    "extension_id": "fixture.projected-dataset-provider",
                    "local_connector_id": "fixture_connector",
                },
            }
        ]


def test_projected_dataset_executes_through_neutral_host_fixture(monkeypatch) -> None:
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    db = _ProjectedDatasetFixtureDB()
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: _Registry(db),
    )
    fixture_root = Path(__file__).resolve().parents[1] / "fixtures" / "projected_dataset_provider"
    fixture_extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "fixture.projected-dataset-provider",
        "extension_id": "fixture.projected-dataset-provider",
        "bundle_root": str(fixture_root),
        "entrypoint": {"mode": "subprocess", "command": [sys.executable, "main.py"]},
        "compiled_manifest": {
            "extension_id": "fixture.projected-dataset-provider",
            "bundle_root": str(fixture_root),
        },
    }
    monkeypatch.setattr(
        "core.application.orchestration.dataset_query_step_runtime._extension_for_connection",
        lambda _db, _connection: (fixture_extension, None),
    )

    result = execute_dataset_query_step(
        step={
            "override": {
                "dataset_query": {
                    "query_template_id": db.query_artifact_id,
                    "row_limit": 10,
                    "result_target": "state.projected_records_result",
                }
            }
        },
        state={
            "job_id": "job-1",
            "workflow_id": "fixture_projected_dataset_workflow",
        },
    )

    assert result["ok"] is True, result
    assert result["result"]["rows"] == [{"record_id": 101}]
    assert result["state_patch"]["projected_records_result"]["row_count"] == 1


def test_projected_dataset_bridge_write_rolls_back_on_pinned_postgres_transaction() -> None:
    """A bridge write must not escape the extension lifecycle transaction."""
    from core.application.extensions.projected_dataset_bridge import (
        materialize_projected_dataset_bridges,
    )
    from core.application.registry.db_store import RegistryDBStore
    from core.infrastructure.db import psycopg_runtime
    from tests._db_env import resolve_test_db_dsn

    dsn = resolve_test_db_dsn()
    if not dsn:
        pytest.skip("No database DSN configured (FS_DB_DSN / DATABASE_URL)")
    install_id = f"rollback-{uuid.uuid4().hex}"
    dataset_id = projection_bridge_id(install_id, "orders")
    store = RegistryDBStore(dsn)
    pg_conn = psycopg_runtime.connect(dsn)
    pg_conn.autocommit = False
    store._set_tx_conn(pg_conn)
    try:
        try:
            materialize_projected_dataset_bridges(
                store,
                account_id="acc_default",
                install_id=install_id,
                release_id="release-rollback",
                projections=[
                    {
                        "artifact_kind": "dataset",
                        "artifact_id": "orders",
                        "projection": {
                            "dataset_id": "orders",
                            "display_name": "Rollback orders",
                            "connector_id": "extension.connector",
                        },
                    }
                ],
            )
            assert store.get_dataset(dataset_id, include_trashed=True) is not None
            raise RuntimeError("force lifecycle rollback")
        except RuntimeError:
            pg_conn.rollback()
    finally:
        store._set_tx_conn(None)
        pg_conn.close()

    assert store.get_dataset(dataset_id, include_trashed=True) is None


def test_policy_read_returns_an_idle_connection_before_registry_use() -> None:
    """A policy SELECT must not poison the shared PostgreSQL pool."""
    from core.application.registry.db_store import RegistryDBStore
    from core.infrastructure.db.db_pool import _TRANSACTION_STATUS_IDLE
    from core.infrastructure.security.policy_loader import (
        _get_threaded_pool,
        load_project_policy_override,
    )
    from tests._db_env import resolve_test_db_dsn

    dsn = resolve_test_db_dsn()
    if not dsn:
        pytest.skip("No database DSN configured (FS_DB_DSN / DATABASE_URL)")
    pool = _get_threaded_pool(dsn)
    assert load_project_policy_override(str(uuid.uuid4())) is None

    conn = pool.getconn()
    try:
        assert int(conn.get_transaction_status()) == _TRANSACTION_STATUS_IDLE
    finally:
        pool.putconn(conn)

    # This is the next pooled DB consumer in the reported failure sequence.
    assert RegistryDBStore(dsn).get_workflow(f"missing-{uuid.uuid4().hex}") is None
