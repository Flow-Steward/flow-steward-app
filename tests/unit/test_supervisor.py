"""
Unit tests for orchestrator/supervisor.py — OrchestratorSupervisor.

The supervisor only routes: ``resolve_workflow_routing`` validates and hydrates
the explicitly selected workflow binding (classification, re-entry lineage,
project visibility, test-mode override). It plans nothing; the run executes the
pinned definition through the step hub.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.application.orchestration.classifier import TaskClassification
from core.application.orchestration.supervisor import OrchestratorSupervisor

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _workflow(workflow_id: str, *, enabled: bool = True, tags: list[str] | None = None):
    workflow = MagicMock()
    workflow.workflow_id = workflow_id
    workflow.enabled = enabled
    workflow.tags = list(tags or [])
    workflow.policies = {}
    workflow.settings = {"policies": {}}
    return workflow


def make_mock_registry(workflow_id: str = "weekly_report"):
    """A RegistryManager mock whose workflow lookup returns one enabled workflow."""
    registry = MagicMock()
    workflow = _workflow(workflow_id)
    registry.workflows.find_by_trigger.return_value = workflow
    registry.workflows.get_workflow.return_value = workflow
    return registry


def _serve(registry, *workflows) -> None:
    """Let the registry return exactly *workflows* by id (None for anything else)."""
    by_id = {workflow.workflow_id: workflow for workflow in workflows}
    registry.workflows.get_workflow.side_effect = by_id.get


def make_state(**overrides) -> dict:
    """Minimal orchestrator state dict."""
    base = {
        "task_id": "test-001",
        "task_summary": "Fix broken login",
        "task_source": "manual",
        "project_name": "test-project",
        "project_config": {},
        "iteration_count": 0,
        "agent_messages": [],
        "user_approved": None,
        "review_feedback": None,
        "status": "in_progress",
        "kb_context": "",
        "workflow_id": "weekly_report",
        "workflow_selection_source": "explicit_request",
    }
    base.update(overrides)
    return base


def make_envelope(**overrides) -> dict:
    base = {
        "source": "manual",
        "source_id": "001",
        "task_type": "feature",
        "title": "Test",
        "description": "Test",
        "priority": "medium",
        "project_id": "proj",
        "metadata": {},
        "trigger_config": {},
    }
    base.update(overrides)
    return base


def classified(**overrides) -> TaskClassification:
    fields: dict[str, Any] = {
        "effort_hint": "standard",
        "confidence": 0.9,
        "risk_level": "low",
        "automation_recommendation": "supervised",
        "reasoning": "test",
        "task_type": "feature",
    }
    fields.update(overrides)
    return TaskClassification(**fields)


def route(registry, envelope: dict, state: dict, classification: TaskClassification | None = None):
    supervisor = OrchestratorSupervisor(registry)
    with patch(
        "core.application.orchestration.classifier.classify_task",
        return_value=classification or classified(),
    ):
        return supervisor.resolve_workflow_routing(envelope, state)


# ---------------------------------------------------------------------------
# Explicit binding beats classifier shape (no task-class or family selection)
# ---------------------------------------------------------------------------


class TestExplicitBindingSelection:
    def test_uses_explicit_standard_feature_when_workflow_is_present(self):
        registry = make_mock_registry()
        routing = route(
            registry,
            make_envelope(title="Unknown task type", description="Unknown"),
            make_state(workflow_id="weekly_report"),
            classified(confidence=0.5, reasoning="unknown"),
        )

        assert routing.workflow_id == "weekly_report"
        assert "workflow_selection=explicit_workflow_binding" in routing.routing_reason

    def test_ignores_auto_selected_test_workflow_without_family_fallback(self):
        registry = make_mock_registry()
        registry.workflows.find_by_trigger.return_value = _workflow("test_wf", tags=["test"])
        _serve(registry, _workflow("weekly_digest"), _workflow("weekly_report"))

        routing = route(
            registry,
            make_envelope(title="Change Dag crontime", description="Small change"),
            make_state(),
            classified(effort_hint="micro", confidence=0.92, automation_recommendation="auto"),
        )

        assert routing.workflow_id == "weekly_report"
        registry.workflows.find_by_trigger.assert_not_called()

    def test_low_complexity_uses_task_class_selection_without_family_fallback(self):
        registry = make_mock_registry()
        registry.workflows.find_by_trigger.return_value = None
        _serve(registry, _workflow("weekly_digest"), _workflow("weekly_report"))

        routing = route(
            registry,
            make_envelope(title="Tiny update", description="One-line adjustment"),
            make_state(),
            classified(effort_hint="micro", confidence=0.8, automation_recommendation="auto"),
        )

        assert routing.workflow_id == "weekly_report"

    def test_explicit_standard_feature_ignores_classifier_maintenance_shape(self):
        registry = make_mock_registry()
        _serve(
            registry,
            _workflow("maintenance"),
            _workflow("weekly_digest"),
            _workflow("weekly_report"),
        )

        routing = route(
            registry,
            make_envelope(title="Change Dag crontime", description="Small schedule tweak"),
            make_state(),
            classified(
                effort_hint="micro",
                confidence=0.92,
                automation_recommendation="auto",
                task_type="maintenance",
            ),
        )

        assert routing.workflow_id == "weekly_report"

    def test_incident_micro_task_does_not_use_family_shortcut(self):
        registry = make_mock_registry()
        registry.workflows.find_by_trigger.return_value = _workflow("alert_triage")
        _serve(registry, _workflow("weekly_report"))

        routing = route(
            registry,
            make_envelope(task_type="incident", title="P1 incident", priority="high"),
            make_state(),
            classified(
                effort_hint="micro", confidence=0.88, risk_level="medium", task_type="incident"
            ),
        )

        assert routing.workflow_id == "weekly_report"


class TestWorkflowRoutingWithoutFamily:
    def test_classifier_fallback_keeps_explicit_workflow_routing_executable(self):
        """AUDIT-020: an unavailable classifier must not prevent task execution."""
        registry = make_mock_registry()
        supervisor = OrchestratorSupervisor(registry)
        state = make_state()
        state["workflow_id"] = "weekly_report"

        with patch(
            "core.application.orchestration.classifier._classify_with_llm",
            side_effect=RuntimeError("model unavailable"),
        ):
            routing = supervisor.resolve_workflow_routing(
                {"title": "Prepare the weekly digest", "project_id": "proj"}, state
            )

        assert routing.workflow_id == "weekly_report"
        assert routing.classification.reasoning == "default_classification"

    def test_bug_with_parent_linkage_does_not_change_selection_without_task_class_mapping(self):
        registry = make_mock_registry()

        routing = route(
            registry,
            make_envelope(
                source_id="t-1",
                task_type="bugfix",
                title="Fix follow-up regression",
                metadata={"parent_job_id": 99},
            ),
            make_state(parent_job_id=99, resume_parent=True),
            classified(effort_hint="small", risk_level="medium", task_type="bugfix"),
        )

        assert routing.workflow_id == "weekly_report"
        assert routing.reentry_context["parent_job_id"] == 99
        assert routing.reentry_context["resume_parent"] is True
        assert routing.routing_signals["reentry_context"]["parent_job_id"] == 99

    def test_feature_reentry_keeps_task_class_selection(self):
        registry = make_mock_registry()

        routing = route(
            registry,
            make_envelope(
                source_id="t-1c",
                title="Adjust schedule defaults",
                metadata={"parent_job_id": 99},
            ),
            make_state(parent_job_id=99, resume_parent=True),
            classified(confidence=0.92, reasoning="feature fallback"),
        )

        assert routing.workflow_id == "weekly_report"

    def test_self_contained_small_change_routes_quick_fix(self):
        registry = make_mock_registry()

        routing = route(
            registry,
            make_envelope(source_id="t-2", title="Adjust cron schedule text", priority="low"),
            make_state(),
            classified(effort_hint="small", confidence=0.95, automation_recommendation="auto"),
        )

        assert routing.workflow_id == "weekly_report"

    @pytest.mark.parametrize(
        ("effort_hint", "metadata"),
        [("small", {}), ("standard", {"across_codebase": True})],
        ids=["small", "large"],
    )
    def test_maintenance_uses_explicit_maintenance_workflow(self, effort_hint, metadata):
        registry = make_mock_registry()
        _serve(
            registry,
            _workflow("weekly_digest"),
            _workflow("maintenance"),
            _workflow("weekly_report"),
        )

        routing = route(
            registry,
            make_envelope(task_type="maintenance", metadata=metadata),
            make_state(workflow_id="maintenance"),
            classified(effort_hint=effort_hint, task_type="maintenance"),
        )

        assert routing.workflow_id == "maintenance"

    def test_quick_fix_bugfix_uses_registry_step(self):
        registry = make_mock_registry()
        _serve(registry, _workflow("weekly_digest"), _workflow("weekly_report"))

        routing = route(
            registry,
            make_envelope(task_type="bugfix", title="Fix small regression"),
            make_state(workflow_id="weekly_digest"),
            classified(effort_hint="small", automation_recommendation="auto", task_type="bugfix"),
        )

        assert routing.workflow_id == "weekly_digest"

    def test_explicit_standard_feature_uses_registry_step_even_for_quick_fix_classification(self):
        registry = make_mock_registry()
        _serve(registry, _workflow("weekly_digest"), _workflow("weekly_report"))

        routing = route(
            registry,
            make_envelope(task_type="maintenance", title="Small refactor"),
            make_state(workflow_id="weekly_report"),
            classified(
                effort_hint="small", automation_recommendation="auto", task_type="maintenance"
            ),
        )

        assert routing.workflow_id == "weekly_report"

    @pytest.mark.parametrize(
        "policies",
        [{}, {"lightweight_phase_path_enabled": True}],
        ids=["without_opt_in_policy", "with_lightweight_policy"],
    )
    def test_micro_task_routes_the_authored_workflow_unchanged(self, policies):
        """No effort-based reshaping: routing hands back the registry's workflow object."""
        registry = make_mock_registry()
        quick = _workflow("weekly_digest")
        quick.policies = dict(policies)
        quick.settings = {"policies": dict(policies)}
        _serve(registry, quick, _workflow("weekly_report"))

        routing = route(
            registry,
            make_envelope(task_type="bugfix", title="Fix typo in endpoint mapping"),
            make_state(workflow_id="weekly_digest"),
            classified(
                effort_hint="micro",
                confidence=0.97,
                automation_recommendation="auto",
                task_type="bugfix",
            ),
        )

        assert routing.workflow is quick
        assert routing.workflow_id == "weekly_digest"

    def test_incident_signal_routes_incident_response(self):
        registry = make_mock_registry()

        routing = route(
            registry,
            make_envelope(
                task_type="incident",
                title="Production outage",
                priority="critical",
                metadata={"severity": "p1"},
            ),
            make_state(),
            classified(
                effort_hint="major",
                confidence=0.8,
                risk_level="high",
                reasoning="incident",
                task_type="incident",
            ),
        )

        assert routing.workflow_id == "weekly_report"

    def test_investigation_only_routes_spike(self):
        registry = make_mock_registry()

        routing = route(
            registry,
            make_envelope(
                title="Spike: investigate flaky websocket behavior",
                metadata={"report_only": True, "no_code": True},
            ),
            make_state(),
            classified(confidence=0.7, automation_recommendation="auto"),
        )

        assert routing.workflow_id == "weekly_report"


