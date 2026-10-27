from __future__ import annotations

import pytest

from core.application.management.errors import ServiceConflictError
from core.application.workflows.workflow_availability_service import WorkflowAvailabilityService


class _InstallAvailabilityStub:
    def __init__(self, mapping: dict[str, dict]):
        self._mapping = mapping

    def get_install_availability(self, install_id: str, *, account_id: str) -> dict:
        _ = account_id
        payload = self._mapping.get(install_id)
        if payload is not None:
            return dict(payload)
        return {
            "install_id": install_id,
            "availability_status": "available",
            "availability_reason_code": "none",
            "availability_reason_text": "",
            "new_runs_blocked": False,
            "visible_in_catalog": True,
        }


def test_workflow_availability_returns_not_found_for_missing_workflow() -> None:
    service = WorkflowAvailabilityService(
        get_logger=lambda: None,
        install_availability_service=_InstallAvailabilityStub({}),
        fetch_workflow_row=lambda _workflow_id, _account_id: None,
        list_dependencies=lambda _workflow_id, _account_id: [],
        list_project_connections=lambda _project_id, _account_id: [],
    )

    payload = service.get_workflow_availability("wf-missing", account_id="acc-1")

    assert payload["availability_status"] == "unavailable"
    assert payload["availability_reason_code"] == "workflow_not_found"
    assert payload["new_runs_blocked"] is True


def test_workflow_availability_blocks_when_owner_install_unavailable() -> None:
    service = WorkflowAvailabilityService(
        get_logger=lambda: None,
        install_availability_service=_InstallAvailabilityStub(
            {
                "inst-owner": {
                    "availability_status": "unavailable",
                    "availability_reason_code": "trial_expired",
                    "availability_reason_text": "Install trial has expired.",
                    "new_runs_blocked": True,
                    "visible_in_catalog": True,
                }
            }
        ),
        fetch_workflow_row=lambda _workflow_id, _account_id: {
            "workflow_id": "wf-1",
            "enabled": True,
            "source_install_id": "inst-owner",
        },
        list_dependencies=lambda _workflow_id, _account_id: [],
        list_project_connections=lambda _project_id, _account_id: [],
    )

    payload = service.get_workflow_availability("wf-1", account_id="acc-1")

    assert payload["availability_status"] == "unavailable"
    assert payload["availability_reason_code"] == "trial_expired"
    assert payload["owner_install_id"] == "inst-owner"


def test_workflow_availability_requires_project_connection_when_declared() -> None:
    service = WorkflowAvailabilityService(
        get_logger=lambda: None,
        install_availability_service=_InstallAvailabilityStub({}),
        fetch_workflow_row=lambda _workflow_id, _account_id: {
            "workflow_id": "wf-1",
            "enabled": True,
        },
        list_dependencies=lambda _workflow_id, _account_id: [
            {
                "dependency_kind": "project_connection",
                "dependency_ref": "conn.crm",
                "required_for_execution": True,
            }
        ],
        list_project_connections=lambda _project_id, _account_id: [],
    )

    payload = service.get_workflow_availability("wf-1", account_id="acc-1", project_id=7)

    assert payload["availability_status"] == "unavailable"
    assert payload["availability_reason_code"] == "missing_project_connection"
    assert payload["missing_connections"] == ["conn.crm"]


def test_global_workflow_availability_defers_project_connection_readiness() -> None:
    service = WorkflowAvailabilityService(
        get_logger=lambda: None,
        install_availability_service=_InstallAvailabilityStub({}),
        fetch_workflow_row=lambda _workflow_id, _account_id: {
            "workflow_id": "wf-1",
            "enabled": True,
        },
        list_dependencies=lambda _workflow_id, _account_id: [
            {
                "dependency_kind": "project_connection",
                "dependency_ref": "conn.crm",
                "required_for_execution": True,
            }
        ],
        list_project_connections=lambda _project_id, _account_id: [],
    )

    payload = service.get_workflow_availability("wf-1", account_id="acc-1")

    assert payload["availability_status"] == "available"
    assert payload["availability_reason_code"] == "none"
    assert payload["new_runs_blocked"] is False


