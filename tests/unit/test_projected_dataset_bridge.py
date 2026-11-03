from __future__ import annotations

import copy
from contextlib import contextmanager
from types import SimpleNamespace

import pytest


class _RegistryStore:
    def __init__(self) -> None:
        self.datasets = {
            "orders": {
                "dataset_id": "orders",
                "display_name": "User-owned orders",
                "metadata_jsonb": {"owner": "user"},
            }
        }
        self.templates = {
            "open_orders": {
                "query_template_id": "open_orders",
                "dataset_id": "orders",
                "display_name": "User-owned query",
                "created_by": "admin",
            }
        }

    def get_dataset(self, dataset_id: str, include_trashed: bool = False):
        _ = include_trashed
        return copy.deepcopy(self.datasets.get(dataset_id))

    def save_dataset(self, payload: dict, author: str = "admin") -> None:
        _ = author
        self.datasets[str(payload["dataset_id"])] = copy.deepcopy(payload)

    def get_dataset_query_template(self, query_template_id: str, include_trashed: bool = False):
        _ = include_trashed
        return copy.deepcopy(self.templates.get(query_template_id))

    def save_dataset_query_template(self, payload: dict, author: str = "admin"):
        _ = author
        self.templates[str(payload["query_template_id"])] = copy.deepcopy(payload)
        return copy.deepcopy(payload)

    def list_datasets(self, include_trashed: bool = False):
        _ = include_trashed
        return [copy.deepcopy(row) for row in self.datasets.values()]

    def list_dataset_query_templates(self, include_trashed: bool = False):
        _ = include_trashed
        return [copy.deepcopy(row) for row in self.templates.values()]

    def purge_dataset(self, dataset_id: str) -> None:
        self.datasets.pop(dataset_id, None)

    def purge_dataset_query_template(self, query_template_id: str) -> None:
        self.templates.pop(query_template_id, None)


def test_projection_bridge_is_namespaced_install_owned_and_collision_safe() -> None:
    from core.application.extensions.projected_dataset_bridge import (
        materialize_projected_dataset_bridges,
        projection_bridge_id,
    )

    store = _RegistryStore()
    materialize_projected_dataset_bridges(
        store,
        account_id="account-1",
        install_id="install-1",
        release_id="release-1",
        projections=[
            {
                "artifact_kind": "dataset",
                "artifact_id": "orders",
                "projection": {
                    "dataset_id": "orders",
                    "display_name": "Projected orders",
                    "connector_id": "runtime_records",
                    "metadata_jsonb": {"connection_resolution": "project_scope"},
                },
            },
            {
                "artifact_kind": "saved_query",
                "artifact_id": "open_orders",
                "projection": {
                    "query_template_id": "open_orders",
                    "dataset_id": "orders",
                    "display_name": "Projected open orders",
                    "query_definition_jsonb": {"sql": "SELECT id FROM orders LIMIT 10"},
                },
            },
        ],
    )

    dataset_bridge_id = projection_bridge_id("install-1", "orders")
    query_bridge_id = projection_bridge_id("install-1", "open_orders")
    assert store.datasets["orders"]["display_name"] == "User-owned orders"
    assert store.templates["open_orders"]["display_name"] == "User-owned query"
    assert store.datasets[dataset_bridge_id]["metadata_jsonb"]["projection_bridge"] == {
        "immutable": True,
        "install_id": "install-1",
        "release_id": "release-1",
        "artifact_kind": "dataset",
        "artifact_id": "orders",
    }
    assert store.datasets[dataset_bridge_id]["metadata_jsonb"]["account_id"] == "account-1"
    assert store.templates[query_bridge_id]["dataset_id"] == dataset_bridge_id
    assert (
        store.templates[query_bridge_id]["validation_result_jsonb"]["projection_bridge"][
            "artifact_kind"
        ]
        == "saved_query"
    )


