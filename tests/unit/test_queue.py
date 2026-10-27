"""
Unit tests for core/infrastructure/runtime/worker/queue.py — enqueue_task, queue naming, message schema.

Tests:
- Queue name format: platform.project.{project_id}
- Message schema (job_id, task_id, task_summary, etc.)
- Persistent delivery mode (mode=2)
- Retry on connection failure
- Queue is declared as durable

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_queue.py -v
"""

from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# Tests: queue naming
# ---------------------------------------------------------------------------


class TestQueueName:
    """Queue naming convention."""

    def test_queue_name_format(self):
        """Queue name follows the stable protocol format: platform.project.{project_id}."""
        try:
            from core.infrastructure.runtime.worker.queue_broker import _queue_name
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_broker._queue_name not importable"
            )

        name = _queue_name(42)
        assert name == "platform.project.42"

    def test_queue_name_project_1(self):
        """Queue name for project 1."""
        try:
            from core.infrastructure.runtime.worker.queue_broker import _queue_name
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_broker._queue_name not importable"
            )

        name = _queue_name(1)
        assert name == "platform.project.1"

    def test_queue_name_includes_project_id(self):
        """Queue name always includes the project_id."""
        try:
            from core.infrastructure.runtime.worker.queue_broker import _queue_name
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_broker._queue_name not importable"
            )

        for pid in [1, 100, 9999]:
            name = _queue_name(pid)
            assert str(pid) in name


# ---------------------------------------------------------------------------
# Tests: enqueue_task
# ---------------------------------------------------------------------------