def test_workflow_availability_blocks_when_dependency_install_is_unavailable() -> None:
    service = WorkflowAvailabilityService(
        get_logger=lambda: None,
        install_availability_service=_InstallAvailabilityStub(
            {
                "inst-dep": {
                    "availability_status": "unavailable",
                    "availability_reason_code": "install_incompatible",
                    "availability_reason_text": "Install runtime is incompatible.",
                    "new_runs_blocked": True,
                    "visible_in_catalog": True,
                }
            }
        ),
        fetch_workflow_row=lambda _workflow_id, _account_id: {
            "workflow_id": "wf-1",
            "enabled": True,
        },
        list_dependencies=lambda _workflow_id, _account_id: [
            {
                "dependency_kind": "extension_install",
                "dependency_ref": "extension.crm",
                "install_id": "inst-dep",
                "required_for_execution": True,
            }
        ],
        list_project_connections=lambda _project_id, _account_id: [],
    )

    payload = service.get_workflow_availability("wf-1", account_id="acc-1")

    assert payload["availability_status"] == "unavailable"
    assert payload["availability_reason_code"] == "blocked_dependency"
    assert payload["blocked_dependencies"] == ["extension.crm"]


def test_workflow_availability_ignores_null_like_child_workflow_dependency() -> None:
    service = WorkflowAvailabilityService(
        get_logger=lambda: None,
        install_availability_service=_InstallAvailabilityStub({}),
        fetch_workflow_row=lambda workflow_id, _account_id: {
            "workflow_id": workflow_id,
            "enabled": True,
        },
        list_dependencies=lambda _workflow_id, _account_id: [
            {
                "dependency_kind": "child_workflow",
                "dependency_ref": "None",
                "required_for_execution": True,
            }
        ],
        list_project_connections=lambda _project_id, _account_id: [],
    )

    payload = service.get_workflow_availability("wf-1", account_id="acc-1")

    assert payload["availability_status"] == "available"
    assert payload["availability_reason_code"] == "none"
    assert payload["blocked_dependencies"] == []


def test_workflow_availability_allows_nested_child_workflow_chain() -> None:
    dependencies = {
        "wf-parent": [
            {
                "dependency_kind": "child_workflow",
                "dependency_ref": "wf-child",
                "required_for_execution": True,
            }
        ],
        "wf-child": [
            {
                "dependency_kind": "child_workflow",
                "dependency_ref": "wf-grandchild",
                "required_for_execution": True,
            }
        ],
        "wf-grandchild": [],
    }
    service = WorkflowAvailabilityService(
        get_logger=lambda: None,
        install_availability_service=_InstallAvailabilityStub({}),
        fetch_workflow_row=lambda workflow_id, _account_id: {
            "workflow_id": workflow_id,
            "enabled": True,
        },
        list_dependencies=lambda workflow_id, _account_id: dependencies.get(workflow_id, []),
        list_project_connections=lambda _project_id, _account_id: [],
    )

    payload = service.get_workflow_availability("wf-parent", account_id="acc-1")

    assert payload["availability_status"] == "available"
    assert payload["availability_reason_code"] == "none"
    assert payload["blocked_dependencies"] == []


def test_workflow_availability_blocks_direct_self_child_workflow_dependency() -> None:
    service = WorkflowAvailabilityService(
        get_logger=lambda: None,
        install_availability_service=_InstallAvailabilityStub({}),
        fetch_workflow_row=lambda workflow_id, _account_id: {
            "workflow_id": workflow_id,
            "enabled": True,
        },
        list_dependencies=lambda _workflow_id, _account_id: [
            {
                "dependency_kind": "child_workflow",
                "dependency_ref": "wf-self",
                "required_for_execution": True,
            }
        ],
        list_project_connections=lambda _project_id, _account_id: [],
    )

    payload = service.get_workflow_availability("wf-self", account_id="acc-1")

    assert payload["availability_status"] == "unavailable"
    assert payload["availability_reason_code"] == "blocked_dependency"
    assert payload["blocked_dependencies"] == ["wf-self"]


