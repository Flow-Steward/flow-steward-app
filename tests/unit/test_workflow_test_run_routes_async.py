from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from core.application.management.scope import normalize_scope
from core.entrypoints.admin import workflows_projection
from core.entrypoints.admin.workflows_definition_run_routes import register_run_routes
from tests._workflow_definitions import linear_graph


def _client(
    *,
    enqueue,
    account_id="account-1",
    durable_state=None,
    allowed_project_ids=frozenset({"project-1"}),
    workflow_enabled=True,
    project_workflows=None,
    workflow_settings=None,
    workflow_override=None,
    workflow_validation=None,
    step_validation=None,
    step_contract_preview=None,
) -> tuple[TestClient, MagicMock, MagicMock]:
    db = MagicMock()
    db.dsn = "postgres://flow-steward"
    workflow = workflow_override or {
        "workflow_id": "wf-demo",
        "enabled": workflow_enabled,
        "settings": dict(workflow_settings or {}),
        "steps": [
            {
                "id": "step-row",
                "step_id": "compile",
                "step_kind": "set_variable",
                "display_name": "Compile",
            }
        ],
        "groups": [],
    }
    db.get_workflow.return_value = workflow
    project_store = MagicMock()
    project_store.get_project.return_value = {
        "id": "project-1",
        "name": "Demo",
        "account_id": account_id,
        "project_type": "general",
    }
    run_config = {
        "project_id": "project-1",
        "account_id": account_id,
        "project_name": "Demo",
        "project_root": "/workspace/demo",
    }
    if project_workflows is not None:
        run_config["project_workflows"] = list(project_workflows)
    project_store.get_project_run_config.return_value = run_config
    job_store = MagicMock()
    job_store.get_job.return_value = {
        "id": "41",
        "thread_id": "thread-41",
        "status": "pending",
    }
    router = APIRouter(prefix="/api/workflows")
    ctx = SimpleNamespace(
        get_db=lambda: db,
        get_registry=lambda: SimpleNamespace(
            workflows=SimpleNamespace(get_workflow=lambda _workflow_id: workflow)
        ),
        get_workflow_validation=lambda _db, _workflow_id: dict(
            workflow_validation or {"valid": True, "errors": [], "warnings": []}
        ),
        get_step_test_run_validation=lambda _db, _workflow_id, _step_id: (
            step_validation(_db, _workflow_id, _step_id)
            if callable(step_validation)
            else dict(step_validation or {"valid": True, "errors": [], "warnings": []})
        ),
        resolve_workflow_row=lambda _db, _workflow_id: workflow,
        resolve_step_contract_preview=lambda **_kwargs: dict(step_contract_preview or {}),
        assert_structure_allowed=lambda *_args: None,
        assert_step_test_run_allowed=lambda *_args: None,
        build_simulate_step_preview=lambda _workflow: [],
        authorize_workflow_request=lambda _request, _db, _workflow_id: workflow,
        request_scope=lambda _request: normalize_scope(
            account_id=account_id,
            allowed_project_ids=allowed_project_ids,
            allowed_workflow_ids=frozenset({workflow["workflow_id"]}),
        ),
        durable_test_run_job_state=lambda _thread_id: dict(durable_state or {}),
        enqueue_test_run=enqueue,
    )
    register_run_routes(router, ctx)
    app = FastAPI()

    @app.middleware("http")
    async def auth(request, call_next):
        request.state.auth_user = {
            "id": "operator-1",
            "username": "operator",
            "principal_type": "user",
            "account_id": account_id,
        }
        return await call_next(request)

    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)
    stores = patch.multiple(
        "core.infrastructure.runtime.entrypoint_db_adapters",
        create_project_store=MagicMock(return_value=project_store),
        create_job_store=MagicMock(return_value=job_store),
    )
    client._test_run_store_patch = stores  # type: ignore[attr-defined]
    stores.start()
    return client, project_store, job_store


def _close(client: TestClient) -> None:
    client._test_run_store_patch.stop()  # type: ignore[attr-defined]
    client.close()