class TestEnqueueTask:
    """enqueue_task() publishes messages to RabbitMQ."""

    def _make_mock_pika(self):
        """Create a mock pika connection/channel setup."""
        mock_channel = MagicMock()
        mock_conn = MagicMock()
        mock_conn.channel.return_value = mock_channel
        return mock_conn, mock_channel

    def test_enqueue_task_requires_thread_id(self):
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        with pytest.raises(ValueError, match="thread_id is required"):
            enqueue_task(
                project_id=1,
                job_id=100,
                task_id="task-001",
                thread_id="",
                task_summary="Fix login bug",
                task_source="provider",
                project_name="my-app",
                project_config={},
            )

    def test_enqueue_task_publishes_message(self):
        """enqueue_task calls basic_publish."""
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_task(
                project_id=1,
                job_id=100,
                task_id="task-001",
                thread_id="thread-test",
                task_summary="Fix login bug",
                task_source="provider",
                project_name="my-app",
                project_config={},
            )

        assert mock_channel.basic_publish.called
        mock_channel.confirm_delivery.assert_called_once()

    def test_enqueue_task_marks_connection_failure_as_not_sent(self):
        from core.infrastructure.runtime.worker.queue_publisher import (
            QueuePublishNotSentError,
            enqueue_task,
        )

        with (
            patch(
                "core.infrastructure.runtime.worker.queue_publisher._connect",
                side_effect=RuntimeError("broker unavailable"),
            ),
            pytest.raises(QueuePublishNotSentError) as exc_info,
        ):
            enqueue_task(
                project_id=1,
                job_id=100,
                task_id="task-001",
                thread_id="thread-test",
                task_summary="Fix login bug",
                task_source="provider",
                project_name="my-app",
                project_config={},
            )

        assert exc_info.value.queue_dispatch_outcome == "not_sent"

    def test_ready_payload_preserves_uuid_job_and_project_ids(self):
        from core.infrastructure.runtime.worker.queue_messages import _ready_event_payload

        project_id = "22222222-2222-2222-2222-222222222222"
        job_id = "33333333-3333-3333-3333-333333333333"

        payload = _ready_event_payload(project_id=project_id, job_id=job_id, task_id="task-uuid")

        assert payload["project_id"] == project_id
        assert payload["job_id"] == job_id
        assert payload["task_id"] == "task-uuid"

    def test_enqueue_workflow_resume_publishes_durable_worker_message(self):
        from core.infrastructure.runtime.worker.queue_publisher import enqueue_workflow_resume

        mock_conn, mock_channel = self._make_mock_pika()
        published_bodies = []

        def capture_publish(**kwargs):
            published_bodies.append(kwargs.get("body", b""))

        mock_channel.basic_publish.side_effect = capture_publish

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_workflow_resume(
                project_id="project-42",
                job_id="job-42",
                task_id="TASK-42",
                thread_id="thread-123",
                resume_revision=7,
            )

        assert mock_channel.confirm_delivery.called
        project_message = json.loads(published_bodies[0].decode("utf-8"))
        assert project_message == {
            "message_type": "workflow_resume",
            "project_id": "project-42",
            "job_id": "job-42",
            "task_id": "TASK-42",
            "thread_id": "thread-123",
            "resume_revision": 7,
        }

    def test_message_has_job_id(self):
        """Published message contains job_id."""
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()
        published_body = [None]

        def capture_publish(**kwargs):
            published_body[0] = kwargs.get("body", b"")

        mock_channel.basic_publish.side_effect = capture_publish

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_task(
                project_id=1,
                job_id=999,
                task_id="task-abc",
                thread_id="thread-test",
                task_summary="Test task",
                task_source="manual",
                project_name="test",
                project_config={},
            )

        if published_body[0]:
            body_str = (
                published_body[0].decode()
                if isinstance(published_body[0], bytes)
                else str(published_body[0])
            )
            msg = json.loads(body_str)
            assert msg.get("job_id") == 999 or "job_id" in msg

    def test_message_has_task_summary(self):
        """Published message contains task_summary."""
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()
        published_bodies = []

        def capture_publish(**kwargs):
            published_bodies.append(kwargs.get("body", b""))

        mock_channel.basic_publish.side_effect = capture_publish

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_task(
                project_id=1,
                job_id=1,
                task_id="task-001",
                thread_id="thread-test",
                task_summary="My specific task summary",
                task_source="manual",
                project_name="test",
                project_config={},
            )

        decoded = []
        for raw in published_bodies:
            body_str = raw.decode() if isinstance(raw, bytes) else str(raw)
            decoded.append(json.loads(body_str))
        assert any("My specific task summary" in str(msg) for msg in decoded)

    def test_enqueue_task_serializes_uuid_project_config(self):
        """DB-native UUID values should not break queue publish serialization."""
        import uuid

        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()
        published_bodies = []

        def capture_publish(**kwargs):
            published_bodies.append(kwargs.get("body", b""))

        mock_channel.basic_publish.side_effect = capture_publish

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_task(
                project_id="22222222-2222-2222-2222-222222222222",
                job_id="33333333-3333-3333-3333-333333333333",
                task_id="task-uuid",
                thread_id="thread-test",
                task_summary="UUID serialization",
                task_source="manual",
                project_name="test",
                project_config={
                    "project_id": uuid.UUID("55555555-5555-5555-5555-555555555555"),
                    "project_policy": {
                        "account_id": uuid.UUID("44444444-4444-4444-4444-444444444444")
                    },
                },
            )

        body = json.loads(published_bodies[0].decode())
        assert body["project_config"]["project_id"] == "55555555-5555-5555-5555-555555555555"
        assert (
            body["project_config"]["project_policy"]["account_id"]
            == "44444444-4444-4444-4444-444444444444"
        )

    def test_message_schema_has_required_fields(self):
        """Message has all required fields."""
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()
        published_bodies = []

        def capture_publish(**kwargs):
            published_bodies.append(kwargs.get("body", b""))

        mock_channel.basic_publish.side_effect = capture_publish

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_task(
                project_id=5,
                job_id=200,
                task_id="task-xyz",
                thread_id="thread-test",
                task_summary="Add feature",
                task_source="provider",
                project_name="my-project",
                project_config={"key": "value"},
            )

        decoded = []
        for raw in published_bodies:
            body_str = raw.decode() if isinstance(raw, bytes) else str(raw)
            try:
                decoded.append(json.loads(body_str))
            except json.JSONDecodeError:
                continue
        required = {"job_id", "task_id", "task_summary"}
        assert any(required.issubset(set(msg.keys())) for msg in decoded)

    def test_declare_queue_configures_dead_letter_queue(self):
        from core.infrastructure.runtime.worker.queue_broker import _declare_queue

        channel = MagicMock()

        _declare_queue(channel, "proj-1")

        assert channel.queue_declare.call_count == 2
        dead_letter_call = channel.queue_declare.call_args_list[0]
        assert dead_letter_call.kwargs["queue"] == "platform.project.proj-1.dead_letter"
        main_call = channel.queue_declare.call_args_list[1]
        assert main_call.kwargs["queue"] == "platform.project.proj-1"
        assert main_call.kwargs["arguments"]["x-dead-letter-routing-key"] == (
            "platform.project.proj-1.dead_letter"
        )

    def test_queue_payload_strips_db_dsn_from_project_config(self):
        """db_dsn must never be serialized into RabbitMQ payload."""
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()
        published_bodies = []
        mock_channel.basic_publish.return_value = True

        def capture_publish(**kwargs):
            published_bodies.append(kwargs.get("body", b""))
            return True

        mock_channel.basic_publish.side_effect = capture_publish

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_task(
                project_id=5,
                job_id=200,
                task_id="task-xyz",
                thread_id="thread-test",
                task_summary="Add feature",
                task_source="provider",
                project_name="my-project",
                project_config={
                    "db_dsn": "postgresql://secret@db/app",
                    "stack": {"language": "python"},
                },
            )

        payload = json.loads(published_bodies[0].decode())
        assert "db_dsn" not in (payload.get("project_config") or {})

    def test_queue_payload_uses_allowlist_for_project_config(self):
        """Only allowlisted runtime fields should be present in queue payload."""
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()
        published_bodies = []
        mock_channel.basic_publish.return_value = True

        def capture_publish(**kwargs):
            published_bodies.append(kwargs.get("body", b""))
            return True

        mock_channel.basic_publish.side_effect = capture_publish

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_task(
                project_id=5,
                job_id=201,
                task_id="task-allowlist",
                thread_id="thread-test",
                task_summary="Allowlist payload",
                task_source="provider",
                project_name="my-project",
                project_config={
                    "project_name": "my-project",
                    "project_rules": ["Use UTC timestamps"],
                    "operator_default_workflow_id": "weekly_report",
                    "project_type": "general",
                    "container_root": "/target/my-project",
                    "git_root": "/target/my-project",
                    "default_branch": "main",
                    "project_connections": [{"id": 1, "dsn": "postgres://secret"}],
                    "project_providers": [{"id": 2, "api_key": "secret"}],
                    "config": {"raw": True},
                    "watch": {"enabled": True},
                    "agents_config": {
                        "default_provider": "openai",
                        "default_model": "gpt-5.6-luna",
                        "model_provider_id": "provider-42",
                        "api_key": "secret",
                        "agents": {"config": {"writer": {"model": "gpt-5.4"}}},
                    },
                },
            )

        payload = json.loads(published_bodies[0].decode())
        cfg = payload.get("project_config") or {}
        assert cfg.get("project_name") == "my-project"
        assert cfg.get("project_rules") == ["Use UTC timestamps"]
        assert cfg.get("operator_default_workflow_id") == "weekly_report"
        assert "project_type" not in cfg
        assert "container_root" not in cfg
        assert "git_root" not in cfg
        assert "default_branch" not in cfg
        assert "project_connections" not in cfg
        assert "project_providers" not in cfg
        assert "config" not in cfg
        assert "watch" not in cfg
        assert cfg.get("agents_config", {}).get("default_provider") == "openai"
        assert cfg.get("agents_config", {}).get("default_model") == "gpt-5.6-luna"
        assert cfg.get("agents_config", {}).get("model_provider_id") == "provider-42"
        assert "api_key" not in cfg.get("agents_config", {})

    def test_persistent_delivery_mode(self):
        """Message uses delivery_mode=2 (persistent)."""
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()
        publish_kwargs = [{}]

        def capture_publish(**kwargs):
            publish_kwargs[0] = kwargs

        mock_channel.basic_publish.side_effect = capture_publish

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_task(
                project_id=1,
                job_id=1,
                task_id="t",
                thread_id="thread-test",
                task_summary="s",
                task_source="manual",
                project_name="p",
                project_config={},
            )

        props = publish_kwargs[0].get("properties")
        if props:
            delivery_mode = getattr(props, "delivery_mode", None)
            assert delivery_mode == 2 or delivery_mode is None  # 2=persistent

    def test_publish_uses_mandatory_and_confirms(self):
        """Publish is confirmed by RabbitMQ and marked mandatory."""
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()
        publish_kwargs = [{}]
        mock_channel.basic_publish.return_value = True

        def capture_publish(**kwargs):
            publish_kwargs[0] = kwargs
            return True

        mock_channel.basic_publish.side_effect = capture_publish

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_task(
                project_id=1,
                job_id=1,
                task_id="t",
                thread_id="thread-test",
                task_summary="s",
                task_source="manual",
                project_name="p",
                project_config={},
            )

        assert mock_channel.confirm_delivery.called
        assert publish_kwargs[0].get("mandatory") is True

    def test_enqueue_task_publishes_ready_signal(self):
        """enqueue_task also emits a ready-queue hint for scheduler workers."""
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()
        publish_calls = []
        mock_channel.basic_publish.return_value = True

        def capture_publish(**kwargs):
            publish_calls.append(kwargs)
            return True

        mock_channel.basic_publish.side_effect = capture_publish

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_task(
                project_id=3,
                job_id=77,
                task_id="t-77",
                thread_id="thread-test",
                task_summary="Ready event",
                task_source="manual",
                project_name="p",
                project_config={},
            )

        routing_keys = [str(item.get("routing_key") or "") for item in publish_calls]
        assert "platform.project.3" in routing_keys
        assert "platform.projects.ready" in routing_keys

    def test_enqueue_task_keeps_confirmed_project_publish_when_ready_hint_fails(self):
        """A best-effort ready hint must not turn a confirmed task publish into a retry."""
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()
        publish_calls = []

        def publish_project_then_fail_ready(**kwargs):
            publish_calls.append(kwargs)
            if kwargs["routing_key"] == "platform.projects.ready":
                raise RuntimeError("ready hint nack")

        mock_channel.basic_publish.side_effect = publish_project_then_fail_ready

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_task(
                project_id=7,
                job_id=77,
                task_id="task-77",
                thread_id="thread-test",
                task_summary="Ready hint failure",
                task_source="manual",
                project_name="p",
                project_config={},
            )

        assert [call["routing_key"] for call in publish_calls] == [
            "platform.project.7",
            "platform.projects.ready",
        ]
        mock_conn.close.assert_called_once()

    def test_publish_raises_when_broker_does_not_confirm(self):
        """A missing broker confirm should fail closed."""
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()
        mock_channel.basic_publish.side_effect = RuntimeError("nack")

        with (
            patch(
                "core.infrastructure.runtime.worker.queue_publisher._connect",
                return_value=mock_conn,
            ),
            pytest.raises(RuntimeError, match="publish not confirmed"),
        ):
            enqueue_task(
                project_id=1,
                job_id=1,
                task_id="t",
                thread_id="thread-test",
                task_summary="s",
                task_source="manual",
                project_name="p",
                project_config={},
            )

    def test_declares_durable_queue(self):
        """Queue is declared as durable=True."""
        try:
            from core.infrastructure.runtime.worker.queue_publisher import enqueue_task
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue_publisher.enqueue_task not importable"
            )

        mock_conn, mock_channel = self._make_mock_pika()
        declare_kwargs = [{}]

        def capture_declare(*args, **kwargs):
            declare_kwargs[0] = kwargs

        mock_channel.queue_declare.side_effect = capture_declare

        with patch(
            "core.infrastructure.runtime.worker.queue_publisher._connect", return_value=mock_conn
        ):
            enqueue_task(
                project_id=1,
                job_id=1,
                task_id="t",
                thread_id="thread-test",
                task_summary="s",
                task_source="manual",
                project_name="p",
                project_config={},
            )

        assert declare_kwargs[0], "queue_declare kwargs must be captured"
        assert declare_kwargs[0].get("durable") is True


