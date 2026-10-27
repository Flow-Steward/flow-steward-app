from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _import_common_module():
    fake_model_provider = ModuleType("core.model_provider")
    fake_model_provider.get_chat_model = lambda *args, **kwargs: None

    fake_prompt_templates = ModuleType("core.prompt_templates")
    fake_prompt_templates.build_langchain_prompt = lambda *args, **kwargs: ""

    with patch.dict(
        sys.modules,
        {
            "core.model_provider": fake_model_provider,
            "core.prompt_templates": fake_prompt_templates,
        },
    ):
        return importlib.import_module("core.application.orchestration.agents.common")


class _FakeGraph:
    def __init__(self, snapshots):
        self._snapshots = snapshots
        self.captured_input = None

    def stream(self, input_or_command, config, stream_mode="values"):
        self.captured_input = input_or_command
        yield from self._snapshots


class _FakeLogger:
    def __init__(self):
        self.runtime_updates = []

    def update_job_runtime(self, **kwargs):
        self.runtime_updates.append(kwargs)

    def get_job(self, _job_id):
        return {"status": "in_progress"}


def test_run_graph_with_cancel_check_normalizes_dict_state():
    common = _import_common_module()
    graph = _FakeGraph([{"status": "completed"}])

    result = common.run_graph_with_cancel_check(
        graph,
        {
            "task_id": "TASK-1",
            "task_summary": "Normalize state",
            "project_config": {"project_policy": {"max_retries": 2}},
        },
        config={"configurable": {"thread_id": "t1"}},
    )

    assert result["status"] == "completed"
    assert isinstance(graph.captured_input, dict)
    assert graph.captured_input["project_policy"] == {"max_retries": 2}
    assert graph.captured_input["task_source"] == "manual"
    assert "mode" not in graph.captured_input
    assert "complexity" not in graph.captured_input
    assert graph.captured_input["workflow_path"] == []


def test_run_graph_with_cancel_check_passes_non_dict_command_unchanged():
    common = _import_common_module()
    graph = _FakeGraph([{"status": "completed"}])
    command_obj = object()

    common.run_graph_with_cancel_check(
        graph,
        command_obj,
        config={"configurable": {"thread_id": "t2"}},
    )

    assert graph.captured_input is command_obj


def test_run_graph_with_cancel_check_updates_runtime_from_snapshots():
    common = _import_common_module()
    graph = _FakeGraph(
        [
            {
                "status": "in_progress",
                "current_step_id": "analysis",
                "current_group_id": "per_topic",
                "current_agent": "researcher",
                "workflow_id": "wf-1",
                "error": "phase failed",
            },
            {
                "status": "completed",
                "current_step_id": "delivery",
                "current_group_id": None,
                "current_agent": "writer",
                "workflow_id": "wf-1",
                "error": None,
            },
        ]
    )
    bl = _FakeLogger()

    common.run_graph_with_cancel_check(
        graph,
        {
            "task_id": "TASK-2",
            "task_summary": "Runtime updates",
            "project_config": {"project_policy": {"max_retries": 2}},
        },
        config={"configurable": {"thread_id": "t3"}},
        bl=bl,
        job_id=77,
    )

    assert len(bl.runtime_updates) == 2
    first = bl.runtime_updates[0]
    second = bl.runtime_updates[1]
    assert first["selected_workflow"] == "wf-1"
    assert first["current_step_id"] == "analysis"
    assert first["current_group_id"] == "per_topic"
    assert first["current_agent"] == "researcher"
    assert first["last_error"] == "phase failed"
    assert first["clear_last_error"] is False
    assert second["current_step_id"] == "delivery"
    assert second["current_group_id"] is None
    assert second["current_agent"] == "writer"
    assert second["clear_last_error"] is True