def _workflow_with_unrelated_invalid_connector() -> dict[str, Any]:
    return {
        "workflow_id": "wf-demo",
        "enabled": True,
        "settings": {},
        "steps": [
            {
                "step_id": "stale-store",
                "step_kind": "connector",
                "connector_id": "acme_store",
                "operation_id": "list_locations",
            },
            {
                "step_id": "send-email",
                "step_kind": "connector",
                "connector_id": "outgoing_email",
                "operation_id": "send_email",
            },
        ],
        "groups": [],
    }


def _invalid_store_validation() -> dict[str, Any]:
    issue = {
        "code": "workflow.step_connector_connection_scopes_missing",
        "message": "Authorize the scopes required by this extension operation",
        "path": "workflow.steps.stale-store",
    }
    return {"valid": False, "errors": [issue], "warnings": []}


def test_step_validation_projection_keeps_only_target_and_interface_input_issues() -> None:
    workflow = _workflow_with_unrelated_invalid_connector()
    unrelated_issue = _invalid_store_validation()["errors"][0]
    selected_issue = {
        "code": "workflow.connection_unavailable",
        "message": "Selected email connection is unavailable",
        "path": "workflow.steps.send-email.connection_binding_jsonb.connection_id",
    }
    input_issue = {
        "code": "workflow.interface.invalid_type",
        "message": "Workflow input has an invalid type",
        "path": "workflow.settings.callable.expected_inputs[0].type",
    }
    with (
        patch.object(workflows_projection, "_resolve_workflow_row", return_value=workflow),
        patch.object(
            workflows_projection,
            "_get_workflow_validation",
            return_value={
                "valid": False,
                "errors": [unrelated_issue, selected_issue, input_issue],
                "warnings": [],
            },
        ),
    ):
        validation = workflows_projection._get_step_test_run_validation(
            MagicMock(),
            "wf-demo",
            "send-email",
        )

    assert validation == {
        "valid": False,
        "errors": [selected_issue, input_issue],
        "warnings": [],
    }


def test_step_validation_projection_ignores_only_unrelated_step_issue() -> None:
    workflow = _workflow_with_unrelated_invalid_connector()
    with (
        patch.object(workflows_projection, "_resolve_workflow_row", return_value=workflow),
        patch.object(
            workflows_projection,
            "_get_workflow_validation",
            return_value=_invalid_store_validation(),
        ),
    ):
        validation = workflows_projection._get_step_test_run_validation(
            MagicMock(),
            "wf-demo",
            "send-email",
        )

    assert validation == {"valid": True, "errors": [], "warnings": []}


class _RegistryToolWorkflowDB:
    def __init__(self, workflow: dict[str, Any], *, agents: dict[str, dict] | None = None):
        self.workflow = workflow
        self.agents = agents or {}

    def get_workflow(self, workflow_id: str):
        return self.workflow if workflow_id == self.workflow["workflow_id"] else None

    def get_tool(self, tool_id: str):
        if tool_id == "available.tool":
            return {"tool_id": tool_id, "enabled": True, "availability_status": "available"}
        return None

    def get_agent(self, agent_id: str):
        return self.agents.get(agent_id)

    def get_agent_skills(self, _agent_id: str):
        return []

    def get_agent_inputs(self, _agent_id: str):
        return []

    def get_agent_outputs(self, _agent_id: str):
        return []


def _registry_tool_workflow(*steps: dict[str, Any]) -> dict[str, Any]:
    return {
        "workflow_id": "wf-tools",
        "settings": {},
        "steps": [*steps, {"step_id": "done", "step_kind": "stop_success"}],
        "groups": [],
    }


def test_step_validation_projection_rejects_selected_unavailable_registry_tool() -> None:
    workflow = _registry_tool_workflow(
        {
            "step_id": "selected",
            "step_kind": "connector",
            "connector_id": "connector.missing.tool",
        }
    )

    validation = workflows_projection._get_step_test_run_validation(
        _RegistryToolWorkflowDB(workflow), "wf-tools", "selected"
    )

    assert "workflow.step_connector_tool_unavailable" in {
        issue["code"] for issue in validation["errors"]
    }


