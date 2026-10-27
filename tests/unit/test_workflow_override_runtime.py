from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.application.workflows import workflow_override_runtime


def test_override_workflow_id_from_value_prefers_explicit_keys():
    assert (
        workflow_override_runtime.override_workflow_id_from_value(
            {"selected_workflow_id": "weekly_digest"}
        )
        == "weekly_digest"
    )
    assert (
        workflow_override_runtime.override_workflow_id_from_value("deep_research")
        == "deep_research"
    )


_EXECUTION_ID = "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42"


def test_derive_override_original_value_builds_gate_outcome_snapshot():
    bl = MagicMock()
    bl.get_latest_event.return_value = {
        "detail": {
            "action": "reroute",
            "reason": "proofreader blocked",
            "step_id": "quality_review",
        }
    }

    result = workflow_override_runtime.derive_override_original_value(
        job={"id": 9, "thread_id": "thread-1", "meta": {"requested_workflow_id": "ship"}},
        domain="gate_outcome",
        step_execution_id=_EXECUTION_ID,
        workflow_id="flow-1",
        step_id="step-7",
        bl=bl,
        get_thread_state_values=lambda _thread_id: {
            "current_step_id": "ignored-step",
            "workflow_id": "ignored-workflow",
        },
        to_json_obj=lambda value: value,
    )

    assert result == {
        "action": "reroute",
        "step_id": "step-7",
        "step_execution_id": _EXECUTION_ID,
        "workflow_id": "flow-1",
        "target_step": "quality_review",
        "reason": "proofreader blocked",
    }


def test_derive_override_original_value_takes_the_cursor_scope_by_default():
    bl = MagicMock()
    bl.get_latest_event.return_value = None

    result = workflow_override_runtime.derive_override_original_value(
        job={"id": 9, "thread_id": "thread-1", "meta": {}},
        domain="gate_outcome",
        step_execution_id="",
        workflow_id="",
        step_id="",
        bl=bl,
        get_thread_state_values=lambda _thread_id: {
            "workflow_id": "flow-1",
            "current_step_id": "review",
            "run_cursor": {"step_id": "review", "step_execution_id": _EXECUTION_ID},
        },
        to_json_obj=lambda value: value,
    )

    assert result["step_id"] == "review"
    assert result["step_execution_id"] == _EXECUTION_ID
    assert result["workflow_id"] == "flow-1"


def test_derive_override_original_value_never_borrows_the_cursor_execution_for_another_step():
    result = workflow_override_runtime.derive_override_original_value(
        job={"id": 9, "thread_id": "thread-1", "meta": {}},
        domain="gate_outcome",
        step_execution_id="",
        workflow_id="flow-1",
        step_id="earlier_step",
        bl=None,
        get_thread_state_values=lambda _thread_id: {
            "run_cursor": {"step_id": "review", "step_execution_id": _EXECUTION_ID},
        },
        to_json_obj=lambda value: value,
    )

    assert result["step_id"] == "earlier_step"
    assert result["step_execution_id"] is None


def test_latest_runtime_message_id_filters_by_execution_and_ignores_invalid_rows():
    other_execution = "0b0c5f0e-7a51-4c1b-8f8e-2b8c4f9a6d10"
    runtime_state = {
        "agent_messages": [
            {"id": "bad", "step_execution_id": _EXECUTION_ID},
            {"id": 7, "step_execution_id": _EXECUTION_ID},
            {"id": 11, "step_execution_id": other_execution},
        ]
    }

    assert workflow_override_runtime.latest_runtime_message_id(runtime_state) == "11"
    assert (
        workflow_override_runtime.latest_runtime_message_id(
            runtime_state, step_execution_id=_EXECUTION_ID
        )
        == "7"
    )


def test_runtime_step_scope_id_reads_the_cursor_step():
    assert (
        workflow_override_runtime.runtime_step_scope_id({"current_step_id": "step-2"}) == "step-2"
    )
    assert (
        workflow_override_runtime.runtime_step_scope_id(
            {"run_cursor": {"step_id": "step-3", "step_execution_id": _EXECUTION_ID}}
        )
        == "step-3"
    )
    assert workflow_override_runtime.runtime_step_scope_id({}) == ""


