"""Explicit project selection routes projected saved-query actions to its connection."""

from __future__ import annotations

import copy

import pytest

from core.application.extensions.projected_dataset_bridge import (
    projection_bridge_id,
    projection_bridge_metadata,
)
from core.application.management.scope import normalize_scope
from core.application.registry.data_connection_binding import resolve_project_scoped_data_connection
from core.entrypoints.admin import datasets_helpers
from tests.unit.test_registry_datasets import FakeDatasetDB, _client


class _ProjectedDB(FakeDatasetDB):
    def __init__(self) -> None:
        super().__init__()
        self.install_id = "showcase-install"
        self.release_id = "showcase-release"
        self.dataset_id = projection_bridge_id(self.install_id, "orders")
        self.query_id = projection_bridge_id(self.install_id, "open_orders")
        self.projects["3"]["id"] = "3"
        self.projects["3"]["name"] = "Project Three"
        self.projects["4"]["id"] = "4"
        self.projection_overrides = {}
        self.selected_connection_ids = {"warehouse-primary"}
        self.extension_runtime_store = self
        self.rows[self.dataset_id] = {
            "dataset_id": self.dataset_id,
            "display_name": "Projected orders",
            "account_id": "account-1",
            "tenant_id": "tenant-1",
            "workspace_id": None,
            "availability_mode": "specific_projects_only",
            "project_ids": ["3"],
            "enabled": True,
            "read_only": True,
            "query_language": "sql",
            "connector_id": "warehouse_sql",
            "metadata_jsonb": {
                "connection_resolution": "project_scope",
                "projection_bridge": projection_bridge_metadata(
                    install_id=self.install_id,
                    release_id=self.release_id,
                    artifact_kind="dataset",
                    artifact_id="orders",
                ),
            },
            "resource_scope_jsonb": {
                "version": 1,
                "resources": [
                    {"resource_id": "orders", "columns": ["order_id", "status", "total_amount"]}
                ],
            },
        }
        self.query_templates = [
            {
                "query_template_id": self.query_id,
                "dataset_id": self.dataset_id,
                "account_id": "account-1",
                "tenant_id": "tenant-1",
                "display_name": "Projected open orders",
                "query_language": "sql",
                "query_definition_jsonb": {
                    "sql": "SELECT order_id, status, total_amount FROM orders WHERE status = 'open'"
                },
                "params_schema_jsonb": {"type": "object", "properties": {}},
                "enabled": True,
            }
        ]
        # Another compatible connection exists in the workspace, but this project
        # did not select it. Project-scoped preview must not guess or become ambiguous.
        self.connections["unselected"] = {
            **copy.deepcopy(self.connections["warehouse-primary"]),
            "connection_id": "unselected",
        }

    def list_projections(self, *, account_id: str, only_active: bool = True):
        if account_id != "account-1":
            return []
        dataset = {**copy.deepcopy(self.rows[self.dataset_id]), "dataset_id": "orders"}
        query = {
            **copy.deepcopy(self.query_templates[0]),
            "query_template_id": "open_orders",
            "dataset_id": "orders",
        }
        return [
            {
                "account_id": account_id,
                "install_id": self.install_id,
                "release_id": self.release_id,
                "artifact_kind": kind,
                "artifact_id": artifact,
                "is_active": True,
                "projection": snapshot,
            }
            for kind, artifact, snapshot in (
                ("dataset", "orders", dataset),
                ("saved_query", "open_orders", query),
            )
        ]

    def list_project_data_connections(self, project_id: str) -> list[dict]:
        if str(project_id) != "3":
            return []
        # The canonical store returns assignment summaries, not runtime config.
        return [{"connection_id": key, "project_id": "3"} for key in self.selected_connection_ids]

    def get_extension_registry_projection(self, **lookup):
        if (
            lookup.get("account_id") != "account-1"
            or lookup.get("install_id") != self.install_id
            or lookup.get("artifact_kind") != "dataset"
            or lookup.get("artifact_id") != "orders"
        ):
            return None
        return {
            "account_id": "account-1",
            "install_id": self.install_id,
            "release_id": self.release_id,
            "artifact_kind": "dataset",
            "artifact_id": "orders",
            "is_active": True,
            "availability_status": "available",
            "new_runs_blocked": False,
            **self.projection_overrides,
        }

    def list_dataset_project_catalog(self, dataset_id: str):
        row = self.rows[dataset_id]
        # The production catalog has no workspace projects for projected datasets.
        return {
            "dataset_id": dataset_id,
            "workspace_id": row.get("workspace_id") or "",
            "availability_mode": row["availability_mode"],
            "project_ids": list(row["project_ids"]),
            "explicit_project_ids": list(row["project_ids"]),
            "available_projects": [],
        }