def test_workflow_availability_blocks_child_workflow_cycle() -> None:
    dependencies = {
        "wf-parent": [
            {
                "dependency_kind": "child_workflow",
                "dependency_ref": "wf-child",
                "required_for_execution": True,
            }
        ],
        "wf-child": [
            {
                "dependency_kind": "child_workflow",
                "dependency_ref": "wf-parent",
                "required_for_execution": True,
            }
        ],
    }
    service = WorkflowAvailabilityService(
        get_logger=lambda: None,
        install_availability_service=_InstallAvailabilityStub({}),
        fetch_workflow_row=lambda workflow_id, _account_id: {
            "workflow_id": workflow_id,
            "enabled": True,
        },
        list_dependencies=lambda workflow_id, _account_id: dependencies.get(workflow_id, []),
        list_project_connections=lambda _project_id, _account_id: [],
    )

    payload = service.get_workflow_availability("wf-parent", account_id="acc-1")

    assert payload["availability_status"] == "unavailable"
    assert payload["availability_reason_code"] == "blocked_dependency"
    assert payload["blocked_dependencies"] == ["wf-child"]


def test_assert_can_start_workflow_raises_service_conflict_with_payload() -> None:
    service = WorkflowAvailabilityService(
        get_logger=lambda: None,
        install_availability_service=_InstallAvailabilityStub({}),
        fetch_workflow_row=lambda _workflow_id, _account_id: {
            "workflow_id": "wf-1",
            "enabled": False,
        },
        list_dependencies=lambda _workflow_id, _account_id: [],
        list_project_connections=lambda _project_id, _account_id: [],
    )

    with pytest.raises(ServiceConflictError) as exc:
        service.assert_can_start_workflow("wf-1", account_id="acc-1", project_id=12)

    payload = exc.value.payload or {}
    assert payload["workflow_id"] == "wf-1"
    assert payload["availability_reason_code"] == "workflow_disabled"


def test_annotate_workflow_rows_applies_availability_fields() -> None:
    service = WorkflowAvailabilityService(
        get_logger=lambda: None,
        install_availability_service=_InstallAvailabilityStub({}),
        fetch_workflow_row=lambda workflow_id, _account_id: {
            "workflow_id": workflow_id,
            "enabled": True,
        },
        list_dependencies=lambda _workflow_id, _account_id: [],
        list_project_connections=lambda _project_id, _account_id: [],
    )

    rows = service.annotate_workflow_rows(
        [{"workflow_id": "wf-a"}, {"workflow_id": "wf-b"}],
        account_id="acc-1",
        project_id=1,
    )

    assert rows[0]["availability_status"] == "available"
    assert rows[0]["availability_reason_code"] == "none"
    assert rows[1]["new_runs_blocked"] is False


def test_workflow_availability_blocks_missing_direct_and_agent_mcp_tools() -> None:
    workflow = {
        "workflow_id": "wf-mcp-missing",
        "enabled": True,
        "steps": [
            {
                "step_kind": "connector",
                "connector_id": "connector.remote.lookup",
            },
            {"step_kind": "agent", "agent_id": "agent.remote"},
        ],
    }

    class _Registry:
        def get_agent(self, agent_id: str):
            assert agent_id == "agent.remote"
            return {"agent_id": agent_id, "tools": [{"tool_id": "remote.resolve"}]}

        def get_tool(self, _tool_id: str):
            return None

    service = WorkflowAvailabilityService(
        get_logger=_Registry,
        install_availability_service=_InstallAvailabilityStub({}),
        fetch_workflow_row=lambda _workflow_id, _account_id: workflow,
        list_dependencies=lambda _workflow_id, _account_id: [],
        list_project_connections=lambda _project_id, _account_id: [],
    )

    payload = service.get_workflow_availability("wf-mcp-missing", account_id="acc-1")

    assert payload["availability_status"] == "unavailable"
    assert payload["availability_reason_code"] == "missing_or_unavailable_tool"
    assert payload["blocked_dependencies"] == ["remote.lookup", "remote.resolve"]
    with pytest.raises(ServiceConflictError, match="missing or unavailable registry tool"):
        service.assert_can_start_workflow("wf-mcp-missing", account_id="acc-1")
