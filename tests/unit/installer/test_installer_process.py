from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from core.infrastructure.installer.process import SubprocessExecutor

ROOT = Path(__file__).resolve().parents[3]


def _ready_process_tree_command(pid_path: Path) -> str:
    # Path existence is the readiness signal, so publish only a complete payload.
    temporary_path = pid_path.with_name(f".{pid_path.name}.tmp")
    grandchild = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGINT, signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "os.write(int(sys.argv[1]), b'ready'); "
        "time.sleep(300)"
    )
    return (
        "import os, signal, subprocess, sys, time; from pathlib import Path; "
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(0)); "
        "read_fd, write_fd = os.pipe(); "
        f"grandchild = subprocess.Popen([sys.executable, '-c', {grandchild!r}, "
        "str(write_fd)], pass_fds=(write_fd,)); "
        "os.close(write_fd); "
        "assert os.read(read_fd, 5) == b'ready'; "
        "os.close(read_fd); "
        f"Path({str(temporary_path)!r}).write_text("
        f"f'{{os.getpid()}} {{grandchild.pid}}', encoding='utf-8'); "
        f"os.replace({str(temporary_path)!r}, {str(pid_path)!r}); "
        "time.sleep(300)"
    )


@pytest.mark.parametrize(
    "proc_fault", ["read-denied", "iteration-denied", "malformed", "bad-group"]
)
def test_timeout_cleanup_survives_unknown_proc_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proc_fault: str
) -> None:
    pid_path = tmp_path / "visibility-process-tree.txt"
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
    )
    unreadable_stat = Path(f"/proc/{unrelated.pid}/stat")
    original_read_text = Path.read_text
    original_iterdir = Path.iterdir

    def read_text(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == unreadable_stat:
            if proc_fault == "read-denied":
                raise PermissionError("unrelated process stat is private")
            if proc_fault == "malformed":
                return "unavailable"
            if proc_fault == "bad-group":
                return f"{unrelated.pid} (python) S 1 unavailable"
        return original_read_text(path, encoding=encoding, errors=errors)

    def iterdir(path: Path) -> Iterator[Path]:
        if path == Path("/proc"):
            if proc_fault == "iteration-denied":
                raise PermissionError("process inventory is private")
            return iter((unreadable_stat.parent, *original_iterdir(path)))
        return original_iterdir(path)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    try:
        executor = SubprocessExecutor()
        command = [sys.executable, "-c", _ready_process_tree_command(pid_path)]
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="Process exceeded"):
            executor.run(command, timeout_seconds=2)
        assert time.monotonic() - started < 18
        pids = tuple(map(int, pid_path.read_text(encoding="utf-8").split()))
        assert len(pids) == 2
        for pid in pids:
            _assert_process_stopped(pid)
        assert unrelated.poll() is None
    finally:
        if pid_path.is_file():
            pids = tuple(map(int, pid_path.read_text(encoding="utf-8").split()))
            if pids:
                with suppress(ProcessLookupError):
                    os.killpg(pids[0], signal.SIGKILL)
        unrelated.kill()
        unrelated.wait(timeout=5)


