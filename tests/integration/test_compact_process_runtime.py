from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
from core.infrastructure.runtime.compact.health import RabbitMQProbeTarget, monitor_dependencies
from core.infrastructure.runtime.compact.process_spec import ProcessSpec
from core.infrastructure.runtime.compact.readiness import ReadinessRegistry
from core.infrastructure.runtime.compact.supervisor import CompactSupervisor


def _wait_for(path: Path, *, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()


def _spawn(spec: ProcessSpec):
    return subprocess.Popen(list(spec.command), env=spec.environment, start_new_session=True)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires Linux /proc process ownership")
def test_role_crash_restarts_group_and_shutdown_reaps_separate_session_descendant(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "role.py"
    helper.write_text(
        "import os, pathlib, signal, subprocess, sys, time\n"
        "root = pathlib.Path(sys.argv[1]); role = sys.argv[2]\n"
        "generation = len(list(root.glob(role + '-*.pid'))) + 1\n"
        "(root / f'{role}-{generation}.pid').write_text(str(os.getpid()))\n"
        "if role == 'app':\n"
        " child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        " (root / f'child-{generation}.pid').write_text(str(child.pid))\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    environment = {"PATH": os.environ["PATH"]}
    specs = tuple(
        ProcessSpec(
            role=role,
            command=(sys.executable, str(helper), str(tmp_path), role),
            uid=os.getuid(),
            gid=os.getgid(),
            port=None,
            environment=environment,
        )
        for role in ("router", "app")
    )
    settings = CompactRuntimeSettings.from_environment(
        {
            "FS_DEPLOYMENT_PROFILE": "compact",
            "FS_COMPACT_SHUTDOWN_GRACE_SECONDS": "4",
            "FS_COMPACT_KILL_RESERVE_SECONDS": "1",
        }
    )

    supervisor = CompactSupervisor(
        specs=specs,
        settings=settings,
        process_factory=_spawn,
        sleep=lambda seconds: time.sleep(min(seconds, 0.02)),
    )
    thread = threading.Thread(target=supervisor.run)
    thread.start()
    _wait_for(tmp_path / "app-1.pid")
    first_child = int((tmp_path / "child-1.pid").read_text())
    first_app = int((tmp_path / "app-1.pid").read_text())

    os.kill(first_app, signal.SIGKILL)
    _wait_for(tmp_path / "app-2.pid")
    _wait_for(tmp_path / "router-2.pid")
    supervisor.request_shutdown()
    thread.join(timeout=10)

    assert not thread.is_alive()
    deadline = time.monotonic() + 3
    while Path(f"/proc/{first_child}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{first_child}").exists()


def test_sigterm_drains_every_fixed_role_before_supervisor_exits(tmp_path: Path) -> None:
    helper = tmp_path / "drain.py"
    helper.write_text(
        "import pathlib, signal, sys, time\n"
        "root = pathlib.Path(sys.argv[1]); role = sys.argv[2]\n"
        "(root / f'{role}.ready').touch()\n"
        "def stop(*_):\n"
        " (root / f'{role}.drained').touch(); raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "while True: time.sleep(0.02)\n",
        encoding="utf-8",
    )
    specs = tuple(
        ProcessSpec(
            role=role,
            command=(sys.executable, str(helper), str(tmp_path), role),
            uid=os.getuid(),
            gid=os.getgid(),
            port=None,
            environment={"PATH": os.environ["PATH"]},
        )
        for role in ("router", "app", "worker-0")
    )
    settings = CompactRuntimeSettings.from_environment(
        {
            "FS_DEPLOYMENT_PROFILE": "compact",
            "FS_COMPACT_SHUTDOWN_GRACE_SECONDS": "4",
            "FS_COMPACT_KILL_RESERVE_SECONDS": "1",
        }
    )
    result: list[int] = []
    supervisor = CompactSupervisor(specs=specs, settings=settings, process_factory=_spawn)
    thread = threading.Thread(target=lambda: result.append(supervisor.run()))
    thread.start()
    for role in ("router", "app", "worker-0"):
        _wait_for(tmp_path / f"{role}.ready")

    supervisor.request_shutdown(signal.SIGTERM)
    thread.join(timeout=10)

    assert result == [0]
    assert all((tmp_path / f"{role}.drained").exists() for role in ("router", "app", "worker-0"))


def test_real_crash_loop_exhausts_bounded_restart_budget(tmp_path: Path) -> None:
    attempts = tmp_path / "attempts"
    helper = tmp_path / "crash.py"
    helper.write_text(
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1]); path.mkdir(exist_ok=True)\n"
        "(path / str(len(list(path.iterdir())))).touch()\n"
        "raise SystemExit(23)\n",
        encoding="utf-8",
    )
    spec = ProcessSpec(
        role="app",
        command=(sys.executable, str(helper), str(attempts)),
        uid=os.getuid(),
        gid=os.getgid(),
        port=None,
        environment={"PATH": os.environ["PATH"]},
    )
    settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})
    supervisor = CompactSupervisor(
        specs=(spec,),
        settings=settings,
        process_factory=_spawn,
        sleep=lambda seconds: time.sleep(min(seconds, 0.02)),
    )

    assert supervisor.run() == 1
    assert len(list(attempts.iterdir())) == 4