_ACTIONS = [
    ("preview", "preview_query"),
    ("validate", "validate_query"),
    ("infer-output-schema", "infer_output_schema"),
]


def _capture_connector(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        datasets_helpers, "_connection_extension", lambda *_args: {"extension_id": "warehouse"}
    )

    def connector_input(**kwargs):
        captured["connection_id"] = kwargs["connection"]["connection_id"]
        captured["dataset_id"] = kwargs["dataset"]["dataset_id"]
        captured["connection_config"] = kwargs["connection"].get("connection_config_jsonb")
        return {"saved_query": kwargs["template"]["query_definition_jsonb"]}

    def invoke(_extension, **kwargs):
        captured.update(action=kwargs["action_id"], context=kwargs["context"])
        return {
            "status": "succeeded",
            "response": {"ok": True, "result": {"rows": [{"order_id": 101}], "row_count": 1}},
        }

    monkeypatch.setattr(
        "core.application.orchestration.dataset_query_step_runtime._connector_action_input",
        connector_input,
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action", invoke
    )
    monkeypatch.setattr(
        datasets_helpers,
        "_process_connection_project_state_updates",
        lambda _invocation, **kwargs: captured.update(state_project_id=kwargs["project_id"]),
    )
    return captured


@pytest.mark.parametrize(("endpoint", "action"), _ACTIONS)
def test_project_scoped_query_action_uses_explicit_project_selected_connection(
    monkeypatch, endpoint, action
):
    db = _ProjectedDB()
    client = _client(db, monkeypatch)
    captured = _capture_connector(monkeypatch)
    response = client.post(
        f"/api/datasets/query-templates/{db.query_id}/{endpoint}",
        json={"params": {}, "row_limit": 10, "project_id": "3"},
    )
    assert response.status_code == 200, response.json()
    assert response.json()["ok"] is True
    assert captured["connection_id"] == "warehouse-primary"
    assert captured["dataset_id"] == db.dataset_id
    assert captured["connection_config"] == {"prebuilt": "postgres"}
    assert captured["action"] == action
    assert captured["context"]["project_id"] == "3"
    assert captured["state_project_id"] == "3"


def test_saved_query_timeout_response_exposes_only_typed_safe_error(monkeypatch):
    db = _ProjectedDB()
    client = _client(db, monkeypatch)
    _capture_connector(monkeypatch)
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        lambda *_args, **_kwargs: {
            "status": "failed",
            "detail": "Saved query timed out after 30 seconds.",
            "command": ["PRIVATE_COMMAND_MARKER"],
            "timeout_seconds": 30,
            "response": {},
            "errors": [
                {
                    "code": "extension_timeout",
                    "message": "Saved query timed out after 30 seconds.",
                }
            ],
        },
    )

    response = client.post(
        f"/api/datasets/query-templates/{db.query_id}/preview",
        json={"params": {}, "row_limit": 10, "project_id": "3"},
    )

    assert response.status_code == 200, response.json()
    assert response.json()["ok"] is False
    assert response.json()["errors"] == [
        {
            "code": "extension_timeout",
            "message": "Saved query timed out after 30 seconds.",
        }
    ]
    assert "PRIVATE_COMMAND_MARKER" not in response.text