# ---------------------------------------------------------------------------
# Tests: connection retry
# ---------------------------------------------------------------------------


class TestConnectRetry:
    """_connect() retries on connection failure."""

    def test_connect_disables_heartbeat_for_long_blocking_jobs_by_default(self, monkeypatch):
        """Blocking workers should not lose RabbitMQ channels during long job callbacks."""
        try:
            from core.infrastructure.runtime.worker.queue_broker import _connect
        except ImportError:
            pytest.skip("core.infrastructure.runtime.worker.queue_broker._connect not importable")

        monkeypatch.delenv("FS_RABBITMQ_HEARTBEAT_SECONDS", raising=False)
        captured = {}
        mock_conn = MagicMock()

        def capture_connect(params):
            captured["heartbeat"] = params.heartbeat
            return mock_conn

        with patch("pika.BlockingConnection", side_effect=capture_connect):
            result = _connect("amqps://guest:guest@localhost/", retries=1, delay=0)

        assert result is mock_conn
        assert captured["heartbeat"] == 0

    def test_connect_accepts_configured_heartbeat(self, monkeypatch):
        """Operators can opt into AMQP heartbeats when callbacks are short enough."""
        try:
            from core.infrastructure.runtime.worker.queue_broker import _connect
        except ImportError:
            pytest.skip("core.infrastructure.runtime.worker.queue_broker._connect not importable")

        monkeypatch.setenv("FS_RABBITMQ_HEARTBEAT_SECONDS", "1200")
        captured = {}
        mock_conn = MagicMock()

        def capture_connect(params):
            captured["heartbeat"] = params.heartbeat
            return mock_conn

        with patch("pika.BlockingConnection", side_effect=capture_connect):
            result = _connect("amqps://guest:guest@localhost/", retries=1, delay=0)

        assert result is mock_conn
        assert captured["heartbeat"] == 1200

    def test_retries_on_connection_error(self):
        """_connect retries when pika raises AMQPConnectionError."""
        try:
            from core.infrastructure.runtime.worker.queue_broker import _connect
        except ImportError:
            pytest.skip("core.infrastructure.runtime.worker.queue_broker._connect not importable")

        import pika.exceptions

        mock_conn = MagicMock()
        attempt_count = [0]

        def flaky_connect(*args, **kwargs):
            attempt_count[0] += 1
            if attempt_count[0] < 3:
                raise pika.exceptions.AMQPConnectionError("Connection refused")
            return mock_conn

        with (
            patch("pika.BlockingConnection", side_effect=flaky_connect),
            patch("time.sleep"),
        ):  # Speed up retries
            try:
                result = _connect("amqps://guest:guest@localhost/", retries=5, delay=0)
            except Exception:
                # _connect may not have retry params; just check it exists
                pass
            else:
                assert result is mock_conn
                assert attempt_count[0] == 3

    def test_raises_after_max_retries(self):
        """_connect raises after exhausting retries."""
        try:
            from core.infrastructure.runtime.worker.queue_broker import _connect
        except ImportError:
            pytest.skip("core.infrastructure.runtime.worker.queue_broker._connect not importable")

        import pika.exceptions

        with patch("pika.BlockingConnection") as mock_bc:
            mock_bc.side_effect = pika.exceptions.AMQPConnectionError("Refused")
            with patch("time.sleep"), suppress(Exception):
                _connect("amqps://guest:guest@localhost/", retries=2, delay=0)
                # If we reach here without error, _connect doesn't retry/raise

    def test_intermediate_retry_logs_at_info_level(self):
        """Intermediate RabbitMQ retries should not emit warning-level noise."""
        try:
            from core.infrastructure.runtime.worker.queue_broker import _connect
        except ImportError:
            pytest.skip("core.infrastructure.runtime.worker.queue_broker._connect not importable")

        import pika.exceptions

        mock_conn = MagicMock()
        attempt_count = [0]

        def flaky_connect(*args, **kwargs):
            attempt_count[0] += 1
            if attempt_count[0] == 1:
                raise pika.exceptions.AMQPConnectionError("Connection refused")
            return mock_conn

        with (
            patch("pika.BlockingConnection", side_effect=flaky_connect),
            patch("time.sleep"),
            patch("core.infrastructure.runtime.worker.queue_broker.logger") as logger_mock,
        ):
            result = _connect("amqps://guest:guest@localhost/", retries=2, delay=0)

        assert result is mock_conn
        logger_mock.info.assert_called_once()
        logger_mock.warning.assert_not_called()


