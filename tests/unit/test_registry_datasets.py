from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.application.management.scope import normalize_scope
from core.entrypoints.admin import datasets as datasets_routes
from core.entrypoints.admin import datasets_helpers


class FakeDatasetDB:
    def __init__(self, *, connection_id_data_type: str = "text") -> None:
        self.rows: dict[str, dict] = {}
        self.conn = object()
        self.saved_connection_secrets: list[dict[str, object]] = []
        self.atomic_connection_save_calls: list[dict[str, object]] = []
        self.saved_query_writes: list[dict[str, object]] = []
        self.connection_id_data_type = connection_id_data_type
        self.query_templates = [
            {
                "query_template_id": "qt_assigned",
                "dataset_id": "customer_orders",
                "display_name": "Assigned query",
            },
            {
                "query_template_id": "qt_unassigned",
                "dataset_id": "orphan_dataset",
                "display_name": "Unassigned query",
            },
        ]
        self.connections: dict[str, dict] = {
            "warehouse-primary": {
                "connection_id": "warehouse-primary",
                "workspace_id": "ws-1",
                "tenant_id": "tenant-1",
                "account_id": "account-1",
                "display_name": "Warehouse Primary",
                "description": "",
                "connector_ref_jsonb": {
                    "connector_id": "extension.install_1.warehouse_sql",
                    "local_connector_id": "warehouse_sql",
                    "extension_id": "warehouse-extension",
                    "install_id": "install_1",
                },
                "source_kind": "extension",
                "environment": "dev",
                "tags": [],
                "access_mode": "read_only",
                "enabled": True,
                "health_status": "unknown",
                "connection_config_jsonb": {"prebuilt": "postgres"},
                "secret_refs_jsonb": {"values": {"password": "secret"}},
                "metadata_jsonb": {
                    "last_schema_snapshot": {
                        "schema_version": "dataset_schema_snapshot.v1",
                        "resources": [
                            {
                                "resource_id": "orders",
                                "columns": [
                                    {"name": "order_id"},
                                    {"name": "customer_id"},
                                    {"name": "total_amount"},
                                ],
                            },
                            {
                                "resource_id": "customers",
                                "columns": [{"name": "customer_id"}, {"name": "email"}],
                            },
                        ],
                    }
                },
            }
        }
        self.connectors = [
            {
                "connector_id": "extension.install_1.warehouse_sql",
                "local_connector_id": "warehouse_sql",
                "display_name": "Warehouse SQL",
                "description": "Read-only warehouse connector",
                "connector_kind": "extension",
                "runtime_kind": "local_extension",
                "query_language": "sql",
                "connection_schema": {
                    "type": "object",
                    "properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}},
                },
                "secret_schema": {"type": "object"},
                "capabilities": {"schema_refresh": True},
                "safety_profile": {"read_only": True},
                "preview_support": True,
                "schema_refresh_support": True,
                "runtime_adapter_ref": "extensions.warehouse.DatasetConnector",
                "source_install_id": "install_1",
                "source_release_id": "release_1",
                "extension_id": "warehouse-extension",
                "account_id": "account-1",
                "tenant_id": "tenant-1",
            }
        ]
        self.projects = {
            "3": {
                "project_id": "3",
                "workspace_id": "ws-1",
                "tenant_id": "tenant-1",
                "account_id": "account-1",
                "project_name": "Project 3",
            },
            "4": {
                "project_id": "4",
                "workspace_id": "ws-2",
                "tenant_id": "tenant-1",
                "account_id": "account-1",
                "project_name": "Project 4",
            },
        }

    def _row_with_owner(self, row: dict) -> dict:
        payload = dict(row)
        workspace_id = str(payload.get("workspace_id") or "").strip()
        if workspace_id and not str(payload.get("account_id") or "").strip():
            payload["account_id"] = "account-1"
        if workspace_id and not str(payload.get("tenant_id") or "").strip():
            payload["tenant_id"] = "tenant-1"
        return payload

    def list_datasets(self, *, enabled_only: bool = False, project_id: int | None = None):
        rows = [self._row_with_owner(row) for row in self.rows.values()]
        if enabled_only:
            rows = [row for row in rows if row.get("enabled") is not False]
        if project_id is not None:
            normalized_project_id = str(project_id)
            project_scope = self._get_project_scope(normalized_project_id)
            rows = [
                row
                for row in rows
                if row.get("workspace_id") == project_scope["workspace_id"]
                and (
                    row.get("availability_mode") == "all_workspace_projects"
                    or normalized_project_id in (row.get("project_ids") or [])
                )
            ]
        return rows

    def get_dataset(self, dataset_id: str, include_trashed: bool = False):
        _ = include_trashed
        row = self.rows.get(dataset_id)
        return self._row_with_owner(row) if isinstance(row, dict) else row

    def save_dataset(self, payload: dict, author: str = "admin") -> None:
        _ = author
        self.rows[payload["dataset_id"]] = dict(payload)

    def list_dataset_connectors(self):
        return list(self.connectors)

    def list_workspace_data_connections(self, workspace_id: str | None = None):
        rows = list(self.connections.values())
        if workspace_id:
            rows = [row for row in rows if row.get("workspace_id") == str(workspace_id)]
        return [self._row_with_owner(row) for row in rows]

    def get_workspace_data_connection(self, connection_id: str, include_trashed: bool = False):
        _ = include_trashed
        return self.connections.get(connection_id)

    def save_workspace_data_connection(self, payload: dict, author: str = "admin") -> dict:
        _ = author
        self.connections[payload["connection_id"]] = dict(payload)
        return self.connections[payload["connection_id"]]

    def save_workspace_data_connection_with_secrets(
        self,
        payload: dict,
        *,
        secret_values: dict[str, str],
        author: str = "admin",
    ) -> dict:
        self.atomic_connection_save_calls.append(
            {"payload": dict(payload), "secret_values": dict(secret_values)}
        )
        refs = dict(payload.get("secret_refs_jsonb", {}).get("refs", {}) or {})
        if secret_values:
            connection_id = str(payload.get("connection_id") or "")
            self.saved_connection_secrets.append(
                {
                    "account_id": str(payload.get("account_id") or ""),
                    "connection_id": connection_id,
                    "secrets": dict(secret_values),
                }
            )
            refs.update(
                {name: f"project_connection:{connection_id}:{name}" for name in secret_values}
            )
        saved_payload = dict(payload)
        saved_payload["secret_refs_jsonb"] = {"refs": refs} if refs else {}
        return self.save_workspace_data_connection(saved_payload, author=author)

    def set_project_datasets(self, project_id: int, dataset_ids: list[str], author: str = "admin"):
        _ = author
        normalized_project_id = str(project_id)
        project_scope = self._get_project_scope(normalized_project_id)
        wanted = set(dataset_ids)
        invalid = [
            dataset_id
            for dataset_id in dataset_ids
            if self.rows.get(dataset_id, {}).get("workspace_id") != project_scope["workspace_id"]
        ]
        if invalid:
            raise ValueError(f"Dataset(s) belong to a different workspace: {', '.join(invalid)}")
        for dataset_id, row in self.rows.items():
            if row.get("workspace_id") != project_scope["workspace_id"]:
                continue
            project_ids = list(row.get("project_ids") or [])
            if dataset_id in wanted and normalized_project_id not in project_ids:
                project_ids.append(normalized_project_id)
            if dataset_id not in wanted:
                project_ids = [item for item in project_ids if item != normalized_project_id]
            row["project_ids"] = sorted(project_ids)
        return self.list_datasets(project_id=project_id)

    def _get_project_scope(self, project_id: str):
        return self.projects.get(str(project_id))

    def get_project(self, project_id: str):
        return self.projects.get(str(project_id))

    def get_workspace(self, workspace_id: str, **_kwargs):
        normalized_workspace_id = str(workspace_id or "").strip()
        if normalized_workspace_id in {"ws-1", "ws-2"}:
            return {
                "id": normalized_workspace_id,
                "workspace_id": normalized_workspace_id,
                "account_id": "account-1",
                "tenant_id": "tenant-1",
            }
        return None

    def list_project_dataset_catalog(self, project_id: str):
        normalized_project_id = str(project_id)
        project_scope = self._get_project_scope(normalized_project_id)
        available = [
            self._row_with_owner(row)
            for row in self.rows.values()
            if row.get("workspace_id") == project_scope["workspace_id"]
        ]
        explicit_dataset_ids = sorted(
            [
                row["dataset_id"]
                for row in available
                if normalized_project_id in (row.get("project_ids") or [])
            ]
        )
        dataset_ids = sorted(
            [
                row["dataset_id"]
                for row in available
                if row.get("availability_mode") == "all_workspace_projects"
                or normalized_project_id in (row.get("project_ids") or [])
            ]
        )
        return {
            "project_id": normalized_project_id,
            "workspace_id": project_scope["workspace_id"],
            "dataset_ids": dataset_ids,
            "explicit_dataset_ids": explicit_dataset_ids,
            "available_datasets": available,
        }

    def toggle_dataset(self, dataset_id: str) -> bool:
        row = self.rows.get(dataset_id)
        if not row:
            raise KeyError(dataset_id)
        row["enabled"] = not bool(row.get("enabled", True))
        return bool(row["enabled"])

    def delete_dataset(self, dataset_id: str, author: str = "admin") -> None:
        _ = author
        if dataset_id not in self.rows:
            raise KeyError(dataset_id)
        del self.rows[dataset_id]

    def list_dataset_query_templates(self, dataset_id: str | None = None):
        rows = list(self.query_templates)
        if dataset_id:
            rows = [row for row in rows if row.get("dataset_id") == dataset_id]
        return [dict(row) for row in rows]

    def get_dataset_query_template(self, query_template_id: str):
        for row in self.query_templates:
            if row.get("query_template_id") == query_template_id:
                return row
        return None

    def save_dataset_query_template(self, payload: dict, author: str = "admin") -> dict:
        _ = author
        snapshot = dict(payload)
        self.saved_query_writes.append(snapshot)
        for index, row in enumerate(self.query_templates):
            if row.get("query_template_id") == snapshot.get("query_template_id"):
                self.query_templates[index] = snapshot
                return dict(snapshot)
        self.query_templates.append(snapshot)
        return dict(snapshot)

    @contextmanager
    def _cursor(self):
        class _Cursor:
            def __init__(self, connection_id_data_type: str) -> None:
                self._connection_id_data_type = connection_id_data_type

            def execute(self, _sql: str, _params=None):
                return None

            def fetchone(self):
                # The live registry store uses psycopg RealDictCursor, so
                # information_schema rows arrive as dict rows — not tuples.
                # Mirror that here so the test exercises the real code path
                # (BUG-2: positional row[0]/row[1] indexing raised KeyError: 0).
                data_type = self._connection_id_data_type
                if data_type == "uuid":
                    return {"data_type": "uuid", "udt_name": "uuid"}
                return {"data_type": data_type, "udt_name": data_type}

        yield _Cursor(self.connection_id_data_type)