@pytest.mark.parametrize(("endpoint", "_action"), _ACTIONS)
@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("missing-project", 400, "project_required"),
        ("unknown-project", 404, "project_not_found"),
        ("unassigned-project", 403, "dataset_project_unassigned"),
        ("restricted-project", 403, "project_forbidden"),
        ("restricted-workspace", 403, "project_forbidden"),
        ("foreign-account", 403, "project_forbidden"),
        ("foreign-tenant", 403, "project_forbidden"),
        ("inactive-projection", 409, "connection_rebind_required"),
        ("blocked-projection", 409, "connection_rebind_required"),
        ("stale-release", 409, "connection_rebind_required"),
        ("mutable-bridge", 409, "connection_rebind_required"),
        ("no-selected-connection", 409, "connection_not_found"),
        ("ambiguous-selected-connections", 409, "connection_ambiguous"),
        ("foreign-connection", 409, "connection_not_found"),
        ("other-workspace-connection", 409, "connection_not_found"),
        ("wrong-connector", 409, "connection_not_found"),
        ("disabled-connection", 409, "connection_not_found"),
    ],
)
def test_project_query_refuses_invalid_context_before_connector_execution(
    monkeypatch, endpoint, _action, case, status, code
):
    db = _ProjectedDB()
    project_id = "3"
    scope = normalize_scope(account_id="account-1", tenant_id="tenant-1")
    if case == "missing-project":
        project_id = None
    elif case == "unknown-project":
        project_id = "missing"
    elif case == "unassigned-project":
        project_id = "4"
    elif case == "restricted-project":
        db.rows[db.dataset_id]["project_ids"] = ["3", "4"]
        scope = scope.model_copy(update={"allowed_project_ids": frozenset({"4"})})
    elif case == "restricted-workspace":
        scope = scope.model_copy(update={"allowed_workspace_ids": frozenset({"ws-2"})})
    elif case == "foreign-account":
        db.projects["3"]["account_id"] = "account-2"
    elif case == "foreign-tenant":
        db.projects["3"]["tenant_id"] = "tenant-2"
    elif case == "inactive-projection":
        db.projection_overrides["is_active"] = False
    elif case == "blocked-projection":
        db.projection_overrides["new_runs_blocked"] = True
    elif case == "stale-release":
        db.projection_overrides["release_id"] = "other-release"
    elif case == "mutable-bridge":
        db.rows[db.dataset_id]["metadata_jsonb"]["projection_bridge"]["immutable"] = False
    elif case == "no-selected-connection":
        db.selected_connection_ids = set()
    elif case == "ambiguous-selected-connections":
        db.selected_connection_ids.add("unselected")
    elif case == "foreign-connection":
        db.connections["warehouse-primary"]["account_id"] = "account-2"
    elif case == "other-workspace-connection":
        db.connections["warehouse-primary"]["workspace_id"] = "ws-2"
    elif case == "wrong-connector":
        db.connections["warehouse-primary"]["connector_ref_jsonb"] = {"connector_id": "other"}
    elif case == "disabled-connection":
        db.connections["warehouse-primary"]["enabled"] = False
    client = _client(db, monkeypatch, scope_context=scope)
    captured = _capture_connector(monkeypatch)
    response = client.post(
        f"/api/datasets/query-templates/{db.query_id}/{endpoint}",
        json={"params": {}, "project_id": project_id},
    )
    assert response.status_code == status, response.json()
    assert response.json()["detail"]["code"] == code
    assert captured == {}, "No connector input or query may run before authorization"


@pytest.mark.parametrize(("endpoint", "action"), _ACTIONS)
def test_fixed_connection_saved_query_keeps_existing_preview_without_project(
    monkeypatch, endpoint, action
):
    db = _ProjectedDB()
    db.rows[db.dataset_id].update(
        workspace_id="ws-1", metadata_jsonb={"connection_id": "warehouse-primary"}
    )
    client = _client(db, monkeypatch)
    captured = _capture_connector(monkeypatch)
    response = client.post(
        f"/api/datasets/query-templates/{db.query_id}/{endpoint}", json={"params": {}}
    )
    assert response.status_code == 200, response.json()
    assert response.json()["ok"] is True
    assert captured["connection_id"] == "warehouse-primary"
    assert captured["action"] == action


