from __future__ import annotations

import ast
import importlib
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.application.orchestration.graph_runtime import graph_run_config
from tests.support.source_index import parse_file

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _make_message(
    task_id: str = "test-task-001",
    project_id: int = 1,
    redelivered: bool = False,
    extra: dict | None = None,
) -> tuple:
    """Create a mock (channel, method, properties, body) AMQP message tuple.

    Used by test_worker_container_crash_recovery to verify that helper
    imports succeed and the message contract is stable.
    """
    channel = MagicMock()
    method = MagicMock()
    method.delivery_tag = 1
    method.redelivered = redelivered
    properties = MagicMock()
    payload: dict = {
        "task_id": task_id,
        "project_id": project_id,
        "task_summary": "test task",
        "source": "test",
    }
    if extra:
        payload.update(extra)
    body = json.dumps(payload).encode()
    return channel, method, properties, body


@pytest.fixture(autouse=True)
def _checkpointer_available(monkeypatch):
    """These runs exercise the worker, not checkpoint storage: give them one."""
    from contextlib import contextmanager

    from core.infrastructure.runtime.worker import worker_execution_adapters as adapters

    @contextmanager
    def _available(_self):
        yield object()

    monkeypatch.setattr(adapters.WorkerGraphExecutionAdapter, "_build_checkpointer", _available)


def _import_worker_module():
    queue_name = "core.infrastructure.runtime.worker.queue"
    worker_name = "core.infrastructure.runtime.worker.worker"
    package = importlib.import_module("core.infrastructure.runtime.worker")
    fake_queue = ModuleType(queue_name)
    fake_queue.consume_project_queue = lambda *args, **kwargs: None
    fake_queue.consume_all_projects = lambda *args, **kwargs: None
    missing = object()
    previous_worker = sys.modules.get(worker_name, missing)
    previous_binding = getattr(package, "worker", missing)

    # Roll back only the transport stub. patch.dict(sys.modules) also removed
    # every dependency imported by worker, leaving dangling package attributes.
    with pytest.MonkeyPatch.context() as patches:
        patches.setitem(sys.modules, queue_name, fake_queue)
        patches.setattr(package, "queue", fake_queue, raising=False)
        try:
            return importlib.import_module(worker_name)
        finally:
            if previous_worker is missing:
                # Keep each test's worker shutdown flag isolated, while real
                # dependency modules retain matching sys.modules/package bindings.
                sys.modules.pop(worker_name, None)
                if previous_binding is missing:
                    if hasattr(package, "worker"):
                        delattr(package, "worker")
                else:
                    package.worker = previous_binding


def test_worker_id_distinguishes_process_incarnations_with_same_container_pid(
    monkeypatch,
) -> None:
    from core.infrastructure.runtime.worker import worker_bootstrap

    monkeypatch.setattr(worker_bootstrap.socket, "gethostname", lambda: "container-host")
    monkeypatch.setattr(worker_bootstrap.os, "getpid", lambda: 1)

    monkeypatch.setattr(worker_bootstrap, "_WORKER_INCARNATION_ID", "aaaaaaaaaaaa")
    first = worker_bootstrap._worker_id()
    monkeypatch.setattr(worker_bootstrap, "_WORKER_INCARNATION_ID", "bbbbbbbbbbbb")
    second = worker_bootstrap._worker_id()

    assert first == "container-host-aaaaaaaaaaaa-1"
    assert second == "container-host-bbbbbbbbbbbb-1"
    assert first != second


def _fake_terminal_persistence_module(
    captured_failure_kwargs: dict | None = None,
) -> ModuleType:
    module = ModuleType("core.application.jobs.terminal_persistence")

    class _TerminalPersistenceConflict(RuntimeError):
        pass

    class _TerminalPersistenceService:
        def __init__(self, logger, *, resolve_artifact=None):
            self.logger = logger
            self.resolve_artifact = resolve_artifact

        def complete_success(
            self,
            *,
            job_id,
            workflow_outputs,
            final_report=None,
            root_run_id=None,
            **_kwargs,
        ):
            self.logger.finish_job(
                job_id,
                status="completed",
                final_report=final_report,
                error=None,
                root_run_id=root_run_id,
            )
            return SimpleNamespace(status="completed")

        def complete_failure(
            self,
            *,
            job_id,
            status,
            error_message=None,
            final_report=None,
            root_run_id=None,
            **_kwargs,
        ):
            if captured_failure_kwargs is not None:
                captured_failure_kwargs.update(_kwargs)
            self.logger.finish_job(
                job_id,
                status=status,
                final_report=final_report,
                error=error_message,
                root_run_id=root_run_id,
            )
            return SimpleNamespace(status=status)

    module.TerminalPersistenceService = _TerminalPersistenceService
    module.TerminalPersistenceConflict = _TerminalPersistenceConflict
    return module


def test_sigterm_requests_graceful_shutdown_without_raising_system_exit() -> None:
    worker_mod = _import_worker_module()
    worker_mod._shutdown_requested.clear()

    worker_mod._sigterm_handler(15, None)

    assert worker_mod._shutdown_requested.is_set()


def test_worker_and_orchestration_library_paths_do_not_call_process_exit() -> None:
    roots = (
        Path("core/infrastructure/runtime/worker"),
        Path("core/application/orchestration"),
    )
    violations: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            for node in parse_file(path, encoding=None).nodes:
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "sys"
                    and node.func.attr == "exit"
                ):
                    violations.append(f"{path}:{node.lineno}:sys.exit")
                if (
                    isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Name)
                    and node.exc.func.id == "SystemExit"
                ):
                    violations.append(f"{path}:{node.lineno}:SystemExit")

    assert violations == []


def test_worker_reads_terminal_error_context_from_callback_fail_closed() -> None:
    from core.infrastructure.runtime.worker.worker_execution_adapters import (
        _terminal_error_context,
    )

    callback = SimpleNamespace(
        terminal_error_context=lambda: {
            "step_id": "step-7",
            "group_id": "delivery",
            "occurrence": 2,
        }
    )

    assert _terminal_error_context(callback) == {
        "step_id": "step-7",
        "group_id": "delivery",
        "occurrence": 2,
    }
    assert _terminal_error_context(SimpleNamespace()) == {}


def test_worker_terminal_failure_uses_handled_step_failure_context() -> None:
    from core.application.jobs.execution_contracts import (
        TaskExecutionRequest,
        TaskGraphOutcome,
    )
    from core.infrastructure.runtime.worker import worker_execution_adapters

    service = MagicMock()
    service.complete_failure.return_value = SimpleNamespace(status="failed")
    request = TaskExecutionRequest(
        message={},
        job_id="job-1",
        task_id="task-1",
        thread_id="thread-1",
        task_summary="Validate inputs",
        task_source="test",
        project_name="Project",
        project_config={},
        extra_state={},
        workflow_id="workflow-1",
        test_mode=True,
    )
    result = {
        "status": "failed",
        "error": "Required input repository is missing",
        "unresolved_step_failure": {
            "step_id": "validate-inputs",
            "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
            "attempt": 1,
            "group_id": "implementation",
            "occurrence": 2,
            "error_code": "skill_input_invalid",
            "selector": "state.seed_value",
            "diagnostic_kind": "transform",
        },
    }
    adapter = worker_execution_adapters.WorkerTerminalEffectsAdapter(
        bl=MagicMock(),
        worker_id=lambda: "worker-1",
    )

    with patch.object(
        worker_execution_adapters,
        "_terminal_persistence_service",
        return_value=service,
    ):
        adapter.persist(
            request,
            job_row={},
            project_id="project-1",
            final_status="failed",
            result=result,
            graph_outcome=TaskGraphOutcome.from_result(result),
        )

    assert service.complete_failure.call_args.kwargs["step_id"] == "validate-inputs"
    assert (
        service.complete_failure.call_args.kwargs["step_execution_id"]
        == "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42"
    )
    assert service.complete_failure.call_args.kwargs["group_id"] == "implementation"
    assert service.complete_failure.call_args.kwargs["occurrence"] == 2
    assert service.complete_failure.call_args.kwargs["error_code"] == "skill_input_invalid"
    assert service.complete_failure.call_args.kwargs["selector"] == "state.seed_value"
    assert service.complete_failure.call_args.kwargs["diagnostic_kind"] == "transform"


def test_initial_approval_notification_contains_bounded_occurrence_context() -> None:
    from core.application.jobs.execution_contracts import (
        TaskExecutionRequest,
        TaskGraphOutcome,
    )
    from core.application.jobs.task_execution_service import _normal_notification

    request = TaskExecutionRequest(
        message={},
        job_id="job-1",
        task_id="task-1",
        thread_id="thread-1",
        task_summary="Deploy release",
        task_source="manual",
        project_name="Project",
        project_config={},
        extra_state={},
        workflow_id="workflow-1",
        test_mode=False,
    )
    state = SimpleNamespace(
        tasks=[
            SimpleNamespace(
                interrupts=[
                    SimpleNamespace(
                        id="gate-1",
                        value={
                            "message": "Approve production rollout",
                            "group_id": "release",
                            "step_id": "approval",
                        },
                    )
                ]
            )
        ]
    )

    notification = _normal_notification(
        request,
        "project-1",
        "awaiting_approval",
        {"status": "awaiting_approval"},
        graph_outcome=TaskGraphOutcome.from_result(
            {"status": "awaiting_approval"},
            pending_interrupt=True,
            interrupt_state=state,
        ),
    )

    assert notification is not None
    assert notification.payload["approval_id"] == "gate-1"
    assert notification.payload["summary"] == "Approve production rollout"
    assert notification.payload["group_id"] == "release"
    assert notification.payload["step_id"] == "approval"


