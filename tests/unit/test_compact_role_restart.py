"""Review P2: one crashed Compact role restarts alone, not the whole runtime.

The auditor saw it on a live Compact: kill -9 of one worker restarted app and MCP with
it, because any role's exit shut the whole group down and started it again.

What must not be lost doing it per role is P1-1's guarantee -- no new instance starts
next to the old one's tree -- so the dead role's own session is cleared first, and the
supervisor falls back to the group whenever that cannot be proven.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
from core.infrastructure.runtime.compact import supervisor as supervisor_module
from core.infrastructure.runtime.compact.process_spec import build_process_specs
from core.infrastructure.runtime.compact.supervisor import CompactSupervisor

ROOT = Path(__file__).resolve().parents[2]
_LINUX_ONLY = pytest.mark.skipif(
    not Path("/proc/self/stat").is_file(), reason="needs Linux /proc and a child subreaper"
)

_SCENARIO = textwrap.dedent(
    """
    import json, os, pathlib, signal, subprocess, sys, time
    from types import SimpleNamespace
    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.runtime.compact.supervisor import CompactSupervisor

    work = pathlib.Path(sys.argv[1]); depth = int(sys.argv[2]); escape = sys.argv[3] == "1"
    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_COMPACT_KILL_RESERVE_SECONDS": "5"}
    )
    supervisor = CompactSupervisor(specs=(), settings=settings)
    if not supervisor._subreaper_enabled:
        print(json.dumps({"skip": "no subreaper"})); sys.exit(0)

    for level in range(1, depth + 1):
        start = "sleep 300" if level == depth else f"sh {work}/level-{level + 1}.sh"
        (work / f"level-{level}.sh").write_text(f"{start} & echo $! > {work}/pid-{level}; wait\\n")
    escape_cmd = (
        f"{sys.executable} -c 'import os,time; os.setsid(); "
        f"open(\\"{work}/escaped\\",\\"w\\").write(str(os.getpid())); time.sleep(300)' & "
        if escape else ""
    )
    dead = subprocess.Popen(
        ["sh", "-c", f"{escape_cmd}sh {work}/level-1.sh & echo $! > {work}/pid-0; sleep 0.5"],
        start_new_session=True,
    )
    live = subprocess.Popen(
        ["sh", "-c", f"sleep 300 & echo $! > {work}/live-child; wait"], start_new_session=True
    )
    deadline = time.monotonic() + 5
    wanted = [work / f"pid-{level}" for level in range(depth + 1)] + [work / "live-child"]
    if escape:
        wanted.append(work / "escaped")
    while time.monotonic() < deadline and not all(path.is_file() for path in wanted):
        time.sleep(0.05)
    dead.wait()  # the role dies; its tree is now the subreaper's
    pids = [int((work / f"pid-{level}").read_text()) for level in range(depth + 1)]
    live_child = int((work / "live-child").read_text())
    escaped = int((work / "escaped").read_text()) if escape else None

    spec = SimpleNamespace(role="dead")
    supervisor._children = {"dead": (spec, dead), "live": (SimpleNamespace(role="live"), live)}
    contained = supervisor._contain_role_failure("dead")

    def alive(pid):
        try:
            return pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False

    result = {
        "contained": contained,
        "survivors": [pid for pid in pids if alive(pid)],
        "live_alive": live.poll() is None and alive(live_child),
        "escaped_alive": escaped is not None and alive(escaped),
    }
    for pid in [*pids, live_child, live.pid] + ([escaped] if escaped else []):
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
    print(json.dumps(result))
    """
)


def _run(tmp_path: Path, depth: int, *, escape: bool) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _SCENARIO, str(tmp_path), str(depth), "1" if escape else "0"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    if "skip" in payload:
        pytest.skip(payload["skip"])
    return payload


@_LINUX_ONLY
@pytest.mark.parametrize("depth", (1, 2, 3))
def test_a_dead_roles_tree_is_cleared_and_a_live_role_is_left_alone(
    tmp_path: Path, depth: int
) -> None:
    payload = _run(tmp_path, depth, escape=False)

    assert payload["contained"] is True, payload
    assert payload["survivors"] == [], payload
    assert payload["live_alive"] is True, payload


@_LINUX_ONLY
def test_a_descendant_that_left_the_session_sends_the_supervisor_back_to_the_group(
    tmp_path: Path,
) -> None:
    payload = _run(tmp_path, 1, escape=True)

    assert payload["contained"] is False, payload
    assert payload["survivors"] == [], payload  # what it could attribute is still gone
    assert payload["live_alive"] is True, payload


def _supervisor(monkeypatch: pytest.MonkeyPatch, *, crash_every_app: bool = False):
    """Router and app with fake processes; the first app dies. No /proc involved."""
    monkeypatch.setattr(supervisor_module, "_enable_child_subreaper", lambda: True)
    monkeypatch.setattr(supervisor_module, "_process_table", dict)
    monkeypatch.setattr(supervisor_module, "_linux_child_pids", lambda _pid: set())
    settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})
    specs = build_process_specs(
        settings,
        parent_environment={"PATH": "/usr/bin", "FS_WORKER_MCP_BEARER_TOKEN": "worker-token"},
        public_port=8080,
        observation_fd=7,
    )[:2]
    created: list[tuple[int, str]] = []
    signalled: list[str] = []
    reset: list[str] = []

    class _Process:
        def __init__(self, role: str, pid: int) -> None:
            self.role = role
            self.pid = pid
            self.returncode = 1 if role == "app" and (crash_every_app or pid == 2) else None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return int(self.returncode or 0)

    def factory(spec):
        created.append((len(created) + 1, spec.role))
        return _Process(spec.role, len(created))

    def signal_process(process, signum):
        del signum
        signalled.append(process.role)
        process.returncode = -15

    def sleep(_seconds):
        if len(created) >= 3 and not crash_every_app:
            supervisor.request_shutdown()

    supervisor = CompactSupervisor(
        specs=specs,
        settings=settings,
        process_factory=factory,
        signal_process=signal_process,
        sleep=sleep,
        role_reset=reset.append,
    )
    return supervisor, created, signalled, reset


def test_only_the_crashed_role_is_started_again(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, created, signalled, reset = _supervisor(monkeypatch)

    assert supervisor.run() == 0

    assert created == [(1, "router"), (2, "app"), (3, "app")]
    assert reset == ["app"]
    # The router was never stopped for the app's crash -- only by the final shutdown.
    assert signalled.count("router") == 1


def test_a_role_that_keeps_failing_still_ends_the_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """As before: the container exits and its restart policy decides."""
    supervisor, created, signalled, _reset = _supervisor(monkeypatch, crash_every_app=True)

    assert supervisor.run() == 1

    assert [role for _generation, role in created].count("app") == 4
    assert "router" in signalled  # the rest was shut down, not left running alone


def test_a_failure_that_cannot_be_contained_restarts_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, created, _signalled, _reset = _supervisor(monkeypatch)
    supervisor._contain_role_failure = lambda _role: False  # type: ignore[method-assign]
    original_sleep = supervisor._sleep

    def sleep(seconds):
        if len(created) >= 4:
            supervisor.request_shutdown()
        original_sleep(seconds)

    supervisor._sleep = sleep  # type: ignore[method-assign]

    assert supervisor.run() == 0
    assert created == [(1, "router"), (2, "app"), (3, "router"), (4, "app")]


def test_without_a_subreaper_there_is_no_containment(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, _created, _signalled, _reset = _supervisor(monkeypatch)
    supervisor._subreaper_enabled = False
    supervisor.start()

    assert supervisor._contain_role_failure("app") is False


def test_no_new_instance_starts_while_the_dead_roles_tree_is_still_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-1, per role: a member that will not die keeps the role from being restarted."""
    supervisor, created, _signalled, _reset = _supervisor(monkeypatch)
    supervisor.start()
    dead = supervisor._children["app"][1]
    me = os.getpid()
    # A descendant in the dead role's session that survives every kill.
    monkeypatch.setattr(supervisor_module, "_process_table", lambda: {4242: (me, dead.pid)})
    monkeypatch.setattr(supervisor_module.os, "kill", lambda _pid, _signum: None)
    clock = iter(range(0, 10_000, 1))
    supervisor._monotonic = lambda: float(next(clock))  # type: ignore[method-assign]

    assert supervisor._contain_role_failure("app") is False
    assert created == [(1, "router"), (2, "app")]  # nothing new was started
