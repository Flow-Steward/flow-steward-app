from __future__ import annotations

import importlib

import pytest

from core.application.auth.secrets import encrypt_secret


class _DB:
    def __init__(self) -> None:
        self.jobs = {
            "job-1": {
                "id": "job-1",
                "project_id": "project-1",
                "account_id": "account-1",
                "tenant_id": "tenant-1",
                "workspace_id": "workspace-1",
            }
        }
        self.projects = {
            "project-1": {
                "id": "project-1",
                "account_id": "account-1",
                "tenant_id": "tenant-1",
                "workspace_id": "workspace-1",
            }
        }
        self.templates = {
            "query-1": {
                "query_template_id": "query-1",
                "dataset_id": "dataset-1",
                "display_name": "Users",
                "query_language": "sql",
                "query_definition_jsonb": {"sql": "SELECT id FROM users LIMIT 10"},
            }
        }
        self.datasets = {
            "dataset-1": {
                "dataset_id": "dataset-1",
                "connection_id": "connection-1",
                "project_ids": ["project-1"],
                "resource_scope_jsonb": {
                    "version": 1,
                    "resources": [{"resource_id": "public.users", "columns": ["id"]}],
                },
            }
        }
        self.connections = {
            "connection-1": {
                "connection_id": "connection-1",
                "account_id": "account-1",
                "tenant_id": "tenant-1",
                "workspace_id": "workspace-1",
                "connection_config_jsonb": {
                    "prebuilt": "postgres",
                    "host": "localhost",
                    "database": "flow_steward",
                },
                "secret_refs_jsonb": {"refs": {"password": "secret-ref"}},
                "connector_ref_jsonb": {
                    "extension_id": "fixture.dataset-query-provider",
                    "install_id": "install-dataset-query-provider",
                    "local_connector_id": "toolbox_postgres",
                },
            }
        }

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    def get_project(self, project_id: str):
        return self.projects.get(project_id)

    def get_dataset_query_template(self, query_template_id: str):
        return self.templates.get(query_template_id)

    def get_dataset(self, dataset_id: str):
        return self.datasets.get(dataset_id)

    def get_workspace_data_connection(self, connection_id: str, **_kwargs):
        return self.connections.get(connection_id)

    def list_workspace_data_connections(self, *, workspace_id: str | None = None):
        return [
            row
            for row in self.connections.values()
            if not workspace_id or row.get("workspace_id") == workspace_id
        ]

    def cursor(self):
        return None


class _Registry:
    def __init__(self) -> None:
        self.db_store = _DB()


def _patch_runtime(monkeypatch, *, response: dict | None = None, extension: dict | None = None):
    captured = {}
    registry = _Registry()
    captured["registry"] = registry

    def fake_run_extension_action(*_args, **kwargs):
        captured["kwargs"] = dict(kwargs)
        return {
            "status": "succeeded",
            "response": response
            or {
                "ok": True,
                "result": {"rows": [{"id": 1}, {"id": 2}], "row_count": 2},
                "errors": [],
            },
        }

    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: registry,
    )
    monkeypatch.setattr(
        "core.application.orchestration.dataset_query_step_runtime._extension_for_connection",
        lambda *_args, **_kwargs: (
            extension or {"extension_key": "fixture.dataset-query-provider"},
            None,
        ),
    )
    extension_runtime = importlib.import_module(
        "core.infrastructure.extension_runtime.extension_runtime"
    )
    monkeypatch.setattr(extension_runtime, "run_extension_action", fake_run_extension_action)
    return captured