@pytest.mark.parametrize(
    "stale_write_error",
    [
        LookupError("Job job-1 does not exist"),
        pytest.param(
            "terminal_conflict",
            id="already-terminal",
        ),
    ],
)
def test_worker_terminal_persistence_acks_stale_completion(stale_write_error: object) -> None:
    from core.application.jobs.execution_contracts import TaskExecutionRequest
    from core.application.jobs.terminal_persistence import TerminalPersistenceConflict
    from core.infrastructure.runtime.worker import worker_execution_adapters

    if stale_write_error == "terminal_conflict":
        stale_write_error = TerminalPersistenceConflict("Job job-1 is already terminal")
    service = MagicMock()
    service.complete_failure.side_effect = stale_write_error
    request = TaskExecutionRequest(
        message={},
        job_id="job-1",
        task_id="task-1",
        thread_id="thread-1",
        task_summary="Finish a stale delivery",
        task_source="test",
        project_name="Project",
        project_config={},
        extra_state={},
        workflow_id="workflow-1",
        test_mode=True,
    )
    adapter = worker_execution_adapters.WorkerTerminalEffectsAdapter(
        bl=MagicMock(),
        worker_id=lambda: "worker-1",
    )

    with patch.object(
        worker_execution_adapters,
        "_terminal_persistence_service",
        return_value=service,
    ):
        status = adapter.persist(
            request,
            job_row={},
            project_id="project-1",
            final_status="failed",
            result={"status": "failed", "error": "project deleted during execution"},
            graph_outcome=None,
        )

    assert status == "failed"


def test_worker_terminal_persistence_does_not_ack_unrelated_lookup_error() -> None:
    from core.application.jobs.execution_contracts import TaskExecutionRequest
    from core.infrastructure.runtime.worker import worker_execution_adapters

    service = MagicMock()
    service.complete_failure.side_effect = LookupError("Artifact artifact-1 does not exist")
    request = TaskExecutionRequest(
        message={},
        job_id="job-1",
        task_id="task-1",
        thread_id="thread-1",
        task_summary="Preserve an unrelated persistence failure",
        task_source="test",
        project_name="Project",
        project_config={},
        extra_state={},
        workflow_id="workflow-1",
        test_mode=True,
    )
    adapter = worker_execution_adapters.WorkerTerminalEffectsAdapter(
        bl=MagicMock(),
        worker_id=lambda: "worker-1",
    )

    with (
        patch.object(
            worker_execution_adapters,
            "_terminal_persistence_service",
            return_value=service,
        ),
        pytest.raises(LookupError, match="Artifact artifact-1 does not exist"),
    ):
        adapter.persist(
            request,
            job_row={},
            project_id="project-1",
            final_status="failed",
            result={"status": "failed", "error": "artifact resolution failed"},
            graph_outcome=None,
        )


def test_worker_public_approval_gate_is_checkpoint_backed_and_sanitized():
    from core.infrastructure.runtime.worker.worker_execution_adapters import (
        _public_approval_gate_from_state,
    )

    state = SimpleNamespace(
        tasks=[
            SimpleNamespace(
                interrupts=[
                    SimpleNamespace(
                        id="interrupt-1",
                        value={
                            "message": "Review",
                            "content": {"summary": "safe", "raw_checkpoint": "secret"},
                            "allowed_actions": ["approve"],
                            "raw_checkpoint": {"token": "secret"},
                        },
                    )
                ]
            )
        ]
    )

    gate = _public_approval_gate_from_state(
        job={"id": "job-1", "thread_id": "thread-1"}, state=state
    )

    assert gate is not None
    assert gate.approval_id == "interrupt-1"
    assert gate.content == {"summary": "safe"}
    assert "raw_checkpoint" not in gate.model_dump()


def test_worker_marks_awaiting_before_rejecting_multiple_approval_gates(monkeypatch):
    from contextlib import nullcontext

    import pytest

    from core.application.jobs import workflow_pause_persistence
    from core.infrastructure.runtime.worker.worker_execution_adapters import (
        _persist_awaiting_approval_state,
    )

    state = SimpleNamespace(
        tasks=[
            SimpleNamespace(
                interrupts=[
                    SimpleNamespace(id="interrupt-1", value={}),
                    SimpleNamespace(id="interrupt-2", value={}),
                ]
            )
        ]
    )
    logger = MagicMock()
    logger.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (
        "in_progress",
        "worker-1",
    )
    monkeypatch.setattr(
        workflow_pause_persistence, "transaction_connection", lambda conn: nullcontext(conn)
    )
    logger.get_job.return_value = {
        "id": "job-1",
        "project_id": "project-1",
        "selected_workflow": "workflow-1",
    }

    with pytest.raises(RuntimeError, match="Multiple simultaneous approval gates"):
        _persist_awaiting_approval_state(
            bl=logger,
            expected_worker_id="worker-1",
            job_id="job-1",
            project_id="project-1",
            selected_workflow_id="workflow-1",
            pending_interrupt_state=state,
        )

    logger.update_job.assert_called_once_with("job-1", status="awaiting_approval")


def test_execute_task_uses_canonical_state_builder():
    worker_mod = _import_worker_module()

    captured = {"builder_kwargs": None, "graph_input": None, "graph_build_kwargs": None}

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {}

    class _GraphStub:
        pass

    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config

    def _fake_build_registry_graph(checkpointer=None, **kwargs):
        captured["graph_build_kwargs"] = {"checkpointer": checkpointer, **kwargs}
        return _GraphStub()

    fake_core_graph.build_registry_graph = _fake_build_registry_graph

    fake_core_state = ModuleType("core.application.orchestration.state")

    def _fake_build_initial_orchestrator_state(**kwargs):
        captured["builder_kwargs"] = dict(kwargs)
        return {"from_builder": True, "status": "in_progress"}

    fake_core_state.build_initial_orchestrator_state = _fake_build_initial_orchestrator_state

    fake_agents_common = ModuleType("core.application.orchestration.agents.common")

    class _Cancelled(Exception):
        pass

    def _fake_run_graph_with_cancel_check(graph, input_or_command, config, bl=None, job_id=None):
        captured["graph_input"] = input_or_command
        return {"status": "completed"}

    fake_agents_common.run_graph_with_cancel_check = _fake_run_graph_with_cancel_check
    fake_agents_common.JobCancelledException = _Cancelled

    msg = {
        "job_id": 1,
        "task_id": "TASK-99",
        "thread_id": "thread-99",
        "task_summary": "Verify builder path",
        "task_source": "manual",
        "project_name": "demo",
        "project_config": {"project_rules": "Be concise."},
        "mode": "brownfield",
        "complexity": "feature",
        "test_mode": True,
        "extra_state": {"job_id": 1, "workflow_id": "wf-99", "test_mode": False},
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.application.orchestration.state": fake_core_state,
            "core.application.orchestration.agents.common": fake_agents_common,
            "core.application.jobs.terminal_persistence": _fake_terminal_persistence_module(),
        },
    ):
        worker_mod._execute_task(msg)

    kwargs = captured["builder_kwargs"] or {}
    assert kwargs["task_id"] == "TASK-99"
    assert kwargs["task_summary"] == "Verify builder path"
    assert kwargs["task_source"] == "manual"
    assert kwargs["project_name"] == "demo"
    assert "project_root" not in kwargs
    assert kwargs["project_config"]["project_rules"] == "Be concise."
    assert kwargs["test_mode"] is True
    assert kwargs["extra_state"]["workflow_id"] == "wf-99"
    assert captured["graph_build_kwargs"]["workflow_ids"] == ["wf-99"]
    assert captured["graph_input"] == {"from_builder": True, "status": "in_progress"}


