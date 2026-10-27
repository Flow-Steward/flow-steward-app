from __future__ import annotations

import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.jobs.workflow_resume_execution_service import (
    WORKFLOW_RESUME_LEASE_SECONDS,
)
from core.infrastructure.runtime.worker import worker_resume_adapters
from core.infrastructure.runtime.worker.worker_resume_adapters import (
    WorkerWorkflowResumeLeaseAdapter,
)


class _Repository:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def renew_finalizer(self, **kwargs: Any) -> bool:
        self.calls.append(dict(kwargs))
        return False


def _wait_for_lease_loss(handle: Any) -> None:
    deadline = time.monotonic() + 1.0
    while not handle.cancelled() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert handle.cancelled()


def test_finalizer_heartbeat_uses_injected_repository_without_separate_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    factory_calls: list[Any] = []
    monkeypatch.setattr(worker_resume_adapters, "_LEASE_RENEW_SECONDS", 0.01)
    adapter = WorkerWorkflowResumeLeaseAdapter(
        repository=repository,
        bl=SimpleNamespace(dsn=""),
        repository_factory=lambda bl: factory_calls.append(bl) or _Repository(),
    )

    handle = adapter.start_finalizer(
        job_id="job-7",
        resume_revision=7,
        finalizer_token="finalizer-7",
    )
    try:
        _wait_for_lease_loss(handle)
    finally:
        handle.stop()

    assert factory_calls == []
    assert repository.calls == [
        {
            "job_id": "job-7",
            "resume_revision": 7,
            "finalizer_token": "finalizer-7",
            "lease_seconds": WORKFLOW_RESUME_LEASE_SECONDS,
        }
    ]


def test_finalizer_heartbeat_builds_injected_repository_for_separate_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_repository = _Repository()
    heartbeat_repository = _Repository()
    created_loggers: list[Any] = []
    factory_calls: list[Any] = []

    class _HeartbeatLogger:
        def __init__(self, dsn: str) -> None:
            self.dsn = dsn
            self.conn = SimpleNamespace(closed=False)
            self.conn.close = lambda: setattr(self.conn, "closed", True)
            created_loggers.append(self)

    platform_logger_module = ModuleType("core.infrastructure.db.platform_stores")
    platform_logger_module.PlatformWorkerStore = _HeartbeatLogger
    monkeypatch.setitem(
        sys.modules,
        "core.infrastructure.db.platform_stores",
        platform_logger_module,
    )
    monkeypatch.setattr(worker_resume_adapters, "_LEASE_RENEW_SECONDS", 0.01)
    adapter = WorkerWorkflowResumeLeaseAdapter(
        repository=primary_repository,
        bl=SimpleNamespace(dsn="postgres://worker"),
        repository_factory=lambda bl: factory_calls.append(bl) or heartbeat_repository,
    )

    handle = adapter.start_finalizer(
        job_id="job-7",
        resume_revision=7,
        finalizer_token="finalizer-7",
    )
    try:
        _wait_for_lease_loss(handle)
    finally:
        handle.stop()

    assert primary_repository.calls == []
    assert factory_calls == created_loggers
    assert heartbeat_repository.calls == [
        {
            "job_id": "job-7",
            "resume_revision": 7,
            "finalizer_token": "finalizer-7",
            "lease_seconds": WORKFLOW_RESUME_LEASE_SECONDS,
        }
    ]
    assert len(created_loggers) == 1
    assert created_loggers[0].conn.closed is True


def test_finalizer_heartbeat_marks_lease_lost_when_repository_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_repository = _Repository()
    created_loggers: list[Any] = []

    class _HeartbeatLogger:
        def __init__(self, dsn: str) -> None:
            self.dsn = dsn
            self.conn = SimpleNamespace(closed=False)
            self.conn.close = lambda: setattr(self.conn, "closed", True)
            created_loggers.append(self)

    platform_logger_module = ModuleType("core.infrastructure.db.platform_stores")
    platform_logger_module.PlatformWorkerStore = _HeartbeatLogger
    monkeypatch.setitem(
        sys.modules,
        "core.infrastructure.db.platform_stores",
        platform_logger_module,
    )
    adapter = WorkerWorkflowResumeLeaseAdapter(
        repository=primary_repository,
        bl=SimpleNamespace(dsn="postgres://worker"),
        repository_factory=lambda _bl: (_ for _ in ()).throw(
            RuntimeError("repository setup failed")
        ),
    )

    handle = adapter.start_finalizer(
        job_id="job-7",
        resume_revision=7,
        finalizer_token="finalizer-7",
    )
    try:
        _wait_for_lease_loss(handle)
    finally:
        handle.stop()

    assert primary_repository.calls == []
    assert len(created_loggers) == 1
    assert created_loggers[0].conn.closed is True


