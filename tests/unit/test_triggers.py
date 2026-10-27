"""Unit tests for canonical trigger envelope normalization."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.orchestration.task_envelope import TaskEnvelope


class TestTaskEnvelope:
    def test_basic_creation(self):
        env = TaskEnvelope(
            source_system="manual",
            source_event_id="evt-1",
            source_object_id="obj-1",
            task_type="feature",
            title="Add login",
            description="Add login flow",
        )
        assert env.source_system == "manual"
        assert env.source_event_id == "evt-1"
        assert env.source_object_id == "obj-1"
        assert env.task_type == "feature"
        assert env.priority == "medium"
        assert env.entry_path == "manual_ui"

    def test_to_dict_roundtrip(self):
        env = TaskEnvelope(
            envelope_id="env-1",
            entry_path="task_ingress",
            source_system="sample_tasks",
            source_event_id="evt-42",
            source_object_id="task-42",
            task_type="bugfix",
            title="Fix login bug",
            description="Button is broken",
            priority="high",
            project_id="proj-a",
            requested_workflow_id="weekly_digest",
            inputs={"title": "Fix login bug"},
            files=[{"artifact_id": "a1", "filename": "error.log"}],
            metadata={"tag": "bug"},
            entry_context={"workflow_selection_source": "explicit_request"},
        )
        data = env.to_dict()
        reconstructed = TaskEnvelope.from_dict(data)
        assert reconstructed.to_dict() == data


class TestScheduleTrigger:
    @pytest.fixture
    def trigger(self):
        from core.infrastructure.runtime.triggers.schedule_trigger import ScheduleTrigger

        return ScheduleTrigger()

    def test_normalizes_schedule_payload(self, trigger):
        env = trigger.normalize(
            {
                "title": "Weekly cleanup",
                "description": "Run cleanup scripts",
                "priority": "low",
            },
            project_id="proj",
        )
        assert env.source_system == "schedule"
        assert env.entry_path == "schedule"
        assert env.task_type == ""
        assert env.priority == "low"