def test_connection_secret_and_row_save_share_a_transaction(monkeypatch) -> None:
    from core.application.management import project_connection_secrets
    from core.application.registry.db_store_datasets_mixin import (
        RegistryDBStoreDatasetsMixin,
    )

    class AtomicStore(RegistryDBStoreDatasetsMixin):
        def __init__(self) -> None:
            self.events: list[str] = []
            self.saved_secrets: dict[str, str] = {}
            self._conn = object()

        @contextmanager
        def _transaction(self):
            self.events.append("begin")
            try:
                yield self._conn
            except Exception:
                self.saved_secrets.clear()
                self.events.append("rollback")
                raise
            else:
                self.events.append("commit")

        def _get_tx_conn(self):
            return self._conn

        @staticmethod
        def _json_object(value):
            return dict(value) if isinstance(value, dict) else {}

        def save_workspace_data_connection(self, _data: dict, author: str = "admin") -> dict:
            _ = author
            self.events.append("connection")
            raise ValueError("connection write failed")

    store = AtomicStore()

    def save_secrets(conn, *, account_id, connection_id, secrets):
        assert conn is store._conn
        assert account_id == "account-1"
        assert connection_id == "connection-1"
        store.events.append("secrets")
        store.saved_secrets.update(secrets)
        return {"refs": {"password": "project_connection:connection-1:password"}}

    monkeypatch.setattr(project_connection_secrets, "save_connection_secrets", save_secrets)

    with pytest.raises(ValueError, match="connection write failed"):
        store.save_workspace_data_connection_with_secrets(
            {
                "account_id": "account-1",
                "connection_id": "connection-1",
                "secret_refs_jsonb": {"refs": {"existing": "stored-ref"}},
            },
            secret_values={"password": "new-password"},
        )

    assert store.events == ["begin", "secrets", "connection", "rollback"]
    assert store.saved_secrets == {}


def test_projection_lookup_requires_the_same_active_install_release() -> None:
    from core.application.registry.db_store_datasets_mixin import (
        RegistryDBStoreDatasetsMixin,
    )

    class _Cursor:
        def __init__(self) -> None:
            self.statement = ""
            self.params = ()

        def execute(self, statement, params):
            self.statement = str(statement)
            self.params = params

        @staticmethod
        def fetchone():
            return {
                "account_id": "account-1",
                "install_id": "install-1",
                "release_id": "release-1",
                "artifact_kind": "dataset",
                "artifact_id": "orders",
                "projection_jsonb": {"dataset_id": "orders"},
                "is_active": True,
            }

    class _Store(RegistryDBStoreDatasetsMixin):
        def __init__(self) -> None:
            self.cursor = _Cursor()

        @staticmethod
        def _table_exists(_table_name: str) -> bool:
            return True

        @contextmanager
        def _cursor(self):
            yield self.cursor

        @staticmethod
        def _json_object(value):
            return dict(value) if isinstance(value, dict) else {}

    store = _Store()

    row = store.get_extension_registry_projection(
        account_id="account-1",
        install_id="install-1",
        artifact_kind="dataset",
        artifact_id="orders",
    )

    assert row["projection"] == {"dataset_id": "orders"}
    assert "AND is_active = true" in store.cursor.statement
    assert "install.install_id = extension_registry_projections.install_id" in (
        store.cursor.statement
    )
    assert "install.account_id = extension_registry_projections.account_id" in (
        store.cursor.statement
    )
    assert "install.release_id = extension_registry_projections.release_id" in (
        store.cursor.statement
    )
    assert "install.state = 'active'" in store.cursor.statement
    assert store.cursor.params == ("account-1", "install-1", "dataset", "orders")


def _client(db: FakeDatasetDB, monkeypatch, *, scope_context=None) -> TestClient:
    monkeypatch.setattr(datasets_routes, "_get_db", lambda: db)
    app = FastAPI()
    app.include_router(datasets_routes.router)

    @app.middleware("http")
    async def mock_auth(request, call_next):
        request.state.auth_user = {
            "id": "admin-1",
            "username": "admin",
            "roles": ["admin"],
            "tenant_id": "tenant-1",
            "account_id": "account-1",
        }
        if scope_context is not None:
            request.state.scope_context = scope_context
        return await call_next(request)

    return TestClient(app)


def test_dataset_registry_create_list_update_toggle_delete(monkeypatch) -> None:
    db = FakeDatasetDB()
    client = _client(db, monkeypatch)

    create_response = client.post(
        "/api/datasets",
        json={
            "display_name": "Customer Orders",
            "description": "Orders data source",
            "workspace_id": "ws-1",
            "availability_mode": "specific_projects_only",
            "connection_id": "warehouse-primary",
            "tags": ["orders"],
        },
    )
    assert create_response.status_code == 200
    assert create_response.json()["dataset_id"] == "customer_orders"
    assert db.rows["customer_orders"]["runtime_kind"] == "local_extension"
    assert db.rows["customer_orders"]["query_language"] == "sql"

    assignment_response = client.put(
        "/api/datasets/project/3",
        json={"dataset_ids": ["customer_orders"]},
    )
    assert assignment_response.status_code == 200
    assert assignment_response.json()["dataset_ids"] == ["customer_orders"]

    list_response = client.get("/api/datasets?project_id=3")
    assert list_response.status_code == 200
    assert (
        list_response.json()["datasets"][0]["connector_id"] == "extension.install_1.warehouse_sql"
    )

    update_response = client.put(
        "/api/datasets/customer_orders",
        json={
            "dataset_id": "ignored",
            "display_name": "Orders",
            "workspace_id": "ws-1",
            "availability_mode": "specific_projects_only",
            "connection_id": "warehouse-primary",
            "read_only": True,
            "enabled": False,
            "metadata_jsonb": {"owner": "ops"},
        },
    )
    assert update_response.status_code == 200
    assert db.rows["customer_orders"]["connector_kind"] == "extension"
    assert db.rows["customer_orders"]["metadata_jsonb"]["owner"] == "ops"
    assert (
        db.rows["customer_orders"]["metadata_jsonb"]["dataset_connector"]["local_connector_id"]
        == "warehouse_sql"
    )

    toggle_response = client.patch("/api/datasets/customer_orders/toggle")
    assert toggle_response.status_code == 200
    assert toggle_response.json()["enabled"] is True

    delete_response = client.delete("/api/datasets/customer_orders")
    assert delete_response.status_code == 200
    assert db.rows == {}


def test_dataset_catalog_hides_inactive_durable_projection_bridge(monkeypatch) -> None:
    db = FakeDatasetDB()
    dataset_id = "extension.install-1.demo_orders"
    db.rows[dataset_id] = {
        "dataset_id": dataset_id,
        "workspace_id": "ws-1",
        "account_id": "account-1",
        "tenant_id": "tenant-1",
        "display_name": "Disabled projected dataset",
        "availability_mode": "all_workspace_projects",
        "enabled": True,
        "metadata_jsonb": {"projection_bridge": {"install_id": "install-1"}},
        "project_ids": [],
    }
    client = _client(db, monkeypatch)

    listing = client.get("/api/datasets")
    detail = client.get(f"/api/datasets/{dataset_id}")

    assert listing.status_code == 200
    assert listing.json()["datasets"] == []
    assert detail.status_code == 404


