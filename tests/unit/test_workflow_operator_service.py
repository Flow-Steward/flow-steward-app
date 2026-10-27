from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceNotFoundError
from core.application.management.scope import ScopeContext
from core.application.management.workflow_operator_service import WorkflowOperatorService
from core.application.registry.registry_manager import RegistryManager
from core.application.workflows.workflow_availability_service import WorkflowAvailabilityService
from core.entrypoints.api.workflow_operator import create_workflow_operator_router
from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


@pytest.mark.parametrize("include_validation", [False, True])
def test_catalog_keeps_existing_disabled_workflow_unavailable_with_saved_name(include_validation):
    workflow = {"workflow_id": "wf-disabled", "account_id": DEFAULT_ACCOUNT_ID, "enabled": False}
    db = SimpleNamespace(
        get_workflow=lambda workflow_id: workflow if workflow_id == "wf-disabled" else None
    )
    query = SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope=None: SimpleNamespace(
            project_workflows=[
                {
                    "workflow_id": "wf-disabled",
                    "workflow_display_name": "Импорт склада 日本語",
                    "enabled": False,
                    "availability_status": "unavailable",
                    "new_runs_blocked": True,
                }
            ]
        )
    )
    service = WorkflowOperatorService(
        get_logger=lambda: db,
        project_query_service=lambda: query,
        registry_db_provider=lambda: db,
        workflow_availability_service=WorkflowAvailabilityService(get_logger=lambda: db),
    )

    item = service.list_project_workflows(
        "project-own", enabled_only=False, include_validation=include_validation
    ).workflows[0]

    assert item.availability_status == "unavailable"
    assert item.display_name == "Импорт склада 日本語"
    assert item.enabled is False
    assert item.new_runs_blocked is True
    if include_validation:
        assert item.validation["valid"] is False
        assert item.validation["errors"][0]["code"] == "workflow.unavailable"
    else:
        assert item.validation is None
    assert (
        service.list_project_workflows(
            "project-own", enabled_only=True, include_validation=include_validation
        ).workflows
        == []
    )
    with pytest.raises(ServiceNotFoundError, match="not available"):
        service.workflow_validate("wf-disabled")

    # Both real transports consume the same operator projection; neither may
    # relabel an existing disabled workflow as missing or lose its saved name.
    app = FastAPI()
    app.include_router(
        create_workflow_operator_router(
            workflow_operator_service=lambda: service,
            request_scope=lambda request, **kwargs: ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID, project_id=kwargs.get("project_id")
            ),
            service_error_response=lambda exc: JSONResponse(
                {"error": str(exc)}, status_code=exc.status_code
            ),
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        )
    )
    response = TestClient(app).get(
        "/api/projects/project-own/workflow-catalog",
        params={"enabled_only": "false", "include_validation": str(include_validation).lower()},
    )
    assert response.status_code == 200
    api_item = response.json()["workflows"][0]
    assert api_item["availability_status"] == "unavailable"
    assert api_item["display_name"] == "Импорт склада 日本語"
    assert api_item["enabled"] is False
    assert api_item["new_runs_blocked"] is True

    runtime = SimpleNamespace(
        workflow_operator_service=service,
        project_query_service=query,
        project_command_service=object(),
        user_query_service=object(),
        user_command_service=object(),
        extension_scaffold_service=object(),
    )
    cli = build_click_app(ServiceRegistry(runtime), SimpleNamespace())
    args = ["workflows", "list", "--project-id", "project-own", "--include-disabled", "--json"]
    if include_validation:
        args.append("--include-validation")
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    cli_item = json.loads(result.output)["result"]["workflows"][0]
    assert cli_item["availability_status"] == "unavailable"
    assert cli_item["display_name"] == "Импорт склада 日本語"
    assert cli_item["enabled"] is False
    assert cli_item["new_runs_blocked"] is True


def test_catalog_still_marks_a_really_missing_workflow_missing_through_actual_availability():
    db = SimpleNamespace(get_workflow=lambda workflow_id: None)
    query = SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope=None: SimpleNamespace(
            project_workflows=[
                {
                    "workflow_id": "wf-missing",
                    "enabled": False,
                    "availability_status": "unavailable",
                    "new_runs_blocked": True,
                }
            ]
        )
    )
    service = WorkflowOperatorService(
        get_logger=lambda: db,
        project_query_service=lambda: query,
        registry_db_provider=lambda: db,
        workflow_availability_service=WorkflowAvailabilityService(get_logger=lambda: db),
    )

    item = service.list_project_workflows(
        "project-own", enabled_only=False, include_validation=True
    ).workflows[0]

    assert item.availability_status == "missing"
    assert item.enabled is False
    assert item.new_runs_blocked is True
    assert item.validation["errors"][0]["code"] == "workflow.not_found"