def test_explicit_empty_connection_candidates_never_fall_back_to_workspace():
    db = _ProjectedDB()
    scope = {"account_id": "account-1", "tenant_id": "tenant-1", "workspace_id": "ws-1"}
    # None preserves existing runtime resolution, including workspace ambiguity.
    assert (
        resolve_project_scoped_data_connection(db, db.rows[db.dataset_id], scope)[1]
        == "connection_ambiguous"
    )
    assert (
        resolve_project_scoped_data_connection(
            db, db.rows[db.dataset_id], scope, connection_candidates=[]
        )[1]
        == "connection_not_found"
    )


def test_project_scope_catalog_exposes_only_assigned_authorized_projects(monkeypatch):
    db = _ProjectedDB()
    db.rows[db.dataset_id]["project_ids"] = ["3", "4", "foreign", "missing"]
    db.projects["foreign"] = {
        "id": "foreign",
        "name": "Private foreign name",
        "account_id": "account-2",
        "workspace_id": "ws-2",
    }
    scope = normalize_scope(
        account_id="account-1", tenant_id="tenant-1", allowed_project_ids=frozenset({"3"})
    )
    response = _client(db, monkeypatch, scope_context=scope).get(
        f"/api/datasets/{db.dataset_id}/projects"
    )
    assert response.status_code == 200, response.json()
    catalog = response.json()
    assert catalog["project_ids"] == ["3"]
    assert catalog["explicit_project_ids"] == ["3"]
    assert [(p["id"], p["name"]) for p in catalog["available_projects"]] == [("3", "Project Three")]
    assert "Private foreign name" not in response.text


def test_project_scope_catalog_includes_assigned_cross_workspace_project(monkeypatch):
    db = _ProjectedDB()
    db.rows[db.dataset_id]["project_ids"] = ["3", "4"]
    response = _client(db, monkeypatch).get(f"/api/datasets/{db.dataset_id}/projects")
    assert response.status_code == 200, response.json()
    assert {p["id"] for p in response.json()["available_projects"]} == {"3", "4"}


def test_project_scoped_helper_refuses_missing_authenticated_scope(monkeypatch):
    from fastapi import HTTPException

    from core.entrypoints.admin.datasets_models import DatasetQueryTemplateAction

    db = _ProjectedDB()
    captured = _capture_connector(monkeypatch)
    action = DatasetQueryTemplateAction(project_id="3")
    with pytest.raises(HTTPException) as raised:
        datasets_helpers._invoke_saved_query_action(
            db, db.query_templates[0], "preview_query", action
        )
    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "project_forbidden"
    assert captured == {}


def test_fixed_workspace_project_catalog_keeps_existing_candidates(monkeypatch):
    db = _ProjectedDB()
    db.rows[db.dataset_id].update(
        workspace_id="ws-1", metadata_jsonb={"connection_id": "warehouse-primary"}
    )
    original_catalog = {
        "dataset_id": db.dataset_id,
        "workspace_id": "ws-1",
        "availability_mode": "specific_projects_only",
        "project_ids": ["3"],
        "explicit_project_ids": ["3"],
        "available_projects": [db.projects["3"], db.projects["4"]],
    }
    db.list_dataset_project_catalog = lambda _dataset_id: original_catalog
    response = _client(db, monkeypatch).get(f"/api/datasets/{db.dataset_id}/projects")
    assert response.status_code == 200, response.json()
    assert response.json() == original_catalog