def test_execute_task_routes_workflow_resume_message_to_worker_resume_handler():
    worker_mod = _import_worker_module()

    captured: dict[str, object] = {"resume_kwargs": None, "claim_sql": []}

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://test"}

    class _Cursor:
        rowcount = 1

        def fetchone(self):
            return ({"execution_attempts": 1, "exhaustion_pending": False},)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, params=None):
            captured["claim_sql"].append((sql, params))

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            captured["committed"] = True

    class _PlatformLogger:
        conn = _Conn()

        def get_job(self, job_id):
            assert str(job_id) == "job-42"
            return {
                "id": "job-42",
                "project_id": "project-42",
                "status": "resume_pending",
                "meta": {
                    "workflow_resume": {
                        "state": "pending",
                        "revision": 7,
                        "approval_id": "gate-1",
                        "resume_value": {"approved": True, "comment": "ship it"},
                        "approved": True,
                        "action": "approve",
                        "comment": "ship it",
                        "interrupt_payload": {"stage": "review"},
                        "actor_username": "alice",
                    }
                },
            }

        def get_project(self, project_id):
            return {"id": str(project_id), "account_id": "acc-1", "workspace_id": "ws-1"}

    fake_platform_logger = ModuleType("core.infrastructure.db.platform_logger")
    fake_platform_logger.PlatformLogger = lambda _dsn: _PlatformLogger()
    fake_platform_logger.PlatformWorkerStore = lambda _dsn: _PlatformLogger()
    fake_platform_logger.PlatformProjectStore = lambda _dsn: _PlatformLogger()

    class _PlatformIntegrationStore:
        @classmethod
        def get_shared_instance(cls, _dsn):
            return SimpleNamespace(list_notification_routes=lambda **_kwargs: [])

    fake_platform_logger.PlatformIntegrationStore = _PlatformIntegrationStore
    fake_platform_logger.PlatformLoggerCallback = object

    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config
    fake_core_graph.build_registry_graph = MagicMock(
        side_effect=AssertionError("workflow resume must not use normal task graph path")
    )

    fake_agents_common = ModuleType("core.application.orchestration.agents.common")
    fake_agents_common.JobCancelledException = RuntimeError
    fake_agents_common.run_graph_with_cancel_check = MagicMock(
        side_effect=AssertionError("workflow resume must not run normal queue task")
    )

    fake_resume_wiring = ModuleType("core.infrastructure.runtime.graph_resume_wiring")
    fake_resume_wiring.GraphResumeRuntimePorts = SimpleNamespace
    fake_resume_wiring.GraphResumeThreadConfig = SimpleNamespace

    def _build_graph_resume_runtime_hooks(**_kwargs):
        def _run_graph_resume(**kwargs):
            captured["resume_kwargs"] = dict(kwargs)
            return SimpleNamespace(
                started=True,
                status="completed",
                retryable=False,
                reason="",
                result={"status": "completed"},
                error=None,
                terminal=True,
            )

        return SimpleNamespace(run_graph_resume=_run_graph_resume)

    fake_resume_wiring.build_graph_resume_runtime_hooks = _build_graph_resume_runtime_hooks

    msg = {
        "message_type": "workflow_resume",
        "job_id": "job-42",
        "task_id": "TASK-42",
        "thread_id": "thread-123",
        "project_id": "project-42",
        "resume_revision": 7,
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.infrastructure.db.platform_logger": fake_platform_logger,
            "core.infrastructure.db.platform_stores": fake_platform_logger,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.application.orchestration.agents.common": fake_agents_common,
            "core.infrastructure.runtime.graph_resume_wiring": fake_resume_wiring,
        },
    ):
        result = worker_mod._execute_task(msg)

    assert result == {"action": "ack", "reason": "workflow_resume_accepted"}
    assert captured["committed"] is True
    assert any("status = 'resume_pending'" in sql for sql, _params in captured["claim_sql"])
    assert any(
        "meta->'workflow_resume'->>'lease_token'" in sql and "completed" in sql
        for sql, _params in captured["claim_sql"]
    )
    resume_kwargs = dict(captured["resume_kwargs"] or {})
    assert callable(resume_kwargs.pop("resume_cancelled"))
    assert resume_kwargs.pop("defer_terminal_persistence") is True
    assert resume_kwargs.pop("resume_revision") == 7
    assert resume_kwargs == {
        "thread_id": "thread-123",
        "resume_value": {"approved": True, "comment": "ship it"},
        "approved": True,
        "action": "approve",
        "comment": "ship it",
        "interrupt_payload": {"stage": "review"},
        "actor_username": "alice",
        "approval_id": "gate-1",
    }


def test_workflow_resume_redelivery_can_reclaim_expired_running_lease(monkeypatch):
    from core.infrastructure.runtime.worker import worker_task_execution as task_exec
    from core.infrastructure.runtime.worker.worker_resume_adapters import (
        WorkerWorkflowResumeGraphAdapter,
    )

    captured: dict[str, object] = {"resume_kwargs": None, "claim_sql": []}

    class _Cursor:
        rowcount = 1

        def fetchone(self):
            return ({"execution_attempts": 1, "exhaustion_pending": False},)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, params=None):
            captured["claim_sql"].append((str(sql), params))

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            captured["committed"] = True

    class _BL:
        conn = _Conn()

        @staticmethod
        def get_job(_job_id):
            return {
                "id": "job-42",
                "status": "in_progress",
                "meta": {
                    "workflow_resume": {
                        "state": "running",
                        "revision": 7,
                        "approval_id": "gate-1",
                        "lease_expires_at": "2026-01-01T00:00:00+00:00",
                        "resume_value": {"approved": True, "comment": "ship it"},
                        "approved": True,
                        "action": "approve",
                        "comment": "ship it",
                        "interrupt_payload": {"stage": "review"},
                        "actor_username": "alice",
                    }
                },
            }

    monkeypatch.setattr(
        WorkerWorkflowResumeGraphAdapter,
        "_build_runner",
        lambda _self: (
            lambda **kwargs: (
                captured.update(resume_kwargs=dict(kwargs))
                or SimpleNamespace(
                    started=True,
                    status="awaiting_approval",
                    retryable=False,
                    reason="",
                    result={"status": "awaiting_approval"},
                    error=None,
                    terminal=False,
                )
            )
        ),
    )
    service = task_exec._workflow_resume_service(
        bl=_BL(),
        dependencies=task_exec.compose_worker_execution_dependencies(
            worker_id=lambda: "worker-new",
        ),
    )

    result = service.execute(
        {
            "message_type": "workflow_resume",
            "job_id": "job-42",
            "thread_id": "thread-123",
            "resume_revision": 7,
        }
    )

    assert result == {"action": "ack", "reason": "workflow_resume_accepted"}
    assert captured["committed"] is True
    assert any("status = 'in_progress'" in sql for sql, _params in captured["claim_sql"])
    resume_kwargs = dict(captured["resume_kwargs"] or {})
    assert callable(resume_kwargs.pop("resume_cancelled"))
    assert resume_kwargs.pop("defer_terminal_persistence") is True
    assert resume_kwargs.pop("resume_revision") == 7
    assert resume_kwargs == {
        "thread_id": "thread-123",
        "resume_value": {"approved": True, "comment": "ship it"},
        "approved": True,
        "action": "approve",
        "comment": "ship it",
        "interrupt_payload": {"stage": "review"},
        "actor_username": "alice",
        "approval_id": "gate-1",
    }


def test_workflow_resume_heartbeat_sets_cancel_event_when_lease_is_lost(monkeypatch):
    from core.infrastructure.runtime.worker import worker_resume_adapters
    from core.infrastructure.runtime.worker.worker_resume_adapters import (
        WorkerWorkflowResumeLeaseAdapter,
    )

    calls: list[dict[str, object]] = []

    class _Repository:
        @staticmethod
        def renew_execution(**kwargs):
            calls.append(dict(kwargs))
            return False

    monkeypatch.setattr(worker_resume_adapters, "_LEASE_RENEW_SECONDS", 0.01)
    heartbeat = WorkerWorkflowResumeLeaseAdapter(
        repository=_Repository(),
        repository_factory=lambda _bl: _Repository(),
        bl=object(),
    ).start_execution(
        job_id="job-42",
        resume_revision=7,
        lease_token="lease-old",
    )
    try:
        deadline = time.monotonic() + 0.5
        while not heartbeat.cancelled() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert heartbeat.cancelled()
    finally:
        heartbeat.stop()

    assert calls == [
        {
            "job_id": "job-42",
            "resume_revision": 7,
            "lease_token": "lease-old",
            "lease_seconds": 300,
        }
    ]


def test_workflow_resume_lease_loss_cancels_running_resume_without_completing(
    monkeypatch,
):
    from core.infrastructure.runtime.worker import (
        worker_resume_adapters,
    )
    from core.infrastructure.runtime.worker import (
        worker_task_execution as task_exec,
    )
    from core.infrastructure.runtime.worker.worker_resume_adapters import (
        WorkerWorkflowResumeGraphAdapter,
    )

    events: list[str] = []
    sql_calls: list[str] = []

    class _Cursor:
        rowcount = 1

        def fetchone(self):
            return ({"execution_attempts": 1, "exhaustion_pending": False},)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, _params=None):
            normalized = " ".join(str(sql).split())
            sql_calls.append(normalized)
            self.rowcount = 0 if "SET heartbeat_at = NOW(), meta = jsonb_set" in normalized else 1

    class _Conn:
        @staticmethod
        def cursor():
            return _Cursor()

        @staticmethod
        def commit():
            return None

    class _BL:
        conn = _Conn()

        @staticmethod
        def get_job(_job_id):
            return {
                "id": "job-42",
                "status": "resume_pending",
                "meta": {
                    "workflow_resume": {
                        "state": "pending",
                        "revision": 7,
                        "resume_value": {"approved": True},
                        "approved": True,
                        "action": "approve",
                        "comment": "",
                        "interrupt_payload": {},
                        "actor_username": "alice",
                    }
                },
            }

    def _run_graph_resume(**kwargs):
        assert "resume_cancelled" in kwargs
        deadline = time.monotonic() + 0.5
        while not kwargs["resume_cancelled"]() and time.monotonic() < deadline:
            time.sleep(0.005)
        events.append("cancelled")
        return SimpleNamespace(
            started=True,
            status="lease_lost",
            retryable=True,
            reason="workflow_resume_lease_lost",
            result={},
            error="workflow resume lease lost",
            terminal=False,
        )

    monkeypatch.setattr(
        worker_resume_adapters,
        "_LEASE_RENEW_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        WorkerWorkflowResumeGraphAdapter,
        "_build_runner",
        lambda _self: _run_graph_resume,
    )
    service = task_exec._workflow_resume_service(
        bl=_BL(),
        dependencies=task_exec.compose_worker_execution_dependencies(
            worker_id=lambda: "worker-1",
        ),
    )

    result = service.execute(
        {
            "message_type": "workflow_resume",
            "job_id": "job-42",
            "thread_id": "thread-123",
            "resume_revision": 7,
        }
    )

    assert result == {"action": "discard", "reason": "workflow_resume_lease_lost"}
    assert events == ["cancelled"]
    assert not any('"completed"' in sql for sql in sql_calls)


