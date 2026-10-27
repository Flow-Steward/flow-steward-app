"""Extended tests for canonical task envelope behavior."""

from __future__ import annotations


class TestTaskEnvelope:
    def test_from_dict_canonical_fields(self):
        from core.application.orchestration.task_envelope import TaskEnvelope

        env = TaskEnvelope.from_dict(
            {
                "envelope_id": "env-123",
                "entry_path": "task_ingress",
                "source_system": "sample_tasks",
                "source_event_id": "evt-123",
                "source_object_id": "task-123",
                "task_type": "bugfix",
                "title": "Fix login",
                "description": "Fix login button",
                "priority": "high",
                "project_id": "proj-1",
                "requested_workflow_id": "weekly_digest",
                "inputs": {"title": "Fix login"},
                "files": [{"artifact_id": "a1", "filename": "error.log"}],
                "metadata": {"tag": "bug"},
                "entry_context": {"idempotency_key": "idem-1"},
            }
        )

        assert env.envelope_id == "env-123"
        assert env.source_system == "sample_tasks"
        assert env.source_event_id == "evt-123"
        assert env.requested_workflow_id == "weekly_digest"
        assert env.files[0]["filename"] == "error.log"

    def test_auto_defaults(self):
        from core.application.orchestration.task_envelope import TaskEnvelope

        env = TaskEnvelope(
            source_system="manual",
            title="Do thing",
            description="Do thing",
        )
        assert env.envelope_id
        assert env.priority == "medium"
        assert env.entry_path == "manual_ui"
        assert env.inputs["title"] == "Do thing"

    def test_transport_task_type_is_preserved_as_freeform_text(self):
        from core.application.orchestration.task_envelope import TaskEnvelope

        env = TaskEnvelope(task_type="ops_followup", title="Check queue", description="Check queue")

        assert env.task_type == "ops_followup"