def test_dataset_connection_action_applies_project_state_updates(monkeypatch) -> None:
    from core.application.datasets import connection_actions
    from core.application.extensions import response_envelope

    db = FakeDatasetDB()
    connection = {**db.connections["warehouse-primary"], "project_id": "3"}
    extension = {
        "extension_key": "warehouse-extension",
        "install_id": "install_1",
        "compiled_manifest": {
            "project_state": {
                "writes": [{"namespace": "connection_health", "state_key": "last_check"}]
            }
        },
    }
    captured: dict = {}

    def fake_put_state(*_args, **kwargs):
        captured.update(kwargs)
        return {"revision": 1, **kwargs}

    monkeypatch.setattr(
        connection_actions,
        "resolve_extension_runtime_record_by_install_id",
        lambda _candidate, **_kwargs: (extension, None),
    )
    monkeypatch.setattr(response_envelope.extension_project_state, "put_state", fake_put_state)
    monkeypatch.setattr(
        connection_actions,
        "run_extension_action",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {"healthy": True},
                "project_state_updates": [
                    {
                        "namespace": "connection_health",
                        "state_key": "last_check",
                        "value_jsonb": {"healthy": True},
                    }
                ],
            },
        },
    )

    response = datasets_helpers._invoke_connection_action(db, connection, "test_connection")

    assert response["ok"] is True
    assert captured["account_id"] == "account-1"
    assert captured["project_id"] == "3"
    assert captured["extension_id"] == "warehouse-extension"
    assert captured["extension_install_id"] == "install_1"
    assert captured["connection_id"] == "warehouse-primary"
    assert captured["namespace"] == "connection_health"


def test_dataset_platform_connection_action_uses_shared_dispatcher(monkeypatch) -> None:
    db = FakeDatasetDB()
    connection = {
        **db.connections["warehouse-primary"],
        "runtime_kind": "platform",
        "connector_ref_jsonb": {"connector_id": "platform.postgres"},
    }
    monkeypatch.setattr(
        datasets_helpers,
        "_connection_extension",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("admin helper must not resolve platform.postgres as an extension")
        ),
    )
    monkeypatch.setattr(
        "core.application.datasets.connection_actions.execute_connection_action",
        lambda _connection, _action, **_kwargs: {
            "ok": True,
            "result": {"connected": True},
            "errors": [],
        },
    )

    response = datasets_helpers._invoke_connection_action(db, connection, "test_connection")

    assert response == {"ok": True, "result": {"connected": True}, "errors": []}


def test_dataset_admin_runtime_prefers_connection_install_id(monkeypatch) -> None:
    candidates: list[str] = []

    def resolve(candidate: str, **_kwargs):
        candidates.append(candidate)
        return ({"extension_key": candidate}, None)

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record_by_install_id",
        resolve,
    )

    extension = datasets_helpers._connection_extension(
        None,
        {
            "connector_ref_jsonb": {
                "extension_id": "mutable.catalog-extension",
                "install_id": "install-pinned-release",
            }
        },
    )

    assert candidates == ["install-pinned-release"]
    assert extension == {"extension_key": "install-pinned-release"}


def test_dataset_admin_rejects_missing_pinned_install_without_fallback(monkeypatch) -> None:
    candidates: list[str] = []

    def resolve(candidate: str, **_kwargs):
        candidates.append(candidate)
        return (None, None)

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record_by_install_id",
        resolve,
    )

    with pytest.raises(HTTPException, match="runtime is unavailable"):
        datasets_helpers._connection_extension(
            None,
            {
                "connector_ref_jsonb": {
                    "extension_id": "mutable.catalog-extension",
                    "install_id": "install-missing-release",
                }
            },
        )

    assert candidates == ["install-missing-release"]


def test_dataset_registry_rejects_invalid_payload(monkeypatch) -> None:
    client = _client(FakeDatasetDB(), monkeypatch)

    response = client.post(
        "/api/datasets",
        json={
            "dataset_id": "Invalid ID",
            "display_name": "Bad",
            "workspace_id": "ws-1",
            "connection_id": "warehouse-primary",
        },
    )

    assert response.status_code == 400
    assert "dataset_id must match" in response.json()["detail"]


def test_dataset_connector_catalog_is_extension_backed(monkeypatch) -> None:
    client = _client(FakeDatasetDB(), monkeypatch)

    response = client.get("/api/datasets/connectors")

    assert response.status_code == 200
    connector = response.json()["connectors"][0]
    assert connector["connector_id"] == "extension.install_1.warehouse_sql"
    assert connector["query_language"] == "sql"
    assert connector["source_install_id"] == "install_1"


def _add_mysql_connection(db: FakeDatasetDB) -> None:
    db.connectors.append(
        {
            "connector_id": "extension.install_2.mysql",
            "local_connector_id": "mysql",
            "display_name": "MySQL",
            "connector_kind": "extension",
            "runtime_kind": "local_extension",
            "query_language": "sql",
            "source_install_id": "install_2",
            "source_release_id": "release_2",
            "extension_id": "mysql-extension",
            "account_id": "account-1",
        }
    )
    db.connections["mysql-primary"] = {
        **db.connections["warehouse-primary"],
        "connection_id": "mysql-primary",
        "display_name": "MySQL Primary",
        "connector_ref_jsonb": {
            "connector_id": "extension.install_2.mysql",
            "local_connector_id": "mysql",
            "install_id": "install_2",
            "extension_id": "mysql-extension",
        },
    }


def test_dataset_create_rejects_an_independent_connector_override(monkeypatch) -> None:
    db = FakeDatasetDB()
    _add_mysql_connection(db)
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets",
        json={
            "dataset_id": "mismatched_binding",
            "display_name": "Mismatched binding",
            "workspace_id": "ws-1",
            "connection_id": "mysql-primary",
            "connector_id": "extension.install_1.warehouse_sql",
            "metadata_jsonb": {"connection_id": "mysql-primary"},
        },
    )

    assert response.status_code == 422
    assert "mismatched_binding" not in db.rows


def test_dataset_create_derives_connector_identity_from_the_selected_connection(
    monkeypatch,
) -> None:
    db = FakeDatasetDB()
    _add_mysql_connection(db)
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets",
        json={
            "dataset_id": "mysql_orders",
            "display_name": "MySQL orders",
            "workspace_id": "ws-1",
            "connection_id": "mysql-primary",
        },
    )

    assert response.status_code == 200
    saved = db.rows["mysql_orders"]
    assert saved["connector_id"] == "extension.install_2.mysql"
    assert saved["metadata_jsonb"]["connection_id"] == "mysql-primary"
    assert saved["metadata_jsonb"]["dataset_connector"]["source_install_id"] == "install_2"


def test_dataset_create_overwrites_reserved_binding_metadata(monkeypatch) -> None:
    db = FakeDatasetDB()
    _add_mysql_connection(db)
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets",
        json={
            "dataset_id": "canonical_binding",
            "display_name": "Canonical binding",
            "workspace_id": "ws-1",
            "connection_id": "mysql-primary",
            "metadata_jsonb": {
                "connection_id": "warehouse-primary",
                "dataset_connector": {
                    "connector_id": "extension.install_1.warehouse_sql",
                    "source_install_id": "install_1",
                },
            },
        },
    )

    assert response.status_code == 200
    saved = db.rows["canonical_binding"]
    assert saved["metadata_jsonb"]["connection_id"] == "mysql-primary"
    assert (
        saved["metadata_jsonb"]["dataset_connector"]["connector_id"] == "extension.install_2.mysql"
    )


@pytest.mark.parametrize("method", ["post", "put", "patch"])
def test_dataset_writes_cannot_forge_server_owned_projection_metadata(
    monkeypatch, method: str
) -> None:
    db = FakeDatasetDB()
    dataset_id = "projection_forgery"
    if method != "post":
        db.rows[dataset_id] = {
            "dataset_id": dataset_id,
            "display_name": "Projection forgery",
            "workspace_id": "ws-1",
            "availability_mode": "specific_projects_only",
            "connector_id": "extension.install_1.warehouse_sql",
            "connector_kind": "extension",
            "runtime_kind": "local_extension",
            "query_language": "sql",
            "read_only": True,
            "enabled": True,
            "metadata_jsonb": {"connection_id": "warehouse-primary"},
            "project_ids": [],
        }
    client = _client(db, monkeypatch)
    forged_metadata = {
        "connection_resolution": "project_scope",
        "projection_bridge": {
            "immutable": True,
            "install_id": "forged-install",
            "release_id": "forged-release",
            "artifact_kind": "dataset",
            "artifact_id": dataset_id,
        },
        "account_id": "forged-account",
        "tenant_id": "forged-tenant",
        "source_install_id": "forged-install",
        "source_release_id": "forged-release",
    }
    if method == "post":
        response = client.post(
            "/api/datasets",
            json={
                "dataset_id": dataset_id,
                "display_name": "Projection forgery",
                "workspace_id": "ws-1",
                "connection_id": "warehouse-primary",
                "metadata_jsonb": forged_metadata,
            },
        )
    elif method == "put":
        response = client.put(
            f"/api/datasets/{dataset_id}",
            json={
                "display_name": "Projection forgery",
                "workspace_id": "ws-1",
                "connection_id": "warehouse-primary",
                "metadata_jsonb": forged_metadata,
            },
        )
    else:
        response = client.patch(
            f"/api/datasets/{dataset_id}",
            json={"metadata_jsonb": forged_metadata},
        )

    assert response.status_code == 200
    saved_metadata = db.rows[dataset_id]["metadata_jsonb"]
    assert saved_metadata["connection_id"] == "warehouse-primary"
    for reserved_key in (
        "connection_resolution",
        "projection_bridge",
        "account_id",
        "tenant_id",
        "source_install_id",
        "source_release_id",
    ):
        assert reserved_key not in saved_metadata