def test_projection_bridge_refuses_to_overwrite_foreign_namespaced_row() -> None:
    import pytest

    from core.application.extensions.projected_dataset_bridge import (
        materialize_projected_dataset_bridges,
        projection_bridge_id,
    )

    store = _RegistryStore()
    bridge_id = projection_bridge_id("install-1", "orders")
    store.datasets[bridge_id] = {
        "dataset_id": bridge_id,
        "display_name": "Foreign row",
        "metadata_jsonb": {"owner": "user"},
    }

    with pytest.raises(ValueError, match="not owned by install"):
        materialize_projected_dataset_bridges(
            store,
            account_id="account-1",
            install_id="install-1",
            release_id="release-1",
            projections=[
                {
                    "artifact_kind": "dataset",
                    "artifact_id": "orders",
                    "projection": {"dataset_id": "orders", "display_name": "Projected"},
                }
            ],
        )


def test_projection_bridge_update_removes_obsolete_install_owned_rows() -> None:
    from core.application.extensions.projected_dataset_bridge import (
        materialize_projected_dataset_bridges,
        projection_bridge_id,
    )

    store = _RegistryStore()
    original = [
        {
            "artifact_kind": "dataset",
            "artifact_id": "orders",
            "projection": {"dataset_id": "orders", "display_name": "Orders"},
        },
        {
            "artifact_kind": "saved_query",
            "artifact_id": "open_orders",
            "projection": {
                "query_template_id": "open_orders",
                "dataset_id": "orders",
                "display_name": "Open orders",
            },
        },
    ]
    materialize_projected_dataset_bridges(
        store,
        account_id="account-1",
        install_id="install-1",
        release_id="release-1",
        projections=original,
    )

    materialize_projected_dataset_bridges(
        store,
        account_id="account-1",
        install_id="install-1",
        release_id="release-2",
        projections=[],
    )

    assert projection_bridge_id("install-1", "orders") not in store.datasets
    assert projection_bridge_id("install-1", "open_orders") not in store.templates
    assert "orders" in store.datasets
    assert "open_orders" in store.templates


def test_projection_bridge_cleanup_removes_only_install_owned_canonical_rows() -> None:
    from core.application.extensions.projected_dataset_bridge import (
        materialize_projected_dataset_bridges,
        projection_bridge_id,
        remove_projected_dataset_bridges,
    )

    store = _RegistryStore()
    materialize_projected_dataset_bridges(
        store,
        account_id="account-1",
        install_id="install-1",
        release_id="release-1",
        projections=[
            {
                "artifact_kind": "dataset",
                "artifact_id": "orders",
                "projection": {"dataset_id": "orders", "display_name": "Orders"},
            },
            {
                "artifact_kind": "saved_query",
                "artifact_id": "open_orders",
                "projection": {
                    "query_template_id": "open_orders",
                    "dataset_id": "orders",
                    "display_name": "Open orders",
                },
            },
        ],
    )

    removed = remove_projected_dataset_bridges(store, install_id="install-1")

    assert removed == {
        "datasets": [projection_bridge_id("install-1", "orders")],
        "saved_queries": [projection_bridge_id("install-1", "open_orders")],
    }
    assert set(store.datasets) == {"orders"}
    assert set(store.templates) == {"open_orders"}


def test_install_materialization_binds_registry_writes_to_lifecycle_connection() -> None:
    from core.application.extensions.command_context import ExtensionCommandContext
    from core.application.extensions.command_install_projection import (
        ExtensionInstallProjectionService,
    )

    class _LifecycleRegistryStore(_RegistryStore):
        def __init__(self) -> None:
            super().__init__()
            self.transaction_connections: list[object | None] = []

        def _set_tx_conn(self, connection: object | None) -> None:
            self.transaction_connections.append(connection)

    lifecycle_connection = object()
    registry_store = _LifecycleRegistryStore()
    service = ExtensionInstallProjectionService(
        context=ExtensionCommandContext(
            get_logger=lambda: SimpleNamespace(
                conn=lifecycle_connection,
                db_store=registry_store,
            ),
            get_runtime_store=lambda: SimpleNamespace(
                list_projections=lambda **_kwargs: [],
            ),
        )
    )

    service.materialize_dataset_bridges(
        install_record=SimpleNamespace(install_id="install-1"),
        release=SimpleNamespace(release_id="release-1"),
        account_id="account-1",
    )

    assert registry_store.transaction_connections == [lifecycle_connection, None]