def test_list_project_workflows_marks_stale_binding_when_validation_workflow_missing(monkeypatch):
    query = SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope=None: SimpleNamespace(
            project_workflows=[
                {
                    "workflow_id": "deleted_demo_workflow",
                    "display_name": "Deleted Demo Workflow",
                    "description": "Stale project binding left after workflow removal",
                    "enabled": True,
                    "availability_status": "available",
                    "new_runs_blocked": False,
                }
            ]
        )
    )
    service = WorkflowOperatorService(
        get_logger=lambda: None,
        project_query_service=lambda: query,
    )

    def missing_workflow(_workflow_id):
        raise ServiceNotFoundError("Workflow 'deleted_demo_workflow' not found")

    monkeypatch.setattr(service, "_workflow_validation_payload", missing_workflow)

    catalog = service.list_project_workflows("project-1", include_validation=True)

    assert len(catalog.workflows) == 1
    item = catalog.workflows[0]
    assert item.workflow_id == "deleted_demo_workflow"
    assert item.enabled is False
    assert item.availability_status == "missing"
    assert item.new_runs_blocked is True
    assert item.validation is not None
    assert item.validation["valid"] is False
    assert item.validation["errors"][0]["code"] == "workflow.not_found"


def test_list_project_workflows_projects_blocked_binding_as_disabled():
    query = SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope=None: SimpleNamespace(
            project_workflows=[
                {
                    "workflow_id": "trashed_workflow",
                    "display_name": "Trashed Workflow",
                    "enabled": True,
                    "availability_status": "unavailable",
                    "new_runs_blocked": True,
                }
            ]
        )
    )
    service = WorkflowOperatorService(
        get_logger=lambda: None,
        project_query_service=lambda: query,
    )

    catalog = service.list_project_workflows("project-1", enabled_only=False)

    assert len(catalog.workflows) == 1
    assert catalog.workflows[0].enabled is False
    assert catalog.workflows[0].new_runs_blocked is True


def test_list_project_workflows_excludes_blocked_binding_when_enabled_only():
    query = SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope=None: SimpleNamespace(
            project_workflows=[
                {
                    "workflow_id": "trashed_workflow",
                    "enabled": True,
                    "availability_status": "unavailable",
                    "new_runs_blocked": True,
                }
            ]
        )
    )
    service = WorkflowOperatorService(
        get_logger=lambda: None,
        project_query_service=lambda: query,
    )

    catalog = service.list_project_workflows("project-1", enabled_only=True)

    assert catalog.workflows == []


def test_list_project_entrypoints_excludes_workflows_outside_scoped_bindings():
    query = SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope=None: SimpleNamespace(
            project_workflows=[{"workflow_id": "wf-allowed", "enabled": True}]
        )
    )
    service = WorkflowOperatorService(
        get_logger=lambda: object(),
        project_query_service=lambda: query,
    )
    rows = [
        {
            "entry_key": "allowed",
            "workflow_id": "wf-allowed",
            "enabled": True,
            "input_mapping_jsonb": {},
        },
        {
            "entry_key": "unrelated",
            "workflow_id": "wf-unrelated",
            "enabled": True,
            "input_mapping_jsonb": {},
        },
    ]

    with patch(
        "core.application.management.workflow_operator_service.workflow_entrypoint_repository.list_project_workflow_entrypoints",
        return_value=rows,
    ):
        payload = service.list_project_entrypoints(
            "project-1",
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="machine-1",
                principal_type="service_account",
                allowed_workflow_ids=frozenset({"wf-allowed"}),
            ),
        )

    assert [entry.workflow_id for entry in payload.entrypoints] == ["wf-allowed"]


def test_list_project_entrypoints_keeps_all_rows_for_unrestricted_scope():
    query = SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope=None: SimpleNamespace(
            project_workflows=[{"workflow_id": "wf-1", "enabled": True}]
        )
    )
    service = WorkflowOperatorService(
        get_logger=lambda: object(),
        project_query_service=lambda: query,
    )
    rows = [
        {
            "entry_key": "first",
            "workflow_id": "wf-1",
            "enabled": True,
            "input_mapping_jsonb": {},
        },
        {
            "entry_key": "second",
            "workflow_id": "wf-2",
            "enabled": True,
            "input_mapping_jsonb": {},
        },
    ]

    with patch(
        "core.application.management.workflow_operator_service.workflow_entrypoint_repository.list_project_workflow_entrypoints",
        return_value=rows,
    ):
        payload = service.list_project_entrypoints(
            "project-1",
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="operator-1",
                principal_type="operator",
                allowed_workflow_ids=frozenset(),
            ),
        )

    assert [entry.workflow_id for entry in payload.entrypoints] == ["wf-1", "wf-2"]