def test_dataset_patch_rederives_binding_from_persisted_connection(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["orders"] = {
        "dataset_id": "orders",
        "display_name": "Orders",
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "connector_id": "stale.connector",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "read_only": True,
        "enabled": True,
        "metadata_jsonb": {"connection_id": "warehouse-primary"},
        "project_ids": [],
    }
    db.connections["warehouse-primary"]["health_status"] = "unhealthy"
    client = _client(db, monkeypatch)

    response = client.patch("/api/datasets/orders", json={"display_name": "Orders v2"})

    assert response.status_code == 200
    saved = db.rows["orders"]
    assert saved["display_name"] == "Orders v2"
    assert saved["connector_id"] == "extension.install_1.warehouse_sql"
    assert saved["metadata_jsonb"]["connection_id"] == "warehouse-primary"


def test_dataset_project_assignment_keeps_connection_as_binding_authority(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["orders"] = {
        "dataset_id": "orders",
        "display_name": "Orders",
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "connector_id": "stale.connector",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "read_only": True,
        "enabled": True,
        "metadata_jsonb": {"connection_id": "warehouse-primary"},
        "project_ids": [],
    }
    client = _client(db, monkeypatch)

    response = client.put("/api/datasets/orders/projects", json={"project_ids": ["3"]})

    assert response.status_code == 200
    saved = db.rows["orders"]
    assert saved["project_ids"] == ["3"]
    assert saved["connector_id"] == "extension.install_1.warehouse_sql"
    assert saved["metadata_jsonb"]["connection_id"] == "warehouse-primary"


def test_dataset_project_assignment_scopes_the_connector_catalog(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["orders"] = {
        "dataset_id": "orders",
        "display_name": "Orders",
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "read_only": True,
        "enabled": True,
        "metadata_jsonb": {"connection_id": "warehouse-primary"},
        "project_ids": [],
    }
    observed_scopes = []
    original_catalog = datasets_routes._connector_catalog_by_id

    def _scoped_catalog(db_arg, *, scope=None):
        observed_scopes.append(scope)
        return original_catalog(db_arg, scope=scope)

    monkeypatch.setattr(datasets_routes, "_connector_catalog_by_id", _scoped_catalog)
    client = _client(db, monkeypatch)

    response = client.put("/api/datasets/orders/projects", json={"project_ids": ["3"]})

    assert response.status_code == 200
    assert len(observed_scopes) == 1
    assert observed_scopes[0] is not None
    assert observed_scopes[0].account_id == "account-1"


@pytest.mark.parametrize(
    ("connection_update", "expected_detail"),
    [
        ({"enabled": False}, "disabled"),
        (
            {
                "connector_ref_jsonb": {
                    "connector_id": "extension.install_1.warehouse_sql",
                    "install_id": "missing-install",
                }
            },
            "not installed",
        ),
    ],
)
def test_dataset_create_rejects_non_runnable_extension_binding(
    monkeypatch, connection_update: dict, expected_detail: str
) -> None:
    db = FakeDatasetDB()
    db.connections["warehouse-primary"].update(connection_update)
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets",
        json={
            "dataset_id": "orders",
            "display_name": "Orders",
            "workspace_id": "ws-1",
            "connection_id": "warehouse-primary",
        },
    )

    assert response.status_code == 400
    assert expected_detail in response.json()["detail"]
    assert "orders" not in db.rows


def test_data_connection_list_marks_only_extension_bindings_query_capable(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.connections["platform-internal"] = {
        **db.connections["warehouse-primary"],
        "connection_id": "platform-internal",
        "display_name": "Flow Steward App Database",
        "connector_ref_jsonb": {"connector_id": "platform.postgres"},
    }
    client = _client(db, monkeypatch)

    response = client.get("/api/datasets/data-connections")

    assert response.status_code == 200
    by_id = {row["connection_id"]: row for row in response.json()["connections"]}
    assert by_id["warehouse-primary"]["query_capable"] is True
    assert by_id["platform-internal"]["query_capable"] is False
    assert "Data Connector extension" in by_id["platform-internal"]["query_capability_issue"]


def test_dataset_create_rejects_the_internal_platform_database(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.connections["platform-internal"] = {
        **db.connections["warehouse-primary"],
        "connection_id": "platform-internal",
        "display_name": "Flow Steward App Database",
        "connector_ref_jsonb": {
            "connector_id": "platform.postgres",
            "local_connector_id": "postgres",
        },
    }
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets",
        json={
            "dataset_id": "internal_platform_database",
            "display_name": "Internal platform database",
            "workspace_id": "ws-1",
            "connection_id": "platform-internal",
            "project_ids": ["3"],
        },
    )

    assert response.status_code == 400
    assert "Data Connector extension" in response.json()["detail"]
    assert "internal_platform_database" not in db.rows


def test_data_connection_create_rejects_the_internal_platform_database(monkeypatch) -> None:
    db = FakeDatasetDB()
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets/data-connections",
        json={
            "workspace_id": "ws-1",
            "connector_id": "platform.postgres",
            "display_name": "Internal platform database",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown dataset connector 'platform.postgres'"
    assert db.atomic_connection_save_calls == []


def test_dataset_connector_catalog_filters_other_account_installs(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.connectors.append(
        {
            "connector_id": "extension.install_2.other_account_sql",
            "local_connector_id": "other_account_sql",
            "display_name": "Other Account SQL",
            "connector_kind": "extension",
            "runtime_kind": "local_extension",
            "query_language": "sql",
            "source_install_id": "install_2",
            "extension_id": "warehouse-extension",
            "account_id": "account-2",
        }
    )
    client = _client(db, monkeypatch)

    response = client.get("/api/datasets/connectors")

    assert response.status_code == 200
    connector_ids = {row["connector_id"] for row in response.json()["connectors"]}
    assert "extension.install_1.warehouse_sql" in connector_ids
    assert "extension.install_2.other_account_sql" not in connector_ids


def test_data_connection_action_rejects_other_account_connection(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.connections["foreign-warehouse"] = {
        **db.connections["warehouse-primary"],
        "connection_id": "foreign-warehouse",
        "account_id": "account-2",
    }
    client = _client(db, monkeypatch)

    invoked = {"called": False}

    def _invoke(_connection, _action_id):
        invoked["called"] = True
        return {"ok": True}

    monkeypatch.setattr(datasets_routes, "_invoke_connection_action", _invoke)

    response = client.post("/api/datasets/data-connections/foreign-warehouse/test")

    assert response.status_code == 404
    assert invoked["called"] is False


def test_saved_query_preview_rejects_other_account_dataset(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["foreign_dataset"] = {
        "dataset_id": "foreign_dataset",
        "workspace_id": "ws-foreign",
        "tenant_id": "tenant-1",
        "account_id": "account-2",
        "availability_mode": "specific_projects_only",
        "display_name": "Foreign Dataset",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": [],
    }
    db.query_templates.append(
        {
            "query_template_id": "qt_foreign",
            "dataset_id": "foreign_dataset",
            "account_id": "account-2",
            "display_name": "Foreign query",
        }
    )
    client = _client(db, monkeypatch)

    invoked = {"called": False}

    def _invoke(_db, _template, _action_id, _body, **_kwargs):
        invoked["called"] = True
        return {"ok": True}

    monkeypatch.setattr(datasets_routes, "_invoke_saved_query_action", _invoke)

    response = client.post(
        "/api/datasets/query-templates/qt_foreign/preview",
        json={"params": {}, "row_limit": 10},
    )

    assert response.status_code == 404
    assert invoked["called"] is False


def test_dataset_create_derives_filtered_schema_from_connection_snapshot(monkeypatch) -> None:
    db = FakeDatasetDB()
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets",
        json={
            "display_name": "Orders dataset",
            "workspace_id": "ws-1",
            "availability_mode": "specific_projects_only",
            "connection_id": "warehouse-primary",
            "resource_scope_jsonb": {
                "version": 1,
                "resources": [{"resource_id": "orders", "columns": ["order_id"]}],
            },
            "schema_snapshot_jsonb": {
                "resources": [{"resource_id": "customers", "columns": [{"name": "email"}]}]
            },
        },
    )

    assert response.status_code == 200
    row = db.rows["orders_dataset"]
    assert row["metadata_jsonb"]["resource_scope_jsonb"]["resources"] == [
        {"resource_id": "orders", "columns": ["order_id"]}
    ]
    assert [resource["resource_id"] for resource in row["schema_snapshot_jsonb"]["resources"]] == [
        "orders"
    ]
    assert row["schema_snapshot_jsonb"]["resources"][0]["columns"] == [{"name": "order_id"}]


def test_connection_introspect_persists_latest_schema_snapshot(monkeypatch) -> None:
    db = FakeDatasetDB()
    client = _client(db, monkeypatch)

    monkeypatch.setattr(
        datasets_routes,
        "_invoke_connection_action",
        lambda connection, action_id: {
            "ok": True,
            "result": {
                "schema_snapshot": {
                    "schema_version": "dataset_schema_snapshot.v1",
                    "resources": [{"resource_id": "projects", "columns": [{"name": "id"}]}],
                }
            },
            "commands": [],
            "events": [],
            "reload": [],
            "errors": [],
        },
    )

    response = client.post("/api/datasets/data-connections/warehouse-primary/introspect")

    assert response.status_code == 200
    assert db.connections["warehouse-primary"]["metadata_jsonb"]["last_schema_snapshot"] == {
        "schema_version": "dataset_schema_snapshot.v1",
        "resources": [{"resource_id": "projects", "columns": [{"name": "id"}]}],
    }
    assert db.connections["warehouse-primary"]["health_status"] == "healthy"
    assert db.connections["warehouse-primary"]["last_checked_at"]
    assert db.connections["warehouse-primary"]["last_schema_refresh_at"]
    assert db.connections["warehouse-primary"]["last_error"] == ""


def test_connection_test_persists_operational_metadata(monkeypatch) -> None:
    db = FakeDatasetDB()
    client = _client(db, monkeypatch)

    monkeypatch.setattr(
        datasets_routes,
        "_invoke_connection_action",
        lambda connection, action_id: {
            "ok": True,
            "result": {"status": "healthy"},
            "commands": [],
            "events": [],
            "reload": [],
            "errors": [],
        },
    )

    response = client.post("/api/datasets/data-connections/warehouse-primary/test")

    assert response.status_code == 200
    assert db.connections["warehouse-primary"]["health_status"] == "healthy"
    assert db.connections["warehouse-primary"]["last_checked_at"]
    assert db.connections["warehouse-primary"]["last_error"] == ""


def test_connection_test_persists_failure_metadata(monkeypatch) -> None:
    db = FakeDatasetDB()
    client = _client(db, monkeypatch)

    monkeypatch.setattr(
        datasets_routes,
        "_invoke_connection_action",
        lambda connection, action_id: {
            "ok": False,
            "result": {},
            "commands": [],
            "events": [],
            "reload": [],
            "errors": [{"code": "connector_runtime_failed", "message": "no password supplied"}],
        },
    )

    response = client.post("/api/datasets/data-connections/warehouse-primary/test")

    assert response.status_code == 200
    assert db.connections["warehouse-primary"]["health_status"] == "unhealthy"
    assert db.connections["warehouse-primary"]["last_checked_at"]
    assert db.connections["warehouse-primary"]["last_error"] == "no password supplied"


def test_connection_test_returns_safe_binding_error_and_persists_unhealthy_state(
    monkeypatch,
) -> None:
    db = FakeDatasetDB()
    client = _client(db, monkeypatch)

    monkeypatch.setattr(
        "core.application.datasets.connection_actions.resolve_extension_runtime_record_by_install_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("internal identifier error")),
    )

    response = client.post("/api/datasets/data-connections/warehouse-primary/test")

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "result": {},
        "errors": [
            {
                "code": "connector_binding_invalid",
                "message": "The selected data connector is invalid. Save the connection again.",
            }
        ],
    }
    assert db.connections["warehouse-primary"]["health_status"] == "unhealthy"
    assert db.connections["warehouse-primary"]["last_error"] == (
        "The selected data connector is invalid. Save the connection again."
    )


def test_connection_test_unwraps_json_error_message(monkeypatch) -> None:
    db = FakeDatasetDB()
    client = _client(db, monkeypatch)

    monkeypatch.setattr(
        datasets_routes,
        "_invoke_connection_action",
        lambda connection, action_id: {
            "ok": False,
            "result": {},
            "commands": [],
            "events": [],
            "reload": [],
            "errors": [
                {
                    "code": "connector_runtime_failed",
                    "message": '{"error": "connection failed: password authentication failed"}',
                }
            ],
        },
    )

    response = client.post("/api/datasets/data-connections/warehouse-primary/test")

    assert response.status_code == 200
    assert db.connections["warehouse-primary"]["health_status"] == "unhealthy"
    assert (
        db.connections["warehouse-primary"]["last_error"]
        == "connection failed: password authentication failed"
    )


def test_connection_update_preserves_existing_secret_refs_when_password_is_blank(
    monkeypatch,
) -> None:
    db = FakeDatasetDB()
    db.connections["warehouse-primary"]["secret_refs_jsonb"] = {
        "refs": {"password": "stored-password-ref"}
    }
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets/data-connections",
        json={
            "connection_id": "warehouse-primary",
            "workspace_id": "ws-1",
            "connector_id": "extension.install_1.warehouse_sql",
            "display_name": "Warehouse Primary renamed",
            "description": "updated",
            "environment": "dev",
            "enabled": True,
            "tags": [],
            "connection_config_jsonb": {"prebuilt": "postgres"},
            "secret_refs_jsonb": {},
            "metadata_jsonb": {},
        },
    )

    assert response.status_code == 200
    assert db.connections["warehouse-primary"]["display_name"] == "Warehouse Primary renamed"
    assert db.connections["warehouse-primary"]["secret_refs_jsonb"] == {
        "refs": {"password": "stored-password-ref"}
    }


def test_connection_update_saves_raw_password_as_secret_ref(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.connections["warehouse-primary"]["secret_refs_jsonb"] = {}
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets/data-connections",
        json={
            "connection_id": "warehouse-primary",
            "workspace_id": "ws-1",
            "connector_id": "extension.install_1.warehouse_sql",
            "display_name": "Warehouse Primary",
            "description": "updated",
            "environment": "dev",
            "enabled": True,
            "tags": [],
            "connection_config_jsonb": {"prebuilt": "postgres"},
            "secret_refs_jsonb": {"password": "fresh-password"},
            "metadata_jsonb": {},
        },
    )

    assert response.status_code == 200
    assert db.saved_connection_secrets == [
        {
            "account_id": "account-1",
            "connection_id": "warehouse-primary",
            "secrets": {"password": "fresh-password"},
        }
    ]
    assert db.connections["warehouse-primary"]["secret_refs_jsonb"] == {
        "refs": {"password": "project_connection:warehouse-primary:password"}
    }


def test_connection_create_requires_atomic_repository_contract(monkeypatch) -> None:
    class IncompleteDatasetDB(FakeDatasetDB):
        save_workspace_data_connection_with_secrets = None

    client = _client(IncompleteDatasetDB(), monkeypatch)

    response = client.post(
        "/api/datasets/data-connections",
        json={
            "workspace_id": "ws-1",
            "connector_id": "extension.install_1.warehouse_sql",
            "display_name": "Unavailable atomic save",
            "connection_config_jsonb": {"prebuilt": "postgres"},
            "secret_refs_jsonb": {"password": "secret"},
            "metadata_jsonb": {},
        },
    )

    assert response.status_code == 503
    assert (
        response.json()["detail"] == datasets_routes.DATA_CONNECTION_STORAGE_IS_NOT_AVAILABLE_VALUE
    )


def test_connection_create_uses_uuid_when_live_column_is_uuid(monkeypatch) -> None:
    db = FakeDatasetDB(connection_id_data_type="uuid")
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets/data-connections",
        json={
            "workspace_id": "ws-1",
            "connector_id": "extension.install_1.warehouse_sql",
            "display_name": "UI Browser Postgres",
            "description": "new",
            "environment": "dev",
            "enabled": True,
            "tags": [],
            "connection_config_jsonb": {"prebuilt": "postgres"},
            "secret_refs_jsonb": {"values": {"password": "secret"}},
            "metadata_jsonb": {},
        },
    )

    assert response.status_code == 200
    connection_id = response.json()["connection_id"]
    assert len(connection_id) == 36
    assert connection_id.count("-") == 4


def test_connection_create_rejects_non_numeric_port(monkeypatch) -> None:
    db = FakeDatasetDB()
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets/data-connections",
        json={
            "workspace_id": "ws-1",
            "connector_id": "extension.install_1.warehouse_sql",
            "display_name": "Invalid Port Warehouse",
            "description": "new",
            "environment": "dev",
            "enabled": True,
            "tags": [],
            "connection_config_jsonb": {
                "prebuilt": "postgres",
                "port": "not-a-port",
            },
            "secret_refs_jsonb": {"values": {"password": "secret"}},
            "metadata_jsonb": {},
        },
    )

    assert response.status_code == 400
    assert "connection_config_jsonb.port: expected type integer" in response.json()["detail"]
    assert "conn_invalid_port_warehouse" not in db.connections


def test_connection_create_validates_manifest_schema_and_applies_defaults(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.connectors[0]["connector_catalog"] = [
        {
            "connector_type_id": "acme_warehouse_v2",
            "preset_config": {"mode": "warehouse-v2"},
            "connection_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["host"],
                "properties": {
                    "mode": {"type": "string", "enum": ["warehouse-v2"]},
                    "host": {"type": "string", "minLength": 1},
                    "port": {"type": "integer", "default": 5432, "minimum": 1},
                },
            },
            "secret_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["password"],
                "properties": {"password": {"type": "string", "minLength": 1}},
            },
        }
    ]
    client = _client(db, monkeypatch)
    payload = {
        "workspace_id": "ws-1",
        "connector_id": "extension.install_1.warehouse_sql",
        "display_name": "Validated warehouse",
        "connection_config_jsonb": {},
        "secret_refs_jsonb": {},
        "metadata_jsonb": {"dataset_connector": {"local_connector_id": "acme_warehouse_v2"}},
    }

    response = client.post("/api/datasets/data-connections", json=payload)
    assert response.status_code == 400
    assert "connection_config_jsonb.host: required property is missing" in response.json()["detail"]

    payload["connection_config_jsonb"] = {"host": "warehouse.internal"}
    response = client.post("/api/datasets/data-connections", json=payload)
    assert response.status_code == 400
    assert "secret_refs_jsonb.password: required property is missing" in response.json()["detail"]

    payload["secret_refs_jsonb"] = {"password": "secret"}
    response = client.post("/api/datasets/data-connections", json=payload)
    assert response.status_code == 200
    connection = db.connections[response.json()["connection_id"]]
    assert connection["connection_config_jsonb"] == {
        "mode": "warehouse-v2",
        "host": "warehouse.internal",
        "port": 5432,
    }
    assert connection["connector_ref_jsonb"]["local_connector_id"] == "acme_warehouse_v2"


def test_connection_create_validates_secrets_before_persisting_them(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.connectors[0]["connection_schema"] = {
        "type": "object",
        "required": ["host"],
        "properties": {"host": {"type": "string"}},
    }
    db.connectors[0]["secret_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["password"],
        "properties": {"password": {"type": "string", "minLength": 1}},
    }
    client = _client(db, monkeypatch)
    payload = {
        "workspace_id": "ws-1",
        "connector_id": "extension.install_1.warehouse_sql",
        "display_name": "Rejected warehouse",
        "connection_config_jsonb": {"host": "warehouse.internal"},
        "metadata_jsonb": {},
    }

    response = client.post(
        "/api/datasets/data-connections",
        json={**payload, "secret_refs_jsonb": {"unexpected": "written-too-early"}},
    )
    assert response.status_code == 400
    assert "secret_refs_jsonb.password: required property is missing" in response.json()["detail"]
    assert db.atomic_connection_save_calls == []

    response = client.post(
        "/api/datasets/data-connections",
        json={**payload, "secret_refs_jsonb": {"refs": {"password": ""}}},
    )
    assert response.status_code == 400
    assert "secret_refs_jsonb.password: required property is missing" in response.json()["detail"]
    assert db.atomic_connection_save_calls == []


def test_query_templates_can_be_filtered_by_project_assignment(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["customer_orders"] = {
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "display_name": "Customer Orders",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": ["3"],
    }
    db.rows["orphan_dataset"] = {
        "dataset_id": "orphan_dataset",
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "display_name": "Orphan Dataset",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": [],
    }
    client = _client(db, monkeypatch)

    response = client.get("/api/datasets/query-templates?project_id=3")

    assert response.status_code == 200
    assert [row["query_template_id"] for row in response.json()["query_templates"]] == [
        "qt_assigned"
    ]


@pytest.mark.parametrize(
    ("query_definition", "params_schema", "expected_path"),
    [
        (
            {"statement": "SELECT order_id FROM orders LIMIT 1"},
            {},
            "query_definition_jsonb.statement",
        ),
        ({}, {}, "query_definition_jsonb.sql"),
        ({"sql": "DELETE FROM orders"}, {}, "query_definition_jsonb.sql"),
        (
            {"sql": "SELECT customer_id FROM customers LIMIT 1"},
            {},
            "query_definition_jsonb.sql",
        ),
        (
            {"sql": "SELECT order_id FROM orders WHERE order_id = :order_id"},
            {"type": "object", "properties": {}},
            "params_schema_jsonb.properties.order_id",
        ),
    ],
)
def test_saved_query_create_rejects_invalid_contract_before_write(
    monkeypatch,
    query_definition: dict,
    params_schema: dict,
    expected_path: str,
) -> None:
    db = FakeDatasetDB()
    db.rows["customer_orders"] = {
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "availability_mode": "all_workspace_projects",
        "resource_scope_jsonb": {
            "version": 1,
            "resources": [{"resource_id": "orders", "columns": ["order_id"]}],
        },
        "enabled": True,
        "project_ids": [],
    }
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets/query-templates",
        json={
            "query_template_id": "orders.invalid",
            "dataset_id": "customer_orders",
            "display_name": "Invalid orders query",
            "query_definition_jsonb": query_definition,
            "params_schema_jsonb": params_schema,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "saved_query_definition_invalid"
    assert response.json()["detail"]["path"] == expected_path
    assert db.saved_query_writes == []


def test_saved_query_create_rejects_empty_dataset_scope_before_write(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["customer_orders"] = {
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "availability_mode": "all_workspace_projects",
        "resource_scope_jsonb": {"version": 1, "resources": []},
        "enabled": True,
        "project_ids": [],
    }
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets/query-templates",
        json={
            "query_template_id": "orders.empty-scope",
            "dataset_id": "customer_orders",
            "display_name": "Empty scope query",
            "query_definition_jsonb": {"sql": "SELECT order_id FROM orders LIMIT 1"},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["path"] == "dataset.resource_scope_jsonb"
    assert db.saved_query_writes == []


def test_saved_query_create_accepts_valid_postgres_contract(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["customer_orders"] = {
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "availability_mode": "all_workspace_projects",
        "resource_scope_jsonb": {
            "version": 1,
            "resources": [{"resource_id": "orders", "columns": ["order_id"]}],
        },
        "enabled": True,
        "project_ids": [],
    }
    client = _client(db, monkeypatch)

    response = client.post(
        "/api/datasets/query-templates",
        json={
            "query_template_id": "orders.by-id",
            "dataset_id": "customer_orders",
            "display_name": "Orders by id",
            "query_definition_jsonb": {
                "sql": "SELECT order_id FROM orders WHERE order_id = :order_id"
            },
            "params_schema_jsonb": {
                "type": "object",
                "properties": {"order_id": {"type": "integer"}},
                "required": ["order_id"],
                "additionalProperties": False,
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert db.saved_query_writes[0]["enabled"] is True


def test_saved_query_contract_accepts_dataset_scope_persisted_in_metadata() -> None:
    from core.application.registry.saved_query_contract import validate_saved_query_contract

    validate_saved_query_contract(
        {
            "query_template_id": "orders.projected",
            "dataset_id": "extension.install-1.orders",
            "query_definition_jsonb": {"sql": "SELECT order_id FROM orders LIMIT 1"},
        },
        dataset={
            "dataset_id": "extension.install-1.orders",
            "metadata_jsonb": {
                "resource_scope_jsonb": {
                    "version": 1,
                    "resources": [{"resource_id": "orders", "columns": ["order_id"]}],
                }
            },
        },
    )


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT count(*)::int FROM jobs LIMIT 1",
        "SELECT id::text::varchar FROM jobs LIMIT 1",
        "SELECT ARRAY[id]::pg_catalog.int8[] FROM jobs LIMIT 1",
        'SELECT "column:name" FROM jobs LIMIT 1',
        "SELECT id::text FROM jobs WHERE id = :job_id AND ':ignored' = ':ignored'",
        "SELECT id::text FROM jobs WHERE id = :job_id /* :ignored ::text */",
    ),
)
def test_saved_query_contract_distinguishes_postgres_casts_from_named_parameters(
    sql: str,
) -> None:
    from core.application.registry.saved_query_contract import validate_saved_query_contract

    validate_saved_query_contract(
        {
            "query_template_id": "jobs.cast-contract",
            "dataset_id": "jobs",
            "query_definition_jsonb": {"sql": sql},
            "params_schema_jsonb": {
                "type": "object",
                "properties": {"job_id": {"type": "integer"}},
            },
        },
        dataset={
            "dataset_id": "jobs",
            "resource_scope_jsonb": {
                "version": 1,
                "resources": [{"resource_id": "jobs", "columns": ["id"]}],
            },
        },
    )


def test_saved_query_contract_error_survives_context_manager_traceback_assignment() -> None:
    from core.application.registry.saved_query_contract import (
        SavedQueryContractError,
        validate_saved_query_contract,
    )

    @contextmanager
    def transaction_boundary():
        yield

    with pytest.raises(SavedQueryContractError) as caught, transaction_boundary():
        validate_saved_query_contract(
            {
                "query_template_id": "orders.invalid",
                "dataset_id": "orders",
                "query_definition_jsonb": {"sql": "SELECT order_id FROM orders LIMIT 1"},
            },
            dataset={"dataset_id": "orders", "resource_scope_jsonb": {}},
        )

    assert caught.value.as_detail() == {
        "code": "saved_query_definition_invalid",
        "field": "dataset",
        "path": "dataset.resource_scope_jsonb",
        "message": "Dataset resource scope is empty",
    }


def test_saved_query_failed_update_preserves_prior_version(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["customer_orders"] = {
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "availability_mode": "all_workspace_projects",
        "resource_scope_jsonb": {
            "version": 1,
            "resources": [{"resource_id": "orders", "columns": ["order_id"]}],
        },
        "enabled": True,
        "project_ids": [],
    }
    prior = {
        "query_template_id": "qt_assigned",
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "account_id": "account-1",
        "tenant_id": "tenant-1",
        "display_name": "Assigned query",
        "query_definition_jsonb": {"sql": "SELECT order_id FROM orders LIMIT 1"},
        "params_schema_jsonb": {"type": "object", "properties": {}},
        "enabled": True,
        "version": 7,
    }
    db.query_templates[0] = dict(prior)
    client = _client(db, monkeypatch)

    response = client.put(
        "/api/datasets/query-templates/qt_assigned",
        json={
            "dataset_id": "customer_orders",
            "display_name": "Broken replacement",
            "query_definition_jsonb": {"statement": "SELECT order_id FROM orders LIMIT 1"},
            "params_schema_jsonb": {"type": "object", "properties": {}},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["path"] == "query_definition_jsonb.statement"
    assert db.saved_query_writes == []
    assert db.get_dataset_query_template("qt_assigned") == prior


def test_saved_query_list_inherits_scope_from_its_dataset(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["customer_orders"] = {
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "availability_mode": "all_workspace_projects",
        "display_name": "Customer Orders",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": [],
    }
    client = _client(db, monkeypatch)

    response = client.get("/api/datasets/query-templates?dataset_id=customer_orders")

    assert response.status_code == 200
    assert [row["query_template_id"] for row in response.json()["query_templates"]] == [
        "qt_assigned"
    ]


def test_saved_query_catalog_filters_canonical_dataset_after_projection_merge(monkeypatch) -> None:
    from core.application.extensions.projected_dataset_bridge import projection_bridge_metadata
    from core.application.registry import projected_artifact_rows

    db = FakeDatasetDB()
    db.projects["4"]["workspace_id"] = "ws-1"
    db.rows = {
        "extension.install-a.orders": {
            "dataset_id": "extension.install-a.orders",
            "workspace_id": "ws-1",
            "availability_mode": "specific_projects_only",
            "project_ids": ["3"],
        },
        "extension.install-b.orders": {
            "dataset_id": "extension.install-b.orders",
            "workspace_id": "ws-1",
            "availability_mode": "specific_projects_only",
            "project_ids": ["4"],
        },
        "native_c": {
            "dataset_id": "native_c",
            "workspace_id": "ws-1",
            "availability_mode": "all_workspace_projects",
            "project_ids": [],
        },
        "foreign_dataset": {
            "dataset_id": "foreign_dataset",
            "workspace_id": "ws-2",
            "availability_mode": "all_workspace_projects",
            "project_ids": [],
        },
    }
    # Assignments/provenance belong to durable host rows, never package snapshots.
    for install_id in ("install-a", "install-b"):
        durable = db.rows[f"extension.{install_id}.orders"]
        durable["account_id"] = "account-1"
        # This fake returns normalized read rows; production derives this from assignments.
        durable["workspace_resolution_status"] = "derived_from_projects"
        durable["metadata_jsonb"] = {
            "connection_resolution": "project_scope",
            "projection_bridge": projection_bridge_metadata(
                install_id=install_id,
                release_id="release-1",
                artifact_kind="dataset",
                artifact_id="orders",
            ),
        }
    db.query_templates = [
        {"query_template_id": "a_host", "dataset_id": "extension.install-a.orders"},
        {"query_template_id": "b_host", "dataset_id": "extension.install-b.orders"},
        {"query_template_id": "c_host", "dataset_id": "native_c"},
        {
            "query_template_id": "foreign_account",
            "dataset_id": "extension.install-a.orders",
            "account_id": "account-2",
        },
        {
            "query_template_id": "foreign_workspace",
            "dataset_id": "extension.install-a.orders",
            "account_id": "account-1",
            "workspace_id": "ws-2",
        },
        {"query_template_id": "foreign_dataset_query", "dataset_id": "foreign_dataset"},
        {
            "query_template_id": "inactive_host",
            "dataset_id": "extension.install-a.orders",
            "source_install_id": "inactive-install",
        },
    ]

    def projection(
        install_id, artifact_kind, artifact_id, snapshot, *, active=True, account_id="account-1"
    ):
        return {
            "account_id": account_id,
            "install_id": install_id,
            "release_id": "release-1",
            "artifact_kind": artifact_kind,
            "artifact_id": artifact_id,
            "projection": snapshot,
            "is_active": active,
        }

    projection_rows = [
        projection(
            "install-a",
            "dataset",
            "orders",
            {
                "dataset_id": "orders",
                "tenant_id": "tenant-1",
                "metadata_jsonb": {"connection_resolution": "project_scope"},
            },
        ),
        projection(
            "install-a",
            "saved_query",
            "open_orders",
            {"query_template_id": "open_orders", "dataset_id": "orders"},
        ),
        projection(
            "install-b",
            "dataset",
            "orders",
            {
                "dataset_id": "orders",
                "tenant_id": "tenant-1",
                "metadata_jsonb": {"connection_resolution": "project_scope"},
            },
        ),
        projection(
            "install-b",
            "saved_query",
            "open_orders",
            {"query_template_id": "open_orders", "dataset_id": "orders"},
        ),
        projection(
            "inactive-install",
            "saved_query",
            "hidden",
            {"query_template_id": "hidden", "dataset_id": "extension.install-a.orders"},
            active=False,
        ),
        projection(
            "foreign-install",
            "saved_query",
            "hidden",
            {"query_template_id": "hidden", "dataset_id": "extension.install-a.orders"},
            account_id="account-2",
        ),
    ]

    class ProjectionStore:
        def list_projections(self, *, account_id, only_active):
            assert account_id == "account-1"
            assert only_active is True
            return [
                row
                for row in projection_rows
                if row["account_id"] == account_id and row["is_active"]
            ]

    db.extension_runtime_store = ProjectionStore()
    monkeypatch.setattr(
        projected_artifact_rows, "active_extension_ids_for_account", lambda *_a, **_k: set()
    )
    monkeypatch.setattr(
        projected_artifact_rows, "active_extension_versions_for_account", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        projected_artifact_rows,
        "resolve_projection_dependency_availability",
        lambda *_a, **_k: {"new_runs_blocked": False},
    )
    client = _client(
        db,
        monkeypatch,
        scope_context=normalize_scope(
            account_id="account-1", tenant_id="tenant-1", allowed_workspace_ids=frozenset({"ws-1"})
        ),
    )

    def query(params, expected, *, scoped_client=client):
        response = scoped_client.get("/api/datasets/query-templates", params=params)
        assert response.status_code == 200
        payload = response.json()
        assert {row["query_template_id"] for row in payload["query_templates"]} == expected
        assert payload["count"] == len(expected)

    a = {"a_host", "extension.install-a.open_orders"}
    b = {"b_host", "extension.install-b.open_orders"}
    c = {"c_host"}
    query({"dataset_id": "extension.install-a.orders"}, a)
    query({"dataset_id": "extension.install-b.orders"}, b)
    query({"dataset_id": "native_c"}, c)
    query({"dataset_id": "missing"}, set())
    query({}, a | b | c)
    account_wide = _client(db, monkeypatch)
    query(
        {},
        a | b | c | {"foreign_workspace", "foreign_dataset_query"},
        scoped_client=account_wide,
    )
    query({"dataset_id": "   "}, a | b | c)
    query({"dataset_id": " extension.install-a.orders "}, a)
    query({"project_id": "3"}, a | c)
    query({"dataset_id": "extension.install-b.orders", "project_id": "3"}, set())
    query({"dataset_id": "EXTENSION.install-a.orders"}, set())

    restricted = _client(
        db,
        monkeypatch,
        scope_context=normalize_scope(
            account_id="account-1",
            tenant_id="tenant-1",
            allowed_workspace_ids=frozenset({"ws-1"}),
            allowed_project_ids=frozenset({"3"}),
        ),
    )
    query({"dataset_id": "extension.install-b.orders"}, set(), scoped_client=restricted)
    query({}, a | c, scoped_client=restricted)
    workspace_restricted = _client(
        db,
        monkeypatch,
        scope_context=normalize_scope(
            account_id="account-1", tenant_id="tenant-1", allowed_workspace_ids=frozenset({"ws-1"})
        ),
    )
    query({}, a | b | c, scoped_client=workspace_restricted)


def test_inactive_projected_saved_query_is_hidden_and_not_executable(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["customer_orders"] = {
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "availability_mode": "all_workspace_projects",
        "enabled": True,
        "project_ids": [],
    }
    db.query_templates[0]["validation_result_jsonb"] = {
        "projection_bridge": {
            "install_id": "install-disabled",
            "artifact_kind": "saved_query",
        }
    }
    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.projected_artifact_rows",
        lambda *_args, **_kwargs: [],
    )
    invoked = {"called": False}

    def _invoke(*_args, **_kwargs):
        invoked["called"] = True
        return {"ok": True}

    monkeypatch.setattr(datasets_routes, "_invoke_saved_query_action", _invoke)
    client = _client(db, monkeypatch)

    listing = client.get("/api/datasets/query-templates?dataset_id=customer_orders")
    preview = client.post(
        "/api/datasets/query-templates/qt_assigned/preview",
        json={"params": {}},
    )

    assert listing.status_code == 200
    assert listing.json()["query_templates"] == []
    assert preview.status_code == 404
    assert preview.json()["detail"] == "Saved query not found"
    assert invoked["called"] is False


def test_project_dataset_catalog_is_workspace_scoped_and_honors_workspace_wide_mode(
    monkeypatch,
) -> None:
    db = FakeDatasetDB()
    db.rows["workspace_wide"] = {
        "dataset_id": "workspace_wide",
        "workspace_id": "ws-1",
        "availability_mode": "all_workspace_projects",
        "display_name": "Workspace Wide",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": [],
    }
    db.rows["explicit_only"] = {
        "dataset_id": "explicit_only",
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "display_name": "Explicit Only",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": ["3"],
    }
    db.rows["other_workspace"] = {
        "dataset_id": "other_workspace",
        "workspace_id": "ws-2",
        "availability_mode": "all_workspace_projects",
        "display_name": "Other Workspace",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": [],
    }
    client = _client(db, monkeypatch)

    response = client.get("/api/datasets/project/3")

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace_id"] == "ws-1"
    assert payload["dataset_ids"] == ["explicit_only", "workspace_wide"]
    assert payload["explicit_dataset_ids"] == ["explicit_only"]
    assert sorted(row["dataset_id"] for row in payload["available_datasets"]) == [
        "explicit_only",
        "workspace_wide",
    ]


def test_cross_workspace_dataset_assignment_is_rejected(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["other_workspace"] = {
        "dataset_id": "other_workspace",
        "workspace_id": "ws-2",
        "availability_mode": "specific_projects_only",
        "display_name": "Other Workspace",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": [],
    }
    client = _client(db, monkeypatch)

    response = client.put("/api/datasets/project/3", json={"dataset_ids": ["other_workspace"]})

    assert response.status_code == 400
    assert "different workspace" in response.json()["detail"]


def test_project_dataset_assignment_rejects_out_of_scope_project(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.projects["4"]["workspace_id"] = "ws-1"
    db.projects["4"]["id"] = "4"
    db.rows["customer_orders"] = {
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "display_name": "Customer Orders",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": [],
    }
    client = _client(
        db,
        monkeypatch,
        scope_context=normalize_scope(
            account_id="account-1",
            allowed_project_ids=frozenset({"3"}),
        ),
    )

    response = client.put("/api/datasets/project/4", json={"dataset_ids": ["customer_orders"]})

    assert response.status_code == 403
    assert db.rows["customer_orders"]["project_ids"] == []


def test_dataset_rows_without_owner_are_hidden_for_account_scope(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["foreign_dataset"] = {
        "dataset_id": "foreign_dataset",
        "workspace_id": "",
        "availability_mode": "specific_projects_only",
        "display_name": "Foreign Dataset",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": [],
    }
    client = _client(db, monkeypatch)

    response = client.get("/api/datasets")

    assert response.status_code == 200
    assert response.json()["datasets"] == []


def test_dataset_connector_catalog_filters_missing_install_owner(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.connectors[0].pop("account_id", None)
    client = _client(db, monkeypatch)

    response = client.get("/api/datasets/connectors")

    assert response.status_code == 200
    assert response.json()["connectors"] == []


def test_dataset_side_assignment_rejects_out_of_scope_target_project(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.projects["4"]["workspace_id"] = "ws-1"
    db.projects["4"]["id"] = "4"
    db.rows["customer_orders"] = {
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "display_name": "Customer Orders",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": [],
    }
    client = _client(
        db,
        monkeypatch,
        scope_context=normalize_scope(
            account_id="account-1",
            allowed_project_ids=frozenset({"3"}),
        ),
    )

    response = client.put(
        "/api/datasets/customer_orders/projects",
        json={"project_ids": ["4"]},
    )

    assert response.status_code == 403
    assert db.rows["customer_orders"]["project_ids"] == []


def test_dataset_side_assignment_rejects_cross_workspace_projects(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["customer_orders"] = {
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "display_name": "Customer Orders",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "metadata_jsonb": {"connection_id": "warehouse-primary"},
        "project_ids": ["3"],
    }
    client = _client(db, monkeypatch)

    response = client.put(
        "/api/datasets/customer_orders/projects",
        json={"project_ids": ["4"]},
    )

    assert response.status_code == 400
    assert "same workspace" in response.json()["detail"]
    assert db.rows["customer_orders"]["project_ids"] == ["3"]


def test_dataset_update_rejects_workspace_id_change(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["customer_orders"] = {
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "display_name": "Customer Orders",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": ["3"],
    }
    client = _client(db, monkeypatch)

    response = client.put(
        "/api/datasets/customer_orders",
        json={
            "display_name": "Customer Orders",
            "workspace_id": "ws-2",
            "availability_mode": "specific_projects_only",
            "connection_id": "warehouse-primary",
            "read_only": True,
            "enabled": True,
        },
    )

    assert response.status_code == 400
    assert "workspace_id cannot be changed" in response.json()["detail"]
    assert db.rows["customer_orders"]["workspace_id"] == "ws-1"


def test_dataset_patch_rejects_workspace_id_change(monkeypatch) -> None:
    db = FakeDatasetDB()
    db.rows["customer_orders"] = {
        "dataset_id": "customer_orders",
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "display_name": "Customer Orders",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": ["3"],
    }
    client = _client(db, monkeypatch)

    response = client.patch("/api/datasets/customer_orders", json={"workspace_id": "ws-2"})

    assert response.status_code == 400
    assert "workspace_id cannot be changed" in response.json()["detail"]
    assert db.rows["customer_orders"]["workspace_id"] == "ws-1"


def test_saved_query_preview_rejects_unassigned_dataset_for_project_restricted_admin(
    monkeypatch,
) -> None:
    """FS-DEEP-CAND-047: a project-restricted admin (allowed_project_ids={'3'})
    must not preview a saved query whose same-account dataset is not assigned to
    one of its projects."""
    db = FakeDatasetDB()
    db.rows["unassigned_dataset"] = {
        "dataset_id": "unassigned_dataset",
        "workspace_id": "ws-1",
        "tenant_id": "tenant-1",
        "account_id": "account-1",
        "availability_mode": "specific_projects_only",
        "display_name": "Unassigned Dataset",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": ["4"],  # assigned to project 4, NOT the restricted project 3
    }
    db.query_templates.append(
        {
            "query_template_id": "qt_unassigned_scope",
            "dataset_id": "unassigned_dataset",
            "account_id": "account-1",
            "tenant_id": "tenant-1",
            "display_name": "Unassigned scope query",
        }
    )
    client = _client(
        db,
        monkeypatch,
        scope_context=normalize_scope(
            account_id="account-1",
            tenant_id="tenant-1",
            allowed_project_ids=frozenset({"3"}),
        ),
    )

    invoked = {"called": False}

    def _invoke(_db, _template, _action_id, _body, **_kwargs):
        invoked["called"] = True
        return {"ok": True}

    monkeypatch.setattr(datasets_routes, "_invoke_saved_query_action", _invoke)

    response = client.post(
        "/api/datasets/query-templates/qt_unassigned_scope/preview",
        json={"params": {}, "row_limit": 10},
    )
    assert response.status_code == 404
    assert invoked["called"] is False


def test_saved_query_preview_allows_assigned_dataset_for_project_restricted_admin(
    monkeypatch,
) -> None:
    """Positive: same project-restricted admin can preview a saved query whose
    dataset IS assigned to one of its allowed projects."""
    db = FakeDatasetDB()
    db.rows["assigned_dataset"] = {
        "dataset_id": "assigned_dataset",
        "workspace_id": "ws-1",
        "tenant_id": "tenant-1",
        "account_id": "account-1",
        "availability_mode": "specific_projects_only",
        "display_name": "Assigned Dataset",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": ["3"],
    }
    db.query_templates.append(
        {
            "query_template_id": "qt_assigned_scope",
            "dataset_id": "assigned_dataset",
            "account_id": "account-1",
            "tenant_id": "tenant-1",
            "display_name": "Assigned scope query",
        }
    )
    client = _client(
        db,
        monkeypatch,
        scope_context=normalize_scope(
            account_id="account-1",
            tenant_id="tenant-1",
            allowed_project_ids=frozenset({"3"}),
        ),
    )

    invoked = {"called": False}

    def _invoke(_db, _template, _action_id, _body, **_kwargs):
        invoked["called"] = True
        return {"ok": True, "result": {}}

    monkeypatch.setattr(datasets_routes, "_invoke_saved_query_action", _invoke)

    response = client.post(
        "/api/datasets/query-templates/qt_assigned_scope/preview",
        json={"params": {}, "row_limit": 10},
    )
    assert response.status_code == 200
    assert invoked["called"] is True


def test_saved_query_preview_workspace_wide_dataset_allowed_for_restricted_admin(
    monkeypatch,
) -> None:
    """A workspace-wide dataset (all_workspace_projects) stays previewable even
    for a project-restricted admin, matching the runtime contract."""
    db = FakeDatasetDB()
    db.rows["ws_wide_dataset"] = {
        "dataset_id": "ws_wide_dataset",
        "workspace_id": "ws-1",
        "tenant_id": "tenant-1",
        "account_id": "account-1",
        "availability_mode": "all_workspace_projects",
        "display_name": "Workspace Wide",
        "connector_id": "extension.install_1.warehouse_sql",
        "connector_kind": "extension",
        "runtime_kind": "local_extension",
        "query_language": "sql",
        "enabled": True,
        "read_only": True,
        "project_ids": [],
    }
    db.query_templates.append(
        {
            "query_template_id": "qt_ws_wide",
            "dataset_id": "ws_wide_dataset",
            "account_id": "account-1",
            "tenant_id": "tenant-1",
            "display_name": "Workspace-wide query",
        }
    )
    client = _client(
        db,
        monkeypatch,
        scope_context=normalize_scope(
            account_id="account-1",
            tenant_id="tenant-1",
            allowed_project_ids=frozenset({"3"}),
        ),
    )

    monkeypatch.setattr(
        datasets_routes, "_invoke_saved_query_action", lambda *_a, **_k: {"ok": True, "result": {}}
    )

    response = client.post(
        "/api/datasets/query-templates/qt_ws_wide/preview",
        json={"params": {}, "row_limit": 10},
    )
    assert response.status_code == 200