def test_extension_projection_rejects_invalid_saved_query_before_any_write() -> None:
    from core.application.extensions.command_context import ExtensionCommandContext
    from core.application.extensions.command_install_projection import (
        ExtensionInstallProjectionService,
    )

    writes: list[object] = []
    snapshots = [
        SimpleNamespace(
            artifact_kind="dataset",
            artifact_id="orders",
            publish=True,
            snapshot={
                "dataset_id": "orders",
                "resource_scope_jsonb": {
                    "resources": [{"resource_id": "orders", "columns": ["id"]}]
                },
            },
        ),
        SimpleNamespace(
            artifact_kind="saved_query",
            artifact_id="open_orders",
            publish=True,
            snapshot={
                "query_template_id": "open_orders",
                "dataset_id": "orders",
                "query_definition_jsonb": {"statement": "SELECT id FROM orders LIMIT 1"},
            },
        ),
    ]
    runtime_store = SimpleNamespace(
        list_artifact_snapshots=lambda _release_id: snapshots,
        upsert_projection=writes.append,
    )
    service = ExtensionInstallProjectionService(
        context=ExtensionCommandContext(
            get_logger=lambda: SimpleNamespace(),
            get_runtime_store=lambda: runtime_store,
        )
    )

    _raises_input_313_1 = SimpleNamespace(install_id="install-1")
    _raises_input_313_2 = SimpleNamespace(release_id="release-1", extension_id="extension-1")
    with pytest.raises(ValueError, match="Unsupported query definition field"):
        service.project_release(
            install_record=_raises_input_313_1, release=_raises_input_313_2, account_id="account-1"
        )

    assert writes == []


def test_install_materialization_uses_store_derived_lifecycle_connection() -> None:
    from core.application.extensions.command_context import ExtensionCommandContext
    from core.application.extensions.command_install_projection import (
        ExtensionInstallProjectionService,
    )

    class _LifecycleRegistryStore(_RegistryStore):
        def __init__(self) -> None:
            super().__init__()
            self.transaction_connections: list[object | None] = []

        def _set_tx_conn(self, connection: object | None) -> None:
            self.transaction_connections.append(connection)

    lifecycle_connection = object()
    registry_store = _LifecycleRegistryStore()
    install_store = SimpleNamespace(_conn=lifecycle_connection)
    runtime_store = SimpleNamespace(
        _conn=lifecycle_connection,
        list_projections=lambda **_kwargs: [],
    )

    logger = SimpleNamespace(conn=None, db_store=registry_store)
    context = ExtensionCommandContext(
        get_logger=lambda: logger,
        get_install_store=lambda: install_store,
        get_runtime_store=lambda: runtime_store,
    )
    service = ExtensionInstallProjectionService(context=context)

    with context.pin(
        logger=logger,
        stores=(install_store, runtime_store),
        connection=lifecycle_connection,
    ):
        service.materialize_dataset_bridges(
            install_record=SimpleNamespace(install_id="install-1"),
            release=SimpleNamespace(release_id="release-1"),
            account_id="account-1",
        )

    assert registry_store.transaction_connections == [lifecycle_connection, None]