@pytest.mark.parametrize(
    "method_name",
    [
        "project_workflow_validate",
        "project_workflow_contract",
        "project_workflow_suggestions",
    ],
)
def test_project_workflow_reads_reject_workflows_outside_scoped_bindings(method_name):
    query = SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope=None: SimpleNamespace(
            project_workflows=[{"workflow_id": "wf-allowed", "enabled": True}]
        )
    )
    service = WorkflowOperatorService(
        get_logger=lambda: None,
        project_query_service=lambda: query,
    )

    _raises_callable_206_1 = getattr(service, method_name)
    with pytest.raises(ServiceNotFoundError, match="not linked"):
        _raises_callable_206_1("project-1", "wf-unrelated")

    assert service._linked_project_workflow_ids("project-1") == {"wf-allowed"}


def test_project_workflow_contract_resolves_extension_projection(monkeypatch):
    workflow_id = "extension.install-1.demo_child"
    query = SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope=None: SimpleNamespace(
            project_workflows=[{"workflow_id": workflow_id, "enabled": True}]
        )
    )
    projected = SimpleNamespace(
        workflow_id=workflow_id,
        display_name="Demo Child",
        enabled=True,
        settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "input_mode": "fields",
                "strict_inputs": True,
                "expected_inputs": [{"name": "latitude", "type": "number", "required": True}],
                "output_mode": "variables",
                "expected_outputs": [
                    {"name": "weather_summary", "type": "paragraph", "required": True}
                ],
            }
        },
    )
    registry_manager = SimpleNamespace(
        db_store=SimpleNamespace(get_workflow=lambda _workflow_id: None),
        workflows=SimpleNamespace(
            get_workflow=lambda requested_id: projected if requested_id == workflow_id else None
        ),
    )
    monkeypatch.setattr(RegistryManager, "instance", lambda: registry_manager)
    service = WorkflowOperatorService(
        get_logger=lambda: None,
        project_query_service=lambda: query,
        workflow_availability_service=SimpleNamespace(
            get_workflow_availability=lambda workflow_id, account_id, project_id=None: {
                "availability_status": "available",
                "new_runs_blocked": False,
            }
        ),
    )

    contract = service.project_workflow_contract("project-1", workflow_id)

    assert contract.workflow_id == workflow_id
    assert contract.display_name == "Demo Child"
    assert contract.interface["enabled"] is True
    assert contract.interface["trigger_channels"] == ["parent_workflow"]
    assert contract.interface["expected_inputs"][0]["name"] == "latitude"
    assert contract.definition_issues == []


def test_project_workflow_contract_rejects_effectively_unavailable_binding():
    workflow_id = "extension.install-1.disabled_child"
    query = SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope=None: SimpleNamespace(
            project_workflows=[
                {
                    "workflow_id": workflow_id,
                    "enabled": True,
                    "availability_status": "unavailable",
                    "new_runs_blocked": True,
                }
            ]
        )
    )
    service = WorkflowOperatorService(
        get_logger=lambda: None,
        project_query_service=lambda: query,
        registry_manager_provider=lambda: SimpleNamespace(
            db_store=SimpleNamespace(get_workflow=lambda _workflow_id: None),
            workflows=SimpleNamespace(
                get_workflow=lambda _workflow_id: SimpleNamespace(
                    workflow_id=workflow_id,
                    display_name="Disabled Child",
                    enabled=True,
                    settings={"callable": {"enabled": True}},
                )
            ),
        ),
    )

    try:
        service.project_workflow_contract("project-1", workflow_id)
    except ServiceNotFoundError as exc:
        assert str(exc) == "Workflow not available for project"
    else:
        raise AssertionError("effectively unavailable workflow contract was exposed")


