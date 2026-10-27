"""AC19: the benchmark uses portability and runs the inspected persisted workload."""

from __future__ import annotations

import pytest

from core.application.auth.config import DEFAULT_TENANT_ID
from core.application.management.scope import normalize_scope
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.workflow_contract_service import WorkflowInterfaceService
from core.application.tooling.ops import profile_benchmark_prepare as prepare
from tests._db_env import resolve_test_db_dsn

pytestmark = pytest.mark.integration


@pytest.fixture
def builder_store(pg_conn, default_account, monkeypatch):
    assert pg_conn.info.dbname == "flow_steward_audit"

    class _Index:
        co_location_group = "test_external_delivery"

        def index_record(self, record, *, repository):
            pass

    monkeypatch.setattr(
        "core.application.memory.runtime._build_vector_backend", lambda **_: _Index()
    )
    store = RegistryDBStore(resolve_test_db_dsn())
    scope = normalize_scope({"account_id": default_account, "tenant_id": DEFAULT_TENANT_ID})
    steps = 3
    workflow_id = prepare.benchmark_workflow_id(steps)
    if store.get_workflow(workflow_id) is not None:
        store.delete_workflow(workflow_id)
    yield store, scope, steps
    if store.get_workflow(workflow_id) is not None:
        store.delete_workflow(workflow_id)


def _transform_steps(workflow: dict) -> list[dict]:
    return [step for step in workflow.get("steps") or [] if step.get("step_kind") == "transform"]


def test_the_benchmark_workflow_is_a_chain_of_enabled_transform_steps(builder_store) -> None:
    store, scope, steps = builder_store

    result = prepare.prepare_benchmark_workflow(store=store, scope=scope, steps=steps)

    assert result == {"workflow_id": "fs_benchmark_platform_3", "steps": 3, "created": True}
    workflow = store.get_workflow(result["workflow_id"])
    assert workflow["enabled"] is True
    chain = _transform_steps(workflow)
    assert [step["step_id"] for step in chain] == ["stage_0", "stage_1", "stage_2"]
    assert chain[0]["override"]["transform"]["input"] == {"source": "state.inputs.payload.rows"}
    assert chain[2]["override"]["transform"]["input"] == {"source": "state.stage_1"}
    callable_settings = workflow["settings"]["callable"]
    assert "api" in callable_settings["trigger_channels"]
    assert [item["name"] for item in callable_settings["expected_inputs"]] == ["payload"]


def test_preparing_again_reuses_the_workflow(builder_store) -> None:
    store, scope, steps = builder_store
    prepare.prepare_benchmark_workflow(store=store, scope=scope, steps=steps)

    again = prepare.prepare_benchmark_workflow(store=store, scope=scope, steps=steps)

    assert again["created"] is False
    assert len(_transform_steps(store.get_workflow(again["workflow_id"]))) == steps


def test_a_workflow_it_did_not_create_is_left_alone(builder_store) -> None:
    store, scope, steps = builder_store
    store.save_workflow(
        {
            "workflow_id": prepare.benchmark_workflow_id(steps),
            "enabled": False,
            "steps": [],
            "groups": [],
            "for_each": [],
        }
    )

    with pytest.raises(RuntimeError, match="not created by this benchmark"):
        prepare.prepare_benchmark_workflow(store=store, scope=scope, steps=steps)


def test_an_older_owned_revision_is_replaced(builder_store) -> None:
    store, scope, steps = builder_store
    workflow_id = prepare.benchmark_workflow_id(steps)
    store.save_workflow(
        {
            "workflow_id": workflow_id,
            "enabled": False,
            "tags": ["ac19-benchmark", "ac19-v1"],
            "steps": [],
            "groups": [],
            "for_each": [],
        }
    )

    result = prepare.prepare_benchmark_workflow(store=store, scope=scope, steps=steps)

    assert result["created"] is True
    workflow = store.get_workflow(workflow_id)
    assert workflow["enabled"] is True
    assert {"ac19-benchmark", "ac19-v2"} <= set(workflow["tags"])
    assert len(_transform_steps(workflow)) == steps


def test_benchmark_contract_accepts_the_existing_workflow_run_submit_channel(builder_store):
    store, scope, steps = builder_store
    result = prepare.prepare_benchmark_workflow(store=store, scope=scope, steps=steps)
    workflow = store.get_workflow(result["workflow_id"])
    service = WorkflowInterfaceService()
    interface = service.interface_from_settings(workflow["settings"])

    validation = service.validate_inputs(
        interface,
        {"payload": {"rows": [{"sku": "item-1", "title": "Item", "quantity": 3}]}},
        channel="mcp",
    )

    assert validation.ok is True, validation.errors
