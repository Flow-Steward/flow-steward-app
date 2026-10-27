"""
Unit tests for generic orchestrator final validation.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def make_state(**overrides) -> dict:
    base = {
        "task_id": "test-001",
        "task_summary": "Fix the login bug",
        "initial_requirements": "Fix the login bug",
        "project_name": "test-project",
        "project_root": "/target",
        "project_config": {},
        "status": "in_progress",
        "workflow_id": "weekly_report",
        "workflow_terminal": {
            "kind": "stop_success",
            "status": "pass",
            "step_id": "workflow_success",
            "step_execution_id": "exec-success-1",
        },
        "agent_messages": [],
        "platform_events": [],
        "thread_id": "thread-001",
    }
    base.update(overrides)
    return base


class _ValidationResult(SimpleNamespace):
    def dump(self) -> dict:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "outputs": self.outputs,
            "artifacts": self.artifacts,
            "interface": self.interface,
        }


class TestValidationNode:
    def _common_patch(
        self,
        *,
        ok: bool = True,
        errors: list[dict] | None = None,
        outputs: dict | None = None,
        artifacts: dict | None = None,
    ):
        fake_common = ModuleType("core.application.orchestration.agents.common")
        fake_common.emit_event = lambda *args, **kwargs: None

        validation_result = _ValidationResult(
            ok=ok,
            errors=errors or [],
            outputs=outputs or {},
            artifacts=artifacts or {},
            interface={"enabled": False},
        )

        class _WorkflowInterfaceService:
            @staticmethod
            def interface_from_settings(_settings):
                return SimpleNamespace(enabled=False)

            @staticmethod
            def validate_outputs(*_args, **_kwargs):
                return validation_result

            @staticmethod
            def validate_outputs_for_workflow_id(*_args, **_kwargs):
                return validation_result

        @contextlib.contextmanager
        def _patched():
            with (
                patch.dict(
                    sys.modules,
                    {"core.application.orchestration.agents.common": fake_common},
                ),
                patch(
                    "core.application.registry.workflow_contract_service.WorkflowInterfaceService",
                    return_value=_WorkflowInterfaceService(),
                ),
            ):
                yield

        return _patched()

    def test_passes_when_terminal_success_and_contract_valid(self):
        from core.application.orchestration.validation_node import orchestrator_validation_node

        state = make_state()

        with self._common_patch(outputs={"final": "ok"}, artifacts={"summary": "done"}):
            result = orchestrator_validation_node(state)

        assert result["status"] == "completed"
        assert result["final_report"] == "Task completed."
        assert result["outputs"] == {"final": "ok"}
        assert result["artifacts"] == {"summary": "done"}
        assert result["workflow_validation_complete"] is True

    def test_fails_when_terminal_success_missing(self):
        from core.application.orchestration.validation_node import orchestrator_validation_node

        state = make_state(
            workflow_terminal={},
            run_cursor={"step_id": "review", "step_execution_id": "exec-review-1", "attempt": 1},
        )

        with self._common_patch():
            result = orchestrator_validation_node(state)

        assert result["status"] == "failed"
        assert result["last_failure_class"] == "workflow_completion_contract"
        assert "stop_success" in result["final_report"]
        assert result["proof_of_work"] == {"terminal_success": False, "current_step_id": "review"}
        assert result["workflow_validation_complete"] is True

    def test_a_stop_fail_terminal_is_not_terminal_success(self):
        from core.application.orchestration.validation_node import orchestrator_validation_node

        state = make_state(
            workflow_terminal={
                "kind": "stop_fail",
                "status": "fail",
                "step_id": "rejected",
                "step_execution_id": "exec-rejected-1",
            }
        )

        with self._common_patch():
            result = orchestrator_validation_node(state)

        assert result["status"] == "failed"
        assert result["last_failure_class"] == "workflow_completion_contract"

    def test_terminal_success_needs_the_execution_that_reached_it(self):
        from core.application.orchestration.validation_node import orchestrator_validation_node

        state = make_state(
            workflow_terminal={"kind": "stop_success", "status": "pass", "step_id": "done"}
        )

        with self._common_patch():
            result = orchestrator_validation_node(state)

        assert result["status"] == "failed"
        assert result["last_failure_class"] == "workflow_completion_contract"

    def test_a_stop_success_message_alone_is_not_terminal_proof(self):
        from datetime import UTC, datetime

        from core.application.orchestration.agent_messages import build_agent_message
        from core.application.orchestration.validation_node import orchestrator_validation_node

        message = build_agent_message(
            from_agent="control_flow_step",
            to_agent="orchestrator",
            skill="control_flow",
            status="pass",
            correlation_id="corr-stop",
            artifacts={"step_kind": "stop_success", "terminal": "pass"},
            published_at=datetime.now(UTC).isoformat(),
            step_id="done",
            step_execution_id="5f0c8f1e-4a1b-4c2d-9e3f-0a1b2c3d4e5f",
            attempt=1,
        )
        state = make_state(workflow_terminal={}, agent_messages=[message])

        with self._common_patch():
            result = orchestrator_validation_node(state)

        assert result["status"] == "failed"
        assert result["last_failure_class"] == "workflow_completion_contract"

    def test_step_test_run_accepts_selected_step_success_without_workflow_terminal(self):
        from core.application.orchestration.validation_node import orchestrator_validation_node

        state = make_state(
            task_source="step_test_run",
            workflow_terminal={},
            workflow_test_run_execution_id="exec-step-1",
            step_test_run_context={
                "workflow_id": "weekly_report",
                "step_id": "store-locations",
                "execution_id": "exec-step-1",
            },
            step_test_run_terminal={
                "status": "pass",
                "step_id": "store-locations",
                "step_execution_id": "exec-store-1",
                "step_kind": "connector",
                "execution_id": "exec-step-1",
            },
            final_report="Connector step 'acme-store' executed via extension",
        )

        with self._common_patch():
            result = orchestrator_validation_node(state)

        assert result["status"] == "completed"
        assert result["proof_of_work"]["step_test_run"]["step_id"] == "store-locations"
        assert result["proof_of_work"]["step_test_run"]["step_execution_id"] == "exec-store-1"

    def test_step_test_run_proof_must_name_the_tested_step(self):
        from core.application.orchestration.validation_node import orchestrator_validation_node

        state = make_state(
            task_source="step_test_run",
            workflow_terminal={},
            workflow_test_run_execution_id="exec-step-1",
            step_test_run_context={
                "workflow_id": "weekly_report",
                "step_id": "store-locations",
                "execution_id": "exec-step-1",
            },
            step_test_run_terminal={
                "status": "pass",
                "step_id": "another-step",
                "step_kind": "connector",
                "execution_id": "exec-step-1",
            },
        )

        with self._common_patch():
            result = orchestrator_validation_node(state)

        assert result["status"] == "failed"
        assert result["last_failure_class"] == "workflow_completion_contract"

    def test_normal_workflow_cannot_use_step_test_run_proof_as_terminal_success(self):
        from core.application.orchestration.validation_node import orchestrator_validation_node

        state = make_state(
            task_source="manual",
            workflow_terminal={},
            step_test_run_terminal={
                "status": "pass",
                "step_id": "store-locations",
                "step_kind": "connector",
            },
        )

        with self._common_patch():
            result = orchestrator_validation_node(state)

        assert result["status"] == "failed"
        assert result["last_failure_class"] == "workflow_completion_contract"

    def test_fails_when_workflow_output_contract_fails(self):
        from core.application.orchestration.validation_node import orchestrator_validation_node

        state = make_state()

        with self._common_patch(
            ok=False,
            errors=[{"message": "Missing required output summary"}],
            outputs={},
            artifacts={},
        ):
            result = orchestrator_validation_node(state)

        assert result["status"] == "failed"
        assert result["last_failure_class"] == "workflow_output_contract"
        assert "Missing required output summary" in result["final_report"]

    def test_skips_when_state_already_terminal_failed(self):
        from core.application.orchestration.validation_node import orchestrator_validation_node

        state = make_state(status="failed", final_report="Task failed earlier")

        with self._common_patch():
            result = orchestrator_validation_node(state)

        assert result["status"] == "failed"
        assert result["final_report"] == "Task failed earlier"
        assert result["workflow_validation_complete"] is True

    def test_stop_success_cannot_mask_unresolved_upstream_step_failure(self):
        from core.application.orchestration.validation_node import orchestrator_validation_node

        unresolved_failure = {
            "status": "fail",
            "step_id": "load_demo_dataset",
            "correlation_id": "corr-dataset-fail",
            "error_code": "dataset_query_failed",
            "message": "Dataset Query failed: Dataset is not assigned to this project",
            "occurrence": 1,
        }
        state = make_state(
            status="in_progress",
            unresolved_step_failure=unresolved_failure,
            workflow_terminal={
                "kind": "stop_success",
                "status": "pass",
                "step_id": "workflow_success",
                "step_execution_id": "exec-success-1",
            },
        )

        with self._common_patch(outputs={"row_count": 0}):
            result = orchestrator_validation_node(state)

        assert result["status"] == "failed"
        assert result["final_report"] == unresolved_failure["message"]
        assert result["last_failure_class"] == "unhandled_step_failure"
        assert result["proof_of_work"] == {
            "terminal_success": False,
            "unresolved_step_failure": unresolved_failure,
        }
        assert result["workflow_validation_complete"] is True

    def test_returns_awaiting_approval_when_gate_paused_before_terminal_success(self):
        from core.application.orchestration.validation_node import orchestrator_validation_node

        state = make_state(
            status="awaiting_approval",
            workflow_terminal={},
            approval_context="Review the generated content before continuing.",
            final_report=None,
        )

        with self._common_patch():
            result = orchestrator_validation_node(state)

        assert result["status"] == "awaiting_approval"
        assert result["final_report"] == "Review the generated content before continuing."

    def test_legacy_evidence_fields_do_not_control_result(self):
        from core.application.orchestration.validation_node import orchestrator_validation_node

        state = make_state(
            files_changed=[],
            checks_run=[],
            checks_skipped=[],
            blocking_issues=["retired issue"],
            developer_verification=None,
            artifacts={
                "files_changed": [],
                "checks_run": [],
                "checks_skipped": [],
                "blocking_issues": ["retired issue"],
            },
        )

        with self._common_patch(outputs={"final": "ok"}):
            result = orchestrator_validation_node(state)

        assert result["status"] == "completed"
        assert "Task completed." in result["final_report"]


def test_a_pinned_run_checks_outputs_against_its_pinned_contract_not_the_registry():
    from core.application.orchestration.run_definition import clear_run_definition_cache
    from core.application.orchestration.validation_node import orchestrator_validation_node
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService
    from tests._step_run_state import pinned_definition, prime

    definition = pinned_definition({"step_id": "workflow_success", "step_kind": "stop_success"})
    definition["settings"] = {"callable": {"pinned": True}}
    fake_common = ModuleType("core.application.orchestration.agents.common")
    fake_common.emit_event = lambda *args, **kwargs: None
    seen: list = []
    real_interface = WorkflowInterfaceService.interface_from_definition

    def spy(self, pinned):
        seen.append(pinned.settings)
        return real_interface(self, pinned)

    def registry_lookup(*_args, **_kwargs):
        raise AssertionError("a pinned run must not read the live workflow contract")

    clear_run_definition_cache()
    try:
        state = make_state(**prime(definition))
        with (
            patch.dict(sys.modules, {"core.application.orchestration.agents.common": fake_common}),
            patch.object(WorkflowInterfaceService, "interface_from_definition", spy),
            patch.object(WorkflowInterfaceService, "interface_for_workflow_id", registry_lookup),
        ):
            result = orchestrator_validation_node(state)
    finally:
        clear_run_definition_cache()

    assert result["status"] == "completed"
    assert seen == [{"callable": {"pinned": True}}]
