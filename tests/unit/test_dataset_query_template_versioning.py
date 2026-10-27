from __future__ import annotations

from contextlib import contextmanager

import pytest

from core.application.registry.db_store_dataset_catalog_mixin import (
    RegistryDBStoreDatasetCatalogMixin,
)


class _Cursor:
    def __init__(self) -> None:
        self.params: dict[str, object] = {}

    def execute(self, _statement, params=None) -> None:
        self.params = dict(params or {})

    def fetchone(self) -> dict[str, object]:
        return dict(self.params)


class _Store(RegistryDBStoreDatasetCatalogMixin):
    def __init__(self, existing: dict[str, object] | None) -> None:
        self.existing = existing
        self.cursor = _Cursor()

    @staticmethod
    def _table_exists(_table_name: str) -> bool:
        return True

    @staticmethod
    def _column_exists(_table_name: str, _column_name: str) -> bool:
        return True

    @staticmethod
    def get_dataset(_dataset_id: str, include_trashed: bool = False) -> dict[str, object]:
        _ = include_trashed
        return {
            "resource_scope_jsonb": {
                "version": 1,
                "resources": [{"resource_id": "orders", "columns": ["id", "active"]}],
            }
        }

    def get_dataset_query_template(
        self,
        _query_template_id: str,
        include_trashed: bool = False,
    ) -> dict[str, object] | None:
        _ = include_trashed
        return dict(self.existing) if self.existing is not None else None

    @contextmanager
    def _transaction(self):
        yield self.cursor


def _payload(*, version: int) -> dict[str, object]:
    return {
        "query_template_id": "orders.active",
        "dataset_id": "orders",
        "display_name": "Active orders",
        "query_definition_jsonb": {"sql": "SELECT * FROM orders WHERE active = true"},
        "version": version,
    }


def test_new_saved_query_starts_at_server_version_one() -> None:
    store = _Store(existing=None)

    result = store.save_dataset_query_template(_payload(version=99))

    assert result["version"] == 1


def test_saved_query_update_increments_persisted_version() -> None:
    store = _Store(
        existing={
            "query_template_id": "orders.active",
            "dataset_id": "orders",
            "version": 3,
        }
    )

    result = store.save_dataset_query_template(_payload(version=1))

    assert result["version"] == 4


def test_invalid_saved_query_never_enters_persistence_transaction() -> None:
    store = _Store(existing=None)
    payload = _payload(version=1)
    payload["query_definition_jsonb"] = {"statement": "SELECT * FROM orders LIMIT 1"}

    with pytest.raises(ValueError, match="Unsupported query definition field"):
        store.save_dataset_query_template(payload)

    assert store.cursor.params == {}