def test_project_assignment_flow_accepts_project_scoped_projection_bridge() -> None:
    from core.application.registry.db_store_dataset_catalog_mixin import (
        RegistryDBStoreDatasetCatalogMixin,
    )

    class _Cursor:
        def __init__(self) -> None:
            self.statements: list[tuple[str, tuple | None]] = []

        def execute(self, statement, params=None):
            self.statements.append((str(statement), params))

        def fetchall(self):
            return []

    class _Store(RegistryDBStoreDatasetCatalogMixin):
        def __init__(self) -> None:
            self.cursor = _Cursor()
            self.row = {
                "dataset_id": "extension.install-1.orders",
                "account_id": "account-1",
                "workspace_id": "",
                "workspace_unresolved": True,
                "availability_mode": "specific_projects_only",
                "project_ids": [],
                "metadata_jsonb": {
                    "connection_resolution": "project_scope",
                    "projection_bridge": {
                        "immutable": True,
                        "install_id": "install-1",
                    },
                },
            }

        def _table_exists(self, _table: str) -> bool:
            return True

        def _get_project_scope(
            self,
            _project_id: str,
            *,
            include_trashed: bool = False,
            account_id: str | None = None,
        ):
            assert include_trashed is False
            assert account_id is None
            return {"workspace_id": "workspace-1", "account_id": "account-1"}

        @staticmethod
        def _normalize_dataset_project_ids(value):
            return [str(item) for item in (value or []) if str(item)]

        def list_datasets(self, enabled_only=False, *, project_id=None, include_trashed=False):
            _ = enabled_only, include_trashed
            if project_id:
                return [{**self.row, "project_ids": [project_id]}]
            return [dict(self.row)]

        @contextmanager
        def _transaction(self):
            yield self.cursor

        def _log_change_cur(self, *_args, **_kwargs):
            return None

    store = _Store()
    catalog = store.list_project_dataset_catalog("project-1")
    linked = store.set_project_datasets("project-1", ["extension.install-1.orders"], author="admin")

    assert catalog["available_datasets"][0]["dataset_id"] == "extension.install-1.orders"
    assert linked[0]["project_ids"] == ["project-1"]
    assert any(
        "INSERT INTO project_datasets" in statement
        for statement, _params in store.cursor.statements
    )


class _MutationCursor:
    rowcount = 1

    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchone(self):
        return {"enabled": False}


class _OrdinaryMutationStore:
    """Persisted reads and a write recorder; mutations are the production methods."""

    def __init__(self, *, owned):
        from core.application.extensions.projected_dataset_bridge import projection_bridge_metadata

        bridge = projection_bridge_metadata(
            install_id="install-1",
            release_id="release-1",
            artifact_kind="dataset",
            artifact_id="orders",
        )
        self.dataset = {
            "dataset_id": "extension.install-1.orders",
            "read_only": True,
            "metadata_jsonb": {"projection_bridge": bridge} if owned else {},
        }
        self.query = {
            "query_template_id": "extension.install-1.query",
            "dataset_id": self.dataset["dataset_id"],
            "validation_result_jsonb": {
                "projection_bridge": {
                    **bridge,
                    "artifact_kind": "saved_query",
                    "artifact_id": "query",
                }
            }
            if owned
            else {},
        }
        self.cursor = _MutationCursor()
        self.write_contexts = 0

    def get_dataset(self, *_args, **_kwargs):
        return copy.deepcopy(self.dataset)

    def get_dataset_query_template(self, *_args, **_kwargs):
        return copy.deepcopy(self.query)

    def _table_exists(self, _table):
        return True

    def _column_exists(self, *_args):
        return True

    @contextmanager
    def _cursor(self):
        self.write_contexts += 1
        yield self.cursor

    _transaction = _cursor

    def _log_change_cur(self, *_args):
        return None


@pytest.mark.parametrize(
    "method",
    [
        "toggle_dataset",
        "delete_dataset",
        "toggle_dataset_query_template",
        "delete_dataset_query_template",
    ],
)
def test_ordinary_mutation_refuses_persisted_install_bridge_before_writes(method):
    from core.application.registry.db_store_dataset_catalog_mixin import (
        RegistryDBStoreDatasetCatalogMixin,
    )

    store = _OrdinaryMutationStore(owned=True)
    identifier = (
        store.query["query_template_id"]
        if "query_template" in method
        else store.dataset["dataset_id"]
    )
    mutate = getattr(RegistryDBStoreDatasetCatalogMixin, method)
    with pytest.raises(ValueError, match="Install-owned projected dataset/query is immutable"):
        mutate(store, identifier)
    assert store.write_contexts == 0
    assert store.cursor.statements == []


