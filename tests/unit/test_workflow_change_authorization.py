"""Each operation of a change set may name only what its workflow may use (§9.2, T25)."""

from __future__ import annotations

import pytest

from core.application.workflows.workflow_change_authorization import (
    REFERENCE_UNAVAILABLE,
    STEP_KIND_UNSUPPORTED,
    ChangeReferenceAccess,
    authorize_change_operations,
    store_reference_access,
)
from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
)

ACCOUNT = "acct-a"
FOREIGN = "acct-b"


def _access(**denied: set[str]) -> ChangeReferenceAccess:
    def allow(kind: str):
        return lambda reference_id: reference_id not in denied.get(kind, set())

    return ChangeReferenceAccess(
        agent=allow("agent"),
        workflow=allow("workflow"),
        connection=allow("connection"),
        install=allow("install"),
    )


def _issues(operations, access) -> list[dict[str, str]]:
    with pytest.raises(WorkflowDefinitionChangeError) as refused:
        authorize_change_operations(operations, access)
    return refused.value.issues


def test_every_reference_of_every_operation_is_checked() -> None:
    operations = [
        {"op": "create_step", "step": {"step_id": "a", "step_kind": "agent", "agent_id": "spy"}},
        {
            "op": "insert_step_between",
            "step": {
                "step_id": "call",
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"workflow_id": "wf_other"}},
            },
        },
        {
            "op": "update_step_config",
            "step_id": "sync",
            "patch": {
                "connector_install_id": "inst-x",
                "connection_binding_jsonb": {"connection_id": "conn-x"},
            },
        },
    ]
    access = _access(
        agent={"spy"}, workflow={"wf_other"}, install={"inst-x"}, connection={"conn-x"}
    )

    issues = _issues(operations, access)

    assert [(issue["code"], issue["path"]) for issue in issues] == [
        (REFERENCE_UNAVAILABLE, "operations[0].step.agent_id"),
        (REFERENCE_UNAVAILABLE, "operations[1].step.override.invoke_workflow.workflow_id"),
        (REFERENCE_UNAVAILABLE, "operations[2].patch.connector_install_id"),
        (REFERENCE_UNAVAILABLE, "operations[2].patch.connection_binding.connection_id"),
    ]


def test_a_new_step_must_be_of_a_supported_kind() -> None:
    issues = _issues(
        [{"op": "create_step", "step": {"step_id": "x", "step_kind": "phase_gate"}}], _access()
    )
    assert [issue["code"] for issue in issues] == [STEP_KIND_UNSUPPORTED]


def test_allowed_references_and_unrelated_operations_pass() -> None:
    authorize_change_operations(
        [
            {"op": "create_step", "step": {"step_id": "a", "step_kind": "agent", "agent_id": "ok"}},
            {"op": "update_step_config", "step_id": "a", "patch": {"display_name": "Renamed"}},
            {"op": "connect", "source": {"kind": "step", "id": "a"}},
            "not-an-object",
        ],
        _access(agent={"denied"}),
    )


def test_a_patch_without_the_broken_reference_can_still_repair_the_step() -> None:
    """Only what an operation names is judged, not what the stored step already has."""
    authorize_change_operations(
        [{"op": "update_step_config", "step_id": "sync", "patch": {"display_name": "Fix"}}],
        _access(agent={"gone"}, install={"gone"}),
    )