def test_workflow_resume_defers_without_completing_when_graph_resume_never_started(
    monkeypatch,
):
    from core.infrastructure.runtime.worker import worker_task_execution as task_exec
    from core.infrastructure.runtime.worker.worker_resume_adapters import (
        WorkerWorkflowResumeGraphAdapter,
    )

    captured: dict[str, object] = {"sql": []}

    class _Cursor:
        rowcount = 1

        def fetchone(self):
            return ({"execution_attempts": 1, "exhaustion_pending": False},)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, params=None):
            captured["sql"].append((str(sql), params))

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            captured["committed"] = True

    class _BL:
        conn = _Conn()

        @staticmethod
        def get_job(_job_id):
            return {
                "id": "job-42",
                "project_id": "project-42",
                "status": "resume_pending",
                "meta": {
                    "workflow_resume": {
                        "state": "pending",
                        "revision": 7,
                        "resume_value": {"approved": True},
                        "approved": True,
                        "action": "approve",
                        "comment": "",
                        "interrupt_payload": {},
                        "actor_username": "alice",
                    }
                },
            }

    monkeypatch.setattr(
        WorkerWorkflowResumeGraphAdapter,
        "_build_runner",
        lambda _self: (
            lambda **_kwargs: SimpleNamespace(
                started=False,
                status="not_started",
                retryable=True,
                reason="checkpointer_unavailable",
                result={},
                error="checkpointer unavailable",
                terminal=False,
            )
        ),
    )
    service = task_exec._workflow_resume_service(
        bl=_BL(),
        dependencies=task_exec.compose_worker_execution_dependencies(
            worker_id=lambda: "worker-1",
        ),
    )

    result = service.execute(
        {
            "message_type": "workflow_resume",
            "job_id": "job-42",
            "thread_id": "thread-123",
            "resume_revision": 7,
        }
    )

    assert result == {"action": "ack", "reason": "workflow_resume_deferred"}
    sql_text = "\n".join(sql for sql, _params in captured["sql"])
    assert '"completed"' not in sql_text
    assert "resume_pending" in sql_text


def test_workflow_resume_retryable_outcome_does_not_complete_intent(monkeypatch):
    from core.infrastructure.runtime.worker import worker_task_execution as task_exec
    from core.infrastructure.runtime.worker.worker_resume_adapters import (
        WorkerWorkflowResumeGraphAdapter,
    )

    sql_calls: list[str] = []

    class _Cursor:
        rowcount = 1

        def fetchone(self):
            return ({"execution_attempts": 1, "exhaustion_pending": False},)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, _params=None):
            sql_calls.append(" ".join(str(sql).split()))

    class _Conn:
        @staticmethod
        def cursor():
            return _Cursor()

        @staticmethod
        def commit():
            return None

    class _BL:
        conn = _Conn()

        @staticmethod
        def get_job(_job_id):
            return {
                "id": "job-42",
                "status": "resume_pending",
                "meta": {
                    "workflow_resume": {
                        "state": "pending",
                        "revision": 7,
                        "resume_value": {"approved": True},
                        "approved": True,
                        "action": "approve",
                        "comment": "",
                        "interrupt_payload": {},
                        "actor_username": "alice",
                    }
                },
            }

    monkeypatch.setattr(
        WorkerWorkflowResumeGraphAdapter,
        "_build_runner",
        lambda _self: (
            lambda **_kwargs: SimpleNamespace(
                started=True,
                status="failed",
                retryable=True,
                reason="job_status_persist_failed",
                result={"status": "failed"},
                error="finish_job failed",
                terminal=False,
            )
        ),
    )
    service = task_exec._workflow_resume_service(
        bl=_BL(),
        dependencies=task_exec.compose_worker_execution_dependencies(
            worker_id=lambda: "worker-1",
        ),
    )

    result = service.execute(
        {
            "message_type": "workflow_resume",
            "job_id": "job-42",
            "thread_id": "thread-123",
            "resume_revision": 7,
        }
    )

    assert result == {"action": "ack", "reason": "workflow_resume_deferred"}
    assert any('"publish_failed"' in sql for sql in sql_calls)
    assert not any('"completed"' in sql for sql in sql_calls)


def test_dispatch_pending_workflow_resumes_republishes_db_outbox_rows():
    from core.infrastructure.runtime.worker import worker_task_execution as task_exec

    captured: dict[str, object] = {"sql": [], "enqueued": []}

    class _Cursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, params=None):
            captured["sql"].append((str(sql), params))

        def fetchall(self):
            return [
                {
                    "id": "job-42",
                    "project_id": "project-42",
                    "task_id": "TASK-42",
                    "thread_id": "thread-123",
                    "resume_revision": 7,
                    "publish_state": "pending",
                    "publish_token": "publish-token-7",
                }
            ]

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            captured["committed"] = True

    class _BL:
        conn = _Conn()

    def enqueue(**kwargs):
        captured["enqueued"].append(dict(kwargs))

    count = task_exec.dispatch_pending_workflow_resumes(
        bl=_BL(),
        enqueue_workflow_resume=enqueue,
        batch_size=10,
    )

    assert count == 1
    assert captured["enqueued"] == [
        {
            "project_id": "project-42",
            "job_id": "job-42",
            "task_id": "TASK-42",
            "thread_id": "thread-123",
            "resume_revision": 7,
        }
    ]
    reservation_sql, reservation_params = captured["sql"][0]
    publish_sql, publish_params = captured["sql"][1]
    assert "WITH candidates AS" in reservation_sql
    assert "FOR UPDATE SKIP LOCKED" in reservation_sql
    assert "UPDATE jobs AS job" in reservation_sql
    assert "resume_pending" in reservation_sql
    assert "finalizing" in reservation_sql
    assert reservation_params[0] == 10
    assert "state', '') = %s" in publish_sql
    assert "publish_token', '') = %s" in publish_sql
    assert publish_params[-2:] == ("pending", "publish-token-7")
    assert json.dumps("published") in publish_params


def test_workflow_resume_outbox_reuses_default_logger(monkeypatch):
    from core.infrastructure.runtime.worker import worker_task_execution as task_exec

    created: list[str] = []

    class _BL:
        conn = None

    fake_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_config.CONFIG = {"fs_observability_dsn": "postgres://test"}
    fake_platform_logger = ModuleType("core.infrastructure.db.platform_logger")
    fake_platform_logger.PlatformLogger = lambda dsn: created.append(str(dsn)) or _BL()
    fake_platform_logger.PlatformWorkerStore = lambda dsn: created.append(str(dsn)) or _BL()
    fake_platform_logger.PlatformProjectStore = lambda dsn: created.append(str(dsn)) or _BL()

    monkeypatch.setattr(task_exec, "_workflow_resume_outbox_logger", None, raising=False)
    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_config,
            "core.infrastructure.db.platform_logger": fake_platform_logger,
            "core.infrastructure.db.platform_stores": fake_platform_logger,
        },
    ):
        task_exec.dispatch_pending_workflow_resumes()
        task_exec.dispatch_pending_workflow_resumes()

    assert created == ["postgres://test"]