def test_dataset_query_resolves_required_parameter_from_workflow_input(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    captured = _patch_runtime(monkeypatch)
    captured["registry"].db_store.templates["query-1"]["params_schema_jsonb"] = {
        "type": "object",
        "properties": {"project_name": {"type": "string"}},
        "required": ["project_name"],
    }

    result = execute_dataset_query_step(
        step={
            "override": {
                "dataset_query": {
                    "query_template_id": "query-1",
                    "params": {"project_name": "state.inputs.project_name"},
                }
            }
        },
        state={"job_id": "job-1", "inputs": {"project_name": "Flow Steward"}},
    )

    assert result["ok"] is True
    assert captured["kwargs"]["input_payload"]["params"] == {"project_name": "Flow Steward"}


@pytest.mark.parametrize(
    "selector",
    ["state.rows[abc].sku", "state.rows[0].sku | default('x')"],
)
def test_dataset_query_rejects_malformed_param_selector_before_registry_or_dispatch(
    monkeypatch,
    selector,
):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    monkeypatch.setattr(
        "core.application.orchestration.dataset_query_step_runtime._registry_store",
        lambda: (_ for _ in ()).throw(AssertionError("registry lookup must not run")),
    )

    result = execute_dataset_query_step(
        step={
            "step_kind": "dataset_query",
            "override": {
                "dataset_query": {
                    "query_template_id": "query-1",
                    "params": {"sku": selector},
                }
            },
        },
        state={"rows": [{"sku": "A1"}]},
    )

    assert result == {
        "ok": False,
        "runtime": "dataset_query",
        "detail": "invalid_input_selector",
        "result": None,
        "errors": [
            {
                "code": "invalid_input_selector",
                "message": "dataset_query has an invalid input selector",
                "selector": selector,
                "path": "override.dataset_query.params.sku",
            }
        ],
        "state_patch": {},
    }


def test_dataset_query_uses_canonical_indexed_state_selector(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    captured = _patch_runtime(monkeypatch)
    captured["registry"].db_store.templates["query-1"]["params_schema_jsonb"] = {
        "type": "object",
        "properties": {"sku": {"type": "string"}},
        "required": ["sku"],
    }

    result = execute_dataset_query_step(
        step={
            "step_kind": "dataset_query",
            "override": {
                "dataset_query": {
                    "query_template_id": "query-1",
                    "params": {"sku": "state.rows[0].sku"},
                }
            },
        },
        state={"job_id": "job-1", "rows": [{"sku": "A1"}]},
    )

    assert result["ok"] is True
    assert captured["kwargs"]["input_payload"]["params"] == {"sku": "A1"}


def test_dataset_query_runtime_rejects_a_persisted_legacy_statement(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    captured = _patch_runtime(monkeypatch)
    template = captured["registry"].db_store.templates["query-1"]
    template["query_definition_jsonb"] = {"statement": "SELECT id FROM users LIMIT 10"}

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"query_template_id": "query-1"}}},
        state={"job_id": "job-1"},
    )

    assert result["ok"] is False
    assert result["errors"] == [
        {
            "code": "saved_query_definition_invalid",
            "message": "Unsupported query definition field 'statement'; use 'sql'",
            "detail": "query_definition_jsonb.statement",
        }
    ]
    assert "kwargs" not in captured