class _Store:
    def __init__(self) -> None:
        self.agents = {
            "own": {"agent_id": "own", "account_id": ACCOUNT, "scope_kind": "account"},
            "shared": {"agent_id": "shared", "account_id": FOREIGN, "scope_kind": "system"},
            "foreign": {"agent_id": "foreign", "account_id": FOREIGN, "scope_kind": "account"},
        }
        self.workflows = {
            "wf_own": {"workflow_id": "wf_own", "account_id": ACCOUNT},
            "wf_hidden": {"workflow_id": "wf_hidden", "account_id": ACCOUNT},
            "wf_foreign": {"workflow_id": "wf_foreign", "account_id": FOREIGN},
        }
        self.connections = {
            "conn_own": {"connection_id": "conn_own", "account_id": ACCOUNT, "project_id": "p1"},
            "conn_other_project": {
                "connection_id": "conn_other_project",
                "account_id": ACCOUNT,
                "project_id": "p2",
            },
            "conn_foreign": {"connection_id": "conn_foreign", "account_id": FOREIGN},
        }
        self.installs = {
            "inst_own": {"install_id": "inst_own", "account_id": ACCOUNT},
            "inst_foreign": {"install_id": "inst_foreign", "account_id": FOREIGN},
        }

    def get_agent(self, agent_id):
        return self.agents.get(agent_id)

    def get_workflow(self, workflow_id):
        return self.workflows.get(workflow_id)

    def get_extension_connection_metadata(self, connection_id):
        return self.connections.get(connection_id)

    def get_extension_install_contract(self, install_id):
        return self.installs.get(install_id)


def test_store_access_is_bound_to_the_workflow_account_and_request_scope() -> None:
    access = store_reference_access(
        _Store(),
        account_id=ACCOUNT,
        workflow_visible=lambda row: row["workflow_id"] != "wf_hidden",
        project_visible=lambda project_id: project_id in {"", "p1"},
    )

    assert access.agent("own")
    assert access.agent("shared")
    assert not access.agent("foreign")
    assert not access.agent("missing")
    assert access.workflow("wf_own")
    assert not access.workflow("wf_hidden")
    assert not access.workflow("wf_foreign")
    assert not access.workflow("wf_missing")
    assert access.connection("conn_own")
    assert not access.connection("conn_other_project")
    assert not access.connection("conn_foreign")
    assert not access.connection("conn_missing")
    assert access.install("inst_own")
    assert not access.install("inst_foreign")
    assert not access.install("inst_missing")


def test_the_changes_route_refuses_a_foreign_reference_without_any_write() -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.entrypoints.admin.workflows_definition import create_workflow_definition_router
    from tests._workflow_definitions import linear_workflow, node

    workflow = linear_workflow(
        {"step_id": "sync", "step_kind": "set_variable"}, workflow_id="wf_auth", enabled=False
    )
    db = MagicMock()
    db.get_workflow.return_value = workflow
    cursor = MagicMock()
    cursor.fetchone.return_value = {"revision": 1}
    db._transaction.side_effect = lambda *_a, **_k: nullcontext(cursor)
    valid = {"valid": True, "errors": [], "warnings": []}
    router = create_workflow_definition_router(
        get_db=lambda: db,
        get_registry=MagicMock,
        get_workflow_validation=lambda _db, _id: dict(valid),
        get_step_test_run_validation=lambda _db, _id, _step: dict(valid),
        get_readiness_validation=lambda _store, _id: dict(valid),
        resolve_workflow_row=lambda store, workflow_id: store.get_workflow(workflow_id),
        assert_workflow_structure_change_allowed=lambda _db, _id, _action: None,
        assert_workflow_step_test_run_allowed=lambda _db, _id, _action: None,
        request_scope=lambda _request: SimpleNamespace(
            account_id=DEFAULT_ACCOUNT_ID, actor_name="operator"
        ),
        enforce_workflow_row_scope=lambda _db, _scope, row: row,
        assert_workflow_mutation_allowed=lambda _row: None,
        validate_context_inject_sources=lambda _value: None,
        change_reference_access=lambda _db, _scope, _row: _access(agent={"foreign_agent"}),
        durable_test_run_job_state=lambda _job_id: {},
        enqueue_test_run=lambda **_kwargs: 1,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/workflows")

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/workflows/wf_auth/changes",
        json={
            "expected_revision": 1,
            "operations": [
                {
                    "op": "insert_step_between",
                    "source": node("sync"),
                    "outcome": {"name": "success"},
                    "step": {"step_id": "spy", "step_kind": "agent", "agent_id": "foreign_agent"},
                }
            ],
        },
    )

    assert response.status_code == 400, response.text
    issues = response.json()["detail"]["issues"]
    assert [issue["code"] for issue in issues] == [REFERENCE_UNAVAILABLE]
    db.save_workflow.assert_not_called()