def test_execute_task_restores_invoke_child_auto_approve_from_job_meta():
    worker_mod = _import_worker_module()

    captured = {"builder_kwargs": None}

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://obs"}

    class _GraphStub:
        pass

    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config
    fake_core_graph.build_registry_graph = lambda checkpointer=None, **_kwargs: _GraphStub()

    fake_core_state = ModuleType("core.application.orchestration.state")

    def _fake_build_initial_orchestrator_state(**kwargs):
        captured["builder_kwargs"] = dict(kwargs)
        return {"status": "in_progress", **kwargs.get("extra_state", {})}

    fake_core_state.build_initial_orchestrator_state = _fake_build_initial_orchestrator_state

    fake_agents_common = ModuleType("core.application.orchestration.agents.common")

    class _Cancelled(Exception):
        pass

    fake_agents_common.JobCancelledException = _Cancelled
    fake_agents_common.run_graph_with_cancel_check = (
        lambda graph, input_or_command, config, bl=None, job_id=None: {"status": "completed"}
    )

    class _FakeLogger:
        def __init__(self, _dsn):
            self.conn = None

        def get_job(self, job_id):
            if str(job_id) == "498":
                return {
                    "project_id": 131,
                    "task_id": "TASK-PARENT",
                    "selected_workflow": "parent-workflow",
                }
            return {
                "project_id": 131,
                "task_source": "invoke_workflow",
                "status": "pending",
                "meta": {
                    "invoke_child_auto_approve": True,
                    "invoke_parent_job_id": 498,
                    "invoke_parent_task_id": "TASK-PARENT",
                    "invoke_parent_workflow_id": "parent-workflow",
                    "invoke_parent_step_id": "fan_out",
                    "invoke_parent_step_agent_id": "invoke_workflow",
                    "invoke_parent_step_skill_id": "draft_summary",
                    "invoke_child_workflow_id": "weekly_report",
                },
            }

        def update_job(self, *_args, **_kwargs):
            return None

        def update_job_runtime(self, *_args, **_kwargs):
            return None

        def finish_job(self, *_args, **_kwargs):
            return None

    class _FakeCallback:
        def __init__(self, *_args, **_kwargs):
            self._root_run_id = None

    fake_logger_mod = ModuleType("core.infrastructure.db.platform_logger")
    fake_logger_mod.PlatformLogger = _FakeLogger
    fake_logger_mod.PlatformWorkerStore = _FakeLogger
    fake_logger_mod.PlatformProjectStore = _FakeLogger
    fake_logger_mod.PlatformLoggerCallback = _FakeCallback

    msg = {
        "job_id": 499,
        "task_id": "TASK-CHILD",
        "thread_id": "thread-child",
        "task_summary": "Queued child invoke should restore auto-approve hint",
        "task_source": "invoke_workflow",
        "project_name": "demo",
        "project_config": {"project_rules": "Be concise."},
        "mode": "brownfield",
        "extra_state": {},
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.application.orchestration.state": fake_core_state,
            "core.application.orchestration.agents.common": fake_agents_common,
            "core.infrastructure.db.platform_logger": fake_logger_mod,
            "core.infrastructure.db.platform_stores": fake_logger_mod,
            "core.application.jobs.terminal_persistence": _fake_terminal_persistence_module(),
        },
    ):
        worker_mod._execute_task(msg)

    kwargs = captured["builder_kwargs"] or {}
    extra_state = kwargs.get("extra_state") or {}
    assert extra_state["invoke_child_auto_approve"] is True
    assert extra_state["invoke_parent_job_id"] == 498
    assert extra_state["invoke_parent_step_id"] == "fan_out"
    assert extra_state["invoke_parent_step_agent_id"] == "invoke_workflow"
    assert extra_state["invoke_parent_step_skill_id"] == "draft_summary"
    assert extra_state["invoke_child_workflow_id"] == "weekly_report"


def test_invoke_child_auto_approval_requires_confirmed_internal_lineage():
    from core.application.jobs.task_execution_request import _has_confirmed_invoke_child_lineage

    class _Logger:
        def get_job(self, job_id):
            if str(job_id) == "parent-1":
                return {
                    "project_id": "project-1",
                    "task_id": "parent-task",
                    "selected_workflow": "parent-workflow",
                }
            return None

    job = {"project_id": "project-1", "task_source": "invoke_workflow"}
    meta = {
        "invoke_child_auto_approve": True,
        "invoke_parent_job_id": "parent-1",
        "invoke_parent_task_id": "parent-task",
        "invoke_parent_workflow_id": "parent-workflow",
        "invoke_child_workflow_id": "child-workflow",
    }

    logger = _Logger()
    assert _has_confirmed_invoke_child_lineage(logger.get_job, job, meta) is True
    assert (
        _has_confirmed_invoke_child_lineage(logger.get_job, {**job, "task_source": "manual"}, meta)
        is False
    )
    assert (
        _has_confirmed_invoke_child_lineage(
            logger.get_job,
            job,
            {**meta, "invoke_parent_workflow_id": "forged-workflow"},
        )
        is False
    )


def test_execute_task_restores_child_task_envelope_from_job_meta_when_extra_state_is_missing():
    worker_mod = _import_worker_module()

    captured = {"builder_kwargs": None}

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://obs"}

    class _GraphStub:
        pass

    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config
    fake_core_graph.build_registry_graph = lambda checkpointer=None, **_kwargs: _GraphStub()

    fake_core_state = ModuleType("core.application.orchestration.state")

    def _fake_build_initial_orchestrator_state(**kwargs):
        captured["builder_kwargs"] = dict(kwargs)
        return {"status": "in_progress", **kwargs.get("extra_state", {})}

    fake_core_state.build_initial_orchestrator_state = _fake_build_initial_orchestrator_state

    fake_agents_common = ModuleType("core.application.orchestration.agents.common")

    class _Cancelled(Exception):
        pass

    fake_agents_common.JobCancelledException = _Cancelled
    fake_agents_common.run_graph_with_cancel_check = (
        lambda graph, input_or_command, config, bl=None, job_id=None: {"status": "completed"}
    )

    class _FakeLogger:
        def __init__(self, _dsn):
            self.conn = None

        def get_job(self, _job_id):
            return {
                "project_id": 131,
                "status": "pending",
                "meta": {
                    "task_envelope": {
                        "project_id": "131",
                        "requested_workflow_id": "weekly_report",
                        "title": "STORY-1: Add visible search input",
                        "description": "Child workflow request",
                        "verification_commands": [
                            "python -m pytest tests/unit/test_child_runtime.py -k envelope -q"
                        ],
                        "inputs": {"current_item": {"id": "ITEM-1"}},
                        "files": [],
                    },
                    "inputs": {"current_item": {"id": "ITEM-1"}},
                },
            }

        def update_job(self, *_args, **_kwargs):
            return None

        def update_job_runtime(self, *_args, **_kwargs):
            return None

        def finish_job(self, *_args, **_kwargs):
            return None

    class _FakeCallback:
        def __init__(self, *_args, **_kwargs):
            self._root_run_id = None

    fake_logger_mod = ModuleType("core.infrastructure.db.platform_logger")
    fake_logger_mod.PlatformLogger = _FakeLogger
    fake_logger_mod.PlatformWorkerStore = _FakeLogger
    fake_logger_mod.PlatformProjectStore = _FakeLogger
    fake_logger_mod.PlatformLoggerCallback = _FakeCallback

    msg = {
        "job_id": 500,
        "task_id": "TASK-CHILD-500",
        "thread_id": "thread-child-500",
        "task_summary": "Queued child invoke should restore task_envelope fallback",
        "task_source": "invoke_workflow",
        "project_name": "demo",
        "project_config": {"project_rules": "Be concise."},
        "mode": "brownfield",
        "extra_state": {},
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.application.orchestration.state": fake_core_state,
            "core.application.orchestration.agents.common": fake_agents_common,
            "core.infrastructure.db.platform_logger": fake_logger_mod,
            "core.infrastructure.db.platform_stores": fake_logger_mod,
            "core.application.jobs.terminal_persistence": _fake_terminal_persistence_module(),
        },
    ):
        worker_mod._execute_task(msg)

    kwargs = captured["builder_kwargs"] or {}
    extra_state = kwargs.get("extra_state") or {}
    assert extra_state["task_envelope"]["requested_workflow_id"] == "weekly_report"
    assert extra_state["task_envelope"]["inputs"]["current_item"]["id"] == "ITEM-1"
    assert extra_state["task_envelope"]["verification_commands"] == [
        "python -m pytest tests/unit/test_child_runtime.py -k envelope -q"
    ]
    assert extra_state["inputs"]["current_item"]["id"] == "ITEM-1"


def test_execute_task_promotes_mcp_workflow_inputs_for_prompt_context():
    worker_mod = _import_worker_module()

    captured = {"graph_input": None}

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {}

    class _GraphStub:
        pass

    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config
    fake_core_graph.build_registry_graph = lambda checkpointer=None, **_kwargs: _GraphStub()

    fake_agents_common = ModuleType("core.application.orchestration.agents.common")

    class _Cancelled(Exception):
        pass

    def _fake_run_graph_with_cancel_check(graph, input_or_command, config, bl=None, job_id=None):
        captured["graph_input"] = input_or_command
        return {"status": "completed"}

    fake_agents_common.JobCancelledException = _Cancelled
    fake_agents_common.run_graph_with_cancel_check = _fake_run_graph_with_cancel_check

    request = "Detailed callable request that must reach the agent prompt unchanged."
    task_envelope = {
        "project_id": "project_alpha",
        "requested_workflow_id": "ai_builder_fix_loop_20260531_224905",
        "title": "Short smoke summary",
        "description": "Short smoke summary",
        "inputs": {
            "request": request,
        },
    }
    msg = {
        "job_id": "job-mcp-1",
        "task_id": "TASK-MCP-1",
        "thread_id": "thread-mcp-1",
        "task_summary": "Short smoke summary",
        "task_source": "mcp_workflow",
        "project_name": "Flow Steward App",
        "project_config": {"project_rules": "Be concise."},
        "extra_state": {
            "workflow_id": "ai_builder_fix_loop_20260531_224905",
            "task_envelope": task_envelope,
            "inputs": dict(task_envelope["inputs"]),
        },
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.application.orchestration.agents.common": fake_agents_common,
        },
    ):
        worker_mod._execute_task(msg)

    state = captured["graph_input"] or {}
    from core.application.orchestration.agents.prompt_builder import PromptBuilder

    prompt_context = PromptBuilder._build_default_task_context(state)

    assert state["task_summary"] == "Short smoke summary"
    assert state["request"] == request
    assert state["original_request"] == request
    assert state["initial_requirements"] == request
    assert state["inputs"]["request"] == request
    assert f"Initial Requirements:\n{request}" in prompt_context