def test_global_workflow_contract_rejects_stale_projection_when_effective_state_is_disabled():
    workflow_id = "extension.install-1.disabled_child"
    projected = SimpleNamespace(
        workflow_id=workflow_id,
        display_name="Disabled Child",
        enabled=True,
        settings={"callable": {"enabled": True}},
    )
    service = WorkflowOperatorService(
        get_logger=lambda: None,
        project_query_service=None,
        registry_manager_provider=lambda: SimpleNamespace(
            db_store=SimpleNamespace(get_workflow=lambda _workflow_id: None),
            workflows=SimpleNamespace(get_workflow=lambda _workflow_id: projected),
        ),
        workflow_availability_service=SimpleNamespace(
            get_workflow_availability=lambda workflow_id, account_id, project_id=None: {
                "availability_status": "unavailable",
                "new_runs_blocked": True,
            }
        ),
    )

    try:
        service.workflow_contract(workflow_id)
    except ServiceNotFoundError as exc:
        assert str(exc) == f"Workflow '{workflow_id}' not available"
    else:
        raise AssertionError("stale disabled workflow projection was exposed globally")


def test_global_workflow_contract_checks_availability_in_workflow_account():
    workflow_id = "extension.install-1.dataset"
    account_id = "account-owner"
    requested_accounts: list[str] = []
    workflow = {
        "workflow_id": workflow_id,
        "account_id": account_id,
        "display_name": "Dataset",
        "enabled": True,
        "settings": {"callable": {"enabled": True}},
    }
    db = SimpleNamespace(
        get_workflow=lambda requested_id: workflow if requested_id == workflow_id else None
    )
    service = WorkflowOperatorService(
        get_logger=lambda: db,
        project_query_service=None,
        registry_manager_provider=lambda: SimpleNamespace(
            db_store=db,
            workflows=SimpleNamespace(get_workflow=lambda _workflow_id: None),
        ),
        workflow_availability_service=SimpleNamespace(
            get_workflow_availability=lambda _workflow_id, account_id, project_id=None: (
                requested_accounts.append(account_id)
                or {"availability_status": "available", "new_runs_blocked": False}
            )
        ),
    )

    contract = service.workflow_contract(workflow_id)

    assert contract.workflow_id == workflow_id
    assert requested_accounts == [account_id]


def test_global_workflow_contract_uses_registry_db_for_default_availability():
    workflow_id = "core.dataset"
    workflow = {
        "workflow_id": workflow_id,
        "display_name": "Dataset",
        "enabled": True,
        "settings": {"callable": {"enabled": True}},
    }
    db = SimpleNamespace(
        get_workflow=lambda requested_id: workflow if requested_id == workflow_id else None
    )
    service = WorkflowOperatorService(
        get_logger=lambda: SimpleNamespace(),
        project_query_service=None,
        registry_manager_provider=lambda: SimpleNamespace(
            db_store=db,
            workflows=SimpleNamespace(get_workflow=lambda _workflow_id: None),
        ),
        registry_db_provider=lambda: db,
    )

    contract = service.workflow_contract(workflow_id)

    assert contract.workflow_id == workflow_id


def test_global_workflow_contract_resolves_fresh_extension_projection(monkeypatch):
    workflow_id = "extension.install-1.dataset"
    projected_row = {
        "workflow_id": workflow_id,
        "account_id": DEFAULT_ACCOUNT_ID,
        "source_install_id": "install-1",
        "display_name": "Projected Dataset",
        "enabled": True,
        "settings": {"callable": {"enabled": True}},
    }
    projected_workflow = SimpleNamespace(**projected_row)
    db = SimpleNamespace(get_workflow=lambda _workflow_id: None)
    service = WorkflowOperatorService(
        get_logger=lambda: db,
        project_query_service=None,
        registry_manager_provider=lambda: SimpleNamespace(
            db_store=db,
            workflows=SimpleNamespace(get_workflow=lambda _workflow_id: projected_workflow),
        ),
        registry_db_provider=lambda: db,
    )
    monkeypatch.setattr(
        "core.application.registry.workflow_lookup.get_workflow_row",
        lambda _db, requested_id, *, account_id: (
            projected_row
            if requested_id == workflow_id and account_id == DEFAULT_ACCOUNT_ID
            else None
        ),
    )
    monkeypatch.setattr(
        WorkflowAvailabilityService,
        "_db_list_dependencies",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "core.application.extensions.install_availability_service.InstallAvailabilityService.get_install_availability",
        lambda _service, install_id, *, account_id: {
            "availability_status": "available",
            "new_runs_blocked": False,
        },
    )

    contract = service.workflow_contract(workflow_id)

    assert contract.workflow_id == workflow_id
    assert contract.display_name == "Projected Dataset"


