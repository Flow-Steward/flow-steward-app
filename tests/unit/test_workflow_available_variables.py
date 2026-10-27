"""Variables the editor offers a step: before it, after it, or with no place yet.

A step that exists only in the unapplied change set has no saved position, yet
its fields still need the variable picker (B7): after the step it is connected
to, inside its For each (with the item names), or every saved step's outputs
when the position is not known. The global variables never depend on position.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.application.registry.io_resolver import GLOBAL_STATE_VARS, IOResolver
from core.entrypoints.admin.workflows_definition_io_routes import register_io_routes
from tests._workflow_definitions import edge, linear_workflow, node


def _assign(step_id: str, target: str) -> dict:
    return {
        "step_id": step_id,
        "step_kind": "set_variable",
        "override": {"set_variable": {"assignments": {target: "ready"}}},
    }


def _linear() -> dict:
    """fetch -> enrich -> done; each set_variable step writes one state name."""
    return linear_workflow(
        _assign("fetch", "state.orders"),
        _assign("enrich", "state.enriched"),
        workflow_id="wf_orders",
        account_id="acc_alpha",
    )


def _with_for_each() -> dict:
    """fetch, then For each order of state.orders: keep (in the body), then done."""
    steps = [
        {**_assign("fetch", "state.orders"), "group_id": None, "for_each_id": None},
        {**_assign("keep", "state.kept"), "group_id": None, "for_each_id": "each_order"},
        {"step_id": "done", "step_kind": "stop_success", "group_id": None, "for_each_id": None},
    ]
    return {
        "workflow_id": "wf_loop",
        "account_id": "acc_alpha",
        "enabled": True,
        "steps": steps,
        "groups": [],
        "for_each": [
            {
                "for_each_id": "each_order",
                "display_name": "Each order",
                "group_id": None,
                "over": "state.orders",
                "item_name": "order",
                "index_name": "order_index",
                "body": {"kind": "sequence", "entry": node("keep")},
            }
        ],
        "settings": {
            "execution_graph": {
                "schema_version": 1,
                "entry": node("fetch"),
                "edges": [
                    edge("fetch", node("each_order", "for_each")),
                    edge("keep", {"kind": "for_each_exit", "for_each_id": "each_order"}),
                    edge("each_order", node("done"), source_kind="for_each"),
                ],
            }
        },
    }


def _resolver(workflow: dict) -> IOResolver:
    db = MagicMock()
    db.get_workflow.return_value = workflow
    db.list_published_block_contracts.return_value = []
    db.get_skill_outputs.return_value = []
    db.get_agent_outputs.return_value = []
    return IOResolver(db)


def _names(rows: list[dict]) -> set[str]:
    return {str(row.get("name")) for row in rows}


def test_after_step_mode_adds_the_source_steps_own_outputs():
    io = _resolver(_linear())

    before = io.get_available_variables("wf_orders", step_id="enrich")
    after = io.get_available_variables("wf_orders", step_id="enrich", position="after")

    assert before["position"] == "before"
    assert _names(before["prior_outputs"]) == {"orders"}
    assert after["position"] == "after"
    # A step connected after "enrich" reads what enrich reads and what enrich writes.
    assert _names(after["prior_outputs"]) == {"orders", "enriched"}
    assert {row["step_id"] for row in after["prior_outputs"]} == {"fetch", "enrich"}
    assert after["state"] == GLOBAL_STATE_VARS
    assert after["loop"] == []


def test_without_a_position_every_saved_steps_outputs_and_the_globals_are_offered():
    io = _resolver(_linear())

    unknown = io.get_available_variables("wf_orders")

    assert unknown["position"] == "unknown"
    assert _names(unknown["prior_outputs"]) == {"orders", "enriched"}
    assert unknown["state"] == GLOBAL_STATE_VARS
    assert unknown["dotted_path"]


def test_a_for_each_body_step_is_offered_the_item_and_index_names():
    io = _resolver(_with_for_each())

    for position in ("before", "after"):
        loop = io.get_available_variables("wf_loop", step_id="keep", position=position)["loop"]
        assert [(row["name"], row["type"]) for row in loop] == [
            ("order", "json"),
            ("order_index", "number"),
        ]
        assert loop[0]["description_key"] == "react.workflows.variable_description.for_each_item"
        assert loop[0]["description_params"] == {"for_each": "Each order"}
        assert loop[1]["description_key"] == "react.workflows.variable_description.for_each_index"

    # A step outside the body sees no item; a For each with no saved step still names its own.
    assert io.get_available_variables("wf_loop", step_id="fetch")["loop"] == []
    container = io.get_available_variables("wf_loop", for_each_id="each_order")
    assert container["position"] == "unknown"
    assert _names(container["loop"]) == {"order", "order_index"}
    assert io.get_available_variables("wf_loop")["loop"] == []


# --- routes -------------------------------------------------------------------------


def _client(workflow: dict, authorize=None) -> tuple[TestClient, MagicMock]:
    db = MagicMock()
    db.get_workflow.return_value = workflow
    db.list_published_block_contracts.return_value = []
    db.get_skill_outputs.return_value = []
    db.get_agent_outputs.return_value = []
    authorize_mock = MagicMock(side_effect=authorize or (lambda _request, _db, _wid: workflow))
    ctx = SimpleNamespace(
        get_db=lambda: db,
        assert_step_test_run_allowed=MagicMock(),
        resolve_step_contract_preview=MagicMock(),
        authorize_workflow_request=authorize_mock,
    )
    router = APIRouter()
    register_io_routes(router, ctx)
    app = FastAPI()
    app.include_router(router, prefix="/api/workflows")
    return TestClient(app, raise_server_exceptions=False), authorize_mock


def test_step_route_answers_the_after_step_mode():
    client, _authorize = _client(_linear())

    before = client.get("/api/workflows/wf_orders/steps/enrich/available-variables")
    after = client.get("/api/workflows/wf_orders/steps/enrich/available-variables?position=after")

    assert before.status_code == 200
    assert before.json()["position"] == "before"
    assert _names(before.json()["prior_outputs"]) == {"orders"}
    assert after.status_code == 200
    assert after.json()["position"] == "after"
    assert _names(after.json()["prior_outputs"]) == {"orders", "enriched"}
    assert (
        client.get(
            "/api/workflows/wf_orders/steps/enrich/available-variables?position=sideways"
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/workflows/wf_orders/steps/missing/available-variables?position=after"
        ).status_code
        == 404
    )


def test_workflow_route_offers_every_saved_output_and_a_for_each_s_item_names():
    client, _authorize = _client(_with_for_each())

    response = client.get("/api/workflows/wf_loop/available-variables?for_each_id=each_order")

    assert response.status_code == 200
    body = response.json()
    assert body["position"] == "unknown"
    assert _names(body["prior_outputs"]) == {"orders", "kept"}
    assert [row["name"] for row in body["loop"]] == ["order", "order_index"]
    assert (
        body["loop"][0]["description_key"] == "react.workflows.variable_description.for_each_item"
    )
    assert len(body["state"]) == len(GLOBAL_STATE_VARS)
    assert (
        client.get("/api/workflows/wf_loop/available-variables?for_each_id=nope").status_code == 404
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/workflows/wf_orders/steps/enrich/available-variables",
        "/api/workflows/wf_orders/steps/enrich/available-variables?position=after",
        "/api/workflows/wf_orders/available-variables",
    ],
)
def test_every_variables_route_runs_the_same_workflow_authorization(path):
    def refuse(_request, _db, _workflow_id):
        raise HTTPException(status_code=403, detail="Workflow belongs to another account")

    client, authorize = _client(_linear(), authorize=refuse)

    response = client.get(path)

    assert response.status_code == 403
    authorize.assert_called_once()
    assert authorize.call_args.args[2] == "wf_orders"
