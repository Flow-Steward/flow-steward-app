from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.infrastructure.runtime import graph_resume_runtime


@pytest.fixture(autouse=True)
def _terminal_persistence_contract(monkeypatch):
    service = MagicMock()
    service.complete_success.return_value = SimpleNamespace(status="completed")
    service.complete_failure.side_effect = lambda **kwargs: SimpleNamespace(status=kwargs["status"])
    monkeypatch.setattr(
        graph_resume_runtime,
        "_default_terminal_persistence_factory",
        lambda _bl, **_kwargs: service,
    )


def test_orchestrator_state_declares_durable_workflow_resume_marker() -> None:
    from core.application.orchestration.state import OrchestratorState

    assert "approval_resume_applied_id" in OrchestratorState.__annotations__


def test_run_graph_resume_does_not_persist_cancelled_when_lease_is_lost() -> None:
    """A lease-loss signal is not a user cancellation and must not become terminal state."""
    graph = MagicMock()
    graph.get_state.return_value = SimpleNamespace(values={}, tasks=(), next=())
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = {
        "id": 42,
        "project_id": 7,
        "workflow_id": "workflow-1",
        "meta": {},
    }

    build_graph = MagicMock(return_value=graph)
    with (
        patch(
            "core.application.orchestration.agents.common.run_graph_with_cancel_check",
            side_effect=lambda *_args, continue_check, **_kwargs: continue_check(),
        ),
        patch(
            "core.infrastructure.runtime.graph_resume_runtime._persist_graph_resume_result"
        ) as persist_result,
    ):
        graph_resume_runtime.run_graph_resume(
            "thread-lease-loss",
            {"approved": True},
            True,
            "approve",
            "ok",
            {},
            approval_id="gate-1",
            resume_cancelled=lambda: True,
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=MagicMock(),
            build_approval_entry=MagicMock(),
            logger=MagicMock(),
        )

    persist_result.assert_not_called()
    assert build_graph.call_args.kwargs.get("validate_contracts") is False


def test_persist_graph_resume_result_does_not_swallow_terminal_persistence_failure() -> None:
    bl = MagicMock()
    job_thread_store = MagicMock()

    with patch(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService"
    ) as terminal_service:
        terminal_service.return_value.complete_success.side_effect = RuntimeError(
            "marker write failed"
        )

        _raises_input_119_1 = MagicMock()
        with pytest.raises(RuntimeError, match="marker write failed"):
            graph_resume_runtime._persist_graph_resume_result(
                bl=bl,
                job_id="job-terminal-persistence",
                thread_id="thread-terminal-persistence",
                graph_status="completed",
                graph_error=None,
                result={"workflow_outputs": {"ok": True}},
                resume_test_mode=False,
                job_thread_store=job_thread_store,
                logger=_raises_input_119_1,
                terminal_persistence_factory=terminal_service,
            )


def test_persist_graph_resume_failure_keeps_callback_error_context() -> None:
    service = MagicMock()
    service.complete_failure.return_value = SimpleNamespace(status="failed")

    graph_resume_runtime._persist_graph_resume_result(
        bl=MagicMock(),
        job_id="job-resumed",
        thread_id="thread-resumed",
        graph_status="failed",
        graph_error="provider failed",
        result={"error_code": "provider_error"},
        resume_test_mode=True,
        job_thread_store=MagicMock(),
        logger=MagicMock(),
        terminal_persistence_factory=lambda *_args, **_kwargs: service,
        terminal_error_context={
            "step_id": "invoke-provider",
            "group_id": "implementation",
            "occurrence": 3,
        },
    )

    service.complete_failure.assert_called_once_with(
        job_id="job-resumed",
        status="failed",
        error_message="provider failed",
        error_code="provider_error",
        category="execution",
        final_report=None,
        step_id="invoke-provider",
        group_id="implementation",
        occurrence=3,
    )


def test_persist_graph_resume_failure_uses_handled_step_failure_context() -> None:
    service = MagicMock()
    service.complete_failure.return_value = SimpleNamespace(status="failed")

    graph_resume_runtime._persist_graph_resume_result(
        bl=MagicMock(),
        job_id="job-resumed",
        thread_id="thread-resumed",
        graph_status="failed",
        graph_error="Required input repository is missing",
        result={
            "unresolved_step_failure": {
                "step_id": "validate-inputs",
                "group_id": "implementation",
                "occurrence": 2,
                "error_code": "skill_input_invalid",
            }
        },
        resume_test_mode=True,
        job_thread_store=MagicMock(),
        logger=MagicMock(),
        terminal_persistence_factory=lambda *_args, **_kwargs: service,
        terminal_error_context={},
    )

    assert service.complete_failure.call_args.kwargs["step_id"] == "validate-inputs"
    assert service.complete_failure.call_args.kwargs["group_id"] == "implementation"
    assert service.complete_failure.call_args.kwargs["occurrence"] == 2
    assert service.complete_failure.call_args.kwargs["error_code"] == "skill_input_invalid"


def test_callback_terminal_error_context_is_bounded_to_public_location_fields() -> None:
    callback = SimpleNamespace(
        terminal_error_context=lambda: {
            "step_id": "invoke-provider",
            "group_id": "implementation",
            "occurrence": 4,
            "raw_exception": "must not propagate",
        }
    )

    assert graph_resume_runtime._callback_terminal_error_context({"callbacks": [callback]}) == {
        "step_id": "invoke-provider",
        "group_id": "implementation",
        "occurrence": 4,
    }


def test_resume_subprocess_does_not_restore_old_gate_after_terminal_persistence_failure(
    tmp_path, monkeypatch
) -> None:
    import core.infrastructure.runtime as runtime_package
    from core.infrastructure.runtime import graph_resume_subprocess

    payload_path = tmp_path / "resume.json"
    payload_path.write_text('{"thread_id":"thread-terminal"}', encoding="utf-8")
    status_update = MagicMock()
    fake_http_app = SimpleNamespace(
        _run_graph_resume=MagicMock(
            side_effect=graph_resume_runtime.TerminalPersistenceError("marker write failed")
        ),
        _set_job_status_by_thread_id=status_update,
    )
    monkeypatch.setattr(
        runtime_package,
        "http_app_events_resume",
        fake_http_app,
        raising=False,
    )

    assert graph_resume_subprocess.main([str(payload_path)]) == 1
    status_update.assert_not_called()


def test_build_state_based_approval_update_materializes_review_outputs_on_approve() -> None:
    update = graph_resume_runtime._build_state_based_approval_update(
        approved=True,
        action=None,
        comment="Looks good to ship.",
        resume_value={"approved": True, "comment": "Looks good to ship."},
        state_values={"workflow_data": {"approved_subject": "Ship it"}},
    )

    assert update["status"] == "in_progress"
    assert update["user_approved"] is True
    assert update["review_feedback"] == "Looks good to ship."
    assert update["reviewer_comments"] == "Looks good to ship."
    assert update["risk_notes"] == "Looks good to ship."
    assert update["workflow_data"] == {
        "approved_subject": "Ship it",
        "reviewer_comments": "Looks good to ship.",
        "risk_notes": "Looks good to ship.",
    }