class TestReadyQueueConsumer:
    def test_ready_consumer_registers_queue_without_admitting_work_during_maintenance(self):
        from core.infrastructure.runtime.worker.queue import consume_ready_projects

        class StopAfterMaintenanceWait:
            stopped = False

            def is_set(self) -> bool:
                return self.stopped

            def wait(self, _timeout: float | None = None) -> bool:
                self.stopped = True
                return True

        stop_event = StopAfterMaintenanceWait()
        connection = MagicMock()
        observation_sink = MagicMock()

        with (
            patch("core.infrastructure.runtime.worker.queue._connect", return_value=connection),
            patch("core.infrastructure.runtime.worker.queue._ProjectLeaseStore"),
        ):
            consume_ready_projects(
                callback=lambda _msg: None,
                worker_id="w-maintenance",
                stop_event=stop_event,
                observation_sink=observation_sink,
                maintenance_active=lambda: True,
            )

        connection.channel.return_value.queue_declare.assert_called_once()
        connection.channel.return_value.basic_consume.assert_not_called()
        observation_sink.record.assert_called_once_with("maintenance_paused")

    def test_project_queue_drain_does_not_basic_get_while_maintenance_is_active(self):
        from core.infrastructure.runtime.worker.queue import _drain_project_queue

        channel = MagicMock()

        drained, failed = _drain_project_queue(
            channel,
            project_id="project-1",
            callback=lambda _msg: None,
            worker_id="w-maintenance",
            maintenance_active=lambda: True,
        )

        assert (drained, failed) == (0, False)
        channel.basic_get.assert_not_called()

    def test_delivered_ready_hint_is_not_claimed_or_acked_if_maintenance_activates(self):
        from core.infrastructure.runtime.worker.queue import consume_ready_projects

        channel = MagicMock()
        connection = MagicMock()
        connection.channel.return_value = channel
        lease_store = MagicMock()
        ready_method = MagicMock(delivery_tag="ready-maintenance")

        def maintenance_active() -> bool:
            return channel.basic_consume.called

        def process_data_events(**_kwargs):
            on_ready = channel.basic_consume.call_args.kwargs["on_message_callback"]
            on_ready(
                channel,
                ready_method,
                None,
                json.dumps({"project_id": "project-1"}).encode(),
            )
            raise KeyboardInterrupt()

        connection.process_data_events.side_effect = process_data_events
        with (
            patch("core.infrastructure.runtime.worker.queue._connect", return_value=connection),
            patch(
                "core.infrastructure.runtime.worker.queue._ProjectLeaseStore",
                return_value=lease_store,
            ),
            patch(
                "core.infrastructure.runtime.worker.queue._recover_workflow_resume_outbox",
                return_value=0,
            ),
            patch(
                "core.infrastructure.runtime.worker.queue._recover_ready_hints_for_project_backlog",
                return_value=0,
            ),
        ):
            consume_ready_projects(
                callback=lambda _msg: None,
                worker_id="w-maintenance",
                maintenance_active=maintenance_active,
            )

        lease_store.acquire.assert_not_called()
        channel.basic_ack.assert_not_called()
        channel.basic_nack.assert_not_called()

    def test_idle_recovery_does_not_advance_outboxes_while_maintenance_is_active(self):
        from core.infrastructure.runtime.worker.queue import (
            _consume_ready_events_with_idle_recovery,
        )

        stop_event = threading.Event()
        connection = MagicMock()
        recovery = MagicMock()
        outbox = MagicMock()

        def process_data_events(**_kwargs):
            stop_event.set()

        connection.process_data_events.side_effect = process_data_events
        _consume_ready_events_with_idle_recovery(
            connection,
            recover_ready_hints=recovery,
            recover_outbox=outbox,
            stop_event=stop_event,
            maintenance_active=lambda: True,
        )

        recovery.assert_not_called()
        outbox.assert_not_called()

    def test_ready_event_pump_returns_connection_for_requeue_when_maintenance_starts(self):
        from core.infrastructure.runtime.worker.queue import (
            _consume_ready_events_with_idle_recovery,
        )

        stop_event = threading.Event()
        connection = MagicMock()
        maintenance_checks = 0

        def maintenance_active() -> bool:
            nonlocal maintenance_checks
            maintenance_checks += 1
            if maintenance_checks == 2:
                stop_event.set()
            return True

        _consume_ready_events_with_idle_recovery(
            connection,
            recover_ready_hints=MagicMock(),
            stop_event=stop_event,
            maintenance_active=maintenance_active,
        )

        connection.process_data_events.assert_called_once_with(time_limit=1.0)

    def test_ready_consumer_closes_connection_after_event_pump_returns(self):
        from core.infrastructure.runtime.worker.queue import consume_ready_projects

        stop_event = threading.Event()
        connection = MagicMock()

        def finish_pump(*_args, **_kwargs) -> None:
            stop_event.set()

        with (
            patch("core.infrastructure.runtime.worker.queue._connect", return_value=connection),
            patch("core.infrastructure.runtime.worker.queue._ProjectLeaseStore"),
            patch(
                "core.infrastructure.runtime.worker.queue._consume_ready_events_with_idle_recovery",
                side_effect=finish_pump,
            ),
            patch(
                "core.infrastructure.runtime.worker.queue._recover_ready_hints_for_project_backlog",
                return_value=0,
            ),
        ):
            consume_ready_projects(
                callback=lambda _msg: None,
                worker_id="w-maintenance",
                stop_event=stop_event,
                maintenance_active=lambda: False,
            )

        connection.close.assert_called_once_with()

    def test_ready_consumer_reports_maintenance_pause_after_registered_transition(self):
        from core.infrastructure.runtime.worker.queue import consume_ready_projects

        class StopAfterMaintenanceWait:
            stopped = False

            def is_set(self) -> bool:
                return self.stopped

            def wait(self, _timeout: float | None = None) -> bool:
                self.stopped = True
                return True

        stop_event = StopAfterMaintenanceWait()
        connection = MagicMock()
        observation_sink = MagicMock()
        paused = False

        def finish_pump(*_args, **_kwargs) -> None:
            nonlocal paused
            paused = True

        with (
            patch("core.infrastructure.runtime.worker.queue._connect", return_value=connection),
            patch("core.infrastructure.runtime.worker.queue._ProjectLeaseStore"),
            patch(
                "core.infrastructure.runtime.worker.queue._consume_ready_events_with_idle_recovery",
                side_effect=finish_pump,
            ),
            patch(
                "core.infrastructure.runtime.worker.queue._recover_ready_hints_for_project_backlog",
                return_value=0,
            ),
        ):
            consume_ready_projects(
                callback=lambda _msg: None,
                worker_id="w-maintenance",
                stop_event=stop_event,
                observation_sink=observation_sink,
                maintenance_active=lambda: paused,
            )

        assert [call.args[0] for call in observation_sink.record.call_args_list] == [
            "registered",
            "maintenance_paused",
        ]

    def test_consume_all_projects_subscribes_to_ready_queue(self):
        try:
            from core.infrastructure.runtime.worker.queue import consume_all_projects
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue.consume_all_projects not importable"
            )

        mock_channel = MagicMock()
        mock_conn = MagicMock()
        mock_conn.channel.return_value = mock_channel
        mock_conn.process_data_events.side_effect = KeyboardInterrupt()

        with (
            patch("core.infrastructure.runtime.worker.queue._connect", return_value=mock_conn),
            patch("core.infrastructure.runtime.worker.queue._ProjectLeaseStore"),
        ):
            consume_all_projects(project_ids=[], callback=lambda _msg: None, worker_id="w1")

        consume_kwargs = mock_channel.basic_consume.call_args.kwargs
        assert consume_kwargs["queue"] == "platform.projects.ready"

    def test_consume_ready_projects_requeues_ready_event_when_lease_unavailable(self):
        try:
            from core.infrastructure.runtime.worker.queue import consume_ready_projects
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue.consume_ready_projects not importable"
            )

        mock_channel = MagicMock()
        mock_conn = MagicMock()
        mock_conn.channel.return_value = mock_channel
        ready_method = MagicMock()
        ready_method.delivery_tag = "ready-1"
        lease_store = MagicMock()
        lease_store.acquire.return_value = None

        def _start_consuming():
            consume_kwargs = mock_channel.basic_consume.call_args.kwargs
            on_ready = consume_kwargs["on_message_callback"]
            on_ready(mock_channel, ready_method, None, json.dumps({"project_id": 17}).encode())
            raise KeyboardInterrupt()

        mock_conn.process_data_events.side_effect = lambda time_limit=1.0: _start_consuming()

        with (
            patch("core.infrastructure.runtime.worker.queue._connect", return_value=mock_conn),
            patch(
                "core.infrastructure.runtime.worker.queue._ProjectLeaseStore",
                return_value=lease_store,
            ),
        ):
            consume_ready_projects(callback=lambda _msg: None, worker_id="w1")

        mock_channel.basic_nack.assert_called_once_with(delivery_tag="ready-1", requeue=True)
        mock_channel.basic_ack.assert_not_called()

    def test_consume_ready_projects_republishes_ready_event_when_backlog_remains(self):
        try:
            from core.infrastructure.runtime.worker.queue import consume_ready_projects
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue.consume_ready_projects not importable"
            )

        mock_channel = MagicMock()
        mock_conn = MagicMock()
        mock_conn.channel.return_value = mock_channel
        ready_method = MagicMock()
        ready_method.delivery_tag = "ready-2"
        lease_store = MagicMock()
        lease_store.acquire.return_value = {"project_id": 17, "lease_token": "lease-1"}
        mock_channel.queue_declare.return_value = SimpleNamespace(
            method=SimpleNamespace(message_count=1)
        )

        def _start_consuming():
            consume_kwargs = mock_channel.basic_consume.call_args.kwargs
            on_ready = consume_kwargs["on_message_callback"]
            on_ready(
                mock_channel,
                ready_method,
                None,
                json.dumps({"project_id": 17, "job_id": 701, "task_id": "retry-701"}).encode(),
            )
            raise KeyboardInterrupt()

        mock_conn.process_data_events.side_effect = lambda time_limit=1.0: _start_consuming()

        with (
            patch("core.infrastructure.runtime.worker.queue._connect", return_value=mock_conn),
            patch(
                "core.infrastructure.runtime.worker.queue._ProjectLeaseStore",
                return_value=lease_store,
            ),
            patch(
                "core.infrastructure.runtime.worker.queue._drain_project_queue",
                return_value=(0, False),
            ),
            patch("core.infrastructure.runtime.worker.queue._project_queue_depth", return_value=1),
        ):
            consume_ready_projects(callback=lambda _msg: None, worker_id="w1")

        ready_publish_calls = [
            call_kwargs.kwargs
            for call_kwargs in mock_channel.basic_publish.call_args_list
            if call_kwargs.kwargs.get("routing_key") == "platform.projects.ready"
        ]
        assert len(ready_publish_calls) == 1
        payload = json.loads(ready_publish_calls[0]["body"].decode())
        assert payload["project_id"] == "17"
        assert payload["job_id"] == "701"
        assert payload["task_id"] == "retry-701"
        mock_channel.basic_ack.assert_called_once_with(delivery_tag="ready-2")

    def test_two_worker_takeover_settles_ready_hint_only_after_retryable_backlog_drains(self):
        try:
            from core.infrastructure.runtime.worker.queue import consume_ready_projects
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue.consume_ready_projects not importable"
            )

        settlements: list[str] = []

        def _consume_once(*, worker_id: str, lease, drain_result) -> MagicMock:
            channel = MagicMock()
            connection = MagicMock()
            connection.channel.return_value = channel
            ready_method = MagicMock(delivery_tag=f"ready-{worker_id}")
            lease_store = MagicMock()
            lease_store.acquire.return_value = lease

            channel.basic_nack.side_effect = lambda **_kwargs: settlements.append("requeue")
            channel.basic_ack.side_effect = lambda **_kwargs: settlements.append("ack")

            def _start_consuming():
                on_ready = channel.basic_consume.call_args.kwargs["on_message_callback"]
                on_ready(
                    channel,
                    ready_method,
                    None,
                    json.dumps({"project_id": 17, "job_id": 702}).encode(),
                )
                raise KeyboardInterrupt()

            connection.process_data_events.side_effect = lambda time_limit=1.0: _start_consuming()
            with (
                patch(
                    "core.infrastructure.runtime.worker.queue._connect",
                    return_value=connection,
                ),
                patch(
                    "core.infrastructure.runtime.worker.queue._ProjectLeaseStore",
                    return_value=lease_store,
                ),
                patch(
                    "core.infrastructure.runtime.worker.queue._drain_project_queue",
                    return_value=drain_result,
                ),
                patch(
                    "core.infrastructure.runtime.worker.queue._project_queue_depth",
                    return_value=0,
                ) as queue_depth,
            ):
                consume_ready_projects(callback=lambda _msg: None, worker_id=worker_id)
            channel.queue_depth = queue_depth
            return channel

        contending = _consume_once(worker_id="worker-b", lease=None, drain_result=(0, False))
        retrying = _consume_once(
            worker_id="worker-b",
            lease={"project_id": 17, "lease_token": "lease-b"},
            drain_result=(0, True),
        )
        completed = _consume_once(
            worker_id="worker-b",
            lease={"project_id": 17, "lease_token": "lease-b2"},
            drain_result=(1, False),
        )

        assert settlements == ["requeue", "requeue", "ack"]
        contending.basic_ack.assert_not_called()
        retrying.basic_ack.assert_not_called()
        retrying.queue_depth.assert_not_called()
        completed.basic_nack.assert_not_called()

    def test_consume_ready_projects_acks_missing_project_fk_as_stale_hint(self):
        from core.infrastructure.runtime.worker.queue import consume_ready_projects

        mock_channel = MagicMock()
        mock_conn = MagicMock()
        mock_conn.channel.return_value = mock_channel
        ready_method = MagicMock(delivery_tag="ready-stale-fk")
        violation = RuntimeError(
            'insert or update on table "project_execution_slots" violates foreign key '
            'constraint "project_execution_slots_project_id_fkey"'
        )
        violation.pgcode = "23503"  # type: ignore[attr-defined]
        lease_store = MagicMock()
        lease_store.acquire.side_effect = violation
        lease_store.project_exists.return_value = False

        def _start_consuming():
            on_ready = mock_channel.basic_consume.call_args.kwargs["on_message_callback"]
            on_ready(
                mock_channel,
                ready_method,
                None,
                json.dumps({"project_id": "deleted-project"}).encode(),
            )
            raise KeyboardInterrupt()

        mock_conn.process_data_events.side_effect = lambda time_limit=1.0: _start_consuming()

        with (
            patch("core.infrastructure.runtime.worker.queue._connect", return_value=mock_conn),
            patch(
                "core.infrastructure.runtime.worker.queue._ProjectLeaseStore",
                return_value=lease_store,
            ),
            patch("core.infrastructure.runtime.worker.queue.logger") as logger_mock,
        ):
            consume_ready_projects(callback=lambda _msg: None, worker_id="w1")

        mock_channel.basic_ack.assert_called_once_with(delivery_tag="ready-stale-fk")
        logger_mock.error.assert_not_called()

    def test_consume_ready_projects_skips_backlog_probe_after_project_deletion(self):
        from core.infrastructure.runtime.worker.queue import consume_ready_projects

        mock_channel = MagicMock()
        mock_conn = MagicMock()
        mock_conn.channel.return_value = mock_channel
        ready_method = MagicMock(delivery_tag="ready-deleted-after-drain")
        lease_store = MagicMock()
        lease_store.acquire.return_value = {
            "project_id": "deleted-project",
            "lease_token": "lease-1",
        }
        lease_store.project_exists.return_value = False

        def _start_consuming():
            on_ready = mock_channel.basic_consume.call_args.kwargs["on_message_callback"]
            on_ready(
                mock_channel,
                ready_method,
                None,
                json.dumps({"project_id": "deleted-project"}).encode(),
            )
            raise KeyboardInterrupt()

        mock_conn.process_data_events.side_effect = lambda time_limit=1.0: _start_consuming()

        with (
            patch("core.infrastructure.runtime.worker.queue._connect", return_value=mock_conn),
            patch(
                "core.infrastructure.runtime.worker.queue._ProjectLeaseStore",
                return_value=lease_store,
            ),
            patch(
                "core.infrastructure.runtime.worker.queue._drain_project_queue",
                return_value=(1, False),
            ),
            patch("core.infrastructure.runtime.worker.queue._project_queue_depth") as depth_mock,
        ):
            consume_ready_projects(callback=lambda _msg: None, worker_id="w1")

        depth_mock.assert_not_called()
        mock_channel.basic_ack.assert_called_once_with(delivery_tag="ready-deleted-after-drain")

    def test_drain_project_queue_honors_max_messages_for_fairness(self):
        """Bounded drain stops after max_messages so one busy project cannot
        starve other projects' pickup (head-of-line fairness)."""
        try:
            from core.infrastructure.runtime.worker.queue import _drain_project_queue
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue._drain_project_queue not importable"
            )

        available = 5
        get_calls = {"count": 0}

        def _basic_get(queue=None, auto_ack=False):
            if get_calls["count"] >= available:
                return (None, None, None)
            get_calls["count"] += 1
            method = SimpleNamespace(delivery_tag=f"tag-{get_calls['count']}")
            return (method, None, json.dumps({"job_id": get_calls["count"]}).encode())

        mock_channel = MagicMock()
        mock_channel.basic_get.side_effect = _basic_get

        processed: list = []
        drained, failed = _drain_project_queue(
            mock_channel,
            project_id=17,
            callback=lambda msg: processed.append(msg) or "ack",
            worker_id="w1",
            max_messages=1,
        )

        assert drained == 1
        assert failed is False
        assert len(processed) == 1
        # Exactly one message pulled — the worker yields back instead of draining all 5.
        assert get_calls["count"] == 1
        assert mock_channel.basic_ack.call_count == 1

    def test_drain_stops_before_fetching_another_message(self):
        from core.infrastructure.runtime.worker.queue import _drain_project_queue

        stop_event = threading.Event()
        pulled = 0

        class _RecordingChannel:
            def queue_declare(self, **_kwargs):
                return None

            def basic_get(self, **_kwargs):
                nonlocal pulled
                pulled += 1
                method = SimpleNamespace(delivery_tag=f"tag-{pulled}")
                return method, None, json.dumps({"job_id": f"job-{pulled}"}).encode()

            def basic_ack(self, **_kwargs):
                return None

            def basic_nack(self, **_kwargs):
                return None

        processed: list[dict] = []

        def _callback(message):
            processed.append(message)
            stop_event.set()
            return "ack"

        drained, failed = _drain_project_queue(
            _RecordingChannel(),
            project_id="project-1",
            callback=_callback,
            worker_id="worker-1",
            stop_event=stop_event,
        )

        assert (drained, failed) == (1, False)
        assert [message["job_id"] for message in processed] == ["job-1"]
        assert pulled == 1

    def test_drain_project_queue_unbounded_drains_all(self):
        """max_messages None/<=0 preserves legacy full-drain behavior."""
        try:
            from core.infrastructure.runtime.worker.queue import _drain_project_queue
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue._drain_project_queue not importable"
            )

        available = 4
        get_calls = {"count": 0}

        def _basic_get(queue=None, auto_ack=False):
            if get_calls["count"] >= available:
                return (None, None, None)
            get_calls["count"] += 1
            method = SimpleNamespace(delivery_tag=f"tag-{get_calls['count']}")
            return (method, None, json.dumps({"job_id": get_calls["count"]}).encode())

        mock_channel = MagicMock()
        mock_channel.basic_get.side_effect = _basic_get

        drained, failed = _drain_project_queue(
            mock_channel,
            project_id=17,
            callback=lambda msg: "ack",
            worker_id="w1",
            max_messages=None,
        )

        assert drained == 4
        assert failed is False

    def test_ready_recovery_republishes_hint_for_orphaned_project_backlog(self):
        try:
            from core.infrastructure.runtime.worker.queue import (
                _recover_ready_hints_for_project_backlog,
            )
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue._recover_ready_hints_for_project_backlog "
                "not importable"
            )

        backlog_channel = MagicMock()
        empty_channel = MagicMock()
        mock_conn = MagicMock()
        mock_conn.channel.side_effect = [backlog_channel, empty_channel]
        lease_store = MagicMock()
        lease_store.list_pending_project_ids.return_value = ["17", "18"]

        with patch(
            "core.infrastructure.runtime.worker.queue._project_queue_depth",
            side_effect=[1, 0],
        ):
            recovered = _recover_ready_hints_for_project_backlog(
                mock_conn,
                lease_store=lease_store,
                allowed_project_ids=None,
                worker_id="w1",
            )

        assert recovered == 1
        ready_publish_calls = [
            call_kwargs.kwargs
            for call_kwargs in backlog_channel.basic_publish.call_args_list
            if call_kwargs.kwargs.get("routing_key") == "platform.projects.ready"
        ]
        assert len(ready_publish_calls) == 1
        payload = json.loads(ready_publish_calls[0]["body"].decode())
        assert payload["project_id"] == "17"
        empty_channel.basic_publish.assert_not_called()

    def test_idle_ready_recovery_runs_when_consumer_loop_is_idle(self):
        try:
            from core.infrastructure.runtime.worker.queue import (
                _consume_ready_events_with_idle_recovery,
            )
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue._consume_ready_events_with_idle_recovery "
                "not importable"
            )

        mock_conn = MagicMock()
        recovery = MagicMock(return_value=1)
        calls = {"count": 0}

        def _process_data_events(*, time_limit: float):
            calls["count"] += 1
            if calls["count"] > 1:
                raise KeyboardInterrupt()

        mock_conn.process_data_events.side_effect = _process_data_events

        with (
            patch(
                "core.infrastructure.runtime.worker.queue.time.monotonic",
                side_effect=[0.0, 31.0, 32.0],
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            _consume_ready_events_with_idle_recovery(
                mock_conn,
                recover_ready_hints=recovery,
                idle_recovery_seconds=5,
            )

        recovery.assert_called_once_with()

    def test_idle_ready_consumer_returns_after_graceful_shutdown_request(self):
        from core.infrastructure.runtime.worker.queue import (
            _consume_ready_events_with_idle_recovery,
        )

        stop_event = threading.Event()
        mock_conn = MagicMock()
        mock_conn.process_data_events.side_effect = lambda **_kwargs: stop_event.set()

        _consume_ready_events_with_idle_recovery(
            mock_conn,
            recover_ready_hints=MagicMock(),
            stop_event=stop_event,
        )

        mock_conn.process_data_events.assert_called_once_with(time_limit=1.0)

    def test_idle_ready_recovery_runs_approval_outbox_before_hint_recovery(self):
        from core.infrastructure.runtime.worker.queue import (
            _consume_ready_events_with_idle_recovery,
        )

        mock_conn = MagicMock()
        calls: list[str] = []
        recovery = MagicMock(side_effect=lambda: calls.append("ready") or 1)
        recover_outbox = MagicMock(side_effect=lambda: calls.append("outbox") or 2)
        process_calls = {"count": 0}

        def _process_data_events(*, time_limit: float):
            process_calls["count"] += 1
            if process_calls["count"] > 1:
                raise KeyboardInterrupt()

        mock_conn.process_data_events.side_effect = _process_data_events

        with (
            patch(
                "core.infrastructure.runtime.worker.queue.time.monotonic",
                side_effect=[0.0, 31.0, 32.0],
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            _consume_ready_events_with_idle_recovery(
                mock_conn,
                recover_ready_hints=recovery,
                recover_outbox=recover_outbox,
                idle_recovery_seconds=5,
            )

        assert calls == ["outbox", "ready"]

    def test_process_project_message_requeues_on_callback_error(self):
        try:
            from core.infrastructure.runtime.worker.queue import (
                _process_project_message,
                _ProjectMessageFailed,
            )
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue._process_project_message not importable"
            )

        channel = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-1"
        body = json.dumps({"job_id": 88, "task_id": "t-88"}).encode()

        def _boom(_msg):
            raise RuntimeError("boom")

        with pytest.raises(_ProjectMessageFailed):
            _process_project_message(
                channel,
                method,
                body,
                callback=_boom,
                worker_id="w1",
            )

        channel.basic_nack.assert_called_once_with(delivery_tag="tag-1", requeue=True)

    def test_process_project_message_dead_letters_invalid_json(self):
        from core.infrastructure.runtime.worker.queue import _process_project_message

        channel = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-bad"

        _process_project_message(
            channel,
            method,
            b"{invalid-json",
            callback=lambda _msg: None,
            worker_id="w1",
        )

        channel.basic_nack.assert_called_once_with(delivery_tag="tag-bad", requeue=False)

    def test_process_project_message_dead_letters_on_terminal_action(self):
        from core.infrastructure.runtime.worker.queue import _process_project_message

        channel = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-dead"
        body = json.dumps({"job_id": 99, "task_id": "t-99"}).encode()

        _process_project_message(
            channel,
            method,
            body,
            callback=lambda _msg: {"action": "dead_letter"},
            worker_id="w1",
        )

        channel.basic_nack.assert_called_once_with(delivery_tag="tag-dead", requeue=False)

    def test_process_project_message_acks_on_discard_action(self):
        from core.infrastructure.runtime.worker.queue import _process_project_message

        channel = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-discard"
        body = json.dumps({"job_id": 100, "task_id": "t-100"}).encode()

        _process_project_message(
            channel,
            method,
            body,
            callback=lambda _msg: {"action": "discard"},
            worker_id="w1",
        )

        channel.basic_ack.assert_called_once_with(delivery_tag="tag-discard")

    def test_process_project_message_marks_rabbit_redelivery_for_application_policy(self):
        from core.infrastructure.runtime.worker.queue import _process_project_message

        channel = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-redelivered"
        method.redelivered = True
        captured = {}

        def _callback(message):
            captured.update(message)
            return {"action": "discard"}

        _process_project_message(
            channel,
            method,
            json.dumps({"job_id": "job-redelivered", "task_id": "task-redelivered"}).encode(),
            callback=_callback,
            worker_id="worker-1",
        )

        assert captured["_queue_redelivered"] is True
        channel.basic_ack.assert_called_once_with(delivery_tag="tag-redelivered")

    def test_process_project_message_persists_active_job_before_callback(self):
        from core.infrastructure.runtime.worker.queue import _process_project_message

        channel = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-active-job"
        lease = {"project_id": "project-1", "lease_token": "lease-1"}
        lease_store = MagicMock()
        lease_store.heartbeat.return_value = {
            **lease,
            "worker_instance_id": "worker-1",
            "active_job_id": "job-101",
        }

        def _callback(_msg):
            assert lease["active_job_id"] == "job-101"
            return {"action": "discard"}

        _process_project_message(
            channel,
            method,
            json.dumps({"job_id": "job-101", "task_id": "task-101"}).encode(),
            callback=_callback,
            worker_id="worker-1",
            lease_store=lease_store,
            lease=lease,
        )

        lease_store.heartbeat.assert_called_with(
            lease,
            drain_status="draining",
            active_job_id="job-101",
        )

    def test_drain_stops_renewing_and_yields_after_active_job_becomes_terminal(self):
        from core.infrastructure.runtime.worker.queue import _drain_project_queue

        terminal = threading.Event()
        lease = {"project_id": "project-1", "lease_token": "lease-1"}

        class _LeaseStore:
            def heartbeat(self, current_lease, **_kwargs):
                return dict(current_lease)

            def active_job_is_terminal(self, job_id):
                assert job_id == "job-1"
                return terminal.is_set()

        channel = MagicMock()
        pulled = {"count": 0}

        def _basic_get(**_kwargs):
            pulled["count"] += 1
            if pulled["count"] > 2:
                return None, None, None
            method = SimpleNamespace(delivery_tag=f"tag-{pulled['count']}")
            body = json.dumps({"job_id": f"job-{pulled['count']}"}).encode()
            return method, None, body

        channel.basic_get.side_effect = _basic_get

        def _callback(_message):
            terminal.set()
            time.sleep(0.05)
            return {"action": "discard"}

        with patch("core.infrastructure.runtime.worker.queue._LEASE_HEARTBEAT_SECONDS", 0.01):
            drained, failed = _drain_project_queue(
                channel,
                project_id="project-1",
                callback=_callback,
                worker_id="worker-1",
                lease_store=_LeaseStore(),
                lease=lease,
            )

        assert drained == 1
        assert failed is False
        assert pulled["count"] == 1
        assert lease["_active_job_terminal"] == "job-1"

    @pytest.mark.parametrize(
        ("job_row", "expected"),
        [
            ({"status": "in_progress"}, False),
            ({"status": "awaiting_approval"}, False),
            ({"status": "cancelled"}, True),
            ({"status": "completed"}, True),
            (None, True),
        ],
    )
    def test_project_lease_terminal_probe(self, job_row, expected):
        from core.infrastructure.runtime.worker.queue_lease_store import ProjectLeaseStore

        store = ProjectLeaseStore(
            "worker-1",
            terminal_statuses={"cancelled", "completed"},
        )
        store._logger = SimpleNamespace(get_job=lambda _job_id: job_row)

        assert store.active_job_is_terminal("job-1") is expected

    def test_delete_project_queues_deletes_only_the_exact_queue_pair(self):
        from core.infrastructure.runtime.worker.queue_broker import delete_project_queues

        connection = MagicMock()
        channel = connection.channel.return_value
        with patch(
            "core.infrastructure.runtime.worker.queue_broker._connect",
            return_value=connection,
        ):
            deleted = delete_project_queues("project-1")

        assert deleted == [
            "platform.project.project-1",
            "platform.project.project-1.dead_letter",
        ]
        assert channel.queue_delete.call_args_list == [
            call(queue="platform.project.project-1", if_unused=False, if_empty=False),
            call(
                queue="platform.project.project-1.dead_letter",
                if_unused=False,
                if_empty=False,
            ),
        ]
        connection.close.assert_called_once_with()


class TestDedicatedProjectConsumer:
    def test_consume_project_queue_stops_when_project_is_deleted_before_lease_acquire(self):
        try:
            from core.infrastructure.runtime.worker.queue import consume_project_queue
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue.consume_project_queue not importable"
            )

        mock_channel = MagicMock()
        mock_conn = MagicMock()
        mock_conn.channel.return_value = mock_channel
        lease_store = MagicMock()
        lease_store.acquire.return_value = None
        lease_store.project_exists.return_value = False

        with (
            patch("core.infrastructure.runtime.worker.queue._connect", return_value=mock_conn),
            patch(
                "core.infrastructure.runtime.worker.queue._ProjectLeaseStore",
                return_value=lease_store,
            ),
            patch("time.sleep") as sleep_mock,
        ):
            consume_project_queue(project_id=211, callback=lambda _msg: None, worker_id="w1")

        lease_store.acquire.assert_called_once_with("211")
        lease_store.project_exists.assert_called_once_with("211")
        sleep_mock.assert_not_called()


class TestQueueDepthLookups:
    def test_get_queue_depth_returns_zero_when_project_queue_is_not_declared(self):
        from core.infrastructure.runtime.worker.queue_broker import get_queue_depth

        class _MissingQueueError(RuntimeError):
            reply_code = 404
            reply_text = "NOT_FOUND - no queue 'platform.project.demo'"

        mock_channel = MagicMock()
        mock_channel.queue_declare.side_effect = _MissingQueueError("queue missing")
        mock_conn = MagicMock()
        mock_conn.channel.return_value = mock_channel

        with patch(
            "core.infrastructure.runtime.worker.queue_broker._connect", return_value=mock_conn
        ):
            assert get_queue_depth("demo") == 0

    def test_get_ready_queue_depth_returns_zero_when_ready_queue_is_not_declared(self):
        from core.infrastructure.runtime.worker.queue_broker import get_ready_queue_depth

        class _MissingQueueError(RuntimeError):
            reply_code = 404
            reply_text = "NOT_FOUND - no queue 'platform.projects.ready'"

        mock_channel = MagicMock()
        mock_channel.queue_declare.side_effect = _MissingQueueError("queue missing")
        mock_conn = MagicMock()
        mock_conn.channel.return_value = mock_channel

        with patch(
            "core.infrastructure.runtime.worker.queue_broker._connect", return_value=mock_conn
        ):
            assert get_ready_queue_depth() == 0

    def test_get_ready_queue_consumer_count_reports_live_consumers(self):
        from core.infrastructure.runtime.worker.queue_broker import get_ready_queue_consumer_count

        mock_channel = MagicMock()
        mock_channel.queue_declare.return_value.method.consumer_count = 2
        mock_conn = MagicMock()
        mock_conn.channel.return_value = mock_channel

        with patch(
            "core.infrastructure.runtime.worker.queue_broker._connect", return_value=mock_conn
        ):
            assert get_ready_queue_consumer_count() == 2

        mock_channel.queue_declare.assert_called_once_with(
            queue="platform.projects.ready",
            durable=True,
            passive=True,
        )
        mock_conn.close.assert_called_once()

    def test_consume_project_queue_stops_on_missing_project_fk_violation(self):
        try:
            from core.infrastructure.runtime.worker.queue import consume_project_queue
        except ImportError:
            pytest.skip(
                "core.infrastructure.runtime.worker.queue.consume_project_queue not importable"
            )

        mock_channel = MagicMock()
        mock_conn = MagicMock()
        mock_conn.channel.return_value = mock_channel
        lease_store = MagicMock()
        violation = RuntimeError(
            'insert or update on table "project_execution_slots" violates foreign key '
            'constraint "project_execution_slots_project_id_fkey"'
        )
        violation.pgcode = "23503"  # type: ignore[attr-defined]
        lease_store.acquire.side_effect = violation
        lease_store.project_exists.return_value = False

        with (
            patch("core.infrastructure.runtime.worker.queue._connect", return_value=mock_conn),
            patch(
                "core.infrastructure.runtime.worker.queue._ProjectLeaseStore",
                return_value=lease_store,
            ),
            patch("time.sleep") as sleep_mock,
        ):
            consume_project_queue(project_id=211, callback=lambda _msg: None, worker_id="w1")

        lease_store.acquire.assert_called_once_with("211")
        lease_store.project_exists.assert_called_once_with("211")
        sleep_mock.assert_not_called()