def test_partial_real_pool_creation_terminates_started_role(tmp_path: Path) -> None:
    marker = tmp_path / "terminated"
    ready = tmp_path / "partial.ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import pathlib,signal,sys,time; "
                "signal.signal(signal.SIGTERM, lambda *_: "
                "(pathlib.Path(sys.argv[1]).touch(), sys.exit(0))); "
                "pathlib.Path(sys.argv[2]).touch(); "
                "time.sleep(60)"
            ),
            str(marker),
            str(ready),
        ],
        start_new_session=True,
    )
    _wait_for(ready)
    specs = tuple(
        ProcessSpec(
            role=role,
            command=("unused",),
            uid=os.getuid(),
            gid=os.getgid(),
            port=None,
            environment={},
        )
        for role in ("worker-0", "worker-1")
    )
    calls = 0

    def factory(_spec: ProcessSpec):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected pool creation failure")
        return process

    supervisor = CompactSupervisor(
        specs=specs,
        settings=CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"}),
        process_factory=factory,
    )

    assert supervisor.run() == 78
    assert process.wait(timeout=5) == 0
    assert marker.exists()


def test_dependency_loss_changes_readiness_without_restarting_optional_metrics() -> None:
    registry = ReadinessRegistry(worker_processes=1)
    registry.set_bootstrap_ready(True)
    registry.set_schema_ready(True)
    for role in ("router", "app", "installer", "mcp", "control"):
        registry.set_role_ready(role, True)
    registry.record_worker(
        role="worker-0", worker_incarnation="worker", state="registered", sequence=1
    )
    stop = threading.Event()
    outcomes = iter((True, True, False, True))

    class Connection:
        """A dependency that is up: it answers its own protocol, not merely TCP (P2)."""

        def __init__(self, address) -> None:
            self._reply = (
                b"R\x00\x00\x00\x08\x00\x00\x00\x00" if address[1] == 5432 else b"\x01\x00\x00"
            )

        def settimeout(self, _timeout) -> None:
            return None

        def sendall(self, _data) -> None:
            return None

        def recv(self, size):
            chunk, self._reply = self._reply[:size], self._reply[size:]
            return chunk

        def close(self) -> None:
            return None

    def connect(address, timeout):
        del timeout
        if not next(outcomes):
            raise OSError("injected dependency outage")
        return Connection(address)

    checks = 0

    def wait(_seconds: float) -> bool:
        nonlocal checks
        checks += 1
        if checks == 2:
            stop.set()
        return stop.is_set()

    stop.wait = wait  # type: ignore[method-assign]
    monitor_dependencies(
        registry,
        postgres=("postgres", 5432),
        rabbitmq=RabbitMQProbeTarget(("rabbit", 5672)),
        stop=stop,
        interval_seconds=0.01,
        connect=connect,
    )

    snapshot = registry.snapshot()
    assert snapshot.dependencies == {"postgres": False, "rabbitmq": True}
    assert snapshot.ready is False
    assert snapshot.roles["control"] is True
