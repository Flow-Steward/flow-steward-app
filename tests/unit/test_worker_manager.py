from __future__ import annotations

import signal
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.infrastructure.runtime.worker.manager import (
    ProjectWorkerManager,
    _parse_project_ids,
    _signal_process_tree,
)


class _FakeProcess:
    def __init__(self, pid: int, returncode: int | None = None):
        self.pid = pid
        self.returncode = returncode
        self.wait_calls: list[float | None] = []
        self.signals: list[int] = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig in (signal.SIGTERM, signal.SIGKILL):
            self.returncode = 0


def test_parse_project_ids_returns_none_for_empty_input():
    assert _parse_project_ids("") is None
    assert _parse_project_ids(None) is None


def test_parse_project_ids_parses_csv():
    assert _parse_project_ids("1, 2,3") == {"1", "2", "3"}


def test_sync_once_spawns_one_worker_per_project():
    spawned: list[list[str]] = []

    def _popen(cmd, **_kwargs):
        spawned.append(cmd)
        return _FakeProcess(pid=100 + len(spawned))

    manager = ProjectWorkerManager(
        discover_projects=lambda: [2, 1],
        discovery_interval=10,
        restart_delay=0,
        popen_factory=_popen,
        sleep_fn=lambda _seconds: None,
    )

    desired = manager.sync_once()

    assert desired == ["1", "2"]
    assert sorted(manager.children) == ["1", "2"]
    assert spawned == [
        [sys.executable, "-m", "core.entrypoints.cli.worker_process", "--project-id", "1"],
        [sys.executable, "-m", "core.entrypoints.cli.worker_process", "--project-id", "2"],
    ]


def test_sync_once_applies_project_filter():
    spawned: list[list[str]] = []

    def _popen(cmd, **_kwargs):
        spawned.append(cmd)
        return _FakeProcess(pid=200 + len(spawned))

    manager = ProjectWorkerManager(
        discover_projects=lambda: [1, 2, 3],
        project_filter={"2"},
        restart_delay=0,
        popen_factory=_popen,
        sleep_fn=lambda _seconds: None,
    )

    manager.sync_once()

    assert sorted(manager.children) == ["2"]
    assert spawned == [
        [sys.executable, "-m", "core.entrypoints.cli.worker_process", "--project-id", "2"]
    ]


def test_sync_once_stops_worker_when_project_removed(monkeypatch):
    calls = iter(([1, 2], [2]))
    stopped: list[tuple[int, int]] = []

    def _popen(cmd, **_kwargs):
        project_id = int(cmd[-1])
        return _FakeProcess(pid=300 + project_id)

    def _signal(proc, sig):
        stopped.append((proc.pid, sig))
        proc.send_signal(sig)

    monkeypatch.setattr("core.infrastructure.runtime.worker.manager._signal_process_tree", _signal)

    manager = ProjectWorkerManager(
        discover_projects=lambda: next(calls),
        restart_delay=0,
        popen_factory=_popen,
        sleep_fn=lambda _seconds: None,
    )

    manager.sync_once()
    manager.sync_once()

    assert sorted(manager.children) == ["2"]
    assert stopped == [(301, signal.SIGTERM)]


def test_sync_once_restarts_exited_worker():
    spawned: list[int] = []
    now = {"value": 0.0}

    def _popen(cmd, **_kwargs):
        project_id = int(cmd[-1])
        spawned.append(project_id)
        return _FakeProcess(pid=400 + len(spawned))

    manager = ProjectWorkerManager(
        discover_projects=lambda: [7],
        restart_delay=0,
        popen_factory=_popen,
        monotonic=lambda: now["value"],
        sleep_fn=lambda _seconds: None,
    )

    manager.sync_once()
    first_child = manager.children["7"].process
    first_child.returncode = 9
    now["value"] = 1.0

    manager.sync_once()

    assert spawned == [7, 7]
    assert manager.children["7"].process is not first_child


def test_discovery_failure_keeps_existing_children():
    calls = {"count": 0}

    def _discover():
        calls["count"] += 1
        if calls["count"] == 1:
            return [5]
        raise RuntimeError("database unavailable")

    manager = ProjectWorkerManager(
        discover_projects=_discover,
        restart_delay=0,
        popen_factory=lambda cmd, **_kwargs: _FakeProcess(pid=500 + int(cmd[-1])),
        sleep_fn=lambda _seconds: None,
    )

    manager.sync_once()
    desired = manager.sync_once()

    assert desired is None
    assert sorted(manager.children) == ["5"]


def test_signal_process_tree_prefers_process_group(monkeypatch):
    calls: list[list[str]] = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("core.infrastructure.runtime.worker.manager.subprocess.run", _run)

    _signal_process_tree(_FakeProcess(pid=321), signal.SIGTERM)

    assert calls == [["kill", "-15", "--", "-321"]]


def test_signal_process_tree_falls_back_to_child_pid(monkeypatch):
    calls: list[list[str]] = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("core.infrastructure.runtime.worker.manager.subprocess.run", _run)

    _signal_process_tree(_FakeProcess(pid=654), signal.SIGKILL)

    assert calls == [
        ["kill", "-9", "--", "-654"],
        ["kill", "-9", "--", "654"],
    ]
