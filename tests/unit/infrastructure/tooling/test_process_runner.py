"""Regression tests for safe typed tooling execution."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.infrastructure.tooling import process_capture, process_runner
from core.infrastructure.tooling.process_runner import OSProcessRunner

_RUNNER = OSProcessRunner(working_directory=Path(__file__).parents[4])
run_callable = _RUNNER.run_callable
run_process = _RUNNER.run_process


def test_run_process_does_not_inherit_ambient_secret_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_key = "MCP_" + "BEARER_TOKEN"
    monkeypatch.setenv(env_key, "bearer-secret")

    result = run_process(
        operation="probe.env",
        command=[
            sys.executable,
            "-c",
            f"import os; print(os.environ.get({env_key!r}) or 'missing')",
        ],
        timeout_seconds=10,
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "missing"


def test_run_process_rejects_explicit_sensitive_environment() -> None:
    with pytest.raises(ValueError, match="MCP_BEARER_TOKEN"):
        run_process(
            operation="probe.env",
            command=[sys.executable, "-c", "print('never')"],
            env={"MCP_BEARER_TOKEN": "secret"},
            timeout_seconds=10,
        )


def test_run_callable_scrubs_ambient_secrets_from_nested_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    env_key = "MCP_" + "BEARER_TOKEN"
    monkeypatch.setenv(env_key, "bearer-secret")

    def target() -> int:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import os; print(os.environ.get({env_key!r}) or 'missing')",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        print(completed.stdout, end="")
        return int(completed.returncode)

    result = run_callable(operation="probe.callable-env", target=target, timeout_seconds=10)

    assert result.exit_code == 0
    assert result.stdout.strip() == "missing"


def test_run_callable_preserves_playwright_browser_runtime_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright")

    def target() -> int:
        print(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "missing")
        return 0

    result = run_callable(
        operation="probe.playwright-browser-path",
        target=target,
        timeout_seconds=10,
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "/ms-playwright"


def test_run_callable_scrubs_explicit_nested_subprocess_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import subprocess

    env_key = "MCP_" + "BEARER_TOKEN"
    monkeypatch.setenv(env_key, "bearer-secret")

    def target() -> int:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import os; print(os.environ.get({env_key!r}) or 'missing')",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        print(completed.stdout, end="")
        return int(completed.returncode)

    result = run_callable(
        operation="probe.callable-explicit-env", target=target, timeout_seconds=10
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "missing"


def test_run_callable_keeps_popen_class_subscriptable_for_lazy_imports() -> None:
    import subprocess

    def target() -> int:
        _ = subprocess.Popen[bytes]
        return 0

    result = run_callable(
        operation="probe.callable-popen-type",
        target=target,
        timeout_seconds=10,
    )

    assert result.exit_code == 0, result.stderr


def test_run_callable_enforces_timeout() -> None:
    started = time.monotonic()

    def target() -> int:
        time.sleep(0.25)
        return 0

    result = run_callable(
        operation="probe.timeout",
        target=target,
        timeout_seconds=0.01,
    )

    assert time.monotonic() - started < 0.20
    assert result.status == "timeout"
    assert result.exit_code == 124


def test_run_callable_timeout_preserves_output_emitted_before_termination() -> None:
    def target() -> int:
        print("before-timeout", end="")
        print("before-timeout-error", end="", file=sys.stderr)
        time.sleep(1)
        return 0

    result = run_callable(
        operation="probe.timeout-output",
        target=target,
        timeout_seconds=0.05,
    )

    assert result.status == "timeout"
    assert result.stdout.strip() == "before-timeout"
    assert result.stdout_full.strip() == "before-timeout"
    assert "before-timeout-error" in result.stderr
    assert "before-timeout-error" in result.stderr_full


def test_run_callable_preserves_string_system_exit_diagnostic() -> None:
    def target() -> int:
        raise SystemExit("[sonar-export] Failed to reach SonarQube: connection refused")

    result = run_callable(
        operation="quality.sonar-export-issues",
        target=target,
        timeout_seconds=10,
    )

    assert result.status == "failed"
    assert result.exit_code == 1
    assert "Failed to reach SonarQube: connection refused" in result.stderr
    assert "Failed to reach SonarQube: connection refused" in result.stderr_full


def test_run_callable_captures_descriptor_and_nested_process_output() -> None:
    def target() -> int:
        import os
        import subprocess

        os.write(1, b"descriptor-stdout\n")
        os.write(2, b"descriptor-stderr\n")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                ("import os; os.write(1, b'nested-stdout\\n'); os.write(2, b'nested-stderr\\n')"),
            ],
            check=False,
        )
        return int(completed.returncode)

    result = run_callable(
        operation="probe.callable-descriptors",
        target=target,
        timeout_seconds=10,
    )

    assert result.exit_code == 0
    assert result.stdout_full == "descriptor-stdout\nnested-stdout\n"
    assert result.stderr_full == "descriptor-stderr\nnested-stderr\n"


def test_run_callable_bounds_output_while_draining_both_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_limit = 1_024
    monkeypatch.setattr(process_capture, "_MAX_CAPTURE_STORAGE_CHARS", capture_limit)

    def target() -> int:
        import os

        stdout_chunk = b"stdout:" + (b"o" * 4_096)
        stderr_chunk = b"stderr:" + (b"e" * 4_096)
        for _ in range(64):
            os.write(1, stdout_chunk)
            os.write(2, stderr_chunk)
        return 7

    result = run_callable(
        operation="probe.callable-bounded",
        target=target,
        timeout_seconds=10,
    )

    assert result.status == "failed"
    assert result.exit_code == 7
    assert len(result.stdout_full) == capture_limit
    assert len(result.stderr_full) == capture_limit
    assert result.stdout_full.startswith("stdout:")
    assert result.stderr_full.startswith("stderr:")


def test_run_callable_timeout_keeps_draining_after_capture_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_limit = 2_048
    monkeypatch.setattr(process_capture, "_MAX_CAPTURE_STORAGE_CHARS", capture_limit)

    def target() -> int:
        import os

        stdout_chunk = b"timeout-out:" + (b"o" * 4_096)
        stderr_chunk = b"timeout-err:" + (b"e" * 4_096)
        while True:
            os.write(1, stdout_chunk)
            os.write(2, stderr_chunk)

    result = run_callable(
        operation="probe.callable-bounded-timeout",
        target=target,
        timeout_seconds=0.10,
    )

    assert result.status == "timeout"
    assert result.exit_code == 124
    assert len(result.stdout_full) == capture_limit
    assert len(result.stderr_full) == capture_limit
    assert result.stdout_full.startswith("timeout-out:")
    assert result.stderr_full.startswith("timeout-err:")
    assert result.stderr_full.endswith("Timed out after 0.1 seconds")


def test_run_callable_cancel_captures_descriptor_output() -> None:
    def target() -> int:
        import os

        os.write(1, b"before-cancel-out\n")
        os.write(2, b"before-cancel-err\n")
        raise KeyboardInterrupt

    result = run_callable(
        operation="probe.callable-descriptor-cancel",
        target=target,
        timeout_seconds=10,
    )

    assert result.status == "cancelled"
    assert result.exit_code == 130
    assert result.stdout_full == "before-cancel-out\n"
    assert result.stderr_full == "before-cancel-err\nOperation interrupted.\n"


def test_run_callable_cancel_preserves_diagnostic_with_saturated_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_limit = 256
    monkeypatch.setattr(process_capture, "_MAX_CAPTURE_STORAGE_CHARS", capture_limit)

    def target() -> int:
        os.write(2, b"cancel-prefix:" + (b"e" * 4_096))
        raise KeyboardInterrupt

    result = run_callable(
        operation="probe.callable-bounded-cancel",
        target=target,
        timeout_seconds=10,
    )

    assert result.status == "cancelled"
    assert result.exit_code == 130
    assert len(result.stderr_full) == capture_limit
    assert result.stderr_full.startswith("cancel-prefix:")
    assert result.stderr_full.endswith("Operation interrupted.\n")


def test_run_callable_error_preserves_diagnostic_with_saturated_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_limit = 256
    monkeypatch.setattr(process_capture, "_MAX_CAPTURE_STORAGE_CHARS", capture_limit)

    def target() -> int:
        os.write(2, b"error-prefix:" + (b"e" * 4_096))
        raise RuntimeError("terminal callable error")

    result = run_callable(
        operation="probe.callable-bounded-error",
        target=target,
        timeout_seconds=10,
    )

    assert result.status == "failed"
    assert result.exit_code == 70
    assert len(result.stderr_full) == capture_limit
    assert result.stderr_full.startswith("error-prefix:")
    assert result.stderr_full.endswith("terminal callable error\n")


def test_callable_pipe_readers_wait_for_eof_after_transient_empty_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_read_fd, stdout_write_fd = os.pipe()
    stderr_read_fd, stderr_write_fd = os.pipe()
    capture = process_capture._StreamingCapture()
    readers = process_runner._CallablePipeReaders(
        stdout_fd=stdout_read_fd,
        stderr_fd=stderr_read_fd,
        capture=capture,
    )
    empty_read_seen = threading.Event()
    original_read = os.read

    def tracking_read(file_descriptor: int, size: int) -> bytes:
        try:
            return original_read(file_descriptor, size)
        except BlockingIOError:
            if file_descriptor == stdout_read_fd:
                empty_read_seen.set()
            raise

    monkeypatch.setattr(process_runner.os, "read", tracking_read)
    readers.start()
    os.close(stderr_write_fd)
    assert empty_read_seen.wait(timeout=1)

    def delayed_write_and_close() -> None:
        time.sleep(0.05)
        os.write(stdout_write_fd, b"arrived-before-eof")
        os.close(stdout_write_fd)

    writer = threading.Thread(target=delayed_write_and_close)
    writer.start()
    readers.finish()
    writer.join(timeout=1)

    assert not writer.is_alive()
    assert capture.stdout == "arrived-before-eof"


def test_run_callable_does_not_read_whole_capture_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_read_text(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("callable output must stream through bounded pipes")

    monkeypatch.setattr(Path, "read_text", reject_read_text)

    result = run_callable(
        operation="probe.callable-no-whole-file-read",
        target=lambda: print("streamed") or 0,
        timeout_seconds=10,
    )

    assert result.exit_code == 0
    assert result.stdout_full == "streamed\n"


def test_run_callable_emits_periodic_running_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    monkeypatch.setattr(process_runner, "_HEARTBEAT_SECONDS", 0.04)

    def target() -> int:
        time.sleep(0.11)
        return 0

    result = run_callable(
        operation="probe.heartbeat",
        target=target,
        timeout_seconds=1,
        progress_sink=events.append,
    )

    assert result.exit_code == 0
    assert any(event.detail.startswith("running for ") for event in events)


def test_run_callable_timeout_stops_late_side_effect(tmp_path: Path) -> None:
    marker = tmp_path / "late-side-effect.txt"

    def target() -> int:
        time.sleep(0.25)
        marker.write_text("late", encoding="utf-8")
        return 0

    result = run_callable(
        operation="probe.timeout-side-effect",
        target=target,
        timeout_seconds=0.01,
    )

    assert result.status == "timeout"
    assert result.exit_code == 124
    time.sleep(0.30)
    assert not marker.exists()


def test_run_process_timeout_stops_grandchild_side_effect(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-side-effect.txt"
    if marker.exists():
        marker.unlink()
    child_code = (
        "import pathlib, time; "
        f"time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )

    result = run_process(
        operation="probe.process-tree-timeout",
        command=[
            sys.executable,
            "-c",
            (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', "
                f"{child_code!r}"
                "]); "
                "time.sleep(5)"
            ),
        ],
        timeout_seconds=0.05,
    )

    assert result.status == "timeout"
    assert result.exit_code == 124
    time.sleep(1.80)
    assert not marker.exists()


def test_terminate_process_tree_signals_captured_group_after_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    class _ExitedLeader:
        pid = 12345

        def is_alive(self) -> bool:
            return False

        def terminate(self) -> None:
            raise AssertionError("exited leader should not be terminated directly")

        def kill(self) -> None:
            raise AssertionError("exited leader should not be killed directly")

        def join(self, timeout: float | None = None) -> None:
            del timeout

    monkeypatch.setattr(process_runner, "_supports_process_groups", lambda: True)
    monkeypatch.setattr(
        process_runner,
        "_signal_process_group_id",
        lambda pgid, signum: calls.append((pgid, signum)) or True,
    )

    process_runner._terminate_process_tree(_ExitedLeader(), process_group_id=777)

    assert calls == [(777, signal.SIGTERM), (777, signal.SIGKILL)]


def test_tool_output_capture_preserves_head_and_tail_when_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_capture, "_MAX_CAPTURE_CHARS", 80)

    captured = process_capture._tail("BEGIN:" + ("x" * 200) + ":END")

    assert captured.startswith("BEGIN:")
    assert "[output truncated" in captured
    assert captured.endswith(":END")


def test_run_callable_timeout_stops_grandchild_side_effect(tmp_path: Path) -> None:
    marker = tmp_path / "callable-grandchild-side-effect.txt"
    if marker.exists():
        marker.unlink()

    def target() -> int:
        import subprocess

        subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import pathlib, time; "
                    "time.sleep(1.5); "
                    f"pathlib.Path({str(marker)!r}).write_text('late', encoding='utf-8')"
                ),
            ]
        )
        time.sleep(5)
        return 0

    result = run_callable(
        operation="probe.callable-tree-timeout",
        target=target,
        timeout_seconds=0.05,
    )

    assert result.status == "timeout"
    assert result.exit_code == 124
    time.sleep(1.80)
    assert not marker.exists()


def test_await_process_group_id_waits_until_child_is_group_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}

    class _StartedProcess:
        pid = 22222

        def is_alive(self) -> bool:
            return True

    def fake_getpgid(pid: int) -> int:
        assert pid == 22222
        calls["count"] += 1
        return 11111 if calls["count"] == 1 else 22222

    monkeypatch.setattr(process_runner, "_supports_process_groups", lambda: True)
    monkeypatch.setattr(process_runner.os, "getpgrp", lambda: 11111)
    monkeypatch.setattr(process_runner.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(process_runner.time, "sleep", lambda _seconds: None)

    process_group_id = process_runner._await_process_group_id(
        _StartedProcess(),
        timeout_seconds=0.05,
        poll_seconds=0.0,
    )

    assert process_group_id == 22222
    assert calls["count"] == 2


def test_run_callable_maps_keyboard_interrupt_to_cancelled() -> None:
    def target() -> int:
        raise KeyboardInterrupt

    result = run_callable(
        operation="probe.cancel",
        target=target,
        timeout_seconds=10,
    )

    assert result.status == "cancelled"
    assert result.exit_code == 130


def test_run_process_keyboard_interrupt_terminates_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[int] = []

    def _interrupting_communicate(process, **_kwargs):
        raise KeyboardInterrupt

    def _fake_terminate(process, **_kwargs):
        terminated.append(int(process.pid or 0))
        process.kill()
        process.wait(timeout=5)

    monkeypatch.setattr(process_runner, "_communicate_streaming", _interrupting_communicate)
    monkeypatch.setattr(process_runner, "_terminate_process_tree", _fake_terminate)

    result = run_process(
        operation="probe.process-keyboardinterrupt",
        command=[sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=10,
    )

    assert result.status == "cancelled"
    assert result.exit_code == 130
    assert terminated


def test_run_process_keyboard_interrupt_preserves_streamed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _interrupting_communicate(process, **kwargs):
        capture = kwargs["capture"]
        capture.append("stdout", "partial stdout\n")
        capture.append("stderr", "partial stderr\n")
        raise KeyboardInterrupt

    monkeypatch.setattr(process_runner, "_communicate_streaming", _interrupting_communicate)
    monkeypatch.setattr(
        process_runner,
        "_terminate_process_tree",
        lambda process, **_kwargs: process.kill(),
    )

    result = run_process(
        operation="probe.process-keyboardinterrupt-output",
        command=[sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=10,
    )

    assert result.status == "cancelled"
    assert result.exit_code == 130
    assert result.stdout_full == "partial stdout\n"
    assert result.stderr_full == "partial stderr\nOperation interrupted."


def test_run_callable_keyboard_interrupt_terminates_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[int] = []

    class _FakeProcess:
        pid = 12345
        exitcode = None

        def start(self):
            return None

        def join(self, timeout=None):
            raise KeyboardInterrupt

        def is_alive(self):
            return True

    fake_process = _FakeProcess()

    class _FakeContext:
        def Queue(self, maxsize=1):
            return SimpleNamespace(get_nowait=lambda: (_ for _ in ()).throw(Exception("empty")))

        def Process(self, **_kwargs):
            return fake_process

    def _fake_terminate(process, **_kwargs):
        terminated.append(int(process.pid or 0))

    monkeypatch.setattr(process_runner, "_process_context", lambda: _FakeContext())
    monkeypatch.setattr(process_runner, "_terminate_process_tree", _fake_terminate)

    result = run_callable(
        operation="probe.callable-keyboardinterrupt",
        target=lambda: 0,
        timeout_seconds=10,
    )

    assert result.status == "cancelled"
    assert result.exit_code == 130
    assert terminated == [12345]