def test_finalizer_heartbeat_does_not_return_before_repository_setup_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_started = threading.Event()
    release_setup = threading.Event()
    start_returned = threading.Event()
    result: dict[str, Any] = {}

    class _HeartbeatLogger:
        def __init__(self, dsn: str) -> None:
            self.dsn = dsn
            self.conn = SimpleNamespace(close=lambda: None)

    def repository_factory(_bl: Any) -> _Repository:
        setup_started.set()
        release_setup.wait(timeout=1.0)
        raise RuntimeError("repository setup failed")

    platform_logger_module = ModuleType("core.infrastructure.db.platform_stores")
    platform_logger_module.PlatformWorkerStore = _HeartbeatLogger
    monkeypatch.setitem(
        sys.modules,
        "core.infrastructure.db.platform_stores",
        platform_logger_module,
    )
    adapter = WorkerWorkflowResumeLeaseAdapter(
        repository=_Repository(),
        bl=SimpleNamespace(dsn="postgres://worker"),
        repository_factory=repository_factory,
    )

    def start() -> None:
        result["handle"] = adapter.start_finalizer(
            job_id="job-7",
            resume_revision=7,
            finalizer_token="finalizer-7",
        )
        start_returned.set()

    caller = threading.Thread(target=start)
    caller.start()
    assert setup_started.wait(timeout=1.0)
    assert not start_returned.wait(timeout=0.05)
    release_setup.set()
    caller.join(timeout=1.0)

    assert not caller.is_alive()
    handle = result["handle"]
    try:
        assert handle.cancelled()
    finally:
        handle.stop()


def _resume_context(status: str, result: dict) -> Any:
    from core.application.jobs.execution_contracts import WorkflowResumeTerminalContext

    return WorkflowResumeTerminalContext(
        job_id="job-1",
        job_row={"id": "job-1", "project_id": "p-1", "task_id": "T-1", "meta": {}},
        final_status=status,
        result=result,
        resume_revision=3,
        finalizer_token="token",
        workflow_id="wf",
        notification_event_type={
            "awaiting_timer": "job.awaiting_timer",
            "awaiting_approval": "job.awaiting_approval",
        }.get(status, ""),
        notification_severity="info",
    )


def test_resume_notify_sends_a_timer_wait_to_reactions_only(monkeypatch) -> None:
    from core.application.notifications import dispatch as notification_dispatch
    from core.infrastructure.runtime.worker import worker_execution_adapters
    from core.infrastructure.runtime.worker.worker_resume_adapters import (
        WorkerWorkflowResumeTerminalAdapter,
    )

    routed: list[dict] = []
    reacted: list[dict] = []
    monkeypatch.setattr(
        notification_dispatch,
        "dispatch_project_notification_event",
        lambda _bl, **kwargs: routed.append(kwargs) or [],
    )
    monkeypatch.setattr(
        worker_execution_adapters,
        "_run_lifecycle_reactions",
        lambda _store, **event: reacted.append(event),
    )
    adapter = WorkerWorkflowResumeTerminalAdapter(bl=MagicMock(), worker_id=lambda: "w")
    wait = {
        "kind": "timer",
        "wait_id": "t-1",
        "step_id": "pause",
        "step_execution_id": "x-1",
        "due_at": "2026-10-13T00:00:00+00:00",
    }

    adapter.notify(_resume_context("awaiting_timer", {"workflow_wait": wait}))

    assert routed == []
    assert reacted[0]["event_type"] == "job.awaiting_timer"
    assert reacted[0]["payload"]["wait_id"] == "t-1"
    assert reacted[0]["payload"]["due_at"] == "2026-10-13T00:00:00+00:00"


def test_resume_notify_sends_the_approval_wait_identity_to_routes_and_reactions(
    monkeypatch,
) -> None:
    from core.application.notifications import dispatch as notification_dispatch
    from core.infrastructure.runtime.worker import worker_execution_adapters
    from core.infrastructure.runtime.worker.worker_resume_adapters import (
        WorkerWorkflowResumeTerminalAdapter,
    )

    routed: list[dict] = []
    reacted: list[dict] = []
    monkeypatch.setattr(
        notification_dispatch,
        "dispatch_project_notification_event",
        lambda _bl, **kwargs: routed.append(kwargs) or [],
    )
    monkeypatch.setattr(
        worker_execution_adapters,
        "_run_lifecycle_reactions",
        lambda _store, **event: reacted.append(event),
    )
    adapter = WorkerWorkflowResumeTerminalAdapter(bl=MagicMock(), worker_id=lambda: "w")
    approval = {
        "approval_id": "gate-2",
        "wait_id": "recovery:req-1",
        "reason": "recovery",
        "step_id": "deploy",
    }

    adapter.notify(_resume_context("awaiting_approval", {"approval": approval}))

    assert routed[0]["event_type"] == "job.awaiting_approval"
    assert reacted[0]["payload"]["wait_id"] == "recovery:req-1"
    assert reacted[0]["payload"]["approval_id"] == "gate-2"
    assert reacted[0]["payload"]["reason"] == "recovery"


def test_resume_notify_catches_up_a_child_approval_when_the_parent_parks(monkeypatch) -> None:
    from core.infrastructure.runtime.worker import worker_execution_adapters
    from core.infrastructure.runtime.worker.worker_resume_adapters import (
        WorkerWorkflowResumeTerminalAdapter,
    )

    caught_up: list[str] = []
    monkeypatch.setattr(
        worker_execution_adapters,
        "_catch_up_child_approval",
        lambda _store, *, job_id: caught_up.append(job_id),
    )
    adapter = WorkerWorkflowResumeTerminalAdapter(bl=MagicMock(), worker_id=lambda: "w")

    adapter.notify(_resume_context("awaiting_child", {}))

    assert caught_up == ["job-1"]