def test_get_all_project_ids_uses_observability_dsn_fallback():
    worker_mod = _import_worker_module()

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://obs"}

    captured = {"dsn": None}

    class _FakeLogger:
        def __init__(self, dsn):
            captured["dsn"] = dsn

        def get_projects(self):
            return [{"id": 10}, {"id": 20}]

    fake_logger_mod = ModuleType("core.infrastructure.db.platform_logger")
    fake_logger_mod.PlatformLogger = _FakeLogger
    fake_logger_mod.PlatformWorkerStore = _FakeLogger
    fake_logger_mod.PlatformProjectStore = _FakeLogger
    fake_logger_mod.PlatformLoggerCallback = type("PlatformLoggerCallback", (), {})

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.infrastructure.db.platform_logger": fake_logger_mod,
            "core.infrastructure.db.platform_stores": fake_logger_mod,
        },
    ):
        ids = worker_mod._get_all_project_ids()

    assert captured["dsn"] == "postgres://obs"
    assert ids == [10, 20]


def test_execute_task_marks_failed_when_graph_build_raises():
    worker_mod = _import_worker_module()

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://obs"}

    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config

    def _boom(*_args, **_kwargs):
        raise RuntimeError("Graph contract validation failed: missing schema")

    fake_core_graph.build_registry_graph = _boom

    captured = {"finish_calls": []}

    class _FakeLogger:
        def __init__(self, _dsn):
            pass

        def get_job(self, _job_id):
            return {"project_id": 1, "status": "pending", "meta": {}}

        def update_job(self, *_args, **_kwargs):
            return None

        def finish_job(self, *args, **kwargs):
            captured["finish_calls"].append({"args": args, "kwargs": kwargs})

    class _FakeCallback:
        def __init__(self, *_args, **_kwargs):
            self._root_run_id = None

    fake_logger_mod = ModuleType("core.infrastructure.db.platform_logger")
    fake_logger_mod.PlatformLogger = _FakeLogger
    fake_logger_mod.PlatformWorkerStore = _FakeLogger
    fake_logger_mod.PlatformProjectStore = _FakeLogger
    fake_logger_mod.PlatformLoggerCallback = _FakeCallback

    fake_agents_common = ModuleType("core.application.orchestration.agents.common")

    class _Cancelled(Exception):
        pass

    fake_agents_common.JobCancelledException = _Cancelled
    fake_agents_common.run_graph_with_cancel_check = lambda *args, **kwargs: {"status": "completed"}

    msg = {
        "job_id": 5,
        "task_id": "TASK-FAIL",
        "thread_id": "thread-fail",
        "task_summary": "Expect graph build failure",
        "task_source": "manual",
        "project_name": "demo",
        "project_config": {"project_rules": "Be concise."},
        "mode": "brownfield",
        "complexity": "bugfix",
        "extra_state": {},
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.infrastructure.db.platform_logger": fake_logger_mod,
            "core.infrastructure.db.platform_stores": fake_logger_mod,
            "core.application.orchestration.agents.common": fake_agents_common,
            "core.application.jobs.terminal_persistence": _fake_terminal_persistence_module(),
        },
    ):
        result = worker_mod._execute_task(msg)

    assert captured["finish_calls"], "finish_job should be called on early graph build failure"
    assert result["action"] == "ack"
    kwargs = captured["finish_calls"][-1]["kwargs"]
    assert kwargs.get("status") == "failed"
    assert "Graph contract validation failed" in str(kwargs.get("error") or "")


def test_execute_task_marks_failed_when_graph_returns_non_terminal_status_without_interrupt():
    worker_mod = _import_worker_module()

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://obs"}

    class _GraphStub:
        @staticmethod
        def get_state(_config):
            state = ModuleType("state")
            state.tasks = []
            return state

    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config
    fake_core_graph.build_registry_graph = lambda checkpointer=None, **_kwargs: _GraphStub()

    fake_core_state = ModuleType("core.application.orchestration.state")
    fake_core_state.build_initial_orchestrator_state = lambda **kwargs: {
        "status": "in_progress",
        **kwargs.get("extra_state", {}),
    }

    fake_agents_common = ModuleType("core.application.orchestration.agents.common")

    class _Cancelled(Exception):
        pass

    fake_agents_common.JobCancelledException = _Cancelled
    fake_agents_common.run_graph_with_cancel_check = (
        lambda graph, initial_state, config, bl=None, job_id=None: {"status": "in_progress"}
    )

    captured = {"finish_calls": [], "failure_kwargs": {}}

    class _FakeLogger:
        def __init__(self, _dsn):
            self.conn = None

        def get_job(self, _job_id):
            return {"project_id": 1, "meta": {}, "status": "pending"}

        def update_job(self, *_args, **_kwargs):
            return None

        def update_job_runtime(self, *_args, **_kwargs):
            return None

        def finish_job(self, *args, **kwargs):
            captured["finish_calls"].append({"args": args, "kwargs": kwargs})

        def log_event(self, *_args, **_kwargs):
            return None

    class _FakeCallback:
        def __init__(self, *_args, **_kwargs):
            self._root_run_id = None

        def terminal_error_context(self):
            return {"step_id": "step-7", "group_id": "delivery", "occurrence": 2}

    fake_logger_mod = ModuleType("core.infrastructure.db.platform_logger")
    fake_logger_mod.PlatformLogger = _FakeLogger
    fake_logger_mod.PlatformWorkerStore = _FakeLogger
    fake_logger_mod.PlatformProjectStore = _FakeLogger
    fake_logger_mod.PlatformLoggerCallback = _FakeCallback

    msg = {
        "job_id": 7,
        "task_id": "TASK-NONTERMINAL",
        "thread_id": "thread-nonterminal",
        "task_summary": "Graph returned in_progress",
        "task_source": "manual",
        "project_name": "demo",
        "project_config": {"project_rules": "Be concise."},
        "mode": "brownfield",
        "complexity": "bugfix",
        "extra_state": {},
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.application.orchestration.state": fake_core_state,
            "core.infrastructure.db.platform_logger": fake_logger_mod,
            "core.infrastructure.db.platform_stores": fake_logger_mod,
            "core.application.orchestration.agents.common": fake_agents_common,
            "core.application.jobs.terminal_persistence": _fake_terminal_persistence_module(
                captured["failure_kwargs"]
            ),
        },
    ):
        worker_mod._execute_task(msg)

    assert captured["finish_calls"], (
        "finish_job should be called when graph returns non-terminal status"
    )
    kwargs = captured["finish_calls"][-1]["kwargs"]
    assert kwargs.get("status") == "failed"
    assert "non-terminal status 'in_progress'" in str(kwargs.get("error") or "")
    failure_kwargs = captured["failure_kwargs"]
    # RS5-012: this used to assert error_code == "failed" — the job status
    # restated as a diagnosis, which is the defect, not the contract. When the
    # graph supplies no code, none is invented here; the public projection's own
    # fallback ladder (job_failed / cancelled / preflight_failed) then runs.
    assert failure_kwargs["error_code"] is None
    assert failure_kwargs["category"] == "execution"
    assert failure_kwargs["retryable"] is False
    assert failure_kwargs["step_id"] == "step-7"
    assert failure_kwargs["group_id"] == "delivery"
    assert failure_kwargs["occurrence"] == 2
    assert failure_kwargs["expected_worker_id"]


def test_execute_task_marks_failed_when_graph_result_omits_status_without_interrupt():
    worker_mod = _import_worker_module()

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://obs"}

    class _GraphStub:
        @staticmethod
        def get_state(_config):
            state = ModuleType("state")
            state.tasks = []
            return state

    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config
    fake_core_graph.build_registry_graph = lambda checkpointer=None, **_kwargs: _GraphStub()

    fake_core_state = ModuleType("core.application.orchestration.state")
    fake_core_state.build_initial_orchestrator_state = lambda **kwargs: {
        "status": "in_progress",
        **kwargs.get("extra_state", {}),
    }

    fake_agents_common = ModuleType("core.application.orchestration.agents.common")

    class _Cancelled(Exception):
        pass

    fake_agents_common.JobCancelledException = _Cancelled
    fake_agents_common.run_graph_with_cancel_check = (
        lambda graph, initial_state, config, bl=None, job_id=None: {"current_step_id": "analysis"}
    )

    captured = {"finish_calls": []}

    class _FakeLogger:
        def __init__(self, _dsn):
            self.conn = None

        def get_job(self, _job_id):
            return {"project_id": 1, "meta": {}, "status": "pending"}

        def update_job(self, *_args, **_kwargs):
            return None

        def update_job_runtime(self, *_args, **_kwargs):
            return None

        def finish_job(self, *args, **kwargs):
            captured["finish_calls"].append({"args": args, "kwargs": kwargs})

        def log_event(self, *_args, **_kwargs):
            return None

    class _FakeCallback:
        def __init__(self, *_args, **_kwargs):
            self._root_run_id = None

    fake_logger_mod = ModuleType("core.infrastructure.db.platform_logger")
    fake_logger_mod.PlatformLogger = _FakeLogger
    fake_logger_mod.PlatformWorkerStore = _FakeLogger
    fake_logger_mod.PlatformProjectStore = _FakeLogger
    fake_logger_mod.PlatformLoggerCallback = _FakeCallback

    msg = {
        "job_id": 8,
        "task_id": "TASK-MISSING-STATUS",
        "thread_id": "thread-missing-status",
        "task_summary": "Graph returned partial state without status",
        "task_source": "manual",
        "project_name": "demo",
        "project_config": {"project_rules": "Be concise."},
        "mode": "brownfield",
        "complexity": "bugfix",
        "extra_state": {},
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.application.orchestration.state": fake_core_state,
            "core.infrastructure.db.platform_logger": fake_logger_mod,
            "core.infrastructure.db.platform_stores": fake_logger_mod,
            "core.application.orchestration.agents.common": fake_agents_common,
            "core.application.jobs.terminal_persistence": _fake_terminal_persistence_module(),
        },
    ):
        worker_mod._execute_task(msg)

    assert captured["finish_calls"], "finish_job should be called for missing graph status"
    kwargs = captured["finish_calls"][-1]["kwargs"]
    assert kwargs.get("status") == "failed"
    assert "non-terminal status 'in_progress'" in str(kwargs.get("error") or "")