def test_dataset_query_rejects_unresolved_required_parameter_before_connector(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    captured = _patch_runtime(monkeypatch)
    captured["registry"].db_store.templates["query-1"]["params_schema_jsonb"] = {
        "type": "object",
        "properties": {"project_name": {"type": "string"}},
        "required": ["project_name"],
    }

    result = execute_dataset_query_step(
        step={
            "override": {
                "dataset_query": {
                    "query_template_id": "query-1",
                    "params": {"project_name": "state.inputs.project_name"},
                }
            }
        },
        state={"job_id": "job-1", "inputs": {}},
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "query_parameter_mapping_invalid"
    assert "project_name" in result["errors"][0]["message"]
    assert "kwargs" not in captured


def test_dataset_query_runtime_prefers_connection_install_id(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import _extension_for_connection

    candidates: list[str] = []

    def resolve(candidate: str, **_kwargs):
        candidates.append(candidate)
        return ({"extension_key": candidate}, None)

    monkeypatch.setattr(
        "core.application.orchestration.dataset_query_step_runtime.resolve_extension_runtime_record_by_install_id",
        resolve,
    )

    extension, _logger = _extension_for_connection(
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


def test_dataset_query_runtime_rejects_missing_pinned_install_without_fallback(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import _extension_for_connection

    candidates: list[str] = []

    def resolve(candidate: str, **_kwargs):
        candidates.append(candidate)
        return (None, None)

    monkeypatch.setattr(
        "core.application.orchestration.dataset_query_step_runtime.resolve_extension_runtime_record_by_install_id",
        resolve,
    )

    extension, _logger = _extension_for_connection(
        None,
        {
            "connector_ref_jsonb": {
                "extension_id": "mutable.catalog-extension",
                "install_id": "install-missing-release",
            }
        },
    )

    assert candidates == ["install-missing-release"]
    assert extension is None


def test_dataset_query_rejects_raw_sql():
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"sql": "SELECT 1"}}},
        state={},
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "raw_query_not_allowed"


def test_dataset_query_recovers_scope_from_job_and_executes(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    captured = _patch_runtime(monkeypatch)

    result = execute_dataset_query_step(
        step={
            "override": {
                "dataset_query": {
                    "query_template_id": "query-1",
                    "result_target": "users_result",
                }
            }
        },
        state={"job_id": "job-1"},
    )

    assert result["ok"] is True
    assert result["result"]["row_count"] == 2
    assert result["state_patch"]["users_result"]["rows"] == [{"id": 1}, {"id": 2}]
    payload = captured["kwargs"]["input_payload"]
    assert payload["connection_config"]["prebuilt"] == "postgres"
    assert payload["secret_refs"] == {"refs": {"password": "secret-ref"}}
    assert (
        payload["query_template"]["query_definition_jsonb"]["sql"]
        == "SELECT id FROM users LIMIT 10"
    )
    assert (
        payload["dataset"]["resource_scope_jsonb"]["resources"][0]["resource_id"] == "public.users"
    )
    assert "connection" not in payload


def _use_join_dataset(captured, sql: str) -> None:
    db = captured["registry"].db_store
    db.datasets["dataset-1"]["resource_scope_jsonb"] = {
        "version": 1,
        "resources": [
            {"resource_id": "public.users", "columns": ["id", "email"]},
            {"resource_id": "public.orders", "columns": ["id", "user_id", "total"]},
        ],
    }
    db.templates["query-1"]["query_definition_jsonb"] = {"sql": sql}


def test_dataset_query_executes_a_saved_join_and_returns_its_rows(monkeypatch):
    """A saved JOIN reaches the connector unchanged and its rows land in state."""
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    sql = (
        "SELECT u.email, o.total FROM public.users u "
        "LEFT JOIN orders o ON o.user_id = u.id WHERE o.total > :minimum"
    )
    captured = _patch_runtime(
        monkeypatch,
        response={
            "ok": True,
            "result": {
                "rows": [{"email": "a@example.com", "total": 12}, {"email": "b@example.com"}],
                "row_count": 2,
            },
            "errors": [],
        },
    )
    _use_join_dataset(captured, sql)
    captured["registry"].db_store.templates["query-1"]["params_schema_jsonb"] = {
        "type": "object",
        "properties": {"minimum": {"type": "integer"}},
        "required": ["minimum"],
    }

    result = execute_dataset_query_step(
        step={
            "override": {
                "dataset_query": {
                    "query_template_id": "query-1",
                    "params": {"minimum": "state.inputs.minimum"},
                    "result_target": "orders_by_user",
                }
            }
        },
        state={"job_id": "job-1", "inputs": {"minimum": 10}},
    )

    assert result["ok"] is True, result["errors"]
    assert result["state_patch"]["orders_by_user"]["rows"] == [
        {"email": "a@example.com", "total": 12},
        {"email": "b@example.com"},
    ]
    payload = captured["kwargs"]["input_payload"]
    assert payload["query_template"]["query_definition_jsonb"]["sql"] == sql
    assert payload["params"] == {"minimum": 10}
    assert [
        resource["resource_id"]
        for resource in payload["dataset"]["resource_scope_jsonb"]["resources"]
    ] == ["public.users", "public.orders"]


@pytest.mark.parametrize(
    ("sql", "resource"),
    [
        ("SELECT u.id FROM users u JOIN secrets s ON s.user_id = u.id LIMIT 10", "secrets"),
        ("SELECT u.id FROM users u STRAIGHT_JOIN secrets s LIMIT 10", "secrets"),
        (
            "SELECT u.id FROM (users u JOIN orders o ON o.user_id = u.id), secrets s LIMIT 10",
            "secrets",
        ),
        ("SELECT a.id FROM users u JOIN auth.users a ON a.id = u.id LIMIT 10", "auth.users"),
    ],
)
def test_dataset_query_refuses_a_saved_join_outside_the_dataset_before_the_connector(
    monkeypatch, sql: str, resource: str
):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    captured = _patch_runtime(monkeypatch)
    _use_join_dataset(captured, sql)

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"query_template_id": "query-1"}}},
        state={"job_id": "job-1"},
    )

    assert result["ok"] is False
    assert result["errors"] == [
        {
            "code": "saved_query_definition_invalid",
            "message": f"Query resource '{resource}' is outside the dataset scope",
            "detail": "query_definition_jsonb.sql",
        }
    ]
    assert "kwargs" not in captured