@pytest.mark.parametrize(
    "method",
    [
        "toggle_dataset",
        "delete_dataset",
        "toggle_dataset_query_template",
        "delete_dataset_query_template",
    ],
)
def test_ordinary_mutation_keeps_user_owned_rows_writable(method):
    from core.application.registry.db_store_dataset_catalog_mixin import (
        RegistryDBStoreDatasetCatalogMixin,
    )

    store = _OrdinaryMutationStore(owned=False)
    if "query_template" in method:
        store.dataset = _OrdinaryMutationStore(owned=True).dataset
    identifier = (
        store.query["query_template_id"]
        if "query_template" in method
        else store.dataset["dataset_id"]
    )
    getattr(RegistryDBStoreDatasetCatalogMixin, method)(store, identifier)
    assert store.write_contexts == 1
    assert len(store.cursor.statements) == 1
    assert "UPDATE" in store.cursor.statements[0][0]


class _ValidatingBridgeStore(_RegistryStore):
    """Real saved-query store with only the database cursor replaced."""

    def __init__(self, catalog):
        super().__init__()
        self.catalog = catalog
        self.persisted_queries = []

    def list_dataset_connectors(self):
        return copy.deepcopy(self.catalog)

    @staticmethod
    def _table_exists(_table):
        return True

    @staticmethod
    def _column_exists(_table, _column):
        return True

    @contextmanager
    def _transaction(self):
        store = self

        class Cursor:
            def execute(self, _statement, params):
                self.params = copy.deepcopy(params)
                store.persisted_queries.append(self.params)

            def fetchone(self):
                return self.params

        yield Cursor()

    def save_dataset_query_template(self, payload, author="admin"):
        from core.application.registry.db_store_dataset_catalog_mixin import (
            RegistryDBStoreDatasetCatalogMixin,
        )

        row = RegistryDBStoreDatasetCatalogMixin.save_dataset_query_template(
            self, payload, author=author
        )
        self.templates[row["query_template_id"]] = copy.deepcopy(row)
        return row

    @staticmethod
    def _normalize_dataset_query_template_row(row):
        from core.application.registry.db_store_dataset_catalog_mixin import (
            RegistryDBStoreDatasetCatalogMixin,
        )

        return RegistryDBStoreDatasetCatalogMixin._normalize_dataset_query_template_row(row)


def _installed_bridge_connector(
    *, language, install_id="install-1", account_id="account-1", local_id="runtime_records"
):
    from core.application.registry.db_store_datasets_mixin import RegistryDBStoreDatasetsMixin

    return RegistryDBStoreDatasetsMixin._dataset_connectors_from_descriptor_row(
        {
            "install_id": install_id,
            "release_id": "release-1",
            "account_id": account_id,
            "extension_id": "provider",
            "descriptor_jsonb": {
                "extension_contract_v2_normalized": {
                    "dataset_connectors": [{"connector_id": local_id, "query_language": language}]
                }
            },
        }
    )


def _materialize_validated_bridge(store, language):
    from core.application.extensions.projected_dataset_bridge import (
        materialize_projected_dataset_bridges,
    )

    definition = (
        {"sql": "SELECT id FROM Orders LIMIT 1"}
        if language == "sql"
        else {
            "resource_id": "Orders",
            "operation": "find",
            "arguments": {"active": {"$param": "active"}},
        }
    )
    return materialize_projected_dataset_bridges(
        store,
        account_id="account-1",
        install_id="install-1",
        release_id="release-1",
        projections=[
            {
                "artifact_kind": "dataset",
                "artifact_id": "orders",
                "projection": {
                    "dataset_id": "orders",
                    "connector_id": "runtime_records",
                    "query_language": language,
                    "resource_scope_jsonb": {"resources": [{"resource_id": "Orders"}]},
                    "metadata_jsonb": {"connection_resolution": "project_scope"},
                },
            },
            {
                "artifact_kind": "saved_query",
                "artifact_id": "open_orders",
                "projection": {
                    "dataset_id": "orders",
                    "query_template_id": "open_orders",
                    "query_language": language,
                    "query_definition_jsonb": definition,
                    "params_schema_jsonb": {"properties": {"active": {"type": "boolean"}}},
                },
            },
        ],
    )