def test_emit_event_queues_payload_when_logger_write_fails():
    common = _import_common_module()

    class _FailingLogger:
        def __init__(self):
            self.replay_called = 0
            self.queued_payload = None
            self.queue_reason = ""

        def replay_queued_events(self, max_events=200):
            self.replay_called += 1
            return 0

        def log_event(self, **kwargs):
            return False

        def queue_event_for_retry(self, payload, failure_reason=""):
            self.queued_payload = payload
            self.queue_reason = failure_reason
            return True

    logger = _FailingLogger()
    with (
        patch.object(common, "_resolve_observability_dsn", return_value="postgres://obs"),
        patch.object(common, "_get_event_logger", return_value=logger),
    ):
        common.emit_event(
            state={"job_id": 123},
            agent="orchestrator",
            event_type="routing_decision",
            title="Route next",
            detail={"phase": "analysis"},
        )

    assert logger.replay_called == 1
    assert logger.queued_payload is not None
    assert logger.queued_payload["job_id"] == "123"
    assert logger.queued_payload["event_type"] == "routing_decision"
    assert logger.queue_reason == "log_event returned false"


def test_emit_event_replays_queued_payloads_on_recovery():
    common = _import_common_module()

    class _CapturingLogger:
        def __init__(self):
            self.replay_called = 0
            self.log_calls = []

        def replay_queued_events(self, max_events=200):
            self.replay_called += 1
            return 1

        def log_event(self, **kwargs):
            self.log_calls.append(kwargs)
            return True

        def queue_event_for_retry(self, payload, failure_reason=""):
            raise AssertionError("queue_event_for_retry should not be called on successful write")

    logger = _CapturingLogger()
    with (
        patch.object(common, "_resolve_observability_dsn", return_value="postgres://obs"),
        patch.object(common, "_get_event_logger", return_value=logger),
    ):
        common.emit_event(
            state={"job_id": 322},
            agent="writer",
            event_type="phase_complete",
            title="new event",
            detail={"ok": True},
        )

    assert logger.replay_called == 1
    assert len(logger.log_calls) == 1
    assert logger.log_calls[0]["job_id"] == "322"


def test_emit_event_replays_queued_payloads_on_cooldown():
    common = _import_common_module()

    class _CapturingLogger:
        def __init__(self):
            self.replay_calls = []
            self.log_calls = []

        def replay_queued_events(self, max_events=200):
            self.replay_calls.append(max_events)
            return 0

        def log_event(self, **kwargs):
            self.log_calls.append(kwargs)
            return True

        def queue_event_for_retry(self, payload, failure_reason=""):
            raise AssertionError("queue_event_for_retry should not be called on successful write")

    logger = _CapturingLogger()
    common._event_replay_next_attempt_by_logger.pop(id(logger), None)
    with (
        patch.object(common, "_resolve_observability_dsn", return_value="postgres://obs"),
        patch.object(common, "_get_event_logger", return_value=logger),
    ):
        common.emit_event(
            state={"job_id": 322},
            agent="writer",
            event_type="phase_complete",
            title="first event",
            detail={"ok": True},
        )
        common.emit_event(
            state={"job_id": 322},
            agent="writer",
            event_type="phase_complete",
            title="second event",
            detail={"ok": True},
        )

    assert logger.replay_calls == [common._EVENT_REPLAY_MAX_BATCH]
    assert len(logger.log_calls) == 2


def test_emit_event_queues_payload_when_log_event_raises():
    common = _import_common_module()

    class _ExplodingLogger:
        def __init__(self):
            self.queued_payload = None
            self.queue_reason = ""

        def replay_queued_events(self, max_events=200):
            return 0

        def log_event(self, **kwargs):
            raise RuntimeError("db unavailable")

        def queue_event_for_retry(self, payload, failure_reason=""):
            self.queued_payload = payload
            self.queue_reason = failure_reason
            return True

    logger = _ExplodingLogger()
    with (
        patch.object(common, "_resolve_observability_dsn", return_value="postgres://obs"),
        patch.object(common, "_get_event_logger", return_value=logger),
    ):
        common.emit_event(
            state={"job_id": 456},
            agent="checker",
            event_type="error",
            title="Failed to validate",
            detail={"step": "contract"},
            severity="error",
        )

    assert logger.queued_payload is not None
    assert logger.queued_payload["job_id"] == "456"
    assert "db unavailable" in logger.queue_reason