def test_dataset_query_recovers_scope_from_project_config(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    _patch_runtime(monkeypatch)

    result = execute_dataset_query_step(
        step={
            "override": {
                "dataset_query": {
                    "query_template_id": "query-1",
                    "result_target": "users_result",
                }
            }
        },
        state={
            "project_config": {
                "project_id": "project-1",
                "account_id": "account-1",
                "tenant_id": "tenant-1",
                "workspace_id": "workspace-1",
            }
        },
    )

    assert result["ok"] is True
    assert result["result"]["row_count"] == 2


def test_dataset_query_result_target_writes_state_path(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    _patch_runtime(monkeypatch)

    result = execute_dataset_query_step(
        step={
            "override": {
                "dataset_query": {
                    "query_template_id": "query-1",
                    "result_target": "state.catalog.row_checks",
                }
            }
        },
        state={"job_id": "job-1", "workflow_data": {"existing": True}},
    )

    assert result["ok"] is True
    assert "state.catalog.row_checks" not in result["state_patch"]
    assert result["state_patch"]["catalog"]["row_checks"]["row_count"] == 2
    assert result["state_patch"]["workflow_data"]["catalog"]["row_checks"]["row_count"] == 2
    assert result["state_patch"]["workflow_data"]["existing"] is True


def test_dataset_query_result_target_cannot_overwrite_trusted_test_mode(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )
    from core.application.orchestration.runtime_identity import runtime_identity_from_state

    _patch_runtime(monkeypatch)
    state = {
        "job_id": "job-1",
        "trusted_runtime_context": {"test_mode": False},
    }

    result = execute_dataset_query_step(
        step={
            "override": {
                "dataset_query": {
                    "query_template_id": "query-1",
                    "result_target": "state.trusted_runtime_context.test_mode",
                }
            }
        },
        state=state,
    )
    merged = {**state, **result["state_patch"]}

    assert result["ok"] is True
    assert result["state_patch"].get("trusted_runtime_context") is None
    assert runtime_identity_from_state(merged).test_mode is False


def test_dataset_query_applies_project_state_updates(monkeypatch):
    from core.application.extensions import response_envelope
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    captured: dict = {}

    def _put_state(*_args, **kwargs):
        captured.update(kwargs)
        return {"revision": 1, **kwargs}

    monkeypatch.setattr(response_envelope.extension_project_state, "put_state", _put_state)
    _patch_runtime(
        monkeypatch,
        extension={
            "extension_key": "fixture.dataset-query-provider",
            "install_id": "install-dataset-query-provider",
            "compiled_manifest": {
                "project_state": {
                    "writes": [{"namespace": "query_state", "state_key": "last_result"}]
                }
            },
        },
        response={
            "ok": True,
            "result": {"rows": [{"id": 1}], "row_count": 1},
            "project_state_updates": [
                {
                    "namespace": "query_state",
                    "state_key": "last_result",
                    "value_jsonb": {"row_count": 1},
                }
            ],
            "errors": [],
        },
    )

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"query_template_id": "query-1"}}},
        state={"job_id": "job-1", "actor_id": "actor-1"},
    )

    assert result["ok"] is True
    assert captured["account_id"] == "account-1"
    assert captured["project_id"] == "project-1"
    assert captured["extension_id"] == "fixture.dataset-query-provider"
    assert captured["extension_install_id"] == "install-dataset-query-provider"
    assert captured["connection_id"] == "connection-1"
    assert captured["actor_id"] == "actor-1"
    assert captured["namespace"] == "query_state"


def test_dataset_query_fails_closed_without_project_assignment(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    registry = _Registry()
    registry.db_store.datasets["dataset-1"]["project_ids"] = ["project-2"]
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: registry,
    )

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"query_template_id": "query-1"}}},
        state={"job_id": "job-1"},
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "dataset_not_assigned"