def test_state_based_test_run_approval_detects_pending_gate_even_with_tasks() -> None:
    snapshot = SimpleNamespace(
        values={
            "task_source": "workflow_test_run",
            "needs_user_approval": True,
            "status": "awaiting_approval",
        },
        tasks=(SimpleNamespace(interrupts=[{"kind": "human_gate"}]),),
        next=("human_approval_gate",),
    )

    assert graph_resume_runtime._is_state_based_test_run_approval(snapshot) is True


def test_state_based_manual_approval_detects_manual_api_gate_without_interrupts() -> None:
    snapshot = SimpleNamespace(
        values={
            "task_source": "manual_api",
            "needs_user_approval": True,
            "status": "awaiting_approval",
        },
        tasks=(),
        next=("human_approval_gate",),
    )

    assert graph_resume_runtime._is_state_based_manual_approval(snapshot) is True


def test_state_based_manual_approval_does_not_steal_live_interrupt_resume() -> None:
    snapshot = SimpleNamespace(
        values={
            "task_source": "manual_api",
            "needs_user_approval": True,
            "status": "awaiting_approval",
        },
        tasks=(SimpleNamespace(interrupts=[{"kind": "human_gate"}]),),
        next=("human_approval_gate",),
    )

    assert graph_resume_runtime._is_state_based_manual_approval(snapshot) is False


