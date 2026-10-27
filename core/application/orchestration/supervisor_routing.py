# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from __future__ import annotations

from .supervisor_common import *  # noqa: F403,F401  # NOSONAR


class OrchestratorSupervisorRoutingMixin:
    """Workflow routing: validate and hydrate the selected workflow binding."""

    def _classification_from_state(
        self,
        task_envelope: dict,
        state: OrchestratorState | dict[str, Any] | None,
    ) -> TaskClassification | None:
        _ = task_envelope
        from core.application.orchestration.classifier import TaskClassification

        state_dict = state if isinstance(state, dict) else {}
        effort_hint = str(state_dict.get("effort_hint") or "").strip().lower()
        routing_reason = str(state_dict.get("routing_reason") or "").strip()
        has_routing_metadata = bool(
            effort_hint or routing_reason or state_dict.get("confidence_score") is not None
        )
        if not has_routing_metadata:
            return None
        return TaskClassification(
            effort_hint=effort_hint or "standard",
            confidence=float(state_dict.get("confidence_score") or 0.8),
            risk_level=str(state_dict.get("risk_level") or "medium"),
            automation_recommendation=str(
                state_dict.get("automation_recommendation") or "supervised"
            ),
            reasoning="Loaded from existing state routing metadata",
        )

    @staticmethod
    def _build_reentry_context(
        task_envelope: dict,
        state: OrchestratorState | dict[str, Any] | None,
    ) -> dict:
        state_dict = state if isinstance(state, dict) else {}
        metadata = task_envelope.get("metadata") if isinstance(task_envelope, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}

        def _first_non_empty(*values):
            for value in values:
                if isinstance(value, str):
                    if value.strip():
                        return value.strip()
                elif value is not None:
                    return value
            return None

        parent_task_id = _first_non_empty(
            state_dict.get("parent_task_id"),
            metadata.get("parent_task_id"),
            metadata.get("original_task_id"),
        )
        parent_job_id = _first_non_empty(
            state_dict.get("parent_job_id"),
            metadata.get("parent_job_id"),
        )
        parent_workflow_id = _first_non_empty(
            state_dict.get("parent_workflow_id"),
            metadata.get("parent_workflow_id"),
        )
        feedback_type = _first_non_empty(
            state_dict.get("feedback_type"),
            metadata.get("feedback_type"),
        )

        resume_parent = bool(
            state_dict.get("resume_parent")
            or metadata.get("resume_parent")
            or metadata.get("is_feedback")
            or feedback_type
            or parent_task_id
            or parent_job_id
        )

        return {
            "resume_parent": resume_parent,
            "parent_task_id": parent_task_id,
            "parent_job_id": parent_job_id,
            "parent_workflow_id": parent_workflow_id,
            "feedback_type": feedback_type,
        }

    @staticmethod
    def _strip_parent_lineage_context(reentry_context: dict | None) -> dict:
        """Drop lineage-only routing fields when parent lineage data is unavailable."""
        sanitized = dict(reentry_context or {})
        sanitized.update(
            {
                "resume_parent": False,
                "parent_task_id": None,
                "parent_job_id": None,
                "parent_workflow_id": None,
                "feedback_type": "",
            }
        )
        return sanitized

    def resolve_workflow_routing(
        self,
        task_envelope: dict,
        state: OrchestratorState | dict[str, Any] | None = None,
    ) -> RoutingResolution:
        """Validate and hydrate an already-selected workflow binding."""
        from core.application.orchestration.classifier import TaskClassification, classify_task

        state_dict = state if isinstance(state, dict) else {}
        project_config = state_dict.get("project_config") or {}
        kb_context = state_dict.get("kb_context") or ""
        envelope = task_envelope or {}
        metadata: dict[str, Any] = (
            envelope.get("metadata") if isinstance(envelope.get("metadata"), dict) else {}
        )
        envelope_source = (
            str(
                metadata.get("source")
                or envelope.get("source")
                or state_dict.get("task_source")
                or ""
            )
            .strip()
            .lower()
        )

        classification = self._classification_from_state(envelope, state_dict)
        if classification is None:
            classification = classify_task(envelope, kb_context, project_config)

        reentry_context = self._build_reentry_context(envelope, state_dict)
        selection_source = (
            str(
                state_dict.get("workflow_selection_source")
                or (envelope.get("entry_context") or {}).get("workflow_selection_source")
                or (envelope.get("metadata") or {}).get("workflow_selection_source")
                or ""
            )
            .strip()
            .lower()
        )
        test_mode = bool(
            state_dict.get("test_mode")
            or state_dict.get("debug_test_run")
            or metadata.get("test_mode")
            or envelope_source in {"workflow_test_run", "step_test_run"}
        )
        internal_parent_call_pinned = selection_source == "parent_call"
        requested_workflow_id = str(
            state_dict.get("workflow_id") or envelope.get("requested_workflow_id") or ""
        ).strip()
        explicit_override = bool(requested_workflow_id)
        if not selection_source and explicit_override:
            selection_source = "explicit_request"
        routing_reason = "workflow_binding"
        routing_signals: dict[str, object] = {
            "selection_source": selection_source or "explicit_request",
            "explicit_override": bool(explicit_override),
            "requested_workflow_id": requested_workflow_id,
        }
        if isinstance(reentry_context, dict):
            routing_signals["reentry_context"] = {
                "resume_parent": bool(reentry_context.get("resume_parent")),
                "parent_job_id": reentry_context.get("parent_job_id"),
                "parent_task_id": reentry_context.get("parent_task_id"),
                "parent_workflow_id": reentry_context.get("parent_workflow_id"),
            }

        workflow = None
        workflow_binding_reason = ""
        workflow_binding_detail: dict = {}
        if internal_parent_call_pinned and requested_workflow_id:
            workflow = self.registry.workflows.get_workflow(requested_workflow_id)
            if workflow is None:
                raise RuntimeError(
                    f"Child workflow '{requested_workflow_id}' requested by parent_call was not found"
                )
            if not bool(getattr(workflow, "enabled", True)) and not test_mode:
                raise RuntimeError(
                    f"Child workflow '{requested_workflow_id}' requested by parent_call is disabled"
                )
            workflow_binding_reason = "parent_call_pinned_workflow"
            workflow_binding_detail = {
                "selected_workflow_id": requested_workflow_id,
                "selection_source": "parent_call",
            }
        if workflow is None:
            if not requested_workflow_id:
                raise RuntimeError("workflow_id is required before orchestration begins")
            workflow = self.registry.workflows.get_workflow(requested_workflow_id)
            if workflow is None:
                raise RuntimeError(
                    f"Explicit workflow override '{requested_workflow_id}' was not found"
                )
            if not bool(getattr(workflow, "enabled", True)) and not test_mode:
                raise RuntimeError(
                    f"Explicit workflow override '{requested_workflow_id}' is disabled"
                )
            if selection_source != "parent_call":
                project_links_resolver = getattr(
                    self.registry.workflows, "_linked_workflow_ids_for_project", None
                )
                visible_resolver = getattr(
                    self.registry.workflows, "_is_workflow_visible_to_project", None
                )
                linked_workflow_ids = set()
                if callable(project_links_resolver):
                    linked_result = project_links_resolver(project_config)
                    if isinstance(linked_result, (set, list, tuple)):
                        linked_workflow_ids = {
                            str(item or "").strip()
                            for item in linked_result
                            if str(item or "").strip()
                        }
                if linked_workflow_ids and requested_workflow_id not in linked_workflow_ids:
                    raise RuntimeError(
                        f"Explicit workflow override '{requested_workflow_id}' is not linked to this project"
                    )
                if (
                    callable(visible_resolver)
                    and isinstance(project_config, dict)
                    and project_config
                    and not visible_resolver(workflow, project_config)
                ):
                    raise RuntimeError(
                        f"Explicit workflow override '{requested_workflow_id}' is not visible for this project"
                    )
            workflow_binding_reason = "explicit_workflow_binding"
            workflow_binding_detail = {
                "selected_workflow_id": requested_workflow_id,
                "selection_source": selection_source or "explicit_request",
            }

        if workflow_binding_reason:
            routing_reason = f"{routing_reason};workflow_selection={workflow_binding_reason}"
            routing_signals = {
                **routing_signals,
                "workflow_selection_reason": workflow_binding_reason,
                "workflow_selection_detail": workflow_binding_detail,
            }
        return RoutingResolution(
            classification=TaskClassification(
                effort_hint=classification.effort_hint,
                confidence=classification.confidence,
                risk_level=classification.risk_level,
                automation_recommendation=classification.automation_recommendation,
                reasoning=classification.reasoning,
            ),
            workflow=workflow,
            workflow_id=str(getattr(workflow, "workflow_id", "") or ""),
            routing_reason=routing_reason,
            routing_signals=routing_signals,
            reentry_context=reentry_context,
        )


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
