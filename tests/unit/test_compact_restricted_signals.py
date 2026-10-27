"""Restricted root supervisors must still stop isolated Compact role UIDs."""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from core.infrastructure.runtime.compact import supervisor


def _denied(*_args: object) -> None:
    raise PermissionError(errno.EPERM, "Operation not permitted")


def _drop_to_role_uid() -> None:
    os.setgroups([])
    os.setgid(10005)
    os.setuid(10005)


@pytest.mark.parametrize("signum", (signal.SIGTERM, signal.SIGKILL))
def test_permission_denied_signal_stops_role_without_changing_parent_uid(
    monkeypatch: pytest.MonkeyPatch,
    signum: int,
) -> None:
    child = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        preexec_fn=_drop_to_role_uid if os.geteuid() == 0 else None,
    )
    kill = os.kill
    getresuid = os.getresuid
    before = getresuid()
    try:
        assert child.poll() is None
        monkeypatch.setattr(os, "kill", _denied)
        monkeypatch.setattr(os, "killpg", _denied)
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        supervisor._signal_process(child, signum)

        assert child.wait(timeout=5) == -signum
        assert getresuid() == before
    finally:
        if child.poll() is None:
            kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def test_non_root_permission_failure_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        pid = 123456

    monkeypatch.setattr(os, "kill", _denied)
    monkeypatch.setattr(os, "geteuid", lambda: 10001)

    process = Process()
    with pytest.raises(PermissionError):
        supervisor._signal_process(process, signal.SIGTERM)


def test_disappeared_role_during_permission_fallback_is_harmless(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Process:
        pid = 123456

    monkeypatch.setattr(os, "kill", _denied)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(supervisor, "_PATH_PROC", str(tmp_path))

    supervisor._signal_process(Process(), signal.SIGTERM)


@pytest.mark.parametrize("cleanup", ("role_failure", "adopted", "group"))
def test_restricted_supervisor_clears_orphans_without_touching_live_roles(
    cleanup: str,
) -> None:
    scenario = textwrap.dedent(
        """
        import errno, json, os, signal, subprocess, sys, time
        from pathlib import Path
        from types import SimpleNamespace
        from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
        from core.infrastructure.runtime.compact.supervisor import CompactSupervisor, _signal_process

        def drop_to_role_uid():
            os.setgroups([])
            os.setgid(10005)
            os.setuid(10005)

        settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})
        owner = CompactSupervisor(specs=(), settings=settings)
        assert owner._subreaper_enabled
        role = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c",
             "import subprocess; p=subprocess.Popen(['sleep','30']); print(p.pid, flush=True)"],
            start_new_session=True,
            stdout=subprocess.PIPE,
            text=True,
            preexec_fn=drop_to_role_uid if os.geteuid() == 0 else None,
        )
        orphan = int(role.stdout.readline())
        role.stdout.close()
        role.wait(timeout=5)
        live = subprocess.Popen(["sleep", "30"], start_new_session=True)
        original_kill = os.kill
        before = os.getresuid()

        def denied(*_args):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        os.kill = denied
        os.killpg = denied
        os.geteuid = lambda: 0
        try:
            if sys.argv[1] == "role_failure":
                owner._children = {
                    "dead": (SimpleNamespace(role="dead"), role),
                    "live": (SimpleNamespace(role="live"), live),
                }
                assert owner._contain_role_failure("dead")
            elif sys.argv[1] == "adopted":
                owner._children = {"live": (SimpleNamespace(role="live"), live)}
                assert owner._terminate_adopted_descendants()
            else:
                _signal_process(role, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{orphan}/stat").read_text().rsplit(")", 1)[1].split()[0]
                    if state == "Z":
                        break
                except FileNotFoundError:
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("orphan survived")
            print(json.dumps({
                "parent_uid_unchanged": os.getresuid() == before,
                "unrelated_role_alive": live.poll() is None,
            }))
        finally:
            for pid in (orphan, live.pid):
                try:
                    original_kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            live.wait(timeout=5)
            try:
                os.waitpid(orphan, 0)
            except ChildProcessError:
                pass
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", scenario, cleanup],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "parent_uid_unchanged": True,
        "unrelated_role_alive": True,
    }
