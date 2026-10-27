"""Exercise production dataset save/readback; fake only persistence/catalog I/O."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy

import pytest

from core.application.registry.db_store_dataset_catalog_mixin import (
    RegistryDBStoreDatasetCatalogMixin,
)
from core.application.registry.db_store_datasets_mixin import RegistryDBStoreDatasetsMixin
from tests.unit.test_registry_datasets import FakeDatasetDB, _client


class _DatasetCursor:
    def __init__(self, store):
        self.store = store
        self.results = []

    def execute(self, statement, params=None):
        text = str(statement)
        if isinstance(params, dict) and "INSERT INTO registry_datasets" in text:
            # Store exactly the SQL-bound values, including serialized JSONB.
            row = deepcopy(params)
            row["project_ids"] = []
            self.store.rows[row["dataset_id"]] = row
            self.results = []
        elif "FROM registry_datasets d" in text:
            row = self.store.rows.get(params[0])
            self.results = [deepcopy(row)] if row else []
        elif "FROM workspace_data_connections" in text:
            self.results = [
                {"connection_id": key, "workspace_id": row["workspace_id"]}
                for key, row in self.store.connections.items()
                if key in params[0]
            ]
        elif "FROM workspaces" in text:
            self.results = [
                {
                    "workspace_id": workspace_id,
                    "account_id": "account-1",
                    "tenant_id": "tenant-1",
                }
                for workspace_id in params[0]
            ]
        else:
            raise AssertionError(f"Unexpected database operation: {text}")

    def fetchone(self):
        return deepcopy(self.results[0]) if self.results else None

    def fetchall(self):
        return deepcopy(self.results)


class _DatasetStore(FakeDatasetDB, RegistryDBStoreDatasetsMixin):
    # Bind the actual repository methods, not FakeDatasetDB's save/get stubs.
    save_dataset = RegistryDBStoreDatasetCatalogMixin.save_dataset
    get_dataset = RegistryDBStoreDatasetsMixin.get_dataset

    def __init__(self):
        super().__init__()
        self.audit_writes = []

    def _table_exists(self, name):
        return name in {"registry_datasets", "workspace_data_connections", "workspaces"}

    def _column_exists(self, _table, _column):
        return True

    def _column_is_generated(self, _table, _column):
        return False

    @contextmanager
    def _cursor(self):
        yield _DatasetCursor(self)

    @contextmanager
    def _transaction(self):
        before = deepcopy(self.rows)
        try:
            yield _DatasetCursor(self)
        except Exception:
            self.rows = before
            raise

    def _log_change_cur(self, _cursor, *args):
        self.audit_writes.append(deepcopy(args))


def _base_payload():
    return {
        "dataset_id": "customer_orders",
        "display_name": "Customer Orders",
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "connection_id": "warehouse-primary",
    }


@pytest.mark.parametrize(
    ("updates", "expected"),
    [({}, ["orders"]), ({"tags": []}, []), ({"tags": ["new"]}, ["new"])],
    ids=["omitted-preserves", "empty-clears", "replacement"],
)
def test_repository_dataset_tags_save_and_readback(updates, expected):
    store = _DatasetStore()
    store.save_dataset({**_base_payload(), "tags": ["orders"]})
    assert store.get_dataset("customer_orders")["tags"] == ["orders"]

    store.save_dataset({**_base_payload(), "display_name": "Edited", **updates})

    row = store.get_dataset("customer_orders")
    assert row["display_name"] == "Edited"
    assert row["tags"] == expected
    assert store.rows["customer_orders"]["tags"] == expected
    assert len(store.audit_writes) == 2


@pytest.fixture
def persisted_dataset(monkeypatch):
    store = _DatasetStore()
    with _client(store, monkeypatch) as client:
        response = client.post(
            "/api/datasets",
            json={
                **_base_payload(),
                "tags": ["orders"],
                "metadata_jsonb": {"owner": "ops", "custom_flags": {"keep": True}},
            },
        )
        assert response.status_code == 200, response.text
        readback = client.get("/api/datasets/customer_orders")
        assert readback.status_code == 200, readback.text
        assert readback.json()["metadata_jsonb"]["owner"] == "ops"
        assert readback.json()["tags"] == ["orders"]
        yield client, store


@pytest.mark.parametrize("method", ["put", "patch"])
@pytest.mark.parametrize(
    ("updates", "expected_custom"),
    [
        ({}, {"owner": "ops", "custom_flags": {"keep": True}}),
        ({"metadata_jsonb": {}}, {}),
        ({"metadata_jsonb": {"owner": "new-owner"}}, {"owner": "new-owner"}),
    ],
    ids=["omitted-preserves", "empty-clears-custom", "explicit-replaces-custom"],
)
def test_dataset_metadata_request_save_and_readback(
    persisted_dataset, method, updates, expected_custom
):
    client, store = persisted_dataset
    payload = _base_payload()
    payload.pop("dataset_id")
    response = getattr(client, method)(
        "/api/datasets/customer_orders",
        json={**payload, "display_name": "Edited", **updates},
    )
    assert response.status_code == 200, response.text

    readback = client.get("/api/datasets/customer_orders")
    assert readback.status_code == 200, readback.text
    row = readback.json()
    assert row["display_name"] == "Edited"
    metadata = row["metadata_jsonb"]
    assert {key: metadata[key] for key in ("owner", "custom_flags") if key in metadata} == (
        expected_custom
    )
    # Clearing/replacing user metadata must not remove derived binding metadata.
    assert metadata["connection_id"] == "warehouse-primary"
    assert metadata["workspace_id"] == "ws-1"
    assert metadata["dataset_connector"]["local_connector_id"] == "warehouse_sql"
    assert store.get_dataset("customer_orders")["metadata_jsonb"] == metadata


@pytest.mark.parametrize(
    ("method", "updates", "expected"),
    [("patch", {}, ["orders"]), ("patch", {"tags": []}, []), ("put", {"tags": []}, [])],
    ids=["patch-omitted-preserves", "patch-empty-clears", "put-empty-clears"],
)
def test_dataset_tags_request_save_and_readback(persisted_dataset, method, updates, expected):
    client, store = persisted_dataset
    payload = _base_payload()
    payload.pop("dataset_id")
    response = getattr(client, method)(
        "/api/datasets/customer_orders",
        json={**payload, "display_name": "Edited", **updates},
    )
    assert response.status_code == 200, response.text
    readback = client.get("/api/datasets/customer_orders")
    assert readback.status_code == 200, readback.text
    assert readback.json()["display_name"] == "Edited"
    assert readback.json()["tags"] == expected
    assert store.get_dataset("customer_orders")["tags"] == expected