@pytest.mark.parametrize(("endpoint", "action"), _ACTIONS)
def test_registry_store_uses_real_project_port_summaries(monkeypatch, endpoint, action):
    from contextlib import contextmanager

    from core.infrastructure.db.logger_lifecycle_projects_mixin import (
        PlatformLoggerLifecycleProjectsMixin,
    )
    from core.infrastructure.db.logger_policy_projects_mixin import (
        PlatformLoggerPolicyProjectsMixin,
    )

    db = _ProjectedDB()
    project_id = "00000000-0000-0000-0000-000000000003"
    db.rows[db.dataset_id]["project_ids"] = [project_id]
    statements = []

    class Cursor:
        def execute(self, statement, params):
            statements.append((statement, params))

        def fetchone(self):
            return {**db.projects["3"], "id": project_id}

        def fetchall(self):
            return [db.connections["warehouse-primary"]]

    class Connection:
        @contextmanager
        def cursor(self, **_kwargs):
            yield Cursor()

    class ProjectPort(PlatformLoggerLifecycleProjectsMixin, PlatformLoggerPolicyProjectsMixin):
        conn = Connection()

        def _column_exists(self, *_args):
            return False

        def _table_exists(self, *_args):
            return True

    class RegistryPort:
        # Match the real RegistryDBStore: it does not own project operations.
        get_project = None
        list_project_data_connections = None
        dsn = "postgresql://contract.invalid/unused"

        def __getattr__(self, name):
            return getattr(db, name)

    project_store_lookups = []

    def project_store(dsn):
        project_store_lookups.append(dsn)
        return ProjectPort()

    monkeypatch.setattr(
        "core.infrastructure.db.platform_stores.PlatformProjectStore.get_shared_instance",
        project_store,
    )
    captured = _capture_connector(monkeypatch)
    client = _client(RegistryPort(), monkeypatch)
    response = client.post(
        f"/api/datasets/query-templates/{db.query_id}/{endpoint}",
        json={"params": {}, "project_id": project_id},
    )
    assert response.status_code == 200, response.json()
    assert response.json()["ok"] is True
    assert project_store_lookups == [RegistryPort.dsn]
    assert any("FROM projects" in query and params == (project_id,) for query, params in statements)
    assert any(
        "FROM project_data_connections" in query and params == (project_id,)
        for query, params in statements
    )
    assert captured["connection_config"] == {"prebuilt": "postgres"}
    assert captured["context"]["project_id"] == project_id
    assert captured["state_project_id"] == project_id
    assert captured["action"] == action


class _RawPackageDB(_ProjectedDB):
    """Package snapshots never inherit host bridge or assignment metadata."""

    def __init__(self):
        from core.application.extensions.projected_dataset_bridge import (
            materialize_projected_dataset_bridges,
        )

        super().__init__()
        self.package_projections = [
            {
                "account_id": "account-1",
                "install_id": self.install_id,
                "release_id": self.release_id,
                "artifact_kind": "dataset",
                "artifact_id": "orders",
                "projection": {
                    "dataset_id": "orders",
                    "display_name": "Package orders",
                    "read_only": True,
                    "enabled": True,
                    "query_language": "sql",
                    "connector_id": "warehouse_sql",
                    "metadata_jsonb": {"connection_resolution": "project_scope"},
                    "resource_scope_jsonb": {
                        "resources": [
                            {
                                "resource_id": "orders",
                                "columns": ["order_id", "status", "total_amount"],
                            }
                        ]
                    },
                },
            },
            {
                "account_id": "account-1",
                "install_id": self.install_id,
                "release_id": self.release_id,
                "artifact_kind": "saved_query",
                "artifact_id": "open_orders",
                "projection": {
                    "query_template_id": "open_orders",
                    "dataset_id": "orders",
                    "display_name": "Package query",
                    "enabled": True,
                    "query_language": "sql",
                    "query_definition_jsonb": {
                        "sql": "SELECT order_id, status, total_amount FROM orders WHERE status = 'open'"
                    },
                },
            },
        ]
        self.rows.pop(self.dataset_id)
        self.query_templates = []
        materialize_projected_dataset_bridges(
            self,
            account_id="account-1",
            install_id=self.install_id,
            release_id=self.release_id,
            projections=self.package_projections,
        )
        self.rows[self.dataset_id]["project_ids"] = ["3"]
        self.rows[self.dataset_id]["display_name"] = (
            "Durable stale content must not replace package content"
        )

    def get_dataset_query_template(self, identifier, include_trashed=False):
        return super().get_dataset_query_template(identifier)

    def list_projections(self, *, account_id, only_active=True):
        assert only_active is True
        return copy.deepcopy(self.package_projections) if account_id == "account-1" else []


