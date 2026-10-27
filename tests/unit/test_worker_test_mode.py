from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


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
    fake_queue = ModuleType("core.infrastructure.runtime.worker.queue")
    fake_queue.consume_project_queue = lambda *args, **kwargs: None
    fake_queue.consume_all_projects = lambda *args, **kwargs: None

    with patch.dict(sys.modules, {"core.infrastructure.runtime.worker.queue": fake_queue}):
        return importlib.import_module("core.infrastructure.runtime.worker.worker")


def test_worker_skips_external_side_effects_in_test_mode(monkeypatch):
    worker_mod = _import_worker_module()

    side_effect_calls = {"sample_tasks": 0, "notify": 0, "finish": 0}

    fake_core_config = ModuleType("core.infrastructure.config.runtime_config")
    fake_core_config.CONFIG = {"fs_observability_dsn": "postgres://obs"}

    class _GraphStub:
        pass

    fake_core_graph = ModuleType("core.application.orchestration.graph_runtime")
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
        lambda graph, initial_state, config, bl=None, job_id=None: {"status": "completed"}
    )

    class _FakeLogger:
        def __init__(self, _dsn):
            self.conn = None

        def get_job(self, _job_id):
            return {"project_id": 7, "meta": {"test_mode": True}, "status": "pending"}

        def update_job(self, *_args, **_kwargs):
            return None

        def finish_job(self, *_args, **_kwargs):
            side_effect_calls["finish"] += 1

    class _FakeCallback:
        def __init__(self, *_args, **_kwargs):
            self._root_run_id = None

    fake_logger_mod = ModuleType("core.infrastructure.db.platform_logger")
    fake_logger_mod.PlatformLogger = _FakeLogger
    fake_logger_mod.PlatformLoggerCallback = _FakeCallback

    fake_terminal_persistence = ModuleType("core.application.jobs.terminal_persistence")

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
            expected_worker_id=None,
        ):
            self.logger.finish_job(
                job_id,
                status="completed",
                final_report=final_report,
                error=None,
                root_run_id=root_run_id,
            )
            return SimpleNamespace(status="completed")

    fake_terminal_persistence.TerminalPersistenceService = _TerminalPersistenceService
    fake_terminal_persistence.TerminalPersistenceConflict = _TerminalPersistenceConflict

    fake_notify = ModuleType("core.application.notifications.dispatch")
    fake_notify.dispatch_project_notification_event = lambda *args, **kwargs: (
        side_effect_calls.__setitem__("notify", side_effect_calls["notify"] + 1)
    )

    msg = {
        "job_id": 42,
        "task_id": "TASK-42",
        "thread_id": "thread-42",
        "task_summary": "Run test mode flow",
        "task_source": "manual",
        "project_name": "demo",
        "project_config": {"project_name": "demo"},
        "mode": "brownfield",
        "extra_state": {"test_mode": True, "workflow_id": "wf-demo"},
    }

    try:
        with (
            patch.dict(
                sys.modules,
                {
                    "core.infrastructure.config.runtime_config": fake_core_config,
                    "core.application.orchestration.graph_runtime": fake_core_graph,
                    "core.application.orchestration.state": fake_core_state,
                    "core.application.orchestration.agents.common": fake_agents_common,
                    "core.infrastructure.db.platform_logger": fake_logger_mod,
                    "core.application.notifications.dispatch": fake_notify,
                    "core.application.jobs.terminal_persistence": fake_terminal_persistence,
                },
            ),
            patch.object(
                worker_mod._task_execution_runtime,
                "_platform_logger_factory",
                return_value=_FakeLogger,
            ),
        ):
            worker_mod._execute_task(msg)
    finally:
        # Avoid leaking stubbed worker package state into other test modules.
        sys.modules.pop("core.infrastructure.runtime.worker.worker", None)
        sys.modules.pop("worker", None)
        sys.modules.pop("core.infrastructure.runtime.worker.queue", None)

    assert side_effect_calls["finish"] == 1
    assert side_effect_calls["sample_tasks"] == 0
    assert side_effect_calls["notify"] == 0