def test_execute_task_skips_when_job_is_cancelled_after_dequeue_before_claim():
    worker_mod = _import_worker_module()

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://obs"}

    captured = {"graph_built": False}

    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config

    def _build_graph(*_args, **_kwargs):
        captured["graph_built"] = True
        return object()

    fake_core_graph.build_registry_graph = _build_graph

    fake_core_state = ModuleType("core.application.orchestration.state")
    fake_core_state.build_initial_orchestrator_state = lambda **kwargs: {"status": "in_progress"}

    fake_agents_common = ModuleType("core.application.orchestration.agents.common")

    class _Cancelled(Exception):
        pass

    fake_agents_common.JobCancelledException = _Cancelled
    fake_agents_common.run_graph_with_cancel_check = lambda *args, **kwargs: {"status": "completed"}

    class _Cursor:
        def __init__(self):
            self.rowcount = 0

        def execute(self, *_args, **_kwargs):
            self.rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            return None

    class _FakeLogger:
        def __init__(self, _dsn):
            self.conn = _Conn()
            self._job_reads = 0

        def get_job(self, _job_id):
            self._job_reads += 1
            if self._job_reads == 1:
                return {"project_id": 1, "meta": {}, "status": "pending"}
            return {"project_id": 1, "meta": {}, "status": "cancelled"}

        def finish_job(self, *_args, **_kwargs):
            raise AssertionError("finish_job must not be called when cancel wins the claim race")

    fake_logger_mod = ModuleType("core.infrastructure.db.platform_logger")
    fake_logger_mod.PlatformLogger = _FakeLogger
    fake_logger_mod.PlatformWorkerStore = _FakeLogger
    fake_logger_mod.PlatformProjectStore = _FakeLogger
    fake_logger_mod.PlatformLoggerCallback = type("PlatformLoggerCallback", (), {})

    msg = {
        "job_id": 77,
        "task_id": "TASK-CANCEL-RACE",
        "thread_id": "thread-cancel-race",
        "task_summary": "Cancel race should skip execution",
        "task_source": "manual",
        "project_name": "demo",
        "project_config": {"project_rules": "Be concise."},
        "mode": "brownfield",
        "complexity": "bugfix",
        "extra_state": {},
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.application.orchestration.state": fake_core_state,
            "core.application.orchestration.agents.common": fake_agents_common,
            "core.infrastructure.db.platform_logger": fake_logger_mod,
            "core.infrastructure.db.platform_stores": fake_logger_mod,
        },
    ):
        worker_mod._execute_task(msg)

    assert captured["graph_built"] is False


def test_execute_task_treats_generator_exit_after_operator_cancel_as_cancelled():
    worker_mod = _import_worker_module()

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://obs"}

    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config
    fake_core_graph.build_registry_graph = lambda *_args, **_kwargs: object()

    fake_core_state = ModuleType("core.application.orchestration.state")
    fake_core_state.build_initial_orchestrator_state = lambda **kwargs: {"status": "in_progress"}

    fake_agents_common = ModuleType("core.application.orchestration.agents.common")

    class _Cancelled(Exception):
        pass

    def _raise_generator_exit(*_args, **_kwargs):
        raise GeneratorExit

    fake_agents_common.JobCancelledException = _Cancelled
    fake_agents_common.run_graph_with_cancel_check = _raise_generator_exit

    captured = {"finish_calls": [], "errors": []}

    class _FakeLogger:
        def __init__(self, _dsn):
            self.conn = None
            self._job_reads = 0

        def get_job(self, _job_id):
            self._job_reads += 1
            if self._job_reads >= 3:
                return {"project_id": 1, "meta": {}, "status": "cancelled"}
            return {"project_id": 1, "meta": {}, "status": "pending"}

        def update_job(self, *_args, **_kwargs):
            return None

        def update_job_runtime(self, *_args, **_kwargs):
            return None

        def finish_job(self, *args, **kwargs):
            captured["finish_calls"].append({"args": args, "kwargs": kwargs})

        def log_job_error(self, *args, **kwargs):
            captured["errors"].append({"args": args, "kwargs": kwargs})

    class _FakeCallback:
        def __init__(self, *_args, **_kwargs):
            self._root_run_id = None

    fake_logger_mod = ModuleType("core.infrastructure.db.platform_logger")
    fake_logger_mod.PlatformLogger = _FakeLogger
    fake_logger_mod.PlatformWorkerStore = _FakeLogger
    fake_logger_mod.PlatformProjectStore = _FakeLogger
    fake_logger_mod.PlatformLoggerCallback = _FakeCallback

    msg = {
        "job_id": 88,
        "task_id": "TASK-CANCEL-GENERATOR",
        "thread_id": "thread-cancel-generator",
        "task_summary": "Cancel should not record GeneratorExit as unknown error",
        "task_source": "manual",
        "project_name": "demo",
        "project_config": {"project_rules": "Be concise."},
        "mode": "brownfield",
        "complexity": "bugfix",
        "extra_state": {},
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.application.orchestration.state": fake_core_state,
            "core.application.orchestration.agents.common": fake_agents_common,
            "core.infrastructure.db.platform_logger": fake_logger_mod,
            "core.infrastructure.db.platform_stores": fake_logger_mod,
            "core.application.jobs.terminal_persistence": _fake_terminal_persistence_module(),
        },
    ):
        result = worker_mod._execute_task(msg)

    assert result["action"] == "ack"
    assert result["final_status"] == "cancelled"
    assert captured["errors"] == []
    assert captured["finish_calls"], "finish_job should record terminal cancellation"
    assert captured["finish_calls"][-1]["kwargs"]["status"] == "cancelled"
    assert captured["finish_calls"][-1]["kwargs"].get("error") is None


def test_execute_task_discards_duplicate_delivery_for_active_in_progress_job():
    worker_mod = _import_worker_module()

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://obs"}

    graph_calls = {"count": 0}
    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config

    def _build_graph_stub(*_args, **_kwargs):
        graph_calls["count"] += 1
        return object()

    fake_core_graph.build_registry_graph = _build_graph_stub

    captured = {"update_job": None, "runtime": None}

    class _FakeLogger:
        def __init__(self, _dsn):
            pass

        def get_job(self, _job_id):
            return {
                "id": 5,
                "project_id": 1,
                "status": "in_progress",
                "worker_id": "worker-active",
                "meta": {},
                "thread_id": "thread-5",
                "current_step_id": "implementation",
                "current_agent": "composer",
            }

        def get_logs(self, *_args, **_kwargs):
            return [
                {
                    "id": 101,
                    "agent": "composer",
                    "action_type": "prompt_request",
                    "model_used": "gpt-4o-mini",
                    "meta": {
                        "correlation_id": "corr-101",
                        "skill": "write_section",
                        "step_id": "implementation",
                        "provider": "openai",
                    },
                    "payload": {
                        "prompt_version": "v1",
                        "system_prompt": "You are a coding agent",
                        "user_prompt": "Implement the fix",
                    },
                }
            ]

        def get_latest_agent_message(self, *_args, **_kwargs):
            return None

        def update_job(self, *args, **kwargs):
            captured["update_job"] = {"args": args, "kwargs": kwargs}

        def update_job_runtime(self, *args, **kwargs):
            captured["runtime"] = {"args": args, "kwargs": kwargs}

        def log_event(self, *_args, **_kwargs):
            return None

    fake_logger_mod = ModuleType("core.infrastructure.db.platform_logger")
    fake_logger_mod.PlatformLogger = _FakeLogger
    fake_logger_mod.PlatformWorkerStore = _FakeLogger
    fake_logger_mod.PlatformProjectStore = _FakeLogger
    fake_logger_mod.PlatformLoggerCallback = type("PlatformLoggerCallback", (), {})

    msg = {
        "job_id": 5,
        "task_id": "TASK-5",
        "thread_id": "thread-5",
        "task_summary": "Recover interrupted run",
        "task_source": "manual",
        "project_name": "demo",
        "project_config": {"project_rules": "Be concise."},
        "mode": "brownfield",
        "extra_state": {},
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.infrastructure.db.platform_logger": fake_logger_mod,
            "core.infrastructure.db.platform_stores": fake_logger_mod,
        },
    ):
        result = worker_mod._execute_task(msg)

    assert graph_calls["count"] == 0
    assert result == {"action": "discard", "reason": "duplicate_in_progress"}
    assert captured["update_job"] is None
    assert captured["runtime"] is None