def test_step_validation_projection_rejects_selected_agent_unavailable_tool() -> None:
    workflow = _registry_tool_workflow(
        {"step_id": "selected", "step_kind": "agent", "agent_id": "writer"}
    )
    db = _RegistryToolWorkflowDB(
        workflow,
        agents={"writer": {"agent_id": "writer", "tools": [{"tool_id": "missing.tool"}]}},
    )

    validation = workflows_projection._get_step_test_run_validation(db, "wf-tools", "selected")

    assert "workflow.agent_tool_unavailable" in {issue["code"] for issue in validation["errors"]}


def test_step_validation_projection_ignores_unrelated_unavailable_registry_tool() -> None:
    workflow = _registry_tool_workflow(
        {
            "step_id": "unrelated",
            "step_kind": "connector",
            "connector_id": "connector.missing.tool",
        },
        {
            "step_id": "selected",
            "step_kind": "connector",
            "connector_id": "connector.available.tool",
        },
    )

    validation = workflows_projection._get_step_test_run_validation(
        _RegistryToolWorkflowDB(workflow), "wf-tools", "selected"
    )

    assert validation["valid"] is True
    assert "workflow.step_connector_tool_unavailable" not in {
        issue["code"] for issue in validation["errors"]
    }


def test_step_validation_projection_ignores_unrelated_agent_unavailable_tool() -> None:
    workflow = _registry_tool_workflow(
        {"step_id": "unrelated", "step_kind": "agent", "agent_id": "writer"},
        {
            "step_id": "selected",
            "step_kind": "connector",
            "connector_id": "connector.available.tool",
        },
    )
    db = _RegistryToolWorkflowDB(
        workflow,
        agents={"writer": {"agent_id": "writer", "tools": [{"tool_id": "missing.tool"}]}},
    )

    validation = workflows_projection._get_step_test_run_validation(db, "wf-tools", "selected")

    assert validation["valid"] is True
    assert "workflow.agent_tool_unavailable" not in {
        issue["code"] for issue in validation["errors"]
    }


