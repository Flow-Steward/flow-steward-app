from __future__ import annotations

import signal
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock


class _FakeLogger:
    def __init__(self, rows):
        self._rows = rows
        self.logged: list[dict] = []

    def get_logs(self, **_kwargs):
        return list(self._rows)

    def get_job(self, job_id):
        return {
            "id": job_id,
            "project_id": "project-1",
            "task_id": "task-1",
            "task_summary": "Run workflow",
        }

    def log_action(self, **kwargs):
        self.logged.append(kwargs)


def _running_row(*, timestamp=None, pid=4242, worker_id="worker-1", kind="tool_process"):
    return {
        "timestamp": timestamp or datetime.now(UTC),
        "agent": "composer",
        "action_type": "worker_subprocess",
        "status": "running",
        "payload": {
            "pid": pid,
            "worker_id": worker_id,
            "kind": kind,
            "cmdline": "python -m compileall",
            "cwd": "/app",
        },
    }


def test_terminate_process_group_kills_isolated_group_and_waits(monkeypatch):
    from core.infrastructure.runtime import subprocess_lifecycle

    proc = MagicMock(pid=4242)
    monkeypatch.setattr(subprocess_lifecycle.os, "name", "posix")
    monkeypatch.setattr(subprocess_lifecycle.os, "getpgid", lambda _pid: 4242)
    monkeypatch.setattr(subprocess_lifecycle.os, "getpgrp", lambda: 1111)
    killpg = MagicMock()
    monkeypatch.setattr(subprocess_lifecycle.os, "killpg", killpg, raising=False)

    subprocess_lifecycle.terminate_process_group(proc)

    killpg.assert_called_once_with(4242, signal.SIGKILL)
    proc.kill.assert_not_called()
    proc.wait.assert_called_once_with(timeout=5.0)


def test_terminalize_running_worker_subprocesses_marks_cancelled():
    from core.infrastructure.runtime.subprocess_lifecycle import (
        terminalize_running_worker_subprocesses,
    )

    logger = _FakeLogger([_running_row()])

    count = terminalize_running_worker_subprocesses(
        logger,
        job_id="job-1",
        status="cancelled",
        reason="operator cancelled",
    )

    assert count == 1
    assert logger.logged[0]["status"] == "cancelled"
    assert logger.logged[0]["project_id"] == "project-1"
    assert logger.logged[0]["payload"]["pid"] == 4242
    assert logger.logged[0]["result"]["reason"] == "operator cancelled"


def test_log_worker_subprocess_uses_worker_id_compatible_with_worker_leases(monkeypatch):
    from core.infrastructure.runtime.subprocess_lifecycle import log_worker_subprocess

    logger = _FakeLogger([])
    monkeypatch.delenv("FS_WORKER_ID", raising=False)
    monkeypatch.setenv("HOSTNAME", "worker-host")

    log_worker_subprocess(
        bl=logger,
        project_id="project-1",
        job_id="job-1",
        task_id="task-1",
        task_summary="Run workflow",
        agent="composer",
        status="running",
        pid=4242,
        cmd="python -m compileall",
        cwd="/app",
        kind="tool_process",
    )

    assert logger.logged[0]["payload"]["worker_id"].startswith("worker-host-")


def test_reap_stale_worker_subprocesses_marks_vanished_process_failed():
    from core.infrastructure.runtime.subprocess_lifecycle import reap_stale_worker_subprocesses

    logger = _FakeLogger(
        [_running_row(timestamp=datetime.now(UTC) - timedelta(minutes=5), pid=9999)]
    )

    count = reap_stale_worker_subprocesses(
        logger,
        job_id="job-1",
        grace_seconds=60,
        pid_exists=lambda _pid: False,
    )

    assert count == 1
    assert logger.logged[0]["status"] == "failed"
    assert "process no longer exists" in logger.logged[0]["error"]


def test_reap_stale_worker_subprocesses_uses_stale_worker_evidence_without_pid_probe():
    from core.infrastructure.runtime.subprocess_lifecycle import reap_stale_worker_subprocesses

    logger = _FakeLogger(
        [_running_row(timestamp=datetime.now(UTC) - timedelta(minutes=5), pid=9999)]
    )

    count = reap_stale_worker_subprocesses(
        logger,
        job_id="job-1",
        grace_seconds=60,
        stale_worker_ids={"worker-1"},
    )

    assert count == 1
    assert logger.logged[0]["status"] == "failed"
    assert "owning worker is no longer live" in logger.logged[0]["error"]


def test_reap_stale_worker_subprocesses_does_not_probe_cross_namespace_pid_by_default():
    from core.infrastructure.runtime.subprocess_lifecycle import reap_stale_worker_subprocesses

    logger = _FakeLogger(
        [_running_row(timestamp=datetime.now(UTC) - timedelta(minutes=5), pid=9999)]
    )

    count = reap_stale_worker_subprocesses(
        logger,
        job_id="job-1",
        grace_seconds=60,
    )

    assert count == 0
    assert logger.logged == []


def test_reap_stale_worker_subprocesses_leaves_live_process_running():
    from core.infrastructure.runtime.subprocess_lifecycle import reap_stale_worker_subprocesses

    logger = _FakeLogger(
        [_running_row(timestamp=datetime.now(UTC) - timedelta(minutes=5), pid=9999)]
    )

    count = reap_stale_worker_subprocesses(
        logger,
        job_id="job-1",
        grace_seconds=60,
        pid_exists=lambda _pid: True,
    )

    assert count == 0
    assert logger.logged == []