def test_dataset_query_denies_unassigned_project_before_template_content_validation(
    monkeypatch,
):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    registry = _Registry()
    registry.db_store.datasets["dataset-1"]["project_ids"] = ["project-2"]
    registry.db_store.templates["query-1"]["query_definition_jsonb"] = {
        "statement": "SELECT secret_column FROM private_table"
    }
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: registry,
    )

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"query_template_id": "query-1"}}},
        state={"job_id": "job-1"},
    )

    assert result["errors"] == [
        {
            "code": "dataset_not_assigned",
            "message": "Dataset is not assigned to this project",
        }
    ]


def test_dataset_query_fails_closed_when_dataset_has_no_project_assignment(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    registry = _Registry()
    registry.db_store.datasets["dataset-1"]["project_ids"] = []
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: registry,
    )

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"query_template_id": "query-1"}}},
        state={"job_id": "job-1"},
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "dataset_not_assigned"


def test_dataset_query_rejects_connection_outside_project_scope(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    registry = _Registry()
    registry.db_store.datasets["dataset-1"]["project_ids"] = ["project-1"]
    monkeypatch.setitem(
        registry.db_store.connections["connection-1"], "workspace_id", "workspace-2"
    )
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: registry,
    )

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"query_template_id": "query-1"}}},
        state={"job_id": "job-1"},
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "connection_scope_mismatch"


def test_ordinary_dataset_cannot_select_a_project_connection_with_metadata(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    registry = _Registry()
    registry.db_store.datasets["dataset-1"].pop("connection_id")
    registry.db_store.datasets["dataset-1"]["metadata_jsonb"] = {
        "connection_resolution": "project_scope"
    }
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: registry,
    )

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"query_template_id": "query-1"}}},
        state={"job_id": "job-1"},
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "connection_rebind_required"


def test_projected_saved_query_resolves_through_owning_install_and_project_connection(
    monkeypatch,
):
    from core.application.extensions.projected_dataset_bridge import projection_bridge_id
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    install_id = "install-example"
    release_id = "release-example"
    query_bridge_id = projection_bridge_id(install_id, "sample_open_orders_query")
    dataset_bridge_id = projection_bridge_id(install_id, "sample_open_orders")
    registry = _Registry()
    db = registry.db_store
    db.connections["connection-1"]["workspace_id"] = "workspace-1"
    db.templates = {
        query_bridge_id: {
            "query_template_id": query_bridge_id,
            "dataset_id": dataset_bridge_id,
            "display_name": "Open orders",
            "query_language": "sql",
            "query_definition_jsonb": {"sql": "SELECT order_id FROM orders LIMIT 10"},
            "validation_result_jsonb": {
                "projection_bridge": {
                    "install_id": install_id,
                    "artifact_kind": "saved_query",
                    "artifact_id": "sample_open_orders_query",
                }
            },
        }
    }
    db.datasets = {
        dataset_bridge_id: {
            "dataset_id": dataset_bridge_id,
            "connector_id": "toolbox_postgres",
            "project_ids": ["project-1"],
            "metadata_jsonb": {
                "connection_resolution": "project_scope",
                "projection_bridge": {
                    "immutable": True,
                    "install_id": install_id,
                    "release_id": release_id,
                    "artifact_kind": "dataset",
                    "artifact_id": "sample_open_orders",
                },
            },
            "resource_scope_jsonb": {
                "version": 1,
                "resources": [{"resource_id": "orders", "columns": ["order_id"]}],
            },
        }
    }
    db.workflows = {
        "sample_dataset_open_orders": {"source_install_id": install_id},
    }
    db.projections = {
        ("saved_query", "sample_open_orders_query"): {
            "account_id": "account-1",
            "install_id": install_id,
            "release_id": release_id,
            "artifact_kind": "saved_query",
            "artifact_id": "sample_open_orders_query",
            "is_active": True,
            "availability_status": "available",
            "new_runs_blocked": False,
            "projection": {
                "query_template_id": "sample_open_orders_query",
                "dataset_id": "sample_open_orders",
            },
        },
        ("dataset", "sample_open_orders"): {
            "account_id": "account-1",
            "install_id": install_id,
            "release_id": release_id,
            "artifact_kind": "dataset",
            "artifact_id": "sample_open_orders",
            "is_active": True,
            "availability_status": "available",
            "new_runs_blocked": False,
        },
    }
    db.get_workflow = lambda workflow_id: db.workflows.get(workflow_id)
    db.get_extension_registry_projection = lambda **kwargs: db.projections.get(
        (kwargs["artifact_kind"], kwargs["artifact_id"])
    )
    captured = _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: registry,
    )

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"query_template_id": "sample_open_orders_query"}}},
        state={"job_id": "job-1", "workflow_id": "sample_dataset_open_orders"},
    )

    assert result["ok"] is True
    assert (
        captured["kwargs"]["input_payload"]["query_template"]["query_template_id"]
        == "sample_open_orders_query"
    )
    assert captured["kwargs"]["input_payload"]["dataset"]["dataset_id"] == dataset_bridge_id


