"""A local extension runs confined (isolation phase 1).

It ran as a plain child of the service: only that child was killed at timeout,
so anything it started kept running; it had no CPU, memory or file-size limit;
a setuid program could raise its privileges; and it shared the service's
temporary directory. Each test here starts a real process.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.infrastructure.extension_runtime import process_confinement
from core.infrastructure.extension_runtime.extension_runtime_invocation import (
    _run_subprocess_capped,
)

pytestmark = pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups")


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed child of ours that nobody reaped yet is a zombie, not running.
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        return "State:\tZ" not in status.read_text()
    return True


def _wait_gone(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def test_what_the_extension_started_does_not_outlive_it(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "print('{\"ok\": true}')\n"
    )

    returncode, stdout, _stderr, _exceeded = _run_subprocess_capped(
        _python(code), cwd=None, env=dict(os.environ), timeout=20
    )

    assert returncode == 0
    assert json.loads(stdout) == {"ok": True}
    assert _wait_gone(int(pid_file.read_text()))


def test_a_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )

    command = _python(code)
    environment = dict(os.environ)
    with pytest.raises(subprocess.TimeoutExpired):
        _run_subprocess_capped(command, cwd=None, env=environment, timeout=2)

    assert _wait_gone(int(pid_file.read_text()))


def test_each_invocation_has_its_own_scratch_directory_removed_afterwards() -> None:
    code = (
        "import json, os, tempfile\n"
        "path = os.path.join(tempfile.gettempdir(), 'scratch.txt')\n"
        "open(path, 'w').write('x')\n"
        "print(json.dumps({'tmp': tempfile.gettempdir(), 'env': os.environ.get('TMPDIR')}))\n"
    )

    first = json.loads(
        _run_subprocess_capped(_python(code), cwd=None, env=dict(os.environ), timeout=20)[1]
    )
    second = json.loads(
        _run_subprocess_capped(_python(code), cwd=None, env=dict(os.environ), timeout=20)[1]
    )

    assert first["tmp"] == first["env"]
    assert Path(first["tmp"]).name.startswith("fs-extension-")
    assert first["tmp"] != second["tmp"]
    assert not Path(first["tmp"]).exists()
    assert not Path(second["tmp"]).exists()


def test_the_extension_runs_under_resource_limits() -> None:
    code = (
        "import json, resource\n"
        "print(json.dumps({\n"
        "  'cpu': resource.getrlimit(resource.RLIMIT_CPU)[0],\n"
        "  'fsize': resource.getrlimit(resource.RLIMIT_FSIZE)[0],\n"
        "  'core': resource.getrlimit(resource.RLIMIT_CORE)[0],\n"
        "  'as': resource.getrlimit(resource.RLIMIT_AS)[0],\n"
        "}))\n"
    )

    limits = json.loads(
        _run_subprocess_capped(_python(code), cwd=None, env=dict(os.environ), timeout=40)[1]
    )

    assert limits["cpu"] == process_confinement.cpu_seconds_for(40) == 80
    assert limits["fsize"] == process_confinement.FILE_SIZE_LIMIT_BYTES
    assert limits["core"] == 0
    if sys.platform.startswith("linux"):
        assert limits["as"] == process_confinement.MEMORY_LIMIT_BYTES


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux-only")
def test_the_extension_cannot_gain_privileges() -> None:
    code = "print(open('/proc/self/status').read())"

    status = _run_subprocess_capped(_python(code), cwd=None, env=dict(os.environ), timeout=20)[1]

    assert "NoNewPrivs:\t1" in status


def test_a_file_larger_than_the_limit_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Keep the artificial cap small while allowing the collector's SQLite pages.
    monkeypatch.setattr(process_confinement, "FILE_SIZE_LIMIT_BYTES", 1024 * 1024)
    code = (
        "import os, signal, tempfile\n"
        "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
        "path = os.path.join(tempfile.gettempdir(), 'big')\n"
        "try:\n"
        "    with open(path, 'wb', buffering=0) as handle:\n"
        "        for _ in range((2 * 1024 * 1024 // 512) + 1):\n"
        "            handle.write(b'x' * 512)\n"
        "    print('wrote')\n"
        "except OSError:\n"
        "    print('refused')\n"
    )
    coverage_base = tmp_path / "confined-child-coverage"
    repository_root = Path(__file__).resolve().parents[2]
    # Exercise the same subprocess collector that runs in the full gate, with
    # private output so this fixture can verify the data before its temp cleanup.
    environment = {
        **os.environ,
        "COV_CORE_SOURCE": str(repository_root / "core"),
        "COV_CORE_CONFIG": str(repository_root / "pyproject.toml"),
        "COV_CORE_DATAFILE": str(coverage_base),
        "COV_CORE_BRANCH": "enabled",
    }
    returncode, stdout, stderr, _exceeded = _run_subprocess_capped(
        _python(code), cwd=None, env=environment, timeout=20
    )

    assert returncode == 0, stderr
    assert stdout.strip() == "refused"
    coverage_files = sorted(tmp_path.glob(f"{coverage_base.name}.*"))
    assert coverage_files, "The confined subprocess must retain its coverage data"
    for coverage_file in coverage_files:
        connection = sqlite3.connect(coverage_file.as_uri() + "?mode=ro", uri=True)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        finally:
            connection.close()


def test_the_minimum_cpu_budget_covers_short_timeouts() -> None:
    assert process_confinement.cpu_seconds_for(1) == process_confinement.CPU_SECONDS_FLOOR


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="the run marker is found through /proc")
def test_a_descendant_that_left_the_process_group_is_ended_too(tmp_path: Path) -> None:
    """start_new_session puts a child in a group of its own; the run marker still finds it."""
    pid_file = tmp_path / "detached.pid"
    code = (
        "import subprocess, sys\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True\n"
        ")\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "print('{\"ok\": true}')\n"
    )

    returncode, _stdout, _stderr, _exceeded = _run_subprocess_capped(
        _python(code), cwd=None, env=dict(os.environ), timeout=20
    )

    assert returncode == 0
    assert _wait_gone(int(pid_file.read_text()))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="fails closed on Linux")
def test_a_protection_that_cannot_be_applied_stops_the_run() -> None:
    command = process_confinement.confined_command(
        [sys.executable, "-c", "print('ran unconfined')"], timeout_seconds=10
    )
    limits = json.loads(command[4])
    limits["rlimits"]["RLIMIT_NOT_A_LIMIT"] = 1
    command[4] = json.dumps(limits)

    completed = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)

    assert completed.returncode == process_confinement.CONFINEMENT_FAILED_EXIT
    assert "ran unconfined" not in completed.stdout
    assert "extension confinement could not be applied: RLIMIT_NOT_A_LIMIT" in completed.stderr


def test_a_limit_is_never_raised_above_the_service_hard_limit() -> None:
    import resource

    code = "import resource; print(resource.getrlimit(resource.RLIMIT_CPU)[1])"

    stdout = _run_subprocess_capped(_python(code), cwd=None, env=dict(os.environ), timeout=20)[1]

    cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)[1]
    expected = process_confinement.cpu_seconds_for(20)
    if cpu_hard != resource.RLIM_INFINITY:
        expected = min(expected, cpu_hard)
    assert int(stdout.strip()) == expected
