"""Unit tests for orchestrator/routing_node.py.

Tests focus on decision logic:
- workflow_routing_node: pinned-run guard, supervisor failure fallback, pinned selection
  source preservation, and explicit-only routing updates.
"""

from __future__ import annotations

import types
import unittest.mock as mock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_routing_result(
    *,
    workflow_id="wf-001",
    routing_reason="classifier",
    routing_signals=None,
    reentry_context=None,
):
    """Build a minimal routing result object."""
    cls = types.SimpleNamespace
    classification = cls(
        effort_hint="small",
        task_type="feature",
        risk_level="low",
        confidence=0.9,
        automation_recommendation="auto",
    )
    return cls(
        workflow_id=workflow_id,
        routing_reason=routing_reason,
        routing_signals=routing_signals or [],
        reentry_context=reentry_context or {},
        classification=classification,
    )


# ---------------------------------------------------------------------------
# workflow_routing_node
# ---------------------------------------------------------------------------


class TestWorkflowRoutingNode:
    def _call(self, state, routing_result=None, supervisor_raises=None):
        from core.application.orchestration.routing_node import workflow_routing_node

        prepared_state = {
            "task_envelope": {
                "title": "Route task",
                "description": "Route task description",
                "project_id": "proj-1",
                "inputs": {},
            },
            **dict(state or {}),
        }
        supervisor_mock = mock.MagicMock()
        if supervisor_raises:
            supervisor_mock.resolve_workflow_routing.side_effect = supervisor_raises
        else:
            supervisor_mock.resolve_workflow_routing.return_value = (
                routing_result or _make_routing_result()
            )

        with (
            mock.patch(
                "core.application.orchestration.supervisor.OrchestratorSupervisor",
                return_value=supervisor_mock,
            ),
            mock.patch("core.application.registry.registry_manager.RegistryManager.instance"),
        ):
            return workflow_routing_node(prepared_state)

    # -- guard conditions --------------------------------------------------

    def test_a_pinned_run_is_never_rerouted(self):
        state = {"workflow_definition_ref": {"snapshot_id": "s", "revision": 1, "digest": "d"}}
        result = self._call(state)
        assert result == {}

    def test_supervisor_exception_returns_empty(self):
        state = {"task_summary": "build thing"}
        result = self._call(state, supervisor_raises=RuntimeError("registry not ready"))
        assert result == {}

    def test_explicit_request_override_error_is_not_silently_dropped(self):
        from core.application.orchestration.routing_node import workflow_routing_node

        supervisor_mock = mock.MagicMock()
        supervisor_mock.resolve_workflow_routing.side_effect = RuntimeError(
            "Explicit workflow override 'weekly_report' does not support Micro Task"
        )

        with (
            mock.patch(
                "core.application.orchestration.supervisor.OrchestratorSupervisor",
                return_value=supervisor_mock,
            ),
            mock.patch("core.application.registry.registry_manager.RegistryManager.instance"),
            pytest.raises(RuntimeError, match="does not support Micro Task"),
        ):
            workflow_routing_node(
                {
                    "workflow_selection_source": "explicit_request",
                    "task_summary": "Do a tiny thing",
                    "task_envelope": {
                        "title": "Do a tiny thing",
                        "description": "tiny task",
                        "project_id": "proj-1",
                        "inputs": {},
                    },
                }
            )

    def test_explicit_workflow_error_is_not_silently_dropped(self):
        from core.application.orchestration.routing_node import workflow_routing_node

        supervisor_mock = mock.MagicMock()
        supervisor_mock.resolve_workflow_routing.side_effect = RuntimeError(
            "Explicit workflow override 'weekly_digest' is not linked to this project"
        )

        with (
            mock.patch(
                "core.application.orchestration.supervisor.OrchestratorSupervisor",
                return_value=supervisor_mock,
            ),
            mock.patch("core.application.registry.registry_manager.RegistryManager.instance"),
            pytest.raises(RuntimeError, match="not linked to this project"),
        ):
            workflow_routing_node(
                {
                    "task_summary": "Use operator-selected flow",
                    "workflow_selection_source": "explicit_request",
                    "task_envelope": {
                        "title": "Use operator-selected flow",
                        "description": "explicit workflow run",
                        "project_id": "proj-1",
                        "inputs": {},
                    },
                }
            )

    # -- basic routing update ----------------------------------------------

    def test_routing_populates_workflow_id(self):
        state = {}
        result = self._call(state, routing_result=_make_routing_result(workflow_id="wf-xyz"))
        assert result["workflow_id"] == "wf-xyz"

    def test_routing_populates_classification_fields(self):
        state = {}
        result = self._call(state)
        for key in (
            "effort_hint",
            "risk_level",
            "confidence_score",
        ):
            assert key in result, f"missing key: {key}"
        for key in (
            "task_class_id",
            "task_class_key",
            "task_class_label",
            "task_class_reason",
            "task_class_confidence",
            "maintenance_subtype",
        ):
            assert key not in result, f"unexpected retired key: {key}"

    def test_routing_source_defaults_to_explicit_request(self):
        state = {}
        result = self._call(state)
        assert result["workflow_selection_source"] == "explicit_request"

    # -- selection source pinning ------------------------------------------

    def test_parent_call_source_preserves_selection_source(self):
        state = {"workflow_selection_source": "parent_call"}
        result = self._call(state)
        assert result["workflow_selection_source"] == "parent_call"

    def test_pinned_via_source_value_preserves(self):
        state = {"workflow_selection_source": "explicit_request"}
        result = self._call(state)
        assert result["workflow_selection_source"] == "explicit_request"

    def test_unpinned_source_overwritten(self):
        state = {"workflow_selection_source": "some_other_source"}
        result = self._call(state)
        assert result["workflow_selection_source"] == "explicit_request"

    # -- reentry context fields --------------------------------------------

    def test_reentry_context_fields_populated(self):
        reentry = {
            "resume_parent": True,
            "feedback_type": "review_fail",
            "parent_task_id": "T-100",
            "parent_job_id": 5,
            "parent_workflow_id": "wf-parent",
        }
        state = {}
        result = self._call(
            state,
            routing_result=_make_routing_result(reentry_context=reentry),
        )
        assert "resume_phase" not in result
        assert result["resume_parent"] is True
        assert result["feedback_type"] == "review_fail"
        assert result["parent_task_id"] == "T-100"

    def test_routing_updates_do_not_attach_decision_overrides(self):
        result = self._call({})
        assert "decision_overrides_active" not in result

    def test_routing_requires_explicit_task_envelope(self):
        from core.application.orchestration.routing_node import workflow_routing_node

        with pytest.raises(ValueError, match="requires explicit task_envelope"):
            workflow_routing_node(
                {
                    "task_summary": "Import supplier feed",
                    "project_id": "9",
                    "inputs": {
                        "description": "Parse the latest supplier csv feed",
                        "feed_type": "supplier_csv",
                    },
                }
            )

    def test_explicit_task_envelope_routes_without_state_backfill(self):
        from core.application.orchestration.routing_node import workflow_routing_node

        supervisor_mock = mock.MagicMock()
        supervisor_mock.resolve_workflow_routing.return_value = _make_routing_result()

        with (
            mock.patch(
                "core.application.orchestration.supervisor.OrchestratorSupervisor",
                return_value=supervisor_mock,
            ),
            mock.patch("core.application.registry.registry_manager.RegistryManager.instance"),
        ):
            workflow_routing_node(
                {
                    "task_envelope": {
                        "title": "Import supplier feed",
                        "description": "Parse the latest supplier csv feed",
                        "project_id": "9",
                        "inputs": {"feed_type": "supplier_csv"},
                    },
                }
            )

        task_envelope = supervisor_mock.resolve_workflow_routing.call_args.args[0]
        assert task_envelope["title"] == "Import supplier feed"
        assert task_envelope["description"] == "Parse the latest supplier csv feed"
        assert task_envelope["project_id"] == "9"
        assert task_envelope["inputs"]["feed_type"] == "supplier_csv"