@pytest.mark.parametrize("language", ["sql", "json"])
def test_projection_materialization_validates_real_store_against_namespaced_connector(language):
    import json

    store = _ValidatingBridgeStore(_installed_bridge_connector(language=language))
    result = _materialize_validated_bridge(store, language)
    assert result["saved_queries"] == ["extension.install-1.open_orders"]
    assert store.datasets["extension.install-1.orders"]["connector_id"] == "runtime_records"
    assert store.persisted_queries[0]["query_language"] == language
    definition = json.loads(store.persisted_queries[0]["query_definition_jsonb"])
    if language == "sql":
        assert definition == {"sql": "SELECT id FROM Orders LIMIT 1"}
    else:
        assert definition == {
            "resource_id": "Orders",
            "operation": "find",
            "arguments": {"active": {"$param": "active"}},
        }


@pytest.mark.parametrize("language", ["sql", "json"])
@pytest.mark.parametrize("catalog_case", ["other_install", "other_account", "unknown", "ambiguous"])
def test_projection_local_connector_scope_rejects_foreign_unknown_or_ambiguous_catalog(
    language, catalog_case
):
    if catalog_case == "other_install":
        catalog = _installed_bridge_connector(language=language, install_id="install-2")
    elif catalog_case == "other_account":
        catalog = _installed_bridge_connector(language=language, account_id="account-2")
    elif catalog_case == "unknown":
        catalog = _installed_bridge_connector(language=language, local_id="another")
    else:
        catalog = _installed_bridge_connector(language=language) * 2
    store = _ValidatingBridgeStore(catalog)
    with pytest.raises(ValueError, match="connector binding is unavailable"):
        _materialize_validated_bridge(store, language)
    assert store.persisted_queries == []


@pytest.mark.parametrize(
    "tamper",
    [
        "mutable",
        "foreign_account",
        "missing_account",
        "wrong_kind",
        "wrong_handle",
        "foreign_connector",
    ],
)
def test_projected_binding_requires_valid_immutable_owner_before_using_local_alias(tamper):
    from core.application.datasets.saved_query_binding import validate_saved_query_connector_binding
    from core.application.extensions.projected_dataset_bridge import projection_bridge_metadata

    catalog = _installed_bridge_connector(language="sql") + _installed_bridge_connector(
        language="sql", install_id="install-2"
    )
    store = _ValidatingBridgeStore(catalog)
    dataset = {
        "dataset_id": "extension.install-1.orders",
        "connector_id": "runtime_records",
        "metadata_jsonb": {
            "account_id": "account-1",
            "projection_bridge": projection_bridge_metadata(
                install_id="install-1",
                release_id="release-1",
                artifact_kind="dataset",
                artifact_id="orders",
            ),
        },
    }
    if tamper == "mutable":
        dataset["metadata_jsonb"]["projection_bridge"]["immutable"] = False
    elif tamper == "foreign_account":
        dataset["account_id"] = "account-2"
    elif tamper == "missing_account":
        del dataset["metadata_jsonb"]["account_id"]
    elif tamper == "wrong_kind":
        dataset["metadata_jsonb"]["projection_bridge"]["artifact_kind"] = "saved_query"
    elif tamper == "wrong_handle":
        dataset["dataset_id"] = "extension.install-2.orders"
    else:
        dataset["connector_id"] = "extension.install-2.runtime_records"
    with pytest.raises(ValueError, match="connector binding is unavailable"):
        validate_saved_query_connector_binding(
            {"query_language": "sql", "account_id": "account-1"}, dataset=dataset, db=store
        )


def test_local_connector_alias_without_bridge_ownership_is_rejected():
    from core.application.datasets.saved_query_binding import validate_saved_query_connector_binding

    store = _ValidatingBridgeStore(_installed_bridge_connector(language="sql"))
    with pytest.raises(ValueError, match="connector binding is unavailable"):
        validate_saved_query_connector_binding(
            {"query_language": "sql"},
            dataset={"connector_id": "runtime_records", "account_id": "account-1"},
            db=store,
        )