def test_projected_saved_query_fails_closed_when_projection_blocks_new_runs(monkeypatch):
    from core.application.extensions.projected_dataset_bridge import projection_bridge_id
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    install_id = "install-example"
    query_bridge_id = projection_bridge_id(install_id, "sample_open_orders_query")
    registry = _Registry()
    registry.db_store.templates = {
        query_bridge_id: {
            "query_template_id": query_bridge_id,
            "dataset_id": projection_bridge_id(install_id, "sample_open_orders"),
        }
    }
    registry.db_store.get_workflow = lambda _workflow_id: {"source_install_id": install_id}
    registry.db_store.get_extension_registry_projection = lambda **_kwargs: {
        "account_id": "account-1",
        "install_id": install_id,
        "artifact_kind": "saved_query",
        "artifact_id": "sample_open_orders_query",
        "is_active": True,
        "availability_status": "unavailable",
        "new_runs_blocked": True,
    }
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: registry,
    )

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"query_template_id": "sample_open_orders_query"}}},
        state={"job_id": "job-1", "workflow_id": "sample_dataset_open_orders"},
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "projection_unavailable"


def test_platform_workflow_cannot_execute_inactive_projected_saved_query(monkeypatch):
    from core.application.extensions.projected_dataset_bridge import projection_bridge_id
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    install_id = "install-disabled"
    query_bridge_id = projection_bridge_id(install_id, "open_orders")
    registry = _Registry()
    registry.db_store.templates = {
        query_bridge_id: {
            "query_template_id": query_bridge_id,
            "dataset_id": "dataset-1",
            "validation_result_jsonb": {
                "projection_bridge": {
                    "install_id": install_id,
                    "artifact_kind": "saved_query",
                }
            },
        }
    }
    registry.db_store.get_workflow = lambda _workflow_id: {}
    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.projected_artifact_rows",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: registry,
    )

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"query_template_id": query_bridge_id}}},
        state={"job_id": "job-1", "workflow_id": "platform-workflow"},
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "query_template_not_found"


