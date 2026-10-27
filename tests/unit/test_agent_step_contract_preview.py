"""Explicit Agent preview intention retains the actual step and old caller contract.

These exercise preview routing/resolution with narrow store/scope ports. No write,
provider, Agent execution, or claim about backend null materialization is involved.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.application.registry.io_resolver import IOResolver
from core.entrypoints.admin import workflows_definition as routes


@pytest.fixture
def agent_preview(monkeypatch):
    row = {
        "step_id": "agent_a",
        "step_kind": "agent",
        "agent_id": "writer",
        "skill_id": "draft",
        "group_id": "real_group",
        "block_key": "block.legacy",
        "override": {"published_block_key": "block.a"},
        "boundary_contract": {
            "entry": [
                {
                    "name": "boundary_value",
                    "type": "text",
                    "source": "state.upstream",
                    "required": False,
                }
            ]
        },
    }
    workflow = {
        "workflow_id": "wf",
        "account_id": "account-a",
        "enabled": False,
        "settings": {"participants": [{"participant_key": "author", "agent_id": "writer"}]},
        "steps": [row],
    }
    blocks = {
        key: {
            "block_key": key,
            "enabled": True,
            "source_kind": "agent",
            "source_ref": "writer::draft",
        }
        for key in ["block.auto", "block.a", "block.b", "block.legacy"]
    }
    db = SimpleNamespace(
        get_workflow=lambda *_a, **_k: deepcopy(workflow),
        get_published_block_contract=lambda key, **_k: deepcopy(blocks.get(key)),
        list_published_block_contracts=lambda **_k: [deepcopy(blocks["block.auto"])],
    )
    resolver = IOResolver(db)
    context = []
    monkeypatch.setattr(
        resolver,
        "_build_available_type_map",
        lambda workflow_id, step_id: (
            context.append((workflow_id, step_id)) or {"state.upstream": "text"}
        ),
    )
    monkeypatch.setattr(resolver, "_merge_step_input_contract", lambda **_k: [])
    monkeypatch.setattr(resolver, "_merge_step_output_contract", lambda **_k: [])
    monkeypatch.setattr(resolver, "_get_agent", lambda _id: {})
    monkeypatch.setattr(resolver, "_get_skill", lambda _id: {})
    import core.application.registry.io_resolver as resolver_module

    monkeypatch.setattr(resolver_module, "IOResolver", lambda _db: resolver)
    catalog_accounts = []
    monkeypatch.setattr(
        routes,
        "catalog_agent_row",
        lambda _db, _id, *, account_id: catalog_accounts.append(account_id) or {"enabled": True},
    )
    monkeypatch.setattr(
        routes, "catalog_skill_row", lambda _db, _id, *, account_id: {"enabled": True}
    )
    monkeypatch.setattr(routes, "agent_primary_skill_id", lambda *_a, **_k: "draft")
    scope_calls = []
    denied = [False]

    def enforce(_db, scope, current):
        scope_calls.append((scope.account_id, current["workflow_id"]))
        if denied[0]:
            raise HTTPException(status_code=403, detail="Workflow scope denied")
        return current

    router = routes.create_workflow_definition_router(
        get_db=lambda: db,
        get_registry=lambda: SimpleNamespace(),
        get_workflow_validation=lambda *_a: {},
        get_step_test_run_validation=lambda *_a: {},
        get_readiness_validation=lambda *_a: {},
        resolve_workflow_row=lambda *_a: deepcopy(workflow),
        assert_workflow_structure_change_allowed=lambda *_a: None,
        assert_workflow_step_test_run_allowed=lambda *_a: None,
        request_scope=lambda _r: SimpleNamespace(account_id="account-a"),
        enforce_workflow_row_scope=enforce,
        assert_workflow_mutation_allowed=lambda *_a: None,
        validate_context_inject_sources=lambda *_a: None,
        change_reference_access=lambda *_a: None,
        durable_test_run_job_state=lambda *_a: {},
        enqueue_test_run=lambda **_k: 0,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/workflows")
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            resolver=resolver,
            context=context,
            row=row,
            blocks=blocks,
            denied=denied,
            scope_calls=scope_calls,
            catalog_accounts=catalog_accounts,
        )


@pytest.mark.parametrize(
    "mode,key,expected",
    [("auto", "", "block.auto"), ("explicit", "block.b", "block.b"), (None, "", "block.legacy")],
)
def test_explicit_auto_and_b_keep_real_step_context_and_legacy_omission(
    agent_preview, mode, key, expected
):
    params = {"step_id": "agent_a", "agent_id": "writer", "participant_key": "author"}
    if mode is not None:
        params["block_binding_mode"] = mode
    if key:
        params["block_key"] = key
    response = agent_preview.client.get("/api/workflows/wf/step-contract-preview", params=params)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["block_key"] == expected
    assert value["step_id"] == "agent_a"
    assert value["group_id"] == "real_group"
    assert any(
        row["name"] == "boundary_value" and row["source"] == "state.upstream"
        for row in value["custom"]
    )
    assert agent_preview.context == [("wf", "agent_a")]
    assert agent_preview.scope_calls == [("account-a", "wf")]
    assert agent_preview.catalog_accounts == ["account-a"]
    assert agent_preview.row["override"] == {"published_block_key": "block.a"}


@pytest.mark.parametrize(
    "params,status",
    [
        ({"block_binding_mode": "unknown"}, 422),
        ({"block_binding_mode": "explicit"}, 400),
        ({"block_binding_mode": "auto", "block_key": "block.b"}, 400),
        ({"block_binding_mode": "explicit", "block_key": "missing"}, 404),
    ],
)
def test_preview_intention_rejects_ambiguous_or_missing_selection(agent_preview, params, status):
    response = agent_preview.client.get(
        "/api/workflows/wf/step-contract-preview",
        params={"step_id": "agent_a", "agent_id": "writer", **params},
    )
    assert response.status_code == status
    assert agent_preview.context == []


def test_disabled_block_and_scope_refusal_are_not_bypassed_by_new_mode(agent_preview):
    agent_preview.blocks["block.b"]["enabled"] = False
    response = agent_preview.client.get(
        "/api/workflows/wf/step-contract-preview",
        params={
            "step_id": "agent_a",
            "agent_id": "writer",
            "block_binding_mode": "explicit",
            "block_key": "block.b",
        },
    )
    assert response.status_code == 400
    agent_preview.denied[0] = True
    response = agent_preview.client.get(
        "/api/workflows/wf/step-contract-preview",
        params={"step_id": "agent_a", "agent_id": "writer", "block_binding_mode": "auto"},
    )
    assert response.status_code == 403
    assert agent_preview.context == []