def test_execute_task_discards_in_progress_redelivery_without_inspecting_snapshot():
    worker_mod = _import_worker_module()

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://obs"}

    graph_calls = {"count": 0}
    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config

    class _GraphStub:
        pass

    def _build_graph_stub(*_args, **_kwargs):
        graph_calls["count"] += 1
        return _GraphStub()

    fake_core_graph.build_registry_graph = _build_graph_stub

    fake_core_state = ModuleType("core.application.orchestration.state")
    fake_core_state.build_initial_orchestrator_state = lambda **kwargs: {
        "status": "in_progress",
        **kwargs.get("extra_state", {}),
    }

    fake_agents_common = ModuleType("core.application.orchestration.agents.common")

    class _Cancelled(Exception):
        pass

    fake_agents_common.JobCancelledException = _Cancelled
    fake_agents_common.run_graph_with_cancel_check = (
        lambda graph, initial_state, config, bl=None, job_id=None: {"status": "completed"}
    )

    captured = {"update_job": []}

    class _FakeLogger:
        def __init__(self, _dsn):
            self.conn = None

        def get_job(self, _job_id):
            return {
                "id": 9,
                "project_id": 1,
                "status": "in_progress",
                "meta": {},
                "thread_id": "thread-9",
                "current_step_id": "implementation",
                "current_agent": "composer",
            }

        def get_logs(self, *_args, **_kwargs):
            # No prompt_request payload -> snapshot not actionable.
            return []

        def get_latest_agent_message(self, *_args, **_kwargs):
            return None

        def update_job(self, *args, **kwargs):
            captured["update_job"].append({"args": args, "kwargs": kwargs})
            return

        def update_job_runtime(self, *_args, **_kwargs):
            return None

        def finish_job(self, *_args, **_kwargs):
            return None

        def log_event(self, *_args, **_kwargs):
            return None

    class _FakeCallback:
        def __init__(self, *_args, **_kwargs):
            self._root_run_id = None

    fake_logger_mod = ModuleType("core.infrastructure.db.platform_logger")
    fake_logger_mod.PlatformLogger = _FakeLogger
    fake_logger_mod.PlatformWorkerStore = _FakeLogger
    fake_logger_mod.PlatformProjectStore = _FakeLogger
    fake_logger_mod.PlatformLoggerCallback = _FakeCallback

    msg = {
        "job_id": 9,
        "task_id": "TASK-9",
        "thread_id": "thread-9",
        "task_summary": "Continue when snapshot is not actionable",
        "task_source": "manual",
        "project_name": "demo",
        "project_config": {"project_rules": "Be concise."},
        "mode": "brownfield",
        "extra_state": {},
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.application.orchestration.state": fake_core_state,
            "core.application.orchestration.agents.common": fake_agents_common,
            "core.infrastructure.db.platform_logger": fake_logger_mod,
            "core.infrastructure.db.platform_stores": fake_logger_mod,
            "core.application.jobs.terminal_persistence": _fake_terminal_persistence_module(),
        },
    ):
        result = worker_mod._execute_task(msg)

    assert result == {"action": "discard", "reason": "duplicate_in_progress"}
    assert graph_calls["count"] == 0
    retryable_marks = [
        c
        for c in captured["update_job"]
        if c["kwargs"].get("status") == "retryable_interrupted_step"
    ]
    assert not retryable_marks


def test_execute_task_recovers_dead_owner_and_eventually_executes_same_delivery():
    worker_mod = _import_worker_module()

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://obs"}

    graph_calls = {"count": 0}
    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
    fake_core_graph.graph_run_config = graph_run_config
    fake_core_graph.build_registry_graph = lambda *_args, **_kwargs: object()

    fake_core_state = ModuleType("core.application.orchestration.state")
    fake_core_state.build_initial_orchestrator_state = lambda **kwargs: {
        "status": "in_progress",
        **kwargs.get("extra_state", {}),
    }

    fake_agents_common = ModuleType("core.application.orchestration.agents.common")

    class _Cancelled(Exception):
        pass

    fake_agents_common.JobCancelledException = _Cancelled

    def _run_graph(*_args, **_kwargs):
        graph_calls["count"] += 1
        return {"status": "completed", "final_report": "recovered"}

    fake_agents_common.run_graph_with_cancel_check = _run_graph

    state = {"status": "in_progress", "worker_id": "crashed-worker", "recoveries": 0}

    class _FakeLogger:
        def __init__(self, _dsn):
            self.conn = None

        def get_job(self, _job_id):
            return {
                "id": 19,
                "project_id": "project-19",
                "status": state["status"],
                "worker_id": state["worker_id"],
                "meta": {},
            }

        def get_project_execution_lease(self, project_id, **kwargs):
            assert project_id == "project-19"
            assert kwargs == {
                "active_only": True,
                "worker_instance_id": "crashed-worker",
            }

        def recover_in_progress_job(self, job_id, *, expected_worker_id, reason):
            assert str(job_id) == "19"
            assert expected_worker_id == "crashed-worker"
            assert "owner lease expired" in reason
            assert state["status"] == "in_progress"
            state.update(status="orphaned", worker_id=None, recoveries=1)
            return True

        def update_job(self, _job_id, **kwargs):
            state["status"] = kwargs.get("status", state["status"])
            state["worker_id"] = kwargs.get("worker_id", state["worker_id"])

        def update_job_runtime(self, *_args, **_kwargs):
            return None

        def finish_job(self, *_args, **_kwargs):
            state["status"] = "completed"

        def log_event(self, *_args, **_kwargs):
            return None

    class _FakeCallback:
        def __init__(self, *_args, **_kwargs):
            self._root_run_id = None

    fake_logger_mod = ModuleType("core.infrastructure.db.platform_logger")
    fake_logger_mod.PlatformLogger = _FakeLogger
    fake_logger_mod.PlatformWorkerStore = _FakeLogger
    fake_logger_mod.PlatformProjectStore = _FakeLogger
    fake_logger_mod.PlatformLoggerCallback = _FakeCallback

    msg = {
        "job_id": 19,
        "task_id": "TASK-19",
        "thread_id": "thread-19",
        "task_summary": "Recover after worker crash",
        "task_source": "manual",
        "project_name": "demo",
        "project_config": {"project_rules": "Be concise."},
        "mode": "brownfield",
        "extra_state": {},
    }

    with patch.dict(
        sys.modules,
        {
            "core.infrastructure.config.runtime_config": fake_core_config,
            "core.application.orchestration.graph_runtime": fake_core_graph,
            "core.application.orchestration.state": fake_core_state,
            "core.application.orchestration.agents.common": fake_agents_common,
            "core.infrastructure.db.platform_logger": fake_logger_mod,
            "core.infrastructure.db.platform_stores": fake_logger_mod,
            "core.application.jobs.terminal_persistence": _fake_terminal_persistence_module(),
        },
    ):
        result = worker_mod._execute_task(msg)

    assert result["action"] == "ack"
    assert result["final_status"] == "completed"
    assert graph_calls["count"] == 1
    assert state["recoveries"] == 1


def test_in_progress_owner_lease_must_match_the_claimed_job():
    from core.infrastructure.db.worker import JobExecutionRepositoryAdapter

    class _Logger:
        def get_project_execution_lease(self, project_id, **kwargs):
            assert project_id == "project-21"
            assert kwargs["worker_instance_id"] == "worker-21"
            return {"active_job_id": "job-22"}

    adapter = JobExecutionRepositoryAdapter(_Logger(), MagicMock())
    assert (
        adapter.in_progress_owner_has_active_lease(
            {
                "id": "job-21",
                "project_id": "project-21",
                "worker_id": "worker-21",
            }
        )
        is False
    )


def test_recovery_claim_preserves_the_original_job_start_time() -> None:
    from core.infrastructure.db.worker import JobExecutionRepositoryAdapter

    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.rowcount = 1
    connection = MagicMock()
    connection.cursor.return_value = cursor
    adapter = JobExecutionRepositoryAdapter(SimpleNamespace(conn=connection), MagicMock())

    result = adapter.claim(
        "job-recovered",
        worker_id="host-incarnation-1",
        allow_in_progress=False,
    )

    normalized_sql = " ".join(cursor.execute.call_args.args[0].split())
    assert result.claimed is True
    assert "started_at = COALESCE(started_at, NOW())" in normalized_sql
    assert "WHEN status IN ('pending', 'stuck', 'orphaned') THEN NOW()" not in normalized_sql


def test_worker_restores_the_canonical_workflow_key_from_the_job_row() -> None:
    from core.application.jobs.task_execution_request import _restore_job_meta

    extra_state: dict[str, object] = {}
    project_config: dict[str, object] = {}

    _, workflow_id = _restore_job_meta(
        {
            "task_source": "workflow_test_run",
            "selected_workflow_key": "extension.install-a.release_audit",
            "meta": {"test_mode": True},
        },
        extra_state,
        project_config,
        False,
        "",
        None,
    )

    assert workflow_id == "extension.install-a.release_audit"
    assert extra_state["workflow_id"] == workflow_id
    assert project_config["workflow_id"] == workflow_id
