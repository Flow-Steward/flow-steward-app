"""P1-1: after a crash, a worker's grandchildren survived the cleanup.

`_terminate_adopted_descendants` took one snapshot of the processes the supervisor had
adopted. In a chain role -> A -> B, the role's death hands A to the subreaper, the
snapshot sees A, A is killed -- and only then is B handed over, after the snapshot was
taken. B was never killed, and a new worker group started next to it: a surviving
worker grandchild can repeat an external effect the new group then repeats too.

Exercised with real processes under a real child subreaper, which needs Linux. It runs
in a subprocess because enabling the subreaper changes the calling process for good.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_LINUX_ONLY = pytest.mark.skipif(
    not Path("/proc/self/stat").is_file(), reason="needs Linux /proc and a child subreaper"
)

_SCENARIO = textwrap.dedent(
    """
    import json, os, pathlib, signal, subprocess, sys, time
    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.runtime.compact.supervisor import CompactSupervisor

    work = pathlib.Path(sys.argv[1])
    depth = int(sys.argv[2])
    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_COMPACT_KILL_RESERVE_SECONDS": "5"}
    )
    supervisor = CompactSupervisor(specs=(), settings=settings)
    if not supervisor._subreaper_enabled:
        print(json.dumps({"skip": "no subreaper"})); sys.exit(0)

    # role -> A -> B (-> C ...): each level starts the next and then waits on it. One
    # script per level, so no quoting has to nest.
    for level in range(1, depth + 1):
        start = "sleep 300" if level == depth else f"sh {work}/level-{level + 1}.sh"
        (work / f"level-{level}.sh").write_text(f"{start} & echo $! > {work}/pid-{level}; wait\\n")
    role = subprocess.Popen(
        ["sh", "-c", f"sh {work}/level-1.sh & echo $! > {work}/pid-0; sleep 0.3"]
    )
    role.wait()  # the role dies; what it started is now the subreaper's
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not all(
        (work / f"pid-{level}").is_file() for level in range(depth + 1)
    ):
        time.sleep(0.05)
    pids = [int((work / f"pid-{level}").read_text()) for level in range(depth + 1)]

    supervisor._terminate_adopted_descendants()

    def alive(pid):
        try:
            state = pathlib.Path(f"/proc/{pid}/stat").read_text().split()[2]
        except OSError:
            return False
        return state != "Z"

    survivors = [pid for pid in pids if alive(pid)]
    for pid in survivors:  # never leave anything behind, whatever the result
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
    print(json.dumps({"pids": pids, "survivors": survivors}))
    """
)


def _run(tmp_path: Path, depth: int) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _SCENARIO, str(tmp_path), str(depth)],
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
def test_the_cleanup_leaves_no_descendant_of_a_dead_role_alive(tmp_path: Path, depth: int) -> None:
    """Depth 1 is the external review's reproduction, role -> A -> B; deeper chains take more passes."""
    payload = _run(tmp_path, depth)

    assert payload["survivors"] == [], payload


def _crashing_supervisor(*, cleanup_complete: bool):
    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.runtime.compact.process_spec import build_process_specs
    from core.infrastructure.runtime.compact.supervisor import CompactSupervisor

    settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})
    specs = build_process_specs(
        settings,
        parent_environment={"PATH": "/usr/bin", "FS_WORKER_MCP_BEARER_TOKEN": "worker-token"},
        public_port=8080,
        observation_fd=7,
    )[:2]
    created: list[str] = []

    class _Process:
        def __init__(self, generation: int) -> None:
            self.pid = generation
            # The second process created -- the first group's app -- has crashed.
            self.returncode = 1 if generation == 2 else None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return int(self.returncode or 0)

    def factory(spec):
        created.append(spec.role)
        return _Process(len(created))

    def signal_process(process, signum):
        del signum
        process.returncode = -15

    def sleep(_seconds):
        if len(created) >= 4:
            supervisor.request_shutdown()

    supervisor = CompactSupervisor(
        specs=specs,
        settings=settings,
        process_factory=factory,
        signal_process=signal_process,
        sleep=sleep,
    )

    def cleanup(*, deadline=None, reap_managed=False):
        del deadline
        if reap_managed:
            supervisor._reap_exited()
        return cleanup_complete

    supervisor._terminate_adopted_descendants = cleanup  # type: ignore[method-assign]
    # These two pin the group path, which a role falls back to when its own tree cannot
    # be cleared alone; the per-role path is in test_compact_role_restart.py.
    supervisor._contain_role_failure = lambda _role: False  # type: ignore[method-assign]
    return supervisor, created


def test_no_new_group_starts_while_the_old_tree_is_still_alive() -> None:
    """The other half of P1-1: a survivor must not get company."""
    supervisor, created = _crashing_supervisor(cleanup_complete=False)

    assert supervisor.run() == 1
    assert created == ["router", "app"]  # the first group only


def test_a_clean_crash_still_restarts_the_group() -> None:
    """The green direction: with nothing left behind, recovery is unchanged."""
    supervisor, created = _crashing_supervisor(cleanup_complete=True)

    assert supervisor.run() == 0
    assert created == ["router", "app", "router", "app"]