# ---------------------------------------------------------------------------
# Explicit binding validation
# ---------------------------------------------------------------------------


class TestExplicitBindingValidation:
    def test_raises_when_explicit_workflow_is_missing(self):
        registry = make_mock_registry()
        registry.workflows.find_by_trigger.return_value = None
        registry.workflows.get_workflow.return_value = None
        envelope = make_envelope()
        state = make_state(workflow_id="weekly_report")

        with pytest.raises(
            RuntimeError, match="Explicit workflow override 'weekly_report' was not found"
        ):
            route(registry, envelope, state)

    def test_raises_when_workflow_id_missing_before_orchestration(self):
        registry = make_mock_registry()
        envelope = make_envelope()
        state = make_state(workflow_id="", workflow_selection_source="")

        with pytest.raises(
            RuntimeError, match="workflow_id is required before orchestration begins"
        ):
            route(registry, envelope, state)

    def test_uses_requested_workflow_override_when_enabled(self):
        registry = make_mock_registry()
        _serve(registry, _workflow("forced_workflow"), _workflow("weekly_report"))

        routing = route(
            registry,
            make_envelope(title="Force workflow"),
            make_state(workflow_id="forced_workflow"),
        )

        assert routing.workflow_id == "forced_workflow"
        assert routing.routing_signals["explicit_override"] is True
        assert routing.routing_signals["requested_workflow_id"] == "forced_workflow"

    def test_requested_workflow_override_disabled_raises_clear_error(self):
        registry = make_mock_registry()
        _serve(registry, _workflow("forced_workflow", enabled=False), _workflow("weekly_report"))
        envelope = make_envelope()
        state = make_state(workflow_id="forced_workflow")

        with pytest.raises(RuntimeError, match="disabled"):
            route(registry, envelope, state)

    def test_requested_workflow_override_disabled_is_allowed_in_test_mode(self):
        registry = make_mock_registry()
        _serve(registry, _workflow("forced_workflow", enabled=False), _workflow("weekly_report"))

        routing = route(
            registry,
            make_envelope(
                title="Disabled workflow test run", metadata={"source": "workflow_test_run"}
            ),
            make_state(workflow_id="forced_workflow", workflow_selection_source="explicit_request"),
        )

        assert routing.workflow_id == "forced_workflow"

    def test_automation_recommendation_is_carried_in_the_classification(self):
        registry = make_mock_registry()

        routing = route(
            registry,
            make_envelope(title="Autonomy metadata"),
            make_state(),
            classified(confidence=0.91, risk_level="high", automation_recommendation="human_only"),
        )

        assert routing.classification.automation_recommendation == "human_only"
        assert routing.classification.risk_level == "high"