def test_step_run_ignores_unrelated_connector_readiness_and_enqueues_only_target() -> None:
    captured: dict[str, Any] = {}

    def enqueue(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 41

    client, _project_store, _job_store = _client(
        enqueue=enqueue,
        workflow_override=_workflow_with_unrelated_invalid_connector(),
        workflow_validation=_invalid_store_validation(),
        step_validation={"valid": True, "errors": [], "warnings": []},
    )
    try:
        response = client.post(
            "/api/workflows/wf-demo/steps/send-email/test-run",
            json={"project_id": "project-1", "task_summary": "Send only the email"},
        )
    finally:
        _close(client)

    assert response.status_code == 202, response.text
    extra_state = captured["extra_state"]
    assert extra_state["run_cursor"]["step_id"] == "send-email"
    assert extra_state["step_test_run_context"]["step_id"] == "send-email"


def test_step_run_rejects_selected_connector_readiness_issue() -> None:
    enqueue = MagicMock()
    selected_issue = {
        "code": "workflow.step_connector_connection_scopes_missing",
        "message": "Selected connector is not ready",
        "path": "workflow.steps.send-email",
    }
    client, _project_store, _job_store = _client(
        enqueue=enqueue,
        workflow_override=_workflow_with_unrelated_invalid_connector(),
        workflow_validation={"valid": True, "errors": [], "warnings": []},
        step_validation={"valid": False, "errors": [selected_issue], "warnings": []},
    )
    try:
        response = client.post(
            "/api/workflows/wf-demo/steps/send-email/test-run",
            json={"project_id": "project-1", "task_summary": "Send only the email"},
        )
    finally:
        _close(client)

    assert response.status_code == 400
    assert "Selected connector is not ready" in response.text
    enqueue.assert_not_called()


def test_whole_workflow_run_still_rejects_unrelated_connector_issue() -> None:
    enqueue = MagicMock()
    client, _project_store, _job_store = _client(
        enqueue=enqueue,
        workflow_override=_workflow_with_unrelated_invalid_connector(),
        workflow_validation=_invalid_store_validation(),
        step_validation={"valid": True, "errors": [], "warnings": []},
    )
    try:
        response = client.post(
            "/api/workflows/wf-demo/test-run",
            json={"project_id": "project-1", "task_summary": "Run everything"},
        )
    finally:
        _close(client)

    assert response.status_code == 400
    assert "Authorize the scopes required" in response.text
    enqueue.assert_not_called()


def test_step_run_unknown_target_fails_closed() -> None:
    enqueue = MagicMock()
    client, _project_store, _job_store = _client(
        enqueue=enqueue,
        workflow_override=_workflow_with_unrelated_invalid_connector(),
    )
    try:
        response = client.post(
            "/api/workflows/wf-demo/steps/missing-step/test-run",
            json={"project_id": "project-1", "task_summary": "Run missing"},
        )
    finally:
        _close(client)

    assert response.status_code == 404
    enqueue.assert_not_called()


def test_workflow_post_returns_202_and_enqueues_durable_worker_job() -> None:
    captured: dict[str, Any] = {}

    def enqueue(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 41

    client, _project_store, _job_store = _client(enqueue=enqueue)
    try:
        response = client.post(
            "/api/workflows/wf-demo/test-run",
            headers={"x-correlation-id": "request-1", "idempotency-key": "idem-1"},
            json={
                "project_id": "project-1",
                "task_summary": "Validate",
                "mode": "brownfield",
            },
        )
    finally:
        _close(client)

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["thread_id"] == "thread-41"
    assert captured["task_source"] == "workflow_test_run"
    assert captured["meta"]["actor_id"] == "operator"
    assert captured["meta"]["correlation_id"] == "request-1"
    assert captured["meta"]["idempotency_key"] == "idem-1"
    assert captured["meta"]["allowed_project_ids"] == ["project-1"]
    assert captured["meta"]["allowed_workflow_ids"] == ["wf-demo"]


def test_workflow_dry_run_stays_synchronous_and_does_not_enqueue() -> None:
    enqueue = MagicMock()
    client, _project_store, _job_store = _client(enqueue=enqueue)
    try:
        response = client.post(
            "/api/workflows/wf-demo/test-run",
            json={
                "project_id": "project-1",
                "task_summary": "Validate",
                "dry_run": True,
            },
        )
    finally:
        _close(client)

    assert response.status_code == 200
    assert response.json()["status"] == "dry_run"
    enqueue.assert_not_called()


def test_workflow_dry_run_materializes_and_redacts_neutral_secret_input(monkeypatch) -> None:
    from core.infrastructure.security import secret_codec
    from core.infrastructure.security.prompt_safety import REDACTED_CREDENTIAL_MARKER

    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "dry-run-key"})
    enqueue = MagicMock()
    client, _project_store, _job_store = _client(
        enqueue=enqueue,
        workflow_settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["api"],
                "expected_inputs": [{"name": "login", "type": "secret", "required": True}],
                "output_mode": "none",
            }
        },
    )
    plaintext = "dry-run-plaintext-secret"
    try:
        response = client.post(
            "/api/workflows/wf-demo/test-run",
            json={
                "project_id": "project-1",
                "task_summary": "Validate",
                "dry_run": True,
                "task_envelope": {"inputs": {"login": plaintext}},
            },
        )
    finally:
        _close(client)

    assert response.status_code == 200, response.text
    assert plaintext not in response.text
    assert response.json()["task_envelope"]["inputs"]["login"] == REDACTED_CREDENTIAL_MARKER
    enqueue.assert_not_called()