def test_validate_explicit_workflow_override_reloads_registry_when_inline_workflow_missing():
    fake_registry = MagicMock()
    fake_registry.db_store = MagicMock()
    fake_registry.workflows.get_workflow.side_effect = [None, object()]

    routing = SimpleNamespace(workflow_id="weekly_report", classification=None)
    supervisor = MagicMock()
    supervisor.resolve_workflow_routing.return_value = routing

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=fake_registry,
        ),
        patch(
            "core.application.registry.contract_validation.validate_workflow_readiness",
            return_value={"valid": True, "errors": []},
        ),
        patch(
            "core.application.orchestration.state.normalize_orchestrator_state",
            side_effect=lambda state: state,
        ),
        patch(
            "core.application.orchestration.supervisor.OrchestratorSupervisor",
            return_value=supervisor,
        ),
    ):
        workflow_override_runtime.validate_explicit_workflow_override(
            selected_workflow_id="weekly_report",
            selection_source="explicit_request",
            task_summary="Ship the feature cleanly",
            project_name="test-project",
            project_config={
                "project_name": "test-project",
                "project_workflows": [
                    {"workflow_id": "weekly_report", "enabled": True, "is_default": True}
                ],
            },
            build_task_envelope_for_preview=lambda summary: {"task_summary": summary},
        )

    fake_registry.reload_all.assert_called_once()
    supervisor.resolve_workflow_routing.assert_called_once()


def test_validate_explicit_workflow_override_passes_extra_state_to_routing():
    fake_registry = MagicMock()
    fake_registry.db_store = MagicMock()
    fake_registry.workflows.get_workflow.return_value = object()
    captured = {}

    class FakeSupervisor:
        def __init__(self, registry):
            self.registry = registry

        def resolve_workflow_routing(self, envelope, state):
            captured["envelope"] = envelope
            captured["state"] = state
            return SimpleNamespace(workflow_id="alert_triage", classification=None)

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=fake_registry,
        ),
        patch(
            "core.application.registry.contract_validation.validate_workflow_readiness",
            return_value={"valid": True, "errors": []},
        ),
        patch(
            "core.application.orchestration.state.normalize_orchestrator_state",
            side_effect=lambda state: state,
        ),
        patch(
            "core.application.orchestration.supervisor.OrchestratorSupervisor",
            FakeSupervisor,
        ),
    ):
        workflow_override_runtime.validate_explicit_workflow_override(
            selected_workflow_id="alert_triage",
            selection_source="explicit_request",
            task_summary="Incident response smoke",
            project_name="test-project",
            project_config={"project_name": "test-project"},
            build_task_envelope_for_preview=lambda summary: {"task_summary": summary},
            extra_state={
                "task_type": "incident",
                "risk_level": "low",
            },
        )

    assert captured["state"]["task_type"] == "incident"
    assert captured["state"]["risk_level"] == "low"


def test_validate_explicit_workflow_override_retries_once_after_stale_routing_error():
    fake_registry = MagicMock()
    fake_registry.db_store = MagicMock()
    fake_registry.workflows.get_workflow.return_value = object()

    routing = SimpleNamespace(workflow_id="weekly_report", classification=None)
    supervisor = MagicMock()
    supervisor.resolve_workflow_routing.side_effect = [
        RuntimeError("Explicit workflow override 'weekly_report' was not found"),
        routing,
    ]

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=fake_registry,
        ),
        patch(
            "core.application.registry.contract_validation.validate_workflow_readiness",
            return_value={"valid": True, "errors": []},
        ),
        patch(
            "core.application.orchestration.state.normalize_orchestrator_state",
            side_effect=lambda state: state,
        ),
        patch(
            "core.application.orchestration.supervisor.OrchestratorSupervisor",
            return_value=supervisor,
        ),
    ):
        workflow_override_runtime.validate_explicit_workflow_override(
            selected_workflow_id="weekly_report",
            selection_source="explicit_request",
            task_summary="Ship the feature cleanly",
            project_name="test-project",
            project_config={
                "project_name": "test-project",
                "project_workflows": [
                    {"workflow_id": "weekly_report", "enabled": True, "is_default": True}
                ],
            },
            build_task_envelope_for_preview=lambda summary: {"task_summary": summary},
        )

    fake_registry.reload_all.assert_called_once()
    assert supervisor.resolve_workflow_routing.call_count == 2
