"""Unit tests for the SSE global event stream endpoint."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from core.application.jobs.public_workflow_stream_preflight import (
    PublicWorkflowStreamCursorExpired,
)
from core.application.jobs.workflow_run_models import PublicApprovalGateV1, WorkflowRunStateV1
from core.application.management.errors import ServiceError
from core.application.management.scope import ScopeContext

# ---------------------------------------------------------------------------
# Helper to run async generators synchronously in tests
# ---------------------------------------------------------------------------


def _collect_chunks(gen, max_chunks: int = 5):
    """Run an async generator and collect up to max_chunks items."""

    async def _run():
        chunks = []
        async for chunk in gen:
            chunks.append(chunk)
            if len(chunks) >= max_chunks:
                break
        return chunks

    return asyncio.get_event_loop().run_until_complete(_run())


@pytest.mark.parametrize(
    "job",
    [
        {"selected_workflow_id": "workflow-allowed"},
        {"meta": {"selected_workflow_id": "workflow-allowed"}},
        {"meta": {"invocation": {"selected_workflow_id": "workflow-allowed"}}},
        {"meta": json.dumps({"invocation": {"selected_workflow_id": "workflow-allowed"}})},
    ],
)
def test_public_stream_preflight_uses_canonical_workflow_identity(job) -> None:
    from core.application.jobs.public_workflow_stream_preflight import (
        PublicWorkflowStreamPreflight,
    )

    repository = MagicMock()
    repository.get_job.return_value = {
        "id": "job-1",
        "project_id": "project-1",
        **job,
    }
    repository.get_project.return_value = {"id": "project-1"}
    checked_workflows: list[str] = []

    target = PublicWorkflowStreamPreflight(
        repository,
        reconcile_snapshot=lambda _job: None,
        require_project_access_fn=lambda *_args: None,
        require_workflow_access_fn=lambda _scope, workflow_id: checked_workflows.append(
            workflow_id
        ),
    ).execute(
        scope=ScopeContext(account_id="account-1"),
        job_id="job-1",
        cursor="",
    )

    assert target.project_id == "project-1"
    assert checked_workflows == ["workflow-allowed"]


def test_public_stream_cursor_expiry_is_transport_neutral() -> None:
    from core.application.jobs.public_workflow_stream_preflight import (
        PublicWorkflowStreamPreflight,
    )

    repository = MagicMock()
    repository.get_job.return_value = {
        "id": "job-1",
        "project_id": "project-1",
        "selected_workflow": "workflow-1",
    }
    repository.get_project.return_value = {"id": "project-1"}
    repository.get_public_state_marker.return_value = None

    _raises_callable_98_1 = PublicWorkflowStreamPreflight(
        repository,
        reconcile_snapshot=lambda _job: None,
        require_project_access_fn=lambda *_args: None,
        require_workflow_access_fn=lambda *_args: None,
    ).execute
    _raises_input_98_2 = ScopeContext(account_id="account-1")
    with pytest.raises(PublicWorkflowStreamCursorExpired) as exc_info:
        _raises_callable_98_1(scope=_raises_input_98_2, job_id="job-1", cursor="expired-marker")

    assert getattr(exc_info.value, "code", "") == "cursor_expired"
    assert not hasattr(exc_info.value, "status_code")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSerializeEvent:
    def test_datetime_serialized_to_isoformat(self):
        from core.entrypoints.api.events_stream import _serialize_event

        row = {
            "id": 1,
            "timestamp": datetime(2026, 3, 26, 12, 0, 0, tzinfo=UTC),
            "title": "hello",
        }
        result = json.loads(_serialize_event(row))
        assert result["timestamp"] == "2026-03-26T12:00:00+00:00"
        assert result["title"] == "hello"

    def test_non_datetime_passthrough(self):
        from core.entrypoints.api.events_stream import _serialize_event

        row = {"id": 5, "severity": "info", "detail": {"key": "val"}}
        result = json.loads(_serialize_event(row))
        assert result == row


def test_public_stream_limits_use_documented_fs_environment_names() -> None:
    from core.entrypoints.api import events_stream

    source = Path(events_stream.__file__).read_text(encoding="utf-8")

    assert 'CONFIG["public_sse_connections_per_principal"]' in source
    assert 'CONFIG["public_sse_connections_per_account"]' in source
    assert "os.getenv" not in source


class TestEventGenerator:
    def test_yields_retry_then_events(self):
        from core.entrypoints.api.events_stream import _event_generator

        events = [
            {
                "id": 10,
                "job_id": 1,
                "phase": "researcher",
                "agent": "researcher",
                "event_type": "decision",
                "severity": "info",
                "title": "Start",
                "detail": None,
                "project_id": 1,
                "timestamp": "2026-03-26T12:00:00Z",
            },
            {
                "id": 11,
                "job_id": 1,
                "phase": "writer",
                "agent": "writer",
                "event_type": "tool_call",
                "severity": "info",
                "title": "Edit",
                "detail": None,
                "project_id": 1,
                "timestamp": "2026-03-26T12:01:00Z",
            },
        ]
        call_count = 0
        mock_bl = MagicMock()

        def fake_list(cursor_id, account_id, **kwargs):
            nonlocal call_count
            call_count += 1
            return events if call_count == 1 else []

        mock_bl.list_events_since.side_effect = fake_list

        async def _run():
            with patch("core.entrypoints.api.events_stream.asyncio.sleep", return_value=None):
                gen = _event_generator(
                    mock_bl, ScopeContext(account_id="acc_default"), 0, None, None
                )
                chunks = []
                async for chunk in gen:
                    chunks.append(chunk)
                    if len(chunks) >= 5:
                        break
            return chunks

        chunks = asyncio.new_event_loop().run_until_complete(_run())
        assert chunks[0] == "retry: 5000\n\n"
        assert "id: 10\n" in chunks[1]
        assert '"phase": "researcher"' in chunks[1]
        assert "id: 11\n" in chunks[2]
        assert '"phase": "writer"' in chunks[2]

    def test_keepalive_when_no_events(self):
        from core.entrypoints.api.events_stream import _event_generator

        mock_bl = MagicMock()
        mock_bl.list_events_since.return_value = []

        async def _run():
            with patch("core.entrypoints.api.events_stream.asyncio.sleep", return_value=None):
                gen = _event_generator(
                    mock_bl, ScopeContext(account_id="acc_default"), 0, None, None
                )
                chunks = []
                async for chunk in gen:
                    chunks.append(chunk)
                    if len(chunks) >= 3:
                        break
            return chunks

        chunks = asyncio.new_event_loop().run_until_complete(_run())
        assert chunks[0] == "retry: 5000\n\n"
        assert chunks[1] == ": keepalive\n\n"

    def test_cursor_forwarded_correctly(self):
        from core.entrypoints.api.events_stream import _event_generator

        cursors_seen = []

        def fake_list(cursor_id, account_id, **kwargs):
            cursors_seen.append(cursor_id)
            if cursor_id == "50":
                return [
                    {
                        "id": 60,
                        "job_id": 1,
                        "phase": "checker",
                        "agent": "checker",
                        "event_type": "pass",
                        "severity": "info",
                        "title": "OK",
                        "detail": None,
                        "project_id": 1,
                        "timestamp": "now",
                    }
                ]
            return []

        mock_bl = MagicMock()
        mock_bl.list_events_since.side_effect = fake_list

        async def _run():
            with patch("core.entrypoints.api.events_stream.asyncio.sleep", return_value=None):
                gen = _event_generator(
                    mock_bl, ScopeContext(account_id="acc_test"), "50", None, None
                )
                chunks = []
                async for chunk in gen:
                    chunks.append(chunk)
                    if len(chunks) >= 5:
                        break
            return chunks

        asyncio.new_event_loop().run_until_complete(_run())
        assert cursors_seen[0] == "50"
        assert "60" in cursors_seen

    def test_project_and_event_type_filters_passed(self):
        from core.entrypoints.api.events_stream import _event_generator

        mock_bl = MagicMock()
        mock_bl.list_events_since.return_value = []

        async def _run():
            with patch("core.entrypoints.api.events_stream.asyncio.sleep", return_value=None):
                gen = _event_generator(
                    mock_bl,
                    ScopeContext(account_id="acc_x"),
                    "",
                    project_id="7",
                    event_type="decision",
                )
                count = 0
                async for _ in gen:
                    count += 1
                    if count >= 2:  # retry + keepalive (triggers list_events_since)
                        break

        asyncio.new_event_loop().run_until_complete(_run())
        kwargs = mock_bl.list_events_since.call_args[1]
        assert kwargs["project_id"] == "7"
        assert kwargs["event_type"] == "decision"

    def test_restricted_workflow_scope_drops_rows_without_workflow_id(self):
        from core.entrypoints.api.events_stream import _event_generator

        mock_bl = MagicMock()
        mock_bl.list_events_since.return_value = [
            {
                "id": 21,
                "job_id": 1,
                "phase": "researcher",
                "agent": "researcher",
                "event_type": "decision",
                "severity": "info",
                "title": "No workflow",
                "detail": None,
                "project_id": 1,
                "timestamp": "2026-03-26T12:00:00Z",
            }
        ]

        async def _run():
            with patch("core.entrypoints.api.events_stream.asyncio.sleep", return_value=None):
                gen = _event_generator(
                    mock_bl,
                    ScopeContext(
                        account_id="acc_default",
                        allowed_workflow_ids=frozenset({"wf_demo"}),
                    ),
                    0,
                    None,
                    None,
                )
                chunks = []
                async for chunk in gen:
                    chunks.append(chunk)
                    if len(chunks) >= 2:
                        break
            return chunks

        chunks = asyncio.new_event_loop().run_until_complete(_run())
        assert chunks == ["retry: 5000\n\n", ": keepalive\n\n"]

    def test_multi_account_isolation_drops_foreign_account_rows(self):
        from core.entrypoints.api.events_stream import _event_generator

        mock_bl = MagicMock()
        mock_bl.list_events_since.return_value = [
            {
                "id": 31,
                "job_id": 1,
                "phase": "researcher",
                "agent": "researcher",
                "event_type": "decision",
                "severity": "info",
                "title": "Tenant A event",
                "detail": None,
                "project_id": 1,
                "workflow_id": "wf_demo",
                "account_id": "acc_a",
                "timestamp": "2026-03-26T12:00:00Z",
            },
            {
                "id": 32,
                "job_id": 2,
                "phase": "writer",
                "agent": "writer",
                "event_type": "decision",
                "severity": "info",
                "title": "Tenant B event",
                "detail": None,
                "project_id": 2,
                "workflow_id": "wf_demo",
                "account_id": "acc_b",
                "timestamp": "2026-03-26T12:01:00Z",
            },
        ]

        async def _run():
            with patch("core.entrypoints.api.events_stream.asyncio.sleep", return_value=None):
                gen = _event_generator(mock_bl, ScopeContext(account_id="acc_a"), 0, None, None)
                chunks = []
                async for chunk in gen:
                    chunks.append(chunk)
                    if len(chunks) >= 3:
                        break
            return chunks

        chunks = asyncio.new_event_loop().run_until_complete(_run())
        assert chunks[0] == "retry: 5000\n\n"
        assert "id: 31" in chunks[1]
        assert "Tenant A event" in chunks[1]
        assert all("id: 32" not in chunk for chunk in chunks)


class TestEventsStreamRoute:
    def test_route_registered(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception:
            pytest.skip("api_runtime not importable")

        routes = [r.path for r in api_runtime.app.routes if hasattr(r, "path")]
        assert "/api/events/stream" in routes

    def test_endpoint_returns_streaming_response(self):
        """Verify the endpoint function returns a StreamingResponse with SSE media type."""
        from fastapi.responses import StreamingResponse

        from core.entrypoints.api.events_stream import events_stream

        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.state.auth_user = None

        mock_bl = MagicMock()
        mock_bl.list_events_since.return_value = []

        import core.entrypoints.api.dependencies as route_deps

        with patch.object(route_deps, "_get_logger_only", return_value=mock_bl):
            resp = asyncio.new_event_loop().run_until_complete(
                events_stream(mock_request, cursor=0, project_id=None, event_type=None)
            )

        assert isinstance(resp, StreamingResponse)
        assert resp.media_type == "text/event-stream"
        assert resp.headers.get("cache-control") == "no-cache"
        assert resp.headers.get("x-accel-buffering") == "no"


class TestJobScopedEventGenerator:
    @staticmethod
    def _snapshot(*, terminal: bool = False) -> WorkflowRunStateV1:
        return WorkflowRunStateV1(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            status="completed" if terminal else "in_progress",
            terminal=terminal,
            result={"outputs": {}} if terminal else None,
            updated_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
            finished_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC) if terminal else None,
        )

    def test_emits_job_state_projection_and_closes_terminal(self):
        from core.entrypoints.api.events_stream import _job_event_generator

        snapshot = self._snapshot(terminal=True)
        mock_bl = MagicMock()
        mock_bl.get_public_progress_snapshot.return_value = MagicMock(
            snapshot=snapshot, revision=1, marker_id=None
        )
        mock_bl.get_latest_public_state_marker.return_value = {
            "id": "marker-1",
            "detail": {"revision": 1, "projection_fingerprint": "sha256:test"},
        }

        async def _run():
            with patch("core.entrypoints.api.events_stream.asyncio.sleep", return_value=None):
                gen = _job_event_generator(
                    mock_bl,
                    ScopeContext(account_id="acc-1"),
                    "job-1",
                    "",
                    project_id="project-1",
                )
                return [chunk async for chunk in gen]

        chunks = asyncio.new_event_loop().run_until_complete(_run())
        assert chunks[0] == "retry: 5000\n\n"
        assert "event: job.state\n" in chunks[1]
        assert "id: marker-1\n" in chunks[1]
        assert '"job_id": "job-1"' in chunks[1]
        assert all("platform_events" not in chunk for chunk in chunks)
        assert len(chunks) == 2

    def test_strict_db_failure_emits_safe_error_and_closes(self):
        from core.entrypoints.api.events_stream import _job_event_generator

        mock_bl = MagicMock()
        mock_bl.get_public_progress_snapshot.side_effect = RuntimeError("db secret")

        async def _run():
            gen = _job_event_generator(
                mock_bl,
                ScopeContext(account_id="acc-1"),
                "job-1",
                "",
                project_id="project-1",
            )
            return [chunk async for chunk in gen]

        chunks = asyncio.new_event_loop().run_until_complete(_run())
        assert chunks[0] == "retry: 5000\n\n"
        assert "event: stream.error\n" in chunks[1]
        assert '"code": "stream_unavailable"' in chunks[1]
        assert "db secret" not in chunks[1]
        assert len(chunks) == 2

    def test_live_reconnect_at_current_cursor_waits_without_duplicate_snapshot(self):
        from core.entrypoints.api.events_stream import _job_event_generator

        snapshot = self._snapshot(terminal=False)
        mock_bl = MagicMock()
        mock_bl.get_public_progress_snapshot.return_value = MagicMock(
            snapshot=snapshot, revision=1, marker_id=None
        )
        mock_bl.get_latest_public_state_marker.return_value = {"id": "marker-1"}

        async def _run():
            with patch("core.entrypoints.api.events_stream.asyncio.sleep", return_value=None):
                gen = _job_event_generator(
                    mock_bl,
                    ScopeContext(account_id="acc-1"),
                    "job-1",
                    "marker-1",
                    project_id="project-1",
                )
                chunks = []
                async for chunk in gen:
                    chunks.append(chunk)
                    if len(chunks) >= 3:
                        break
                return chunks

        chunks = asyncio.new_event_loop().run_until_complete(_run())
        assert chunks == ["retry: 5000\n\n", ": keepalive\n\n", ": keepalive\n\n"]
        assert all("event: job.state" not in chunk for chunk in chunks)

    def test_disconnect_cleanup_callback_runs_for_terminal_stream(self):
        from core.entrypoints.api.events_stream import _job_event_generator

        snapshot = self._snapshot(terminal=True)
        mock_bl = MagicMock()
        mock_bl.get_public_progress_snapshot.return_value = MagicMock(
            snapshot=snapshot, revision=1, marker_id=None
        )
        mock_bl.get_latest_public_state_marker.return_value = {"id": "marker-1"}
        cleanup = MagicMock()

        async def _run():
            gen = _job_event_generator(
                mock_bl,
                ScopeContext(account_id="acc-1"),
                "job-1",
                "",
                project_id="project-1",
                on_close=cleanup,
            )
            return [chunk async for chunk in gen]

        chunks = asyncio.new_event_loop().run_until_complete(_run())
        assert len(chunks) == 2
        cleanup.assert_called_once_with()


class TestPublicProjectionReconciliation:
    def test_terminal_reconciliation_includes_latest_structured_job_error(self):
        from core.entrypoints.api.events_stream import _reconcile_public_job_snapshot

        stale = WorkflowRunStateV1(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            status="failed",
            terminal=True,
            updated_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
        )
        bl = MagicMock()
        bl.get_public_progress_snapshot.return_value = SimpleNamespace(
            snapshot=stale,
            marker_id="marker-1",
        )
        bl.list_job_errors.return_value = [
            {
                "error_code": "provider_timeout",
                "category": "provider",
                "retryable": True,
                "context_jsonb": {
                    "step_id": "step-7",
                    "group_id": "delivery",
                    "occurrence": 2,
                },
            }
        ]
        bl.record_public_progress_transition.side_effect = lambda **kwargs: SimpleNamespace(
            snapshot=stale.model_copy(update={"error": kwargs["error"]})
        )

        snapshot = _reconcile_public_job_snapshot(
            bl,
            {
                "id": "job-1",
                "project_id": "project-1",
                "selected_workflow": "workflow-1",
                "status": "failed",
            },
        )

        assert snapshot.error is not None
        assert snapshot.error.code == "provider_timeout"
        assert snapshot.error.step_id == "step-7"
        assert snapshot.error.group_id == "delivery"
        assert snapshot.error.occurrence == 2
        bl.list_job_errors.assert_called_once_with("job-1", limit=200)

    def test_marked_awaiting_snapshot_does_not_emit_a_duplicate_marker(self, monkeypatch):
        from core.entrypoints.api.events_stream import _reconcile_public_job_snapshot

        current = WorkflowRunStateV1(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            status="awaiting_approval",
            terminal=False,
            approval=PublicApprovalGateV1(
                approval_id="approval-1",
                wait_id="wait-1",
                reason="authored",
                job_id="job-1",
                thread_id="thread-1",
                created_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
            ),
            updated_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
        )
        bl = MagicMock()
        bl.get_public_progress_snapshot.return_value = SimpleNamespace(
            snapshot=current,
            marker_id="marker-1",
        )
        monkeypatch.setattr(
            "core.entrypoints.api.dependencies.get_thread_state_snapshot",
            lambda _thread_id: SimpleNamespace(
                tasks=[
                    SimpleNamespace(
                        interrupts=[
                            SimpleNamespace(
                                id="approval-1",
                                value={"gate_kind": "approval", "wait_id": "wait-1"},
                            )
                        ]
                    )
                ]
            ),
        )

        assert (
            _reconcile_public_job_snapshot(
                bl,
                {
                    "id": "job-1",
                    "project_id": "project-1",
                    "selected_workflow": "workflow-1",
                    "status": "awaiting_approval",
                    "thread_id": "thread-1",
                },
            )
            == current
        )
        bl.record_public_progress_transition.assert_not_called()

    def test_missing_snapshot_fails_closed_before_stream(self):
        from core.entrypoints.api.events_stream import _reconcile_public_job_snapshot

        bl = MagicMock()
        bl.get_public_progress_snapshot.side_effect = LookupError("missing")

        with pytest.raises(ServiceError, match="state is unavailable"):
            _reconcile_public_job_snapshot(
                bl,
                {
                    "id": "job-1",
                    "project_id": "project-1",
                    "selected_workflow": "workflow-1",
                    "status": "pending",
                },
            )

        bl.record_public_progress_transition.assert_not_called()

    def test_snapshot_without_marker_fails_closed_before_stream(self):
        from core.entrypoints.api.events_stream import _reconcile_public_job_snapshot

        snapshot = WorkflowRunStateV1(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            status="pending",
            terminal=False,
            updated_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
        )
        bl = MagicMock()
        bl.get_public_progress_snapshot.return_value = SimpleNamespace(
            snapshot=snapshot,
            marker_id=None,
        )
        with pytest.raises(ServiceError, match="state is unavailable"):
            _reconcile_public_job_snapshot(
                bl,
                {
                    "id": "job-1",
                    "project_id": "project-1",
                    "selected_workflow": "workflow-1",
                    "status": "pending",
                },
            )

        bl.record_public_progress_transition.assert_not_called()

    def test_marked_snapshot_is_rematerialized_when_job_status_advanced(self):
        from core.entrypoints.api.events_stream import _reconcile_public_job_snapshot

        stale = WorkflowRunStateV1(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            status="awaiting_approval",
            terminal=False,
            updated_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
        )
        current = WorkflowRunStateV1(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            status="in_progress",
            terminal=False,
            updated_at=datetime(2026, 3, 26, 12, 1, tzinfo=UTC),
        )
        bl = MagicMock()
        bl.get_public_progress_snapshot.return_value = SimpleNamespace(
            snapshot=stale,
            marker_id="marker-1",
        )
        bl.record_public_progress_transition.return_value = SimpleNamespace(snapshot=current)

        result = _reconcile_public_job_snapshot(
            bl,
            {
                "id": "job-1",
                "project_id": "project-1",
                "selected_workflow": "workflow-1",
                "status": "in_progress",
            },
        )

        assert result == current
        bl.record_public_progress_transition.assert_called_once()

    def test_multiple_gates_are_rejected_before_opening_stream(self, monkeypatch):
        from core.application.jobs.workflow_run_projection import MultipleApprovalGatesUnsupported
        from core.entrypoints.api.events_stream import _reconcile_public_job_snapshot

        bl = MagicMock()
        bl.get_public_progress_snapshot.return_value = SimpleNamespace(
            snapshot=WorkflowRunStateV1(
                job_id="job-1",
                project_id="project-1",
                workflow_id="workflow-1",
                status="awaiting_approval",
                terminal=False,
                updated_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
            ),
            marker_id="marker-1",
        )
        monkeypatch.setattr(
            "core.entrypoints.api.dependencies.get_thread_state_snapshot",
            lambda _thread_id: SimpleNamespace(
                tasks=[
                    SimpleNamespace(
                        interrupts=[SimpleNamespace(value={}), SimpleNamespace(value={})]
                    )
                ]
            ),
        )

        with pytest.raises(MultipleApprovalGatesUnsupported):
            _reconcile_public_job_snapshot(
                bl,
                {
                    "id": "job-1",
                    "project_id": "project-1",
                    "selected_workflow": "workflow-1",
                    "status": "awaiting_approval",
                    "thread_id": "thread-1",
                },
            )

        bl.record_public_progress_transition.assert_not_called()

    def test_multiple_gates_are_rejected_before_stream_when_job_status_lags(self, monkeypatch):
        from core.application.jobs.workflow_run_projection import MultipleApprovalGatesUnsupported
        from core.entrypoints.api.events_stream import _reconcile_public_job_snapshot

        snapshot = WorkflowRunStateV1(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            status="in_progress",
            terminal=False,
            updated_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
        )
        bl = MagicMock()
        bl.get_public_progress_snapshot.return_value = SimpleNamespace(
            snapshot=snapshot,
            marker_id="marker-1",
        )
        monkeypatch.setattr(
            "core.entrypoints.api.dependencies.get_thread_state_snapshot",
            lambda _thread_id: SimpleNamespace(
                tasks=[
                    SimpleNamespace(
                        interrupts=[SimpleNamespace(value={}), SimpleNamespace(value={})]
                    )
                ]
            ),
        )

        with pytest.raises(MultipleApprovalGatesUnsupported):
            _reconcile_public_job_snapshot(
                bl,
                {
                    "id": "job-1",
                    "project_id": "project-1",
                    "selected_workflow": "workflow-1",
                    "status": "in_progress",
                    "thread_id": "thread-1",
                },
            )

        bl.record_public_progress_transition.assert_not_called()

    def test_in_progress_snapshot_fails_closed_when_checkpoint_is_unavailable(self, monkeypatch):
        from core.entrypoints.api.events_stream import _reconcile_public_job_snapshot

        snapshot = WorkflowRunStateV1(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            status="in_progress",
            terminal=False,
            updated_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
        )
        bl = MagicMock()
        bl.get_public_progress_snapshot.return_value = SimpleNamespace(
            snapshot=snapshot,
            marker_id="marker-1",
        )
        monkeypatch.setattr(
            "core.entrypoints.api.dependencies.get_thread_state_snapshot", lambda _thread_id: None
        )

        with pytest.raises(ServiceError, match="Public workflow state is unavailable"):
            _reconcile_public_job_snapshot(
                bl,
                {
                    "id": "job-1",
                    "project_id": "project-1",
                    "selected_workflow": "workflow-1",
                    "status": "in_progress",
                    "thread_id": "thread-1",
                },
            )

    def test_in_progress_snapshot_fails_closed_when_checkpoint_has_pending_interrupt(
        self, monkeypatch
    ):
        from core.entrypoints.api.events_stream import _reconcile_public_job_snapshot

        snapshot = WorkflowRunStateV1(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            status="in_progress",
            terminal=False,
            updated_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
        )
        bl = MagicMock()
        bl.get_public_progress_snapshot.return_value = SimpleNamespace(
            snapshot=snapshot,
            marker_id="marker-1",
        )
        monkeypatch.setattr(
            "core.entrypoints.api.dependencies.get_thread_state_snapshot",
            lambda _thread_id: SimpleNamespace(
                tasks=[
                    SimpleNamespace(
                        interrupts=[SimpleNamespace(id="approval-1", value={"message": "Approve"})]
                    )
                ]
            ),
        )

        with pytest.raises(ServiceError, match="Public workflow state is unavailable") as exc:
            _reconcile_public_job_snapshot(
                bl,
                {
                    "id": "job-1",
                    "project_id": "project-1",
                    "selected_workflow": "workflow-1",
                    "status": "in_progress",
                    "thread_id": "thread-1",
                },
            )

        assert exc.value.status_code == 503

    def test_disconnect_before_initial_read_closes_and_releases(self):
        from core.entrypoints.api.events_stream import _job_event_generator

        class _DisconnectedRequest:
            async def is_disconnected(self):
                return True

        mock_bl = MagicMock()
        cleanup = MagicMock()

        async def _run():
            gen = _job_event_generator(
                mock_bl,
                ScopeContext(account_id="acc-1"),
                "job-1",
                "",
                project_id="project-1",
                on_close=cleanup,
                request=_DisconnectedRequest(),
            )
            return [chunk async for chunk in gen]

        chunks = asyncio.new_event_loop().run_until_complete(_run())
        assert chunks == ["retry: 5000\n\n"]
        mock_bl.get_public_progress_snapshot.assert_not_called()
        cleanup.assert_called_once_with()


class TestJobScopedEventRoute:
    def test_cursor_control_character_is_rejected_before_lookup(self):
        from core.entrypoints.api.events_stream import events_stream

        request = MagicMock()
        request.headers = {}
        request.state.auth_user = {"id": "u-1", "auth_source": "local", "account_id": "acc-1"}
        response = asyncio.new_event_loop().run_until_complete(
            events_stream(
                request, cursor="\nmarker", project_id=None, event_type=None, job_id="job-1"
            )
        )
        assert response.status_code == 400

    def test_external_token_without_job_id_is_rejected_before_streaming(self):
        from core.entrypoints.api.events_stream import events_stream

        request = MagicMock()
        request.headers = {}
        request.state.auth_user = {
            "id": "pat-1",
            "auth_source": "pat",
            "account_id": "acc-1",
            "scopes": ["jobs.read"],
        }
        response = asyncio.new_event_loop().run_until_complete(
            events_stream(request, cursor="", project_id=None, event_type=None, job_id=None)
        )
        assert response.status_code == 400

    def test_job_mode_rejects_global_filters(self):
        from core.entrypoints.api.events_stream import events_stream

        request = MagicMock()
        request.headers = {}
        request.state.auth_user = {"id": "u-1", "auth_source": "local", "account_id": "acc-1"}
        response = asyncio.new_event_loop().run_until_complete(
            events_stream(
                request, cursor="", project_id="project-1", event_type=None, job_id="job-1"
            )
        )
        assert response.status_code == 400

    def test_missing_projection_returns_json_503_before_streaming(self, monkeypatch):
        from core.entrypoints.api.events_stream import events_stream

        request = MagicMock()
        request.headers = {}
        request.state.auth_user = {"id": "u-1", "auth_source": "local", "account_id": "acc-1"}
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": "job-1",
            "project_id": "project-1",
            "selected_workflow": "workflow-1",
            "status": "pending",
        }
        mock_bl.get_project.return_value = {"id": "project-1", "account_id": "acc-1"}
        monkeypatch.setattr(
            "core.entrypoints.api.events_stream.deps.get_logger_only", lambda: mock_bl
        )
        monkeypatch.setattr(
            "core.entrypoints.api.events_stream.require_project_access", lambda *_args: None
        )
        monkeypatch.setattr(
            "core.entrypoints.api.events_stream.require_workflow_access", lambda *_args: None
        )
        monkeypatch.setattr(
            "core.entrypoints.api.events_stream._reconcile_public_job_snapshot",
            lambda *_args: (_ for _ in ()).throw(
                __import__("core.application.management", fromlist=["ServiceError"]).ServiceError(
                    "Public workflow state is unavailable", status_code=503
                )
            ),
        )

        response = asyncio.new_event_loop().run_until_complete(
            events_stream(request, cursor="", project_id=None, event_type=None, job_id="job-1")
        )

        assert response.status_code == 503
        assert response.media_type != "text/event-stream"


class TestJobStreamConnectionGuard:
    def test_principal_and_account_limits_release_cleanly(self):
        from core.entrypoints.api.events_stream import _JobStreamConnectionGuard

        guard = _JobStreamConnectionGuard(principal_limit=1, account_limit=2)
        assert guard.acquire("principal-1", "account-1") is True
        assert guard.acquire("principal-1", "account-1") is False
        assert guard.acquire("principal-2", "account-1") is True
        assert guard.acquire("principal-3", "account-1") is False
        guard.release("principal-1", "account-1")
        assert guard.acquire("principal-3", "account-1") is True
        guard.release("principal-2", "account-1")
        guard.release("principal-3", "account-1")
        assert guard.counts() == ({}, {})

    def test_preflight_cancellation_releases_connection_quota(self, monkeypatch):
        stream_module = importlib.import_module("core.entrypoints.api.events_stream")
        from core.entrypoints.api.events_stream import _JobStreamConnectionGuard, events_stream

        guard = _JobStreamConnectionGuard(principal_limit=1, account_limit=10)
        entered_preflight = asyncio.Event()

        async def _cancelled_preflight(_callable, *_args, **_kwargs):
            entered_preflight.set()
            await asyncio.Event().wait()

        request = MagicMock()
        request.headers = {}
        request.state.auth_user = {
            "id": "principal-1",
            "auth_source": "local",
            "account_id": "acc-1",
        }
        monkeypatch.setattr(stream_module, "_JOB_STREAM_CONNECTION_GUARD", guard)
        monkeypatch.setattr(stream_module, "run_in_threadpool", _cancelled_preflight)
        monkeypatch.setattr(stream_module.deps, "get_logger_only", lambda: MagicMock())

        async def _run():
            task = asyncio.create_task(
                events_stream(request, cursor="", project_id=None, event_type=None, job_id="job-1")
            )
            await entered_preflight.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_run())
        assert guard.counts() == ({}, {})

    def test_route_rejects_excess_before_generator_and_background_releases(self, monkeypatch):
        stream_module = importlib.import_module("core.entrypoints.api.events_stream")
        from core.entrypoints.api.events_stream import _JobStreamConnectionGuard, events_stream

        guard = _JobStreamConnectionGuard(principal_limit=1, account_limit=10)
        monkeypatch.setattr(stream_module, "_JOB_STREAM_CONNECTION_GUARD", guard)
        monkeypatch.setattr(stream_module, "require_project_access", lambda *_args: None)
        monkeypatch.setattr(stream_module, "require_workflow_access", lambda *_args: None)
        request = MagicMock()
        request.headers = {}
        request.state.auth_user = {
            "id": "principal-1",
            "auth_source": "local",
            "account_id": "acc-1",
        }
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": "job-1",
            "project_id": "project-1",
            "selected_workflow": "workflow-1",
            "status": "pending",
        }
        mock_bl.get_project.return_value = {
            "id": "project-1",
            "account_id": "acc-1",
        }
        mock_bl.get_public_progress_snapshot.return_value = SimpleNamespace(
            snapshot=WorkflowRunStateV1(
                job_id="job-1",
                project_id="project-1",
                workflow_id="workflow-1",
                status="pending",
                terminal=False,
                updated_at=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
            ),
            marker_id="marker-1",
        )
        monkeypatch.setattr(stream_module.deps, "get_logger_only", lambda: mock_bl)

        first = asyncio.new_event_loop().run_until_complete(
            events_stream(request, cursor="", project_id=None, event_type=None, job_id="job-1")
        )
        second = asyncio.new_event_loop().run_until_complete(
            events_stream(request, cursor="", project_id=None, event_type=None, job_id="job-2")
        )
        assert first.status_code == 200
        assert second.status_code == 429
        assert mock_bl.get_job.call_count == 1

        async def _release():
            await first.background()

        asyncio.new_event_loop().run_until_complete(_release())
        third = asyncio.new_event_loop().run_until_complete(
            events_stream(request, cursor="", project_id=None, event_type=None, job_id="job-3")
        )
        assert third.status_code == 200


def test_event_generator_does_not_block_event_loop_during_database_poll():
    from core.entrypoints.api.events_stream import _event_generator

    timings: dict[str, float] = {}

    class _BlockingLogger:
        def list_events_since(self, *_args, **_kwargs):
            time.sleep(0.1)
            timings["database_finished"] = time.monotonic()
            return []

    scope = SimpleNamespace(
        account_id="account-1",
        allowed_project_ids=None,
        allowed_workflow_ids=None,
    )

    async def _run() -> None:
        generator = _event_generator(_BlockingLogger(), scope, "", None, None)
        assert await anext(generator) == "retry: 5000\n\n"

        async def _ticker() -> None:
            await asyncio.sleep(0.01)
            timings["ticker"] = time.monotonic()

        poll = asyncio.create_task(anext(generator))
        ticker = asyncio.create_task(_ticker())
        assert await poll == ": keepalive\n\n"
        await ticker
        await generator.aclose()

    asyncio.run(_run())

    assert timings["ticker"] < timings["database_finished"]


def test_stream_terminal_refresh_cannot_downgrade_committed_stop_metadata(monkeypatch, request):
    from core.entrypoints.api.events_stream import _reconcile_public_job_snapshot
    from tests.unit.test_workflow_run_projection import _terminal_stop_reader_fixture

    fixture = _terminal_stop_reader_fixture(monkeypatch, request)
    # Contract control: seed metadata through the real public store, not a claim
    # that the current producer emits it (the other owning module proves that gap).
    fixture.logger.record_public_progress_transition(
        job_id="job-1",
        project_id="project-1",
        workflow_id="workflow-1",
        job_status="failed",
        occurred_at=fixture.before.snapshot.updated_at,
        finished_at=fixture.before.snapshot.finished_at,
        error={
            "code": "workflow_stop_fail",
            "message": "Intentional expected audit failure",
            "step_id": "done",
            "step_execution_id": fixture.stop_execution,
            "category": "execution",
            "retryable": False,
        },
    )
    enriched = fixture.logger.get_public_progress_snapshot("job-1").snapshot
    before = fixture.logger.get_public_progress_snapshot("job-1")
    fixture.holder["job_errors"] = [
        {"error_code": "job_failed", "category": "unknown", "retryable": False}
    ]
    # The alternate SSE query has no committed reader today; it must at least
    # retain already-verified metadata while reconciling a generic job error.
    observed = _reconcile_public_job_snapshot(fixture.logger, fixture.job)
    assert observed.error == enriched.error
    assert observed.model_dump(mode="json", exclude={"updated_at"}) == enriched.model_dump(
        mode="json", exclude={"updated_at"}
    )
    assert [step.status for step in observed.steps] == ["completed"] * 3
    after = fixture.logger.get_public_progress_snapshot("job-1")
    assert after.revision == before.revision
    assert after.marker_id == before.marker_id


def test_first_public_stop_stream_uses_bootstrap_committed_reader(monkeypatch, request):
    from contextlib import contextmanager
    from copy import deepcopy

    from starlette.requests import Request

    from core.entrypoints.api import dependencies, runtime_port_manifest
    from core.entrypoints.api.app_factory import create_app
    from core.entrypoints.api.events_stream import _JobStreamConnectionGuard
    from core.infrastructure.runtime import http_app_events_resume, state_reader_runtime
    from tests.unit.test_workflow_run_projection import _terminal_stop_reader_fixture

    stream_module = importlib.import_module("core.entrypoints.api.events_stream")
    fixture = _terminal_stop_reader_fixture(monkeypatch, request)
    before = fixture.logger.get_public_progress_snapshot("job-1")
    assert before.snapshot.error is not None
    assert before.snapshot.error.code != "workflow_stop_fail"
    assert fixture.committed_reads == []
    checkpoint_reads = []
    pending_reads = []

    class Checkpointer:
        def get_tuple(self, config):
            checkpoint_reads.append(deepcopy(config))
            return SimpleNamespace(
                checkpoint={"channel_values": deepcopy(fixture.holder["values"])},
                pending_writes=[
                    (
                        "pending-task",
                        "workflow_terminal",
                        {
                            "kind": "stop_fail",
                            "status": "fail",
                            "step_id": "pending-step",
                            "step_execution_id": "pending-execution",
                        },
                    )
                ],
            )

    @contextmanager
    def open_checkpointer(uri):
        assert uri == "postgresql://checkpoint-fixture"
        yield Checkpointer()

    def pending_snapshot(thread_id):
        pending_reads.append(thread_id)
        return SimpleNamespace(values=deepcopy(fixture.holder["pending_values"]), tasks=[])

    def get_job(job_id):
        assert job_id == "job-1"
        return deepcopy(fixture.job)

    def get_project(project_id):
        assert project_id == "project-1"
        return {"id": "project-1", "account_id": "account-1"}

    def latest_marker(job_id, account_id, *, project_id):
        assert (job_id, account_id, project_id) == ("job-1", "account-1", "project-1")
        stored = fixture.logger.get_public_progress_snapshot(job_id)
        return {"id": stored.marker_id, "detail": {"revision": stored.revision}}

    def get_marker(marker_id, job_id, account_id, *, project_id):
        marker = latest_marker(job_id, account_id, project_id=project_id)
        return marker if marker["id"] == marker_id else None

    monkeypatch.setattr(fixture.logger, "get_job", get_job, raising=False)
    monkeypatch.setattr(fixture.logger, "get_project", get_project, raising=False)
    monkeypatch.setattr(
        fixture.logger, "get_latest_public_state_marker", latest_marker, raising=False
    )
    monkeypatch.setattr(fixture.logger, "get_public_state_marker", get_marker, raising=False)
    monkeypatch.setattr(state_reader_runtime, "_open_postgres_checkpointer", open_checkpointer)
    monkeypatch.setitem(
        http_app_events_resume.CONFIG, "langgraph_postgres_uri", "postgresql://checkpoint-fixture"
    )
    monkeypatch.setattr(http_app_events_resume, "_get_job_store", lambda: fixture.logger)
    monkeypatch.setattr(http_app_events_resume, "_get_thread_state_snapshot", pending_snapshot)
    monkeypatch.setattr(runtime_port_manifest, "RUNTIME_SERVICE_PORT_CONTRACTS", ())
    guard = _JobStreamConnectionGuard(principal_limit=1, account_limit=1)
    monkeypatch.setattr(stream_module, "_JOB_STREAM_CONNECTION_GUARD", guard)
    # Restore the real dependency registry after composition, including its future
    # committed-reader slot if present. No callback is fabricated by this test.
    for name in (
        "_get_logger_only",
        "_get_thread_state_snapshot",
        "_to_json_obj",
        "_resolve_artifact",
        "_artifact_presigned_url",
        "_artifact_local_stream",
        "_artifact_read_bytes",
        "_get_committed_checkpoint_values",
    ):
        if hasattr(dependencies, name):
            monkeypatch.setattr(dependencies, name, getattr(dependencies, name))
    app = create_app(initialize_api_dependencies=dependencies.init)
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/events/stream"
    )

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    public_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/events/stream",
            "headers": [],
            "query_string": b"job_id=job-1",
            "state": {
                "auth_user": {
                    "id": "reader-1",
                    "auth_source": "local",
                    "account_id": "account-1",
                }
            },
        },
        receive=receive,
    )

    async def read_first_stream():
        response = await endpoint(
            public_request, cursor="", project_id=None, event_type=None, job_id="job-1"
        )
        assert response.status_code == 200
        assert response.media_type == "text/event-stream"
        try:
            return [chunk async for chunk in response.body_iterator]
        finally:
            if response.background is not None:
                await response.background()

    chunks = asyncio.run(read_first_stream())
    state_chunks = [chunk for chunk in chunks if chunk.startswith("event: job.state\n")]
    assert len(state_chunks) == 1
    payload = json.loads(
        next(line[6:] for line in state_chunks[0].splitlines() if line.startswith("data: "))
    )
    observed = WorkflowRunStateV1.model_validate(payload)
    assert observed.error is not None
    assert observed.error.code == "workflow_stop_fail"
    assert observed.error.step_id == "done"
    assert observed.error.step_execution_id == fixture.stop_execution
    assert "Intentional expected audit failure" in observed.error.message
    assert observed.status == "failed"
    assert observed.terminal is True
    assert [step.status for step in observed.steps] == ["completed"] * 3
    assert observed.model_dump(
        mode="json", exclude={"error", "updated_at"}
    ) == before.snapshot.model_dump(mode="json", exclude={"error", "updated_at"})
    stored = fixture.logger.get_public_progress_snapshot("job-1")
    assert observed == stored.snapshot
    assert stored.revision == before.revision + 1
    assert f"id: {stored.marker_id}\n" in state_chunks[0]
    assert checkpoint_reads == [{"configurable": {"thread_id": "thread-1"}}]
    assert fixture.committed_reads == []  # The direct-query fixture callback was never used.
    assert all(thread_id == "thread-1" for thread_id in pending_reads)
    assert guard.counts() == ({}, {})