def test_a_disabled_workflow_can_be_test_run() -> None:
    """Disabling stops scheduled and triggered runs, not the author's test run.

    Test runs are how a workflow is checked before it is enabled; refusing them
    made a draft impossible to try.
    """
    enqueue = MagicMock(return_value=101)
    client, _project_store, _job_store = _client(
        enqueue=enqueue,
        workflow_enabled=False,
    )
    try:
        response = client.post(
            "/api/workflows/wf-demo/test-run",
            json={"project_id": "project-1", "task_summary": "Validate"},
        )
    finally:
        _close(client)

    assert response.status_code == 202, response.text
    enqueue.assert_called_once()


def test_unlinked_workflow_is_rejected_before_durable_enqueue() -> None:
    enqueue = MagicMock()
    client, _project_store, _job_store = _client(
        enqueue=enqueue,
        project_workflows=["wf-other"],
    )
    try:
        response = client.post(
            "/api/workflows/wf-demo/test-run",
            json={"project_id": "project-1", "task_summary": "Validate"},
        )
    finally:
        _close(client)

    assert response.status_code == 422
    enqueue.assert_not_called()


def test_step_post_returns_202_with_selected_step_worker_state() -> None:
    captured: dict[str, Any] = {}

    def enqueue(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 41

    client, _project_store, _job_store = _client(enqueue=enqueue)
    try:
        response = client.post(
            "/api/workflows/wf-demo/steps/compile/test-run",
            json={"project_id": "project-1", "task_summary": "Compile only"},
        )
    finally:
        _close(client)

    assert response.status_code == 202
    assert response.json()["step_id"] == "compile"
    assert captured["task_source"] == "step_test_run"
    assert captured["extra_state"]["run_cursor"]["step_id"] == "compile"


def test_disabled_step_post_persisted_scope_builds_real_worker_graph_for_only_selected_step(
    monkeypatch,
) -> None:
    from core.application.jobs.task_execution_request import _build_request
    from core.application.registry.workflow_manifest import WorkflowManifest
    from core.infrastructure.runtime.worker.worker_execution_adapters import (
        WorkerGraphExecutionAdapter,
    )

    captured: dict[str, Any] = {}

    def enqueue(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 41

    steps = [
        {
            "step_id": "unrelated",
            "step_kind": "connector",
            "connector_id": "connector.missing.tool",
        },
        {
            "step_id": "selected",
            "step_kind": "connector",
            "connector_id": "connector.available.tool",
        },
        {"step_id": "done", "step_kind": "stop_success"},
    ]
    workflow = {
        "workflow_id": "wf-demo",
        "version": "1",
        "display_name": "Demo",
        "description": "",
        "enabled": False,
        "trigger": {},
        "settings": {"execution_graph": linear_graph(["unrelated", "selected", "done"])},
        "steps": steps,
        "groups": [],
    }
    client, _project_store, _job_store = _client(
        enqueue=enqueue,
        workflow_override=workflow,
        step_validation={"valid": True, "errors": [], "warnings": []},
    )
    try:
        response = client.post(
            "/api/workflows/wf-demo/steps/selected/test-run",
            json={"project_id": "project-1", "task_summary": "Run selected only"},
        )
    finally:
        _close(client)

    assert response.status_code == 202, response.text
    message = {
        **captured,
        "job_id": "41",
        "thread_id": "thread-41",
        "extra_state": {
            **captured["extra_state"],
            "step_test_run_context": {
                "workflow_id": "attacker-workflow",
                "step_id": "attacker-step",
            },
        },
    }
    request = _build_request(
        message,
        {
            "task_source": captured["task_source"],
            "selected_workflow": "wf-demo",
            "meta": captured["meta"],
        },
    )
    assert request.extra_state["step_test_run_context"] == {
        "workflow_id": "wf-demo",
        "step_id": "selected",
        "execution_id": captured["meta"]["execution_id"],
    }
    # The cursor comes from trusted job meta, not from the delivered envelope.
    assert request.extra_state["run_cursor"]["step_id"] == "selected"

    db = _RegistryToolWorkflowDB(workflow)
    registry = SimpleNamespace(
        db_store=db,
        workflows=SimpleNamespace(list_all=lambda: [WorkflowManifest.from_db_row(workflow)]),
        agents=SimpleNamespace(list_all=list),
        skills=SimpleNamespace(list_all=list),
        tools=SimpleNamespace(list_all=list),
        reload_all=lambda: None,
    )
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: registry,
    )
    adapter = WorkerGraphExecutionAdapter(config={}, bl=None)

    graph = adapter._build_graph(request, checkpointer=False)

    assert graph is not None


def test_get_returns_durable_state_and_reapplies_project_scope() -> None:
    client, _project_store, _job_store = _client(
        enqueue=MagicMock(),
        durable_state={
            "status": "queued",
            "thread_id": "thread-41",
            "workflow_id": "wf-demo",
            "project_id": "project-1",
        },
    )
    try:
        response = client.get("/api/workflows/test-runs/thread-41")
    finally:
        _close(client)

    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_get_denies_a_durable_test_run_outside_the_original_project_scope() -> None:
    client, _project_store, _job_store = _client(
        enqueue=MagicMock(),
        allowed_project_ids=frozenset({"project-other"}),
        durable_state={
            "status": "queued",
            "thread_id": "thread-41",
            "workflow_id": "wf-demo",
            "project_id": "project-1",
        },
    )
    try:
        response = client.get("/api/workflows/test-runs/thread-41")
    finally:
        _close(client)

    assert response.status_code == 403


def test_step_post_materializes_secret_using_server_preview_before_enqueue(monkeypatch) -> None:
    from core.infrastructure.security import secret_codec
    from core.infrastructure.security.secret_reference import is_secret_reference

    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "test-run-secret-key"})
    captured: dict[str, Any] = {}

    def enqueue(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 41

    client, _project_store, _job_store = _client(
        enqueue=enqueue,
        step_contract_preview={
            "optional": [{"name": "header", "source": "state.outputs.login", "type": "secret"}]
        },
    )
    try:
        response = client.post(
            "/api/workflows/wf-demo/steps/compile/test-run",
            json={
                "project_id": "project-1",
                "task_summary": "Compile only",
                "task_envelope": {
                    "inputs": {"header": "synthetic-route-input"},
                    "metadata": {
                        "input_bindings": [
                            {"name": "header", "source": "state.outputs.login", "type": "text"}
                        ]
                    },
                },
            },
        )
    finally:
        _close(client)

    assert response.status_code == 202
    assert "synthetic-route-input" not in repr(captured)
    envelope = captured["extra_state"]["task_envelope"]
    assert is_secret_reference(envelope["metadata"]["input_binding_values"]["header"])
    assert len(captured["extra_state"]["_workflow_secret_ref_fingerprints"]) == 1
    assert "synthetic-route-input" not in response.text


@pytest.mark.parametrize("source", ["", "state.inputs.request"])
def test_supplied_step_values_are_validated_before_readiness_and_enqueue(source):
    captured = []
    client, _projects, _jobs = _client(
        enqueue=lambda **kwargs: captured.append(kwargs) or 41,
        step_contract_preview={
            "unresolved_required": [
                {"name": "request", "type": "number", "required": True, "source": source}
            ]
        },
        step_validation={
            "valid": False,
            "errors": [
                {
                    "code": "workflow.step_io.error",
                    "issue_type": "step_required_input_unavailable",
                    "step_id": "compile",
                    "input_name": "request",
                }
            ],
            "warnings": [],
        },
    )
    try:
        response = client.post(
            "/api/workflows/wf-demo/steps/compile/test-run",
            json={
                "project_id": "project-1",
                "task_summary": "Typed step value",
                "task_envelope": {"inputs": {"request": "not-a-number"}},
            },
        )
    finally:
        _close(client)
    assert response.status_code == 422
    assert response.json()["detail"]["errors"][0]["code"] == "workflow.input.invalid_type"
    assert captured == []