def test_raw_package_response_preserves_materialized_host_assignment_and_provenance(monkeypatch):
    db = _RawPackageDB()
    client = _client(db, monkeypatch)
    response = client.get(f"/api/datasets/{db.dataset_id}")
    assert response.status_code == 200, response.json()
    row = response.json()
    assert row.get("project_ids") == ["3"]
    assert (
        row["metadata_jsonb"]["projection_bridge"]
        == db.rows[db.dataset_id]["metadata_jsonb"]["projection_bridge"]
    )
    assert row["display_name"] == "Package orders"
    assert row["workspace_id"] is None
    assert db.rows[db.dataset_id]["workspace_id"] is None
    catalog = client.get(f"/api/datasets/{db.dataset_id}/projects")
    assert catalog.status_code == 200, catalog.json()
    assert catalog.json()["project_ids"] == ["3"]
    assert [p["id"] for p in catalog.json()["available_projects"]] == ["3"]


def test_raw_package_selected_project_preview_uses_materialized_bridge(monkeypatch):
    db = _RawPackageDB()
    captured = _capture_connector(monkeypatch)
    response = _client(db, monkeypatch).post(
        f"/api/datasets/query-templates/{db.query_id}/preview",
        json={"params": {}, "project_id": "3"},
    )
    assert response.status_code == 200, response.json()
    assert captured["connection_id"] == "warehouse-primary"
    assert captured["context"]["project_id"] == "3"
    assert db.rows[db.dataset_id]["workspace_id"] is None


def test_raw_package_project_catalog_retains_authorized_assignment_choices(monkeypatch):
    db = _RawPackageDB()
    response = _client(db, monkeypatch).get(f"/api/datasets/{db.dataset_id}/projects")
    assert response.status_code == 200, response.json()
    assert response.json()["project_ids"] == ["3"]
    assert [p["id"] for p in response.json()["available_projects"]] == ["3"]


def test_projected_ordinary_mutation_routes_return_stable_refusal_without_writes(monkeypatch):
    from contextlib import contextmanager

    from core.application.registry.db_store_dataset_catalog_mixin import (
        RegistryDBStoreDatasetCatalogMixin,
    )
    from tests.unit.test_projected_dataset_bridge import _MutationCursor

    class Store(_RawPackageDB):
        toggle_dataset = RegistryDBStoreDatasetCatalogMixin.toggle_dataset
        delete_dataset = RegistryDBStoreDatasetCatalogMixin.delete_dataset
        toggle_dataset_query_template = (
            RegistryDBStoreDatasetCatalogMixin.toggle_dataset_query_template
        )
        delete_dataset_query_template = (
            RegistryDBStoreDatasetCatalogMixin.delete_dataset_query_template
        )

        def _table_exists(self, _name):
            return True

        def _column_exists(self, *_args):
            return True

        @contextmanager
        def _cursor(self):
            yield self.write_cursor

        _transaction = _cursor

        def _log_change_cur(self, *_args):
            return None

    db = Store()
    db.write_cursor = _MutationCursor()
    client = _client(db, monkeypatch)
    responses = [
        client.patch(f"/api/datasets/{db.dataset_id}/toggle"),
        client.delete(f"/api/datasets/{db.dataset_id}"),
        client.patch(f"/api/datasets/query-templates/{db.query_id}/toggle"),
        client.delete(f"/api/datasets/query-templates/{db.query_id}"),
    ]
    assert [(response.status_code, response.json()) for response in responses] == [
        (400, {"detail": "Install-owned projected dataset/query is immutable"})
    ] * 4
    assert db.write_cursor.statements == []


def test_raw_package_selected_project_preview_preserves_restricted_project_authorization(
    monkeypatch,
):
    db = _RawPackageDB()
    captured = _capture_connector(monkeypatch)
    scope = normalize_scope(
        account_id="account-1", tenant_id="tenant-1", allowed_project_ids=frozenset({"3"})
    )
    response = _client(db, monkeypatch, scope_context=scope).post(
        f"/api/datasets/query-templates/{db.query_id}/preview",
        json={"params": {}, "project_id": "3"},
    )
    assert response.status_code == 200, response.json()
    assert captured["connection_id"] == "warehouse-primary"
    assert captured["context"]["project_id"] == "3"
    assert db.rows[db.dataset_id]["workspace_id"] is None