# ---------------------------------------------------------------------------
# Selection sources and overrides
# ---------------------------------------------------------------------------


class TestDecisionOverrides:
    def test_workflow_selection_decision_override_is_ignored(self):
        registry = make_mock_registry()
        _serve(registry, _workflow("weekly_digest"), _workflow("weekly_report"))

        state = make_state(
            decision_overrides_active=[
                {
                    "id": 301,
                    "domain": "workflow_selection",
                    "active": True,
                    "override_value": {"workflow_id": "weekly_digest"},
                    "reason": "Use quick fix flow",
                }
            ]
        )
        routing = route(
            registry,
            make_envelope(source_id="override-1", title="Apply routing override"),
            state,
            classified(reasoning="default route"),
        )

        assert routing.workflow_id == "weekly_report"
        assert "workflow_override=job_decision_override" not in routing.routing_reason

    def test_parent_call_selection_bypasses_task_class_explicit_checks(self):
        registry = make_mock_registry()
        _serve(registry, _workflow("monthly_rollup", tags=["internal"]))

        routing = route(
            registry,
            make_envelope(
                source="invoke_workflow",
                source_id="child-001",
                title="Run child workflow",
                metadata={"workflow_selection_source": "parent_call"},
            ),
            make_state(workflow_id="monthly_rollup", workflow_selection_source="parent_call"),
            classified(effort_hint="small", automation_recommendation="auto"),
        )

        assert routing.workflow_id == "monthly_rollup"
        assert "workflow_selection=parent_call_pinned_workflow" in routing.routing_reason