def test_emit_event_scans_detail_with_project_and_job_scope():
    common = _import_common_module()

    class _CapturingLogger:
        def replay_queued_events(self, max_events=200):
            return 0

        def log_event(self, **kwargs):
            return True

        def queue_event_for_retry(self, payload, failure_reason=""):
            return True

    logger = _CapturingLogger()
    with (
        patch.object(common, "_resolve_observability_dsn", return_value="postgres://obs"),
        patch.object(common, "_get_event_logger", return_value=logger),
        patch(
            "core.infrastructure.security.integration.scan_outbound",
            return_value=SimpleNamespace(action="allow", redacted_text='{"secret":"[REDACTED]"}'),
        ) as scan,
    ):
        common.emit_event(
            state={"job_id": 456, "project_config": {"project_id": 12}},
            agent="checker",
            event_type="error",
            title="Failed to validate",
            detail={"secret": "token"},
            severity="error",
        )

    kwargs = scan.call_args.kwargs
    assert kwargs["project_id"] == "12"
    assert kwargs["job_id"] == "456"
    assert kwargs["context_type"] == "display"


def test_emit_event_preserves_canonical_registry_id_and_redacts_dotted_token():
    common = _import_common_module()
    from core.infrastructure.security.secret_detection import redact_detected_secrets

    class _CapturingLogger:
        def __init__(self):
            self.log_calls = []

        def replay_queued_events(self, max_events=200):
            return 0

        def log_event(self, **kwargs):
            self.log_calls.append(kwargs)
            return True

        def queue_event_for_retry(self, payload, failure_reason=""):
            raise AssertionError("queue_event_for_retry should not be called on successful write")

    logger = _CapturingLogger()
    canonical = "extension.5cc90460-491e-45e3-a47c-c1110afc37db.demo_workflow"
    dotted_token = "stripe.abcdefghijklmnopqrstuvwxyz123456"

    def _scan(value, *_args, **_kwargs):
        return SimpleNamespace(
            action="redact",
            redacted_text=redact_detected_secrets(value),
        )

    with (
        patch.object(common, "_resolve_observability_dsn", return_value="postgres://obs"),
        patch.object(common, "_get_event_logger", return_value=logger),
        patch("core.infrastructure.security.integration.scan_outbound", side_effect=_scan),
    ):
        common.emit_event(
            state={"job_id": 456, "project_config": {"project_id": 12}},
            agent="orchestrator",
            event_type="routing_decision",
            title="Invoke selected workflow",
            detail={
                "workflow_id": canonical,
                "provider_debug": dotted_token,
            },
        )

    persisted_detail = logger.log_calls[0]["detail"]
    assert persisted_detail["workflow_id"] == canonical
    assert dotted_token not in persisted_detail["provider_debug"]
    assert "dotted_provider_token" in persisted_detail["provider_debug"]


def test_publish_agent_message_uses_message_bus():
    common = _import_common_module()

    class _Bus:
        def __init__(self):
            self.calls = []

        def publish(self, job_id, envelope, *, project_id=None):
            self.calls.append((job_id, envelope, project_id))
            return True

    bus = _Bus()
    with patch("core.application.orchestration.message_bus.MessageBus.for_state", return_value=bus):
        common.publish_agent_message(
            state={"job_id": 73},
            envelope={"schema_version": "agent_message_v1"},
        )

    assert len(bus.calls) == 1
    assert bus.calls[0][0] == "73"
    assert bus.calls[0][2] is None


def test_publish_agent_message_forwards_project_scope_from_state():
    common = _import_common_module()

    class _Bus:
        def __init__(self):
            self.calls = []

        def publish(self, job_id, envelope, *, project_id=None):
            self.calls.append((job_id, envelope, project_id))
            return True

    bus = _Bus()
    with patch("core.application.orchestration.message_bus.MessageBus.for_state", return_value=bus):
        common.publish_agent_message(
            state={"job_id": 73, "project_config": {"project_id": 9}},
            envelope={"schema_version": "agent_message_v1"},
        )

    assert len(bus.calls) == 1
    assert bus.calls[0][2] == "9"