def test_run_graph_resume_repairs_missing_task_envelope_for_live_interrupt() -> None:
    logger = MagicMock()
    graph = MagicMock()
    graph.get_state.side_effect = [
        SimpleNamespace(
            tasks=(SimpleNamespace(interrupts=[{"kind": "human_gate"}]),),
            next=("human_approval_gate",),
            values={
                "task_source": "manual_api",
                "needs_user_approval": True,
                "status": "awaiting_approval",
                "current_step_id": "analysis",
            },
        ),
        SimpleNamespace(tasks=()),
    ]
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = {
        "id": 42,
        "project_id": 7,
        "meta": {"test_mode": True, "selected_workflow_id": "weekly_report"},
    }
    job_thread_store.get_job_meta_by_thread_id.return_value = {
        "meta": {
            "task_envelope": {
                "title": "Repair missing envelope",
                "inputs": {"task_summary": "Use approved requirements"},
            },
            "inputs": {"task_summary": "Use approved requirements"},
        }
    }

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
        return_value={"status": "completed"},
    ) as run_mock:
        graph_resume_runtime.run_graph_resume(
            "thread-live-interrupt",
            {"approved": True, "comment": "Approved"},
            True,
            "approve",
            "Approved",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=lambda **_kwargs: graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            approval_id="gate-live",
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    resume_command = run_mock.call_args.args[1]
    assert resume_command.resume == {"approved": True, "comment": "Approved"}
    assert resume_command.update["approval_resume_applied_id"] == "gate-live"
    assert resume_command.update["task_envelope"]["title"] == "Repair missing envelope"
    assert resume_command.update["inputs"] == {"task_summary": "Use approved requirements"}
    graph.update_state.assert_not_called()


def test_snapshot_has_pending_interrupts_detects_interrupt_tasks() -> None:
    snapshot = SimpleNamespace(
        tasks=(SimpleNamespace(interrupts=[{"kind": "human_gate"}]),),
    )

    assert graph_resume_runtime._snapshot_has_pending_interrupts(snapshot) is True


def test_run_graph_resume_keeps_state_based_resume_for_live_test_run_interrupt() -> None:
    logger = MagicMock()
    reader_graph = MagicMock()
    reader_graph.get_state.return_value = SimpleNamespace(
        values={
            "task_source": "workflow_test_run",
            "workflow_id": "monthly_review",
        }
    )
    graph = MagicMock()
    graph.get_state.side_effect = [
        SimpleNamespace(
            tasks=(SimpleNamespace(interrupts=[{"kind": "human_gate"}]),),
            next=("human_approval_gate",),
            values={
                "task_source": "workflow_test_run",
                "needs_user_approval": True,
                "status": "awaiting_approval",
                "current_step_id": "approval_checkpoint",
            },
        ),
        SimpleNamespace(tasks=()),
    ]
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = None
    job_thread_store.get_job_meta_by_thread_id.return_value = None
    build_graph = MagicMock(side_effect=[reader_graph, graph])

    events: list[str] = []
    graph.update_state.side_effect = lambda *_args, **_kwargs: events.append("checkpoint")
    with (
        patch(
            "core.application.orchestration.agents.common.run_graph_with_cancel_check",
            side_effect=lambda *_args, **_kwargs: events.append("run") or {"status": "completed"},
        ) as run_mock,
    ):
        graph_resume_runtime.run_graph_resume(
            "test-run-live-interrupt",
            {"approved": True, "comment": "Looks good"},
            True,
            "approve",
            "Looks good",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            approval_id="gate-1",
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    graph.update_state.assert_called_once()
    update_payload = graph.update_state.call_args.args[1]
    assert update_payload["user_approved"] is True
    assert update_payload["reviewer_comments"] == "Looks good"
    assert update_payload["risk_notes"] == "Looks good"
    assert events == ["checkpoint", "run"]
    # BUG-3: approve/reject must re-enter the orchestrator hub. human_approval_gate
    # has no static outgoing edge, so the resume seeds the hub explicitly via
    # Command(goto="orchestrator") instead of stream(None) (which would hang).
    resume_command = run_mock.call_args.args[1]
    assert getattr(resume_command, "goto", None) == "orchestrator"


def test_run_graph_resume_shutdown_is_retryable_without_rewriting_run_status(monkeypatch):
    set_status = MagicMock()
    monkeypatch.setattr(graph_resume_runtime, "set_job_status_by_thread_id", set_status)
    logger = MagicMock()

    outcome = graph_resume_runtime.run_graph_resume(
        "thread-1",
        {},
        True,
        None,
        "",
        {},
        resume_shutdown_in_progress=lambda: True,
        get_checkpointer=MagicMock(),
        build_graph=MagicMock(),
        get_logger_only=MagicMock(),
        job_thread_store=MagicMock(),
        to_json_obj=lambda value: value,
        append_approval_history=lambda items, entry: [*items, entry],
        build_approval_entry=lambda **kwargs: kwargs,
        logger=logger,
    )

    set_status.assert_not_called()
    assert outcome is not None
    assert outcome.started is False
    assert outcome.retryable is True


def test_run_graph_resume_missing_checkpoint_is_retryable_without_rewriting_run_status(monkeypatch):
    set_status = MagicMock()
    monkeypatch.setattr(graph_resume_runtime, "set_job_status_by_thread_id", set_status)
    logger = MagicMock()
    build_graph = MagicMock()

    outcome = graph_resume_runtime.run_graph_resume(
        "thread-2",
        {},
        True,
        None,
        "",
        {},
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: None,
        build_graph=build_graph,
        get_logger_only=MagicMock(),
        job_thread_store=MagicMock(),
        to_json_obj=lambda value: value,
        append_approval_history=lambda items, entry: [*items, entry],
        build_approval_entry=lambda **kwargs: kwargs,
        logger=logger,
        checkpointer_attempts=2,
        retry_delay_seconds=0,
        sleep=lambda _seconds: None,
    )

    set_status.assert_not_called()
    assert outcome is not None
    assert outcome.started is False
    assert outcome.retryable is True
    build_graph.assert_not_called()


def test_start_graph_resume_thread_tracks_and_unregisters_thread() -> None:
    resume_threads: set[threading.Thread] = set()
    resume_threads_lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    def _run_graph_resume(*_args, **_kwargs) -> None:
        entered.set()
        release.wait(2.0)

    def _unregister_resume_thread(thread: threading.Thread) -> None:
        graph_resume_runtime.unregister_resume_thread(
            thread,
            resume_threads=resume_threads,
            resume_threads_lock=resume_threads_lock,
        )

    thread = graph_resume_runtime.start_graph_resume_thread(
        "thread-track",
        {"approved": True},
        True,
        None,
        "ok",
        {},
        run_graph_resume=_run_graph_resume,
        unregister_resume_thread=_unregister_resume_thread,
        resume_shutdown_in_progress=lambda: False,
        resume_threads=resume_threads,
        resume_threads_lock=resume_threads_lock,
        logger=MagicMock(),
    )

    try:
        assert thread is not None
        assert entered.wait(0.5)
        active = graph_resume_runtime.snapshot_resume_threads(
            resume_threads=resume_threads,
            resume_threads_lock=resume_threads_lock,
        )
        assert any(item.name == thread.name for item in active)
    finally:
        release.set()
        lingering = graph_resume_runtime.join_resume_threads(
            resume_threads=resume_threads,
            resume_threads_lock=resume_threads_lock,
            timeout_seconds=2.0,
        )

    assert lingering == 0
    active = graph_resume_runtime.snapshot_resume_threads(
        resume_threads=resume_threads,
        resume_threads_lock=resume_threads_lock,
    )
    assert thread.name not in {item.name for item in active}


def test_project_resume_fence_joins_owned_runner_and_rejects_new_launches() -> None:
    resume_threads: set[threading.Thread] = set()
    resume_threads_lock = threading.Lock()
    fenced_project_ids: set[str] = set()
    entered = threading.Event()
    release = threading.Event()

    def _run_graph_resume(*_args, **_kwargs) -> None:
        entered.set()
        release.wait(2.0)

    def _unregister_resume_thread(thread: threading.Thread) -> None:
        graph_resume_runtime.unregister_resume_thread(
            thread,
            resume_threads=resume_threads,
            resume_threads_lock=resume_threads_lock,
        )

    thread = graph_resume_runtime.start_graph_resume_thread(
        "thread-project-a",
        {"approved": False},
        False,
        "abort",
        "cancelled",
        {},
        project_id="project-a",
        run_graph_resume=_run_graph_resume,
        unregister_resume_thread=_unregister_resume_thread,
        resume_shutdown_in_progress=lambda: False,
        resume_threads=resume_threads,
        resume_threads_lock=resume_threads_lock,
        fenced_project_ids=fenced_project_ids,
        logger=MagicMock(),
    )

    try:
        assert thread is not None
        assert entered.wait(0.5)
        assert (
            graph_resume_runtime.fence_project_resume_threads(
                "project-a",
                resume_threads=resume_threads,
                resume_threads_lock=resume_threads_lock,
                fenced_project_ids=fenced_project_ids,
                timeout_seconds=0,
            )
            == 1
        )
        assert (
            graph_resume_runtime.start_graph_resume_thread(
                "thread-project-a-late",
                {"approved": True},
                True,
                None,
                "late",
                {},
                project_id="project-a",
                run_graph_resume=_run_graph_resume,
                unregister_resume_thread=_unregister_resume_thread,
                resume_shutdown_in_progress=lambda: False,
                resume_threads=resume_threads,
                resume_threads_lock=resume_threads_lock,
                fenced_project_ids=fenced_project_ids,
                logger=MagicMock(),
            )
            is None
        )
    finally:
        release.set()

    assert (
        graph_resume_runtime.fence_project_resume_threads(
            "project-a",
            resume_threads=resume_threads,
            resume_threads_lock=resume_threads_lock,
            fenced_project_ids=fenced_project_ids,
            timeout_seconds=2,
        )
        == 0
    )
    graph_resume_runtime.unfence_project_resume_threads(
        "project-a",
        resume_threads_lock=resume_threads_lock,
        fenced_project_ids=fenced_project_ids,
    )
    assert fenced_project_ids == set()


def test_start_graph_resume_thread_can_launch_subprocess_without_running_graph_inline() -> None:
    resume_threads: set[threading.Thread] = set()
    resume_threads_lock = threading.Lock()
    launched = threading.Event()
    release = threading.Event()
    run_graph_resume = MagicMock()

    def _launch_subprocess(**kwargs) -> int:
        assert kwargs["thread_id"] == "thread-process"
        assert kwargs["resume_value"] == {"approved": True}
        launched.set()
        release.wait(2.0)
        return 0

    def _unregister_resume_thread(thread: threading.Thread) -> None:
        graph_resume_runtime.unregister_resume_thread(
            thread,
            resume_threads=resume_threads,
            resume_threads_lock=resume_threads_lock,
        )

    thread = graph_resume_runtime.start_graph_resume_thread(
        "thread-process",
        {"approved": True},
        True,
        None,
        "ok",
        {},
        run_graph_resume=run_graph_resume,
        unregister_resume_thread=_unregister_resume_thread,
        resume_shutdown_in_progress=lambda: False,
        resume_threads=resume_threads,
        resume_threads_lock=resume_threads_lock,
        logger=MagicMock(),
        execution_mode="subprocess",
        launch_subprocess=_launch_subprocess,
    )

    try:
        assert thread is not None
        assert launched.wait(0.5)
        run_graph_resume.assert_not_called()
        active = graph_resume_runtime.snapshot_resume_threads(
            resume_threads=resume_threads,
            resume_threads_lock=resume_threads_lock,
        )
        assert any(item.name == thread.name for item in active)
    finally:
        release.set()
        lingering = graph_resume_runtime.join_resume_threads(
            resume_threads=resume_threads,
            resume_threads_lock=resume_threads_lock,
            timeout_seconds=2.0,
        )

    assert lingering == 0


def test_launch_graph_resume_subprocess_writes_payload_and_removes_file(tmp_path) -> None:
    captured: dict[str, object] = {}

    class _FakeProcess:
        def __init__(self, command: list[str]):
            captured["command"] = command
            payload_path = Path(command[-1])
            captured["payload_path"] = str(payload_path)
            captured["payload"] = json.loads(payload_path.read_text(encoding="utf-8"))

        @staticmethod
        def wait() -> int:
            return 0

    result = graph_resume_runtime.launch_graph_resume_subprocess(
        thread_id="thread-payload",
        resume_value={"approved": True, "comment": "ok"},
        approved=True,
        action=None,
        comment="ok",
        interrupt_payload={"stage": "Planning"},
        actor_username="operator",
        python_executable="/usr/bin/python-test",
        payload_dir=str(tmp_path),
        popen_factory=_FakeProcess,
        logger=MagicMock(),
    )

    assert result == 0
    assert captured["command"] == [
        "/usr/bin/python-test",
        "-m",
        "core.infrastructure.runtime.graph_resume_subprocess",
        captured["payload_path"],
    ]
    assert captured["payload"] == {
        "thread_id": "thread-payload",
        "resume_value": {"approved": True, "comment": "ok"},
        "approved": True,
        "action": None,
        "comment": "ok",
        "interrupt_payload": {"stage": "Planning"},
        "actor_username": "operator",
    }
    assert not Path(str(captured["payload_path"])).exists()


def test_start_graph_resume_thread_returns_none_when_shutdown_active() -> None:
    thread = graph_resume_runtime.start_graph_resume_thread(
        "thread-stop",
        {},
        True,
        None,
        "",
        {},
        run_graph_resume=MagicMock(),
        unregister_resume_thread=MagicMock(),
        resume_shutdown_in_progress=lambda: True,
        resume_threads=set(),
        resume_threads_lock=threading.Lock(),
        logger=MagicMock(),
    )

    assert thread is None


def test_detect_graph_status_marks_non_terminal_resume_as_failed() -> None:
    class _Graph:
        @staticmethod
        def get_state(_config):
            return SimpleNamespace(tasks=[])

    status, error, terminal = graph_resume_runtime._detect_graph_status(
        graph=_Graph(),
        config={"configurable": {"thread_id": "t-1"}},
        result={"status": "in_progress"},
        invoke_error=None,
    )

    assert "completed" in terminal
    assert status == "failed"
    assert "non-terminal status 'in_progress'" in str(error)


def test_detect_graph_status_marks_awaiting_approval_when_interrupt_present() -> None:
    interrupt_task = SimpleNamespace(interrupts=[{"kind": "human_gate"}])

    class _Graph:
        @staticmethod
        def get_state(_config):
            return SimpleNamespace(tasks=[interrupt_task])

    status, _error, _terminal = graph_resume_runtime._detect_graph_status(
        graph=_Graph(),
        config={"configurable": {"thread_id": "t-2"}},
        result={"status": "in_progress"},
        invoke_error=None,
    )

    assert status == "awaiting_approval"


def test_detect_graph_status_preserves_stuck_terminal_status() -> None:
    class _Graph:
        @staticmethod
        def get_state(_config):
            raise AssertionError("terminal stuck status must not probe interrupts")

    status, error, _terminal = graph_resume_runtime._detect_graph_status(
        graph=_Graph(),
        config={"configurable": {"thread_id": "t-stuck"}},
        result={"status": "stuck", "error": "Provider retries exhausted"},
        invoke_error=None,
    )

    assert status == "stuck"
    assert error == "Provider retries exhausted"


def test_run_graph_resume_builds_graph_for_current_job_workflow_only() -> None:
    logger = MagicMock()
    graph = MagicMock()
    graph.get_state.return_value = SimpleNamespace(tasks=[])
    bl = MagicMock()
    bl.conn = MagicMock()
    bl.get_job.return_value = {
        "id": "42",
        "project_id": "7",
        "selected_workflow": "weekly_report",
        "meta": {"selected_workflow_id": "weekly_report"},
    }
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = {
        "id": "42",
        "project_id": "7",
        "meta": {"selected_workflow_id": "weekly_report"},
    }
    job_thread_store.get_job_meta_by_thread_id.return_value = {"meta": {}}
    build_graph = MagicMock(return_value=graph)

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
        return_value={"status": "completed"},
    ):
        graph_resume_runtime.run_graph_resume(
            "thread-wf",
            {"approved": True},
            True,
            "approve",
            "",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    assert build_graph.call_args.kwargs["workflow_ids"] == ["weekly_report"]


def test_run_graph_resume_resolves_workflow_from_checkpoint_for_ephemeral_test_run() -> None:
    logger = MagicMock()
    reader_graph = MagicMock()
    reader_graph.get_state.return_value = SimpleNamespace(
        values={
            "task_source": "workflow_test_run",
            "workflow_id": "test",
            "task_envelope": {
                "metadata": {"requested_workflow_id": "test"},
            },
        }
    )
    runtime_graph = MagicMock()
    runtime_graph.get_state.side_effect = [
        SimpleNamespace(
            tasks=(),
            next=(),
            values={
                "task_source": "workflow_test_run",
                "needs_user_approval": True,
                "status": "awaiting_approval",
            },
        ),
        SimpleNamespace(tasks=()),
    ]
    build_graph = MagicMock(side_effect=[reader_graph, runtime_graph])
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = None
    job_thread_store.get_job_meta_by_thread_id.return_value = None

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
        return_value={"status": "completed"},
    ):
        graph_resume_runtime.run_graph_resume(
            "thread-test-run",
            {"approved": True},
            True,
            "approve",
            "",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    assert build_graph.call_args_list[0].kwargs["validate_contracts"] is False
    assert build_graph.call_args_list[1].kwargs["workflow_ids"] == ["test"]


def test_run_graph_resume_uses_state_based_resume_for_workflow_test_run_without_interrupt() -> None:
    logger = MagicMock()
    reader_graph = MagicMock()
    reader_graph.get_state.return_value = SimpleNamespace(
        values={
            "task_source": "workflow_test_run",
            "workflow_id": "monthly_review",
        }
    )
    graph = MagicMock()
    graph.get_state.side_effect = [
        SimpleNamespace(
            tasks=(),
            next=(),
            values={
                "task_source": "workflow_test_run",
                "needs_user_approval": True,
                "status": "awaiting_approval",
            },
        ),
        SimpleNamespace(tasks=()),
    ]
    graph.update_state.return_value = {
        "configurable": {"thread_id": "thread-test-run"},
    }
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = None
    job_thread_store.get_job_meta_by_thread_id.return_value = None
    build_graph = MagicMock(side_effect=[reader_graph, graph])

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
        return_value={"status": "completed"},
    ) as run_mock:
        graph_resume_runtime.run_graph_resume(
            "thread-test-run",
            {"approved": True, "comment": "Looks good"},
            True,
            "approve",
            "Looks good",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    graph.update_state.assert_called_once()
    update_payload = graph.update_state.call_args.args[1]
    assert update_payload["user_approved"] is True
    assert update_payload["needs_user_approval"] is False
    assert update_payload["review_feedback"] == "Looks good"
    assert update_payload["reviewer_comments"] == "Looks good"
    assert update_payload["risk_notes"] == "Looks good"
    assert update_payload["workflow_data"] == {
        "reviewer_comments": "Looks good",
        "risk_notes": "Looks good",
    }
    assert update_payload["status"] == "in_progress"
    journal = update_payload.get("orchestrator_journal") or []
    assert journal[0]["event"] == "approval_resume_decision"
    assert journal[0]["detail"]["operator_comment"] == "Looks good"
    # BUG-3: approve/reject must re-enter the orchestrator hub. human_approval_gate
    # has no static outgoing edge, so the resume seeds the hub explicitly via
    # Command(goto="orchestrator") instead of stream(None) (which would hang).
    resume_command = run_mock.call_args.args[1]
    assert getattr(resume_command, "goto", None) == "orchestrator"


def test_run_graph_resume_uses_state_based_resume_for_manual_api_approval() -> None:
    logger = MagicMock()
    reader_graph = MagicMock()
    reader_graph.get_state.return_value = SimpleNamespace(
        values={
            "task_source": "manual_api",
            "workflow_id": "weekly_digest",
        }
    )
    graph = MagicMock()
    graph.get_state.side_effect = [
        SimpleNamespace(
            tasks=(),
            next=(),
            values={
                "task_source": "manual_api",
                "needs_user_approval": True,
                "status": "awaiting_approval",
                "current_step_id": "quality",
            },
        ),
        SimpleNamespace(tasks=()),
    ]
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = None
    job_thread_store.get_job_meta_by_thread_id.return_value = None
    build_graph = MagicMock(side_effect=[reader_graph, graph])

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
        return_value={"status": "completed"},
    ) as run_mock:
        graph_resume_runtime.run_graph_resume(
            "thread-manual-api",
            {"approved": True, "comment": "Manual checks passed"},
            True,
            "approve",
            "Manual checks passed",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    graph.update_state.assert_called_once()
    update_payload = graph.update_state.call_args.args[1]
    assert update_payload["user_approved"] is True
    assert update_payload["reviewer_comments"] == "Manual checks passed"
    assert update_payload["orchestrator_journal"][0]["step_id"] == "quality"
    # BUG-3: approve/reject must re-enter the orchestrator hub. human_approval_gate
    # has no static outgoing edge, so the resume seeds the hub explicitly via
    # Command(goto="orchestrator") instead of stream(None) (which would hang).
    resume_command = run_mock.call_args.args[1]
    assert getattr(resume_command, "goto", None) == "orchestrator"


def test_run_graph_resume_uses_state_based_resume_for_manual_api_override() -> None:
    logger = MagicMock()
    reader_graph = MagicMock()
    reader_graph.get_state.return_value = SimpleNamespace(
        values={
            "task_source": "manual_api",
            "workflow_id": "content_review",
        }
    )
    graph = MagicMock()
    graph.get_state.side_effect = [
        SimpleNamespace(
            tasks=(),
            next=(),
            values={
                "task_source": "manual_api",
                "needs_user_approval": True,
                "status": "awaiting_approval",
                "current_step_id": "analysis",
            },
        ),
        SimpleNamespace(tasks=()),
    ]
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = None
    job_thread_store.get_job_meta_by_thread_id.return_value = None
    build_graph = MagicMock(side_effect=[reader_graph, graph])

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
        return_value={"status": "completed"},
    ) as run_mock:
        graph_resume_runtime.run_graph_resume(
            "thread-manual-override",
            {
                "action": "override",
                "approved": True,
                "comment": "Use operator copy",
                "override_content": "Approved copy",
            },
            False,
            "override",
            "Use operator copy",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    update_payload = graph.update_state.call_args.args[1]
    assert update_payload["user_approved"] is True
    assert update_payload["review_feedback"] == "Use operator copy"
    # The hub keys autonomy approvals by step execution itself; a resume never
    # writes them.
    assert "autonomy_gate_approvals" not in update_payload
    # A state-based resume has no declared gate mapping, so the override content
    # is never written into task state.
    assert "initial_requirements" not in update_payload
    assert update_payload["orchestrator_journal"][0]["detail"]["approval_action"] == "override"
    # BUG-3: approve/reject must re-enter the orchestrator hub. human_approval_gate
    # has no static outgoing edge, so the resume seeds the hub explicitly via
    # Command(goto="orchestrator") instead of stream(None) (which would hang).
    resume_command = run_mock.call_args.args[1]
    assert getattr(resume_command, "goto", None) == "orchestrator"


def test_run_graph_resume_state_based_test_run_does_not_reassert_awaiting_approval() -> None:
    logger = MagicMock()
    reader_graph = MagicMock()
    reader_graph.get_state.return_value = SimpleNamespace(
        values={
            "task_source": "workflow_test_run",
            "workflow_id": "monthly_review",
        }
    )
    graph = MagicMock()
    graph.get_state.side_effect = [
        SimpleNamespace(
            tasks=(),
            next=(),
            values={
                "task_source": "workflow_test_run",
                "needs_user_approval": True,
                "status": "awaiting_approval",
            },
        )
    ]
    graph.update_state.return_value = {
        "configurable": {"thread_id": "thread-test-run"},
    }
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = None
    job_thread_store.get_job_meta_by_thread_id.return_value = None
    build_graph = MagicMock(side_effect=[reader_graph, graph])

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
        return_value={"status": "in_progress"},
    ):
        graph_resume_runtime.run_graph_resume(
            "thread-test-run",
            {"approved": True, "comment": "Looks good"},
            True,
            "approve",
            "Looks good",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    job_thread_store.update_job_status_by_thread_id.assert_called_once_with(
        bl.conn,
        thread_id="thread-test-run",
        status="in_progress",
        commit=True,
    )


def test_run_graph_resume_uses_state_based_resume_for_stale_test_run_snapshot() -> None:
    logger = MagicMock()
    reader_graph = MagicMock()
    reader_graph.get_state.return_value = SimpleNamespace(
        values={
            "task_source": "workflow_test_run",
            "workflow_id": "monthly_review",
        }
    )
    graph = MagicMock()
    graph.get_state.side_effect = [
        SimpleNamespace(
            tasks=(SimpleNamespace(interrupts=[{"kind": "human_gate"}]),),
            next=("human_approval_gate",),
            values={
                "task_source": "workflow_test_run",
                "needs_user_approval": False,
                "status": "in_progress",
                "current_step_id": "finalize_outputs",
            },
        ),
        SimpleNamespace(tasks=()),
    ]
    graph.update_state.return_value = {
        "configurable": {"thread_id": "test-run-stale"},
    }
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = None
    job_thread_store.get_job_meta_by_thread_id.return_value = None
    build_graph = MagicMock(side_effect=[reader_graph, graph])

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
        return_value={"status": "completed"},
    ) as run_mock:
        graph_resume_runtime.run_graph_resume(
            "test-run-stale",
            {"approved": True, "comment": "Final escalation approved"},
            True,
            "approve",
            "Final escalation approved",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    graph.update_state.assert_called_once()
    # BUG-3: approve/reject must re-enter the orchestrator hub. human_approval_gate
    # has no static outgoing edge, so the resume seeds the hub explicitly via
    # Command(goto="orchestrator") instead of stream(None) (which would hang).
    resume_command = run_mock.call_args.args[1]
    assert getattr(resume_command, "goto", None) == "orchestrator"


def test_run_graph_resume_state_based_resume_normalizes_stale_awaiting_after_step_advance() -> None:
    logger = MagicMock()
    reader_graph = MagicMock()
    reader_graph.get_state.return_value = SimpleNamespace(
        values={
            "task_source": "workflow_test_run",
            "workflow_id": "monthly_review",
        }
    )
    graph = MagicMock()
    graph.get_state.side_effect = [
        SimpleNamespace(
            tasks=(),
            next=(),
            values={
                "task_source": "workflow_test_run",
                "needs_user_approval": True,
                "status": "awaiting_approval",
                "run_cursor": {"step_id": "approval_checkpoint", "step_execution_id": "e-1"},
            },
        ),
        SimpleNamespace(
            tasks=(),
            next=(),
            values={
                "task_source": "workflow_test_run",
                "needs_user_approval": True,
                "status": "awaiting_approval",
                "run_cursor": {"step_id": "finalize_outputs", "step_execution_id": "e-2"},
            },
        ),
    ]
    graph.update_state.return_value = {
        "configurable": {"thread_id": "thread-test-run"},
    }
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = None
    job_thread_store.get_job_meta_by_thread_id.return_value = None
    build_graph = MagicMock(side_effect=[reader_graph, graph])

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
        return_value={"status": "awaiting_approval"},
    ):
        graph_resume_runtime.run_graph_resume(
            "thread-test-run",
            {"approved": True, "comment": "Looks good"},
            True,
            "approve",
            "Looks good",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    job_thread_store.update_job_status_by_thread_id.assert_called_once_with(
        bl.conn,
        thread_id="thread-test-run",
        status="in_progress",
        commit=True,
    )


def test_run_graph_resume_state_based_resume_prefers_terminal_checkpoint_status() -> None:
    logger = MagicMock()
    reader_graph = MagicMock()
    reader_graph.get_state.return_value = SimpleNamespace(
        values={
            "task_source": "workflow_test_run",
            "workflow_id": "sample_product_catalog_enrichment",
        }
    )
    graph = MagicMock()
    graph.get_state.side_effect = [
        SimpleNamespace(
            tasks=(),
            next=(),
            values={
                "task_source": "workflow_test_run",
                "needs_user_approval": True,
                "status": "awaiting_approval",
                "current_step_id": "validate_rows",
            },
        ),
        SimpleNamespace(
            tasks=(),
            next=(),
            values={
                "task_source": "workflow_test_run",
                "needs_user_approval": False,
                "status": "stuck",
                "error": "Required outputs are missing after retry/path limits",
                "current_step_id": "validate_rows",
            },
        ),
    ]
    graph.update_state.return_value = {
        "configurable": {"thread_id": "thread-test-run"},
    }
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = None
    job_thread_store.get_job_meta_by_thread_id.return_value = None
    build_graph = MagicMock(side_effect=[reader_graph, graph])

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
        return_value={"status": "in_progress"},
    ):
        outcome = graph_resume_runtime.run_graph_resume(
            "thread-test-run",
            {"approved": True, "comment": "Looks good"},
            True,
            "approve",
            "Looks good",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            defer_terminal_persistence=True,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    assert outcome is not None
    assert outcome.status == "stuck"
    assert outcome.terminal is True
    assert outcome.error == "Required outputs are missing after retry/path limits"


def test_run_graph_resume_state_based_test_run_persists_failed_checkpoint_on_invoke_error() -> None:
    logger = MagicMock()
    reader_graph = MagicMock()
    reader_graph.get_state.return_value = SimpleNamespace(
        values={
            "task_source": "workflow_test_run",
            "workflow_id": "monthly_review",
        }
    )
    graph = MagicMock()
    graph.get_state.side_effect = [
        SimpleNamespace(
            tasks=(),
            next=(),
            values={
                "task_source": "workflow_test_run",
                "needs_user_approval": True,
                "status": "awaiting_approval",
                "current_step_id": "approval_checkpoint",
            },
        )
    ]
    graph.update_state.return_value = {
        "configurable": {"thread_id": "thread-test-run"},
    }
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = None
    job_thread_store.get_job_meta_by_thread_id.return_value = None
    build_graph = MagicMock(side_effect=[reader_graph, graph])

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
        side_effect=RuntimeError("Recursion limit of 10000 reached"),
    ):
        graph_resume_runtime.run_graph_resume(
            "thread-test-run",
            {"approved": True, "comment": "Looks good"},
            True,
            "approve",
            "Looks good",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    assert graph.update_state.call_count == 2
    failed_update = graph.update_state.call_args_list[1].args[1]
    assert failed_update["status"] == "failed"
    assert failed_update["needs_user_approval"] is False
    assert "Recursion limit" in failed_update["final_report"]


def test_run_graph_resume_state_based_abort_persists_cancelled_without_reentry() -> None:
    # BUG-3 terminal branch: an abort decision yields status "cancelled" and must
    # be persisted via update_state WITHOUT re-entering the orchestrator hub.
    logger = MagicMock()
    reader_graph = MagicMock()
    reader_graph.get_state.return_value = SimpleNamespace(
        values={
            "task_source": "workflow_test_run",
            "workflow_id": "monthly_review",
        }
    )
    graph = MagicMock()
    graph.get_state.side_effect = [
        SimpleNamespace(
            tasks=(),
            next=(),
            values={
                "task_source": "workflow_test_run",
                "needs_user_approval": True,
                "status": "awaiting_approval",
                "current_step_id": "approval_checkpoint",
            },
        ),
        SimpleNamespace(tasks=()),
    ]
    graph.update_state.return_value = {"configurable": {"thread_id": "test-run-abort"}}
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = None
    job_thread_store.get_job_meta_by_thread_id.return_value = None
    build_graph = MagicMock(side_effect=[reader_graph, graph])

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
    ) as run_mock:
        graph_resume_runtime.run_graph_resume(
            "test-run-abort",
            {"action": "abort", "approved": False, "comment": "Stop this run"},
            False,
            "abort",
            "Stop this run",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=build_graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=logger,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    # Terminal abort: state persisted, but the graph must NOT be re-driven.
    graph.update_state.assert_called_once()
    abort_update = graph.update_state.call_args.args[1]
    assert abort_update["status"] == "cancelled"
    assert abort_update["needs_user_approval"] is False
    run_mock.assert_not_called()


def test_provider_sync_extension_unavailable_is_non_blocking_diagnostic() -> None:
    from core.infrastructure.runtime import graph_resume_runtime

    bl = MagicMock()

    with patch(
        "core.application.extensions.provider_completion.sync_provider_completion_status",
        side_effect=RuntimeError("Extension 'fixture-provider' is not available"),
    ):
        graph_resume_runtime._sync_provider_status(
            bl=bl,
            job_id="job-1",
            graph_status="completed",
            logger=MagicMock(),
        )

    bl.log_job_error.assert_not_called()


def test_provider_sync_generic_extension_unavailable_is_non_blocking_diagnostic() -> None:
    from core.infrastructure.runtime import graph_resume_runtime

    bl = MagicMock()

    with patch(
        "core.application.extensions.provider_completion.sync_provider_completion_status",
        side_effect=RuntimeError("Extension 'jira' is not configured"),
    ):
        graph_resume_runtime._sync_provider_status(
            bl=bl,
            job_id="job-1",
            graph_status="completed",
            logger=MagicMock(),
        )

    bl.log_job_error.assert_not_called()


def test_persist_graph_resume_result_commits_non_terminal_status_updates() -> None:
    bl = MagicMock()
    bl.conn = MagicMock()
    job_thread_store = MagicMock()
    logger = MagicMock()

    graph_resume_runtime._persist_graph_resume_result(
        bl=bl,
        job_id=42,
        thread_id="thread-nonterminal",
        graph_status="in_progress",
        graph_error=None,
        result={"status": "in_progress"},
        resume_test_mode=True,
        job_thread_store=job_thread_store,
        logger=logger,
    )

    job_thread_store.update_job_status_by_thread_id.assert_called_once_with(
        bl.conn,
        thread_id="thread-nonterminal",
        status="in_progress",
        commit=True,
    )


def test_persist_graph_resume_result_uses_shared_terminal_success_path() -> None:
    bl = MagicMock()
    terminal_service = MagicMock()
    terminal_service.complete_success.return_value = SimpleNamespace(status="completed")

    with patch(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService",
        return_value=terminal_service,
    ):
        graph_resume_runtime._persist_graph_resume_result(
            bl=bl,
            job_id="job-1",
            thread_id="thread-1",
            graph_status="completed",
            graph_error=None,
            result={
                "status": "completed",
                "workflow_outputs": {"outputs": {"ticket": "T-1"}, "artifacts": {}},
                "final_report": "internal",
            },
            resume_test_mode=True,
            job_thread_store=MagicMock(),
            logger=MagicMock(),
            terminal_persistence_factory=lambda _bl, **_kwargs: terminal_service,
        )

    terminal_service.complete_success.assert_called_once_with(
        job_id="job-1",
        workflow_outputs={"outputs": {"ticket": "T-1"}, "artifacts": {}},
        final_report="internal",
    )
    bl.finish_job.assert_not_called()


def test_a_state_based_test_run_resume_that_reaches_a_wait_parks_on_its_timer() -> None:
    """After a state-based approval, the run's next durable pause decides the status."""
    wait = {
        "wait_id": "w-1",
        "kind": "timer",
        "step_id": "pause",
        "step_execution_id": "e-1",
        "attempt": 1,
        "due_at": "2026-10-12T18:00:00+00:00",
        "due_epoch_ms": 1791828000000,
    }
    interrupt_task = SimpleNamespace(
        interrupts=[SimpleNamespace(value={"gate_kind": "wait", **wait})]
    )

    class _Graph:
        @staticmethod
        def get_state(_config):
            return SimpleNamespace(tasks=[interrupt_task], values={"pending_wait": wait})

    status, error, _terminal = graph_resume_runtime._detect_graph_status(
        graph=_Graph(),
        config={"configurable": {"thread_id": "t-wait"}},
        result={"status": "in_progress"},
        invoke_error=None,
        skip_post_interrupt_probe=True,
    )

    assert (status, error) == ("awaiting_timer", None)


def test_a_state_based_resume_without_a_durable_pause_stays_in_progress() -> None:
    class _Graph:
        @staticmethod
        def get_state(_config):
            return SimpleNamespace(tasks=[], values={})

    status, _error, _terminal = graph_resume_runtime._detect_graph_status(
        graph=_Graph(),
        config={"configurable": {"thread_id": "t-state"}},
        result={"status": "in_progress"},
        invoke_error=None,
        skip_post_interrupt_probe=True,
    )

    assert status == "in_progress"


@pytest.mark.parametrize("wait_kind", ["timer", "child"])
@pytest.mark.parametrize("projection", ["available", "unavailable", "missing", "wrong_kind"])
def test_an_approved_test_run_that_reaches_its_wait_parks_with_a_claimable_timer(
    monkeypatch, wait_kind, projection
) -> None:
    """Stand regression: test-run approval → Wait must park awaiting_timer, not stay running.

    A builder test run's Approval is state-based (no interrupt). The resume applies
    the decision and runs on to the Wait step, which pauses with a durable interrupt;
    the finalizer gets the timer's workflow_wait (with its due_epoch_ms), which the
    control loop's due-timer claim reads.
    """
    wait = {
        "wait_id": "w-pause",
        "kind": wait_kind,
        "child_job_id": "child-1",
        "step_id": "pause",
        "step_execution_id": "e-pause",
        "attempt": 1,
        "workflow_revision": 2,
        "started_at": "2026-10-12T17:44:51+00:00",
        "duration_ms": 30000,
        "due_at": "2026-10-12T17:45:21+00:00",
        "due_epoch_ms": 1791827121000,
    }
    approval_state = SimpleNamespace(
        tasks=(),
        next=(),
        values={
            "task_source": "workflow_test_run",
            "workflow_id": "approval_wait_demo",
            "needs_user_approval": True,
            "status": "awaiting_approval",
        },
    )
    parked_on_wait = SimpleNamespace(
        tasks=(SimpleNamespace(interrupts=[SimpleNamespace(value={"gate_kind": "wait", **wait})]),),
        next=("wait_step",),
        values={"task_source": "workflow_test_run", "status": "in_progress", "pending_wait": wait},
    )

    class _Graph:
        def __init__(self) -> None:
            self.decided = False

        def get_state(self, _config):
            return parked_on_wait if self.decided else approval_state

        def update_state(self, _config, _update, as_node=None):
            assert as_node == "human_approval_gate"
            self.decided = True

    graph = _Graph()
    original_detect = graph_resume_runtime._detect_graph_status

    def detect_then_lose_projection(**kwargs):
        detected = original_detect(**kwargs)
        if projection == "unavailable":
            graph.get_state = MagicMock(side_effect=RuntimeError("checkpoint read unavailable"))
        elif projection == "missing":
            graph.get_state = MagicMock(return_value=SimpleNamespace(tasks=(), values={}))
        elif projection == "wrong_kind":
            other = {**wait, "kind": "child" if wait_kind == "timer" else "timer"}
            graph.get_state = MagicMock(
                return_value=SimpleNamespace(
                    tasks=(
                        SimpleNamespace(
                            interrupts=[SimpleNamespace(value={"gate_kind": "wait", **other})]
                        ),
                    ),
                    values={},
                )
            )
        return detected

    monkeypatch.setattr(graph_resume_runtime, "_detect_graph_status", detect_then_lose_projection)
    job_thread_store = MagicMock()
    job_thread_store.get_job_context_by_thread_id.return_value = {
        "id": "job-1",
        "project_id": "p-1",
        "meta": {"test_mode": True, "selected_workflow_id": "approval_wait_demo"},
    }
    job_thread_store.get_job_meta_by_thread_id.return_value = {"meta": {}}
    bl = MagicMock()
    bl.conn = MagicMock()

    with patch(
        "core.application.orchestration.agents.common.run_graph_with_cancel_check",
        return_value={"status": "in_progress"},
    ):
        outcome = graph_resume_runtime.run_graph_resume(
            "thread-test-run",
            {"reason": "approval", "approved": True, "comment": ""},
            True,
            None,
            "",
            {},
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            build_graph=lambda **_kwargs: graph,
            get_logger_only=lambda: bl,
            job_thread_store=job_thread_store,
            to_json_obj=lambda value: value,
            append_approval_history=lambda items, entry: [*items, entry],
            build_approval_entry=lambda **kwargs: kwargs,
            logger=MagicMock(),
            approval_id="derived-approval",
            defer_terminal_persistence=True,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    assert outcome is not None
    assert outcome.status == f"awaiting_{wait_kind}"
    if projection != "available":
        assert outcome.retryable is True
        assert outcome.reason == "wait_projection_unavailable"
        assert "workflow_wait" not in outcome.result
        return
    assert outcome.retryable is False
    assert outcome.terminal is False
    projected = outcome.result["workflow_wait"]
    assert (projected["wait_id"], projected["kind"]) == ("w-pause", wait_kind)
    assert int(projected["due_epoch_ms"]) == wait["due_epoch_ms"]


@pytest.mark.parametrize("unavailable", [False, True])
def test_resume_parent_fence_preserves_reason_or_existing_retry_outcome(monkeypatch, unavailable):
    child = {
        "id": "child",
        "project_id": "p",
        "status": "in_progress",
        "meta": {"invoke_parent_job_id": "parent"},
    }
    parent = {
        "id": "parent",
        "project_id": "p",
        "status": "cancelled",
        "meta": {
            "invoke_children": {
                "child": {"child_job_id": "child", "cancel_requested_at": "committed"}
            }
        },
    }
    actions = []

    def get_job(job_id):
        if job_id == "parent" and unavailable:
            raise OSError("store offline")
        return child if job_id == "child" else parent

    class Graph:
        def get_state(self, _config):
            return SimpleNamespace(values={}, tasks=(), next=())

        def stream(self, *_args, **_kwargs):
            actions.append("action")
            yield {"status": "completed"}

    bl = SimpleNamespace(get_job=get_job)
    monkeypatch.setattr(graph_resume_runtime, "_resolve_resume_workflow_id", lambda **kwargs: "wf")
    monkeypatch.setattr(
        graph_resume_runtime, "_attach_resume_callback", lambda **kwargs: (bl, "child", False)
    )
    monkeypatch.setattr(
        graph_resume_runtime, "_build_resume_state_repair_update", lambda **kwargs: {}
    )
    outcome = graph_resume_runtime.run_graph_resume(
        "thread",
        {"approved": True},
        True,
        "approve",
        "",
        {},
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: object(),
        build_graph=lambda **kwargs: Graph(),
        get_logger_only=lambda: bl,
        job_thread_store=SimpleNamespace(),
        to_json_obj=lambda value: value,
        append_approval_history=lambda *args: None,
        build_approval_entry=lambda **kwargs: {},
        logger=MagicMock(),
        defer_terminal_persistence=True,
    )
    assert actions == []
    if unavailable:
        assert outcome.retryable is True
        assert outcome.reason == "invoke_cancellation_fence_unavailable"
        assert outcome.status != "cancelled"
    else:
        assert outcome.retryable is False
        assert outcome.status == "cancelled"
        assert outcome.result["error_code"] == "parent_cancelled"
        assert "parent" in outcome.error


@pytest.mark.parametrize("lease_lost_during_lookup", [False, True])
def test_resume_identity_lookup_failure_stops_timer_before_stream(
    monkeypatch, lease_lost_during_lookup
):
    from core.application.jobs.execution_contracts import WorkflowResumeGraphOutcome

    actions = []
    lease_lost = False
    bl = SimpleNamespace(conn=object(), get_job=MagicMock())

    def unavailable_context(_conn, _thread_id):
        nonlocal lease_lost
        lease_lost = lease_lost_during_lookup
        raise OSError("context store unavailable")

    store = SimpleNamespace(get_job_context_by_thread_id=MagicMock(side_effect=unavailable_context))

    class Graph:
        def get_state(self, _config):
            return SimpleNamespace(
                values={"workflow_id": "wf"},
                tasks=[
                    SimpleNamespace(
                        interrupts=[SimpleNamespace(value={"gate_kind": "wait", "wait_id": "w"})]
                    )
                ],
                next=("wait",),
            )

        def stream(self, *_args, **_kwargs):
            actions.append("timer action")
            yield {"status": "completed"}

    persist_result = MagicMock()
    monkeypatch.setattr(graph_resume_runtime, "_persist_graph_resume_result", persist_result)
    # Isolate the required identity lookup from the earlier workflow-ID hint;
    # lease loss must happen inside the real callback setup, not before it.
    monkeypatch.setattr(graph_resume_runtime, "_resolve_resume_workflow_id", lambda **kwargs: "wf")
    outcome = graph_resume_runtime.run_graph_resume(
        "thread",
        {"reason": "timer", "wait_id": "w", "timer_elapsed": True},
        True,
        None,
        "",
        {},
        resume_shutdown_in_progress=lambda: False,
        resume_cancelled=lambda: lease_lost,
        get_checkpointer=lambda: object(),
        build_graph=lambda **kwargs: Graph(),
        get_logger_only=lambda: bl,
        job_thread_store=store,
        to_json_obj=lambda value: value,
        append_approval_history=lambda *args: None,
        build_approval_entry=lambda **kwargs: {},
        logger=MagicMock(),
        defer_terminal_persistence=True,
    )

    assert actions == []
    store.get_job_context_by_thread_id.assert_called_once_with(bl.conn, "thread")
    bl.get_job.assert_not_called()
    persist_result.assert_not_called()
    if lease_lost_during_lookup:
        assert outcome is None
    else:
        assert outcome == WorkflowResumeGraphOutcome.for_retry(
            reason="invoke_cancellation_fence_unavailable"
        )