# ---------------------------------------------------------------------------
# Authoring versus running
#
# "Not available for new runs" is the right answer for an execution path and
# the wrong one for an editor. The admin contract route reused the run-gated
# projection, so opening the builder for a disabled draft raised
# ServiceNotFoundError — and, unmapped, reached the browser as HTTP 500 twice
# per page load.
# ---------------------------------------------------------------------------


def _disabled_workflow_service(workflow_id: str) -> WorkflowOperatorService:
    workflow = {
        "workflow_id": workflow_id,
        "display_name": "Draft in progress",
        "enabled": False,
        "settings": {"callable": {"enabled": True}},
    }
    db = SimpleNamespace(
        get_workflow=lambda requested: workflow if requested == workflow_id else None
    )
    return WorkflowOperatorService(
        get_logger=lambda: db,
        project_query_service=None,
        registry_manager_provider=lambda: SimpleNamespace(
            db_store=db,
            workflows=SimpleNamespace(get_workflow=lambda _workflow_id: None),
        ),
        workflow_availability_service=SimpleNamespace(
            get_workflow_availability=lambda _workflow_id, account_id, project_id=None: {
                "availability_status": "unavailable",
                "new_runs_blocked": True,
            }
        ),
    )


def test_the_authoring_contract_is_served_for_a_disabled_workflow() -> None:
    workflow_id = "draft_workflow"

    contract = _disabled_workflow_service(workflow_id).workflow_contract(
        workflow_id, require_available=False
    )

    assert contract.workflow_id == workflow_id


def test_the_run_facing_contract_still_refuses_a_disabled_workflow() -> None:
    """The public and operator paths keep their meaning; only authoring is
    exempt, and only because an author is editing rather than running."""
    workflow_id = "draft_workflow"

    _raises_callable_495_1 = _disabled_workflow_service(workflow_id).workflow_contract
    with pytest.raises(ServiceNotFoundError):
        _raises_callable_495_1(workflow_id)


def test_a_workflow_that_does_not_exist_is_still_not_found_when_authoring() -> None:
    """Skipping the availability gate must not skip existence."""
    service = WorkflowOperatorService(
        get_logger=lambda: None,
        project_query_service=None,
        registry_manager_provider=lambda: SimpleNamespace(
            db_store=SimpleNamespace(get_workflow=lambda _workflow_id: None),
            workflows=SimpleNamespace(get_workflow=lambda _workflow_id: None),
        ),
        workflow_availability_service=SimpleNamespace(
            get_workflow_availability=lambda _workflow_id, account_id, project_id=None: {
                "availability_status": "available",
                "new_runs_blocked": False,
            }
        ),
    )

    with pytest.raises(ServiceNotFoundError):
        service.workflow_contract("no_such_workflow", require_available=False)


@pytest.mark.parametrize("command", ["validate", "contract"])
def test_cli_can_diagnose_linked_disabled_draft_before_enabling(command):
    workflow_id = "draft_workflow"
    service = _disabled_workflow_service(workflow_id)
    service._project_query_service = lambda: SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope=None: SimpleNamespace(
            project_workflows=[
                {
                    "workflow_id": workflow_id,
                    "enabled": False,
                    "availability_status": "unavailable",
                    "new_runs_blocked": True,
                    "availability_reason_code": "workflow_disabled",
                }
            ]
        )
    )
    runtime = SimpleNamespace(
        workflow_operator_service=service,
        project_query_service=service._project_query_service(),
        project_command_service=object(),
        user_query_service=object(),
        user_command_service=object(),
        extension_scaffold_service=object(),
    )
    cli = build_click_app(ServiceRegistry(runtime), SimpleNamespace())
    result = CliRunner().invoke(
        cli, ["workflows", command, "--project-id", "project-own", workflow_id, "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["workflow_id"] == workflow_id
    if command == "validate":
        assert all(
            row["code"] != "workflow.validation_temporarily_unavailable"
            for row in payload["validation"]["errors"]
        )
    assert service._registry_db().get_workflow(workflow_id)["enabled"] is False


@pytest.mark.parametrize("method_name", ["project_workflow_validate", "project_workflow_contract"])
def test_disabled_draft_diagnostics_keep_workflow_scope_restrictions(method_name):
    service = _disabled_workflow_service("draft_workflow")
    scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, allowed_workflow_ids=frozenset({"other"}))
    method = getattr(service, method_name)
    with pytest.raises(ServiceNotFoundError, match="not linked"):
        method("project-own", "draft_workflow", scope=scope)