@pytest.mark.parametrize("separate_stderr", [False, True])
@pytest.mark.parametrize("interruption", ["timeout", "sigint"])
def test_interrupt_stops_signal_ignoring_descendant_after_leader_exits(
    tmp_path: Path, interruption: str, separate_stderr: bool
) -> None:
    pid_path = tmp_path / "ready-process-tree.txt"
    child = _ready_process_tree_command(pid_path)
    driver = (
        "import sys; "
        "from core.infrastructure.installer.process import SubprocessExecutor; "
        f"\ntry: SubprocessExecutor().run(sys.argv[1:], separate_stderr={separate_stderr!r}, "
        f"timeout_seconds={2.0 if interruption == 'timeout' else None!r})"
        "\nexcept TimeoutError: raise SystemExit(11)"
        "\nexcept KeyboardInterrupt: raise SystemExit(10)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", driver, sys.executable, "-c", child], cwd=ROOT
    )
    pids: tuple[int, ...] = ()
    try:
        deadline = time.monotonic() + 10
        while not pid_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_path.is_file(), "grandchild did not confirm its signal handlers were ready"
        pids = tuple(map(int, pid_path.read_text(encoding="utf-8").split()))
        assert len(pids) == 2
        if interruption == "sigint":
            process.send_signal(signal.SIGINT)
        assert process.wait(timeout=15) == (11 if interruption == "timeout" else 10)
        for pid in pids:
            _assert_process_stopped(pid)
    finally:
        if pids:
            with suppress(ProcessLookupError):
                os.killpg(pids[0], signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def _assert_process_stopped(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return

    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        # A Docker test container does not necessarily run an init process that
        # promptly reaps an orphaned grandchild. A zombie has terminated and
        # cannot continue the interrupted installer operation.
        assert proc_stat.read_text(encoding="utf-8").split()[2] == "Z"
        return
    pytest.fail(f"process {pid} is still running")


def test_sigint_stops_the_executor_process_group_within_bound(tmp_path: Path) -> None:
    pid_path = tmp_path / "process-tree.txt"
    child = (
        "import os, signal, subprocess, sys, time; "
        "signal.signal(signal.SIGINT, signal.SIG_IGN); "
        "grandchild=subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(300)']); "
        f"open({str(pid_path)!r}, 'w').write(f'{{os.getpid()}} {{grandchild.pid}}'); "
        "time.sleep(300)"
    )
    driver = (
        "import sys; "
        "from core.infrastructure.installer.process import SubprocessExecutor; "
        "command=sys.argv[1:]; "
        "\ntry: SubprocessExecutor().run(command)"
        "\nexcept KeyboardInterrupt: raise SystemExit(10)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", driver, sys.executable, "-c", child],
        cwd=ROOT,
    )
    deadline = time.monotonic() + 10
    while not pid_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_path.is_file()

    started = time.monotonic()
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=12) == 10
    assert time.monotonic() - started < 12

    for pid in map(int, pid_path.read_text(encoding="utf-8").split()):
        _assert_process_stopped(pid)


def test_timeout_stops_the_executor_process_group_without_orphans(tmp_path: Path) -> None:
    pid_path = tmp_path / "timeout-process-tree.txt"
    child = (
        "import os, signal, subprocess, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "grandchild=subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)']); "
        f"open({str(pid_path)!r}, 'w').write(f'{{os.getpid()}} {{grandchild.pid}}'); "
        "time.sleep(300)"
    )

    _raises_callable_80_1 = SubprocessExecutor().run
    with pytest.raises(TimeoutError, match="Process exceeded"):
        _raises_callable_80_1([sys.executable, "-c", child], timeout_seconds=0.5)

    assert pid_path.is_file()
    pids = tuple(map(int, pid_path.read_text(encoding="utf-8").split()))
    try:
        for pid in pids:
            _assert_process_stopped(pid)
    finally:
        for pid in pids:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_timeout_is_enforced_when_child_flushes_partial_line_without_newline() -> None:
    started = time.monotonic()

    _raises_callable_100_1 = SubprocessExecutor().run
    with pytest.raises(TimeoutError, match="Process exceeded"):
        _raises_callable_100_1(
            [
                sys.executable,
                "-c",
                "import sys,time; sys.stdout.write('x'); sys.stdout.flush(); time.sleep(300)",
            ],
            timeout_seconds=0.2,
        )

    assert time.monotonic() - started < 2.0


def test_default_process_execution_keeps_stderr_merged_into_streamed_stdout() -> None:
    streamed: list[str] = []
    outcome = SubprocessExecutor().run(
        [
            sys.executable,
            "-c",
            "import sys; print('stdout-line'); print('stderr-line', file=sys.stderr)",
        ],
        output_sink=streamed.append,
    )

    assert "stdout-line\n" in outcome.stdout
    assert "stderr-line\n" in outcome.stdout
    assert outcome.stderr == ""
    assert set(streamed) == {"stdout-line", "stderr-line"}


def test_separate_stderr_drains_simultaneous_streams_without_merging() -> None:
    outcome = SubprocessExecutor().run(
        [
            sys.executable,
            "-c",
            (
                "import sys, threading; "
                "stdout_text = 'o' * 131072 + '\\n'; "
                "stderr_text = ' ' * 131072 + '9\\n'; "
                "threads = ("
                "threading.Thread(target=sys.stdout.write, args=(stdout_text,)), "
                "threading.Thread(target=sys.stderr.write, args=(stderr_text,))); "
                "[thread.start() for thread in threads]; "
                "[thread.join() for thread in threads]"
            ),
        ],
        timeout_seconds=10,
        separate_stderr=True,
    )

    assert outcome.returncode == 0
    assert outcome.stdout == "o" * 131072 + "\n"
    assert outcome.stderr == " " * 131072 + "9\n"