def test_projected_saved_query_honors_uninstall_dependency_marker(monkeypatch):
    from core.application.extensions.projected_dataset_bridge import projection_bridge_id
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    install_id = "install-example"
    query_bridge_id = projection_bridge_id(install_id, "sample_open_orders_query")
    registry = _Registry()
    registry.db_store.templates = {
        query_bridge_id: {
            "query_template_id": query_bridge_id,
            "dataset_id": projection_bridge_id(install_id, "sample_open_orders"),
        }
    }
    registry.db_store.get_workflow = lambda _workflow_id: {"source_install_id": install_id}
    registry.db_store.get_extension_registry_projection = lambda **_kwargs: {
        "account_id": "account-1",
        "install_id": install_id,
        "artifact_kind": "saved_query",
        "artifact_id": "sample_open_orders_query",
        "is_active": True,
        "projection": {
            "extension_dependency_availability": {
                "availability_status": "unavailable",
                "new_runs_blocked": True,
            }
        },
    }
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: registry,
    )

    result = execute_dataset_query_step(
        step={"override": {"dataset_query": {"query_template_id": "sample_open_orders_query"}}},
        state={"job_id": "job-1", "workflow_id": "sample_dataset_open_orders"},
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "projection_unavailable"


def test_projected_saved_query_dependency_marker_recovers_after_reinstall() -> None:
    from core.application.orchestration.dataset_query_step_runtime import (
        _projection_is_available,
    )

    row = {
        "is_active": True,
        "projection": {
            "dependencies": {"extensions": [{"extension_id": "acme.provider", "required": True}]},
            "extension_dependency_availability": {
                "availability_status": "unavailable",
                "missing_extensions": ["acme.provider"],
                "new_runs_blocked": True,
            },
        },
    }

    assert _projection_is_available(row, active_extension_ids=set()) is False
    assert _projection_is_available(row, active_extension_ids={"acme.provider"}) is True


def test_resolved_secret_refs_reads_secret_ref_storage(monkeypatch):
    from core.application.orchestration.dataset_query_step_runtime import _resolved_secret_refs

    monkeypatch.setenv("ENCRYPTION_KEY", "unit-test-encryption-key")

    class _Cursor:
        def __init__(self) -> None:
            self.row = {
                "encrypted_value": encrypt_secret("resolved-password"),
            }

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return self.row

    class _SecretDB:
        def _cursor(self):
            return _Cursor()

    payload = _resolved_secret_refs(
        _SecretDB(),
        {
            "account_id": "account-1",
            "secret_refs_jsonb": {
                "refs": {"password": "workspace_data_connection:conn-1:password"}
            },
        },
    )

    assert payload["refs"]["password"] == "workspace_data_connection:conn-1:password"
    assert payload["values"]["password"] == "resolved-password"


@pytest.mark.parametrize(
    ("config", "legacy_timeout", "expected"),
    [
        pytest.param({}, None, 30, id="omitted-default"),
        pytest.param({"timeout_seconds": None}, None, 30, id="null-default"),
        pytest.param({"timeout_seconds": 0}, None, 30, id="zero-default"),
        pytest.param({"timeout_seconds": False}, None, 30, id="false-default"),
        pytest.param({"timeout_seconds": ""}, None, 30, id="empty-default"),
        pytest.param({"timeout_seconds": 45}, None, 45, id="integer"),
        pytest.param({"timeout_seconds": True}, None, 1, id="true-coercion"),
        pytest.param({"timeout_seconds": -2}, None, -2, id="negative-forwarded"),
        pytest.param({"timeout_seconds": 1.5}, None, 1, id="fraction-number-truncated"),
        pytest.param({"timeout_seconds": 1e2}, None, 100, id="exponent-number"),
        pytest.param({"timeout_seconds": "45"}, None, 45, id="integer-string"),
        pytest.param({"timeout_seconds": "0"}, None, 0, id="string-zero-forwarded"),
        pytest.param({"timeout_seconds": " 45 "}, None, 45, id="padded-integer-string"),
        pytest.param({"timeout_seconds": 0}, 7, 7, id="falsey-config-legacy-fallback"),
        pytest.param({"timeout_seconds": "0"}, 7, 0, id="truthy-config-before-legacy"),
    ],
)
def test_dataset_timeout_current_resolution_reaches_declared_extension_port(
    monkeypatch,
    config,
    legacy_timeout,
    expected,
):
    """This port witness does not execute a local/remote extension or provider."""
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    captured = _patch_runtime(monkeypatch)
    step = {
        "step_kind": "dataset_query",
        "override": {
            "dataset_query": {
                "query_template_id": "query-1",
                **config,
            }
        },
    }
    if legacy_timeout is not None:
        step["timeout_seconds"] = legacy_timeout
    result = execute_dataset_query_step(step=step, state={"job_id": "job-1"})
    assert result["ok"] is True
    assert captured["kwargs"]["action_id"] == "execute_template"
    assert captured["kwargs"]["timeout_seconds"] == expected
    assert type(captured["kwargs"]["timeout_seconds"]) is int
    assert captured["kwargs"]["input_payload"]["limits"]["timeout_seconds"] == expected
    assert captured["kwargs"]["input_payload"]["limits"]["max_runtime_rows"] == 100


@pytest.mark.parametrize("timeout", [" ", "1.5", "1e2", "abc"])
def test_dataset_timeout_current_unparseable_string_raises_before_extension_dispatch(
    monkeypatch,
    timeout,
):
    """Characterize the existing unhandled conversion, not a desired new policy."""
    from core.application.orchestration.dataset_query_step_runtime import (
        execute_dataset_query_step,
    )

    captured = _patch_runtime(monkeypatch)
    with pytest.raises(ValueError):
        execute_dataset_query_step(
            step={
                "step_kind": "dataset_query",
                "override": {
                    "dataset_query": {
                        "query_template_id": "query-1",
                        "timeout_seconds": timeout,
                    }
                },
            },
            state={"job_id": "job-1"},
        )
    assert "kwargs" not in captured
