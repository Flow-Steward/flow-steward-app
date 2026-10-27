"""Operating-system process adapter for developer and operator tooling."""

from __future__ import annotations

import codecs
import contextlib
import io
import multiprocessing
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from core.application.management.command_specs.contracts import (
    OperationProgressEvent,
    OperationStepStatus,
)
from core.application.tooling.contracts import (
    ToolCallable,
    ToolProgressSink,
    ToolRunResult,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.tooling.process_capture import (
    _append_bounded_diagnostic,
    _StreamingCapture,
    _tail,
)

_MESSAGE_OPERATION_INTERRUPTED = "Operation interrupted."

_HEARTBEAT_SECONDS = 5.0
_TERMINATE_GRACE_SECONDS = 1.0
_PIPE_READ_CHUNK_BYTES = 64 * 1024
_PIPE_DRAIN_POLL_SECONDS = 0.01
_PIPE_READER_JOIN_SECONDS = 1.0
_SAFE_ENV_KEYS = frozenset(
    {
        "CI",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PYTHONPATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
        "VIRTUAL_ENV",
    }
)
_SENSITIVE_ENV_KEY = re.compile(
    r"(?:api[_-]?key|authorization|bearer|cookie|credential|dsn|pass|private[_-]?key|secret|session|token)",
    re.IGNORECASE,
)


class OSProcessRunner:
    """Execute tooling operations through isolated operating-system processes."""

    def __init__(self, *, working_directory: str | Path) -> None:
        self.working_directory = Path(working_directory).resolve()

    def run_callable(
        self,
        *,
        operation: str,
        target: ToolCallable,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        artifacts: list[str] | None = None,
        progress_sink: ToolProgressSink | None = None,
    ) -> ToolRunResult:
        return _run_callable(
            operation=operation,
            target=target,
            env=env,
            timeout_seconds=timeout_seconds,
            artifacts=artifacts,
            progress_sink=progress_sink,
        )

    def run_process(
        self,
        *,
        operation: str,
        command: list[str],
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        artifacts: list[str] | None = None,
        progress_sink: ToolProgressSink | None = None,
    ) -> ToolRunResult:
        return _run_process(
            operation=operation,
            command=command,
            working_directory=self.working_directory,
            env=env,
            timeout_seconds=timeout_seconds,
            artifacts=artifacts,
            progress_sink=progress_sink,
        )


class _CallablePipeReaders:
    """Continuously drain callable stdout and stderr without unbounded storage."""

    def __init__(
        self,
        *,
        stdout_fd: int,
        stderr_fd: int,
        capture: _StreamingCapture,
    ) -> None:
        self._capture = capture
        self._stop = threading.Event()
        self._streams = (("stdout", stdout_fd), ("stderr", stderr_fd))
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for stream_name, file_descriptor in self._streams:
            os.set_blocking(file_descriptor, False)
            thread = threading.Thread(
                target=self._read_stream,
                args=(stream_name, file_descriptor),
                daemon=True,
                name=f"tooling-callable-{stream_name}",
            )
            thread.start()
            self._threads.append(thread)

    def finish(self) -> None:
        # The callable has exited (or its process group has been terminated), so
        # wait for both pipe readers to observe EOF and consume every queued byte.
        for thread in self._threads:
            thread.join(timeout=_PIPE_READER_JOIN_SECONDS)
        # A successfully-returned callable may leave a detached descendant with
        # an inherited writer. Do not wait forever for such an unrelated process.
        self._stop.set()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=_PIPE_READER_JOIN_SECONDS)
        for _stream_name, file_descriptor in self._streams:
            _close_file_descriptor(file_descriptor)
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=_PIPE_READER_JOIN_SECONDS)

    def _read_stream(self, stream_name: str, file_descriptor: int) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                try:
                    chunk = os.read(file_descriptor, _PIPE_READ_CHUNK_BYTES)
                except BlockingIOError:
                    if self._stop.is_set():
                        break
                    self._stop.wait(_PIPE_DRAIN_POLL_SECONDS)
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                self._capture.append(stream_name, decoder.decode(chunk))
        finally:
            trailing_text = decoder.decode(b"", final=True)
            if trailing_text:
                self._capture.append(stream_name, trailing_text)


def _run_callable(
    *,
    operation: str,
    target: ToolCallable,
    env: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
    artifacts: list[str] | None = None,
    progress_sink: ToolProgressSink | None = None,
) -> ToolRunResult:
    """Run one typed tooling function inside a killable child-process boundary."""
    _emit(progress_sink, operation=operation, status=OperationStepStatus.RUNNING, detail="started")
    started = time.monotonic()
    stdout_read_fd, stdout_write_fd, stderr_read_fd, stderr_write_fd = _open_callable_output_pipes()
    capture = _StreamingCapture()
    readers = _CallablePipeReaders(
        stdout_fd=stdout_read_fd,
        stderr_fd=stderr_read_fd,
        capture=capture,
    )
    ctx: Any = _process_context()
    outcome = ctx.Queue(maxsize=1)
    ready = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=_run_callable_child,
        args=(
            target,
            env or {},
            stdout_read_fd,
            stdout_write_fd,
            stderr_read_fd,
            stderr_write_fd,
            outcome,
            ready,
        ),
        name=f"tooling-{operation}",
    )
    process_started = False
    readers_started = False
    process_group_id: int | None = None
    interrupted = False
    timed_out = False
    termination_requested = False
    try:
        process.start()
        process_started = True
        _close_file_descriptor(stdout_write_fd)
        _close_file_descriptor(stderr_write_fd)
        readers.start()
        readers_started = True
        process_group_id = _child_ready_process_group_id(process, ready)
        execution_started = time.monotonic()
        try:
            # Process creation and the process-group handshake are setup work.
            # Starting the callable deadline before that handshake makes short,
            # valid timeouts expire before the child can emit any output when the
            # host is busy (for example during the full quality suite).
            deadline = execution_started + timeout_seconds if timeout_seconds is not None else None
            next_heartbeat = started + _HEARTBEAT_SECONDS
            while process.is_alive():
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    break
                wait_for = min(
                    0.25,
                    max(0.0, next_heartbeat - now),
                    max(0.0, deadline - now) if deadline is not None else 0.25,
                )
                process.join(timeout=wait_for)
                now = time.monotonic()
                if process.is_alive() and now >= next_heartbeat:
                    _emit(
                        progress_sink,
                        operation=operation,
                        status=OperationStepStatus.RUNNING,
                        detail=f"running for {now - started:.1f}s",
                        elapsed_seconds=now - started,
                    )
                    next_heartbeat = now + _HEARTBEAT_SECONDS
        except KeyboardInterrupt:
            termination_requested = True
            _terminate_process_tree(process, process_group_id=process_group_id)
            interrupted = True
        timed_out = not interrupted and process.is_alive()
        if timed_out:
            termination_requested = True
            _terminate_process_tree(process, process_group_id=process_group_id)
        if not interrupted:
            process.join(timeout=0)
    except KeyboardInterrupt:
        interrupted = True
        if process_started:
            termination_requested = True
            _terminate_process_tree(process, process_group_id=process_group_id)
    finally:
        _close_file_descriptor(stdout_write_fd)
        _close_file_descriptor(stderr_write_fd)
        if process_started and not termination_requested and _is_process_alive(process):
            _terminate_process_tree(process, process_group_id=process_group_id)
        if readers_started:
            readers.finish()
        else:
            _close_file_descriptor(stdout_read_fd)
            _close_file_descriptor(stderr_read_fd)

    stdout_text = capture.stdout
    stderr_text = capture.stderr
    if interrupted:
        elapsed = time.monotonic() - started
        interrupted_stderr = _append_bounded_diagnostic(stderr_text, _MESSAGE_OPERATION_INTERRUPTED)
        _emit(
            progress_sink,
            operation=operation,
            status=OperationStepStatus.CANCELLED,
            detail="interrupted",
            elapsed_seconds=elapsed,
        )
        return ToolRunResult(
            operation=operation,
            status="cancelled",
            exit_code=130,
            command=[operation],
            stdout=_tail(stdout_text),
            stderr=_tail(interrupted_stderr),
            stdout_full=stdout_text,
            stderr_full=interrupted_stderr,
            artifacts=list(artifacts or []),
            details={"timeout_seconds": timeout_seconds, "execution": "child_process"},
        )
    if timed_out:
        elapsed = time.monotonic() - started
        timeout_message = f"Timed out after {timeout_seconds} seconds"
        timeout_stderr = _append_bounded_diagnostic(stderr_text, timeout_message)
        _emit(
            progress_sink,
            operation=operation,
            status=OperationStepStatus.FAILED,
            detail=timeout_message,
            elapsed_seconds=elapsed,
        )
        return ToolRunResult(
            operation=operation,
            status="timeout",
            exit_code=124,
            command=[operation],
            stdout=_tail(stdout_text),
            stderr=_tail(timeout_stderr),
            stdout_full=stdout_text,
            stderr_full=timeout_stderr,
            artifacts=list(artifacts or []),
            details={"timeout_seconds": timeout_seconds, "execution": "child_process"},
        )

    status = "failed"
    exit_code = int(process.exitcode or 70)
    child_diagnostic = ""
    try:
        outcome_value = outcome.get_nowait()
        status, exit_code = outcome_value[:2]
        if len(outcome_value) >= 3:
            child_diagnostic = str(outcome_value[2] or "")
    except queue.Empty:
        if exit_code == 0:
            status = "completed"
        stderr_text = stderr_text or "Tooling child process exited without a result"
    if child_diagnostic:
        stderr_text = _append_bounded_diagnostic(stderr_text, child_diagnostic)

    elapsed = time.monotonic() - started
    step_status = OperationStepStatus.FAILED
    if exit_code == 0:
        step_status = OperationStepStatus.SUCCEEDED
    elif status == "cancelled":
        step_status = OperationStepStatus.CANCELLED
    _emit(
        progress_sink,
        operation=operation,
        status=step_status,
        detail=f"finished in {elapsed:.1f}s",
        elapsed_seconds=elapsed,
    )
    return ToolRunResult(
        operation=operation,
        status=status,
        exit_code=exit_code,
        command=[operation],
        stdout=_tail(stdout_text),
        stderr=_tail(stderr_text),
        stdout_full=stdout_text,
        stderr_full=stderr_text,
        artifacts=list(artifacts or []),
        details={"timeout_seconds": timeout_seconds, "execution": "child_process"},
    )


def _process_context() -> multiprocessing.context.BaseContext:
    methods = multiprocessing.get_all_start_methods()
    if "fork" not in methods:
        raise RuntimeError("run_callable requires a fork-capable Python runtime")
    return multiprocessing.get_context("fork")


def _run_callable_child(
    target: ToolCallable,
    env: dict[str, str],
    stdout_read_fd: int,
    stdout_write_fd: int,
    stderr_read_fd: int,
    stderr_write_fd: int,
    outcome: multiprocessing.Queue,
    ready: multiprocessing.Queue,
) -> None:
    stdout_file, stderr_file = _redirect_callable_output(
        stdout_read_fd=stdout_read_fd,
        stdout_write_fd=stdout_write_fd,
        stderr_read_fd=stderr_read_fd,
        stderr_write_fd=stderr_write_fd,
    )
    process_group_id = _become_process_group_leader()
    with contextlib.suppress(queue.Full):
        ready.put_nowait(process_group_id)
    status = "completed"
    exit_code = 0
    diagnostic = ""
    run_env = _child_process_env(env)
    with (
        stdout_file,
        stderr_file,
        _temporary_process_env(run_env),
        _subprocess_environment_guard(run_env),
        contextlib.redirect_stdout(stdout_file),
        contextlib.redirect_stderr(stderr_file),
    ):
        try:
            try:
                result = target()
                if isinstance(result, int):
                    exit_code = int(result)
            finally:
                pending_exception = sys.exc_info()[1]
                if isinstance(pending_exception, SystemExit):
                    exit_code = _system_exit_code(pending_exception)
                    if pending_exception.code is not None and not isinstance(
                        pending_exception.code, int
                    ):
                        diagnostic = f"{pending_exception.code}\n"
                        print(diagnostic, end="", file=stderr_file)
                    status = "completed" if exit_code == 0 else "failed"
                    stdout_file.flush()
                    stderr_file.flush()
                    with contextlib.suppress(queue.Full):
                        outcome.put_nowait((status, exit_code, diagnostic))
                    pending_exception.code = exit_code
        except KeyboardInterrupt:
            exit_code = 130
            status = "cancelled"
            diagnostic = "Operation interrupted.\n"
            print(_MESSAGE_OPERATION_INTERRUPTED, file=stderr_file)
        except Exception as exc:
            exit_code = 70
            status = "failed"
            diagnostic = f"{exc}\n"
            print(diagnostic, end="", file=stderr_file)
        if status == "completed" and exit_code != 0:
            status = "failed"
        stdout_file.flush()
        stderr_file.flush()
    with contextlib.suppress(queue.Full):
        outcome.put_nowait((status, exit_code, diagnostic))


def _open_callable_output_pipes() -> tuple[int, int, int, int]:
    stdout_read_fd, stdout_write_fd = os.pipe()
    try:
        stderr_read_fd, stderr_write_fd = os.pipe()
    except OSError:
        _close_file_descriptor(stdout_read_fd)
        _close_file_descriptor(stdout_write_fd)
        raise
    return stdout_read_fd, stdout_write_fd, stderr_read_fd, stderr_write_fd


def _redirect_callable_output(
    *,
    stdout_read_fd: int,
    stdout_write_fd: int,
    stderr_read_fd: int,
    stderr_write_fd: int,
) -> tuple[io.TextIOWrapper, io.TextIOWrapper]:
    _close_file_descriptor(stdout_read_fd)
    _close_file_descriptor(stderr_read_fd)
    stdout_target_fd = os.dup(stdout_write_fd)
    stderr_target_fd = os.dup(stderr_write_fd)
    _close_file_descriptor(stdout_write_fd)
    _close_file_descriptor(stderr_write_fd)
    os.dup2(stdout_target_fd, 1)
    os.dup2(stderr_target_fd, 2)
    _close_file_descriptor(stdout_target_fd)
    _close_file_descriptor(stderr_target_fd)
    return (
        io.TextIOWrapper(
            os.fdopen(os.dup(1), "wb", buffering=0),
            encoding="utf-8",
            errors="replace",
            write_through=True,
        ),
        io.TextIOWrapper(
            os.fdopen(os.dup(2), "wb", buffering=0),
            encoding="utf-8",
            errors="replace",
            write_through=True,
        ),
    )


def _close_file_descriptor(file_descriptor: int) -> None:
    with contextlib.suppress(OSError):
        os.close(file_descriptor)


def _run_process(
    *,
    operation: str,
    command: list[str],
    working_directory: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
    artifacts: list[str] | None = None,
    progress_sink: ToolProgressSink | None = None,
) -> ToolRunResult:
    """Run a bounded process with a scrubbed environment and safe output capture."""
    run_env = _child_process_env(env or {})
    _emit(progress_sink, operation=operation, status=OperationStepStatus.RUNNING, detail="started")
    started = time.monotonic()
    process: subprocess.Popen[str] | None = None
    process_group_id: int | None = None
    capture = _StreamingCapture()
    try:
        process = subprocess.Popen(
            command,
            cwd=working_directory,
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=_supports_process_groups(),
        )
        process_group_id = _await_process_group_id(process)
        completed = _communicate_streaming(
            process,
            operation=operation,
            timeout_seconds=timeout_seconds,
            progress_sink=progress_sink,
            started=started,
            process_group_id=process_group_id,
            capture=capture,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        _emit(
            progress_sink,
            operation=operation,
            status=OperationStepStatus.FAILED,
            detail=f"timed out after {timeout_seconds} seconds",
            elapsed_seconds=elapsed,
        )
        return ToolRunResult(
            operation=operation,
            status="timeout",
            exit_code=124,
            command=_safe_command(command),
            stdout=_tail(exc.stdout or capture.stdout),
            stderr=_tail(exc.stderr or f"Timed out after {timeout_seconds} seconds"),
            stdout_full=str(exc.stdout or capture.stdout),
            stderr_full=str(exc.stderr or f"Timed out after {timeout_seconds} seconds"),
            artifacts=list(artifacts or []),
            details={"timeout_seconds": timeout_seconds},
        )
    except KeyboardInterrupt:
        if process is not None:
            _terminate_process_tree(process, process_group_id=process_group_id)
        elapsed = time.monotonic() - started
        interrupted_stderr = _append_interrupt_message(capture.stderr)
        _emit(
            progress_sink,
            operation=operation,
            status=OperationStepStatus.CANCELLED,
            detail="interrupted",
            elapsed_seconds=elapsed,
        )
        return ToolRunResult(
            operation=operation,
            status="cancelled",
            exit_code=130,
            command=_safe_command(command),
            stdout=_tail(capture.stdout),
            stderr=_tail(interrupted_stderr),
            stdout_full=capture.stdout,
            stderr_full=interrupted_stderr,
            artifacts=list(artifacts or []),
            details={"timeout_seconds": timeout_seconds, "execution": "subprocess"},
        )
    elapsed = time.monotonic() - started
    _emit(
        progress_sink,
        operation=operation,
        status=OperationStepStatus.SUCCEEDED
        if completed.returncode == 0
        else OperationStepStatus.FAILED,
        detail=f"finished in {elapsed:.1f}s",
        elapsed_seconds=elapsed,
    )
    return ToolRunResult(
        operation=operation,
        status="completed" if completed.returncode == 0 else "failed",
        exit_code=int(completed.returncode),
        command=_safe_command(command),
        stdout=_tail(completed.stdout),
        stderr=_tail(completed.stderr),
        stdout_full=completed.stdout,
        stderr_full=completed.stderr,
        artifacts=list(artifacts or []),
        details={"timeout_seconds": timeout_seconds, "execution": "subprocess"},
    )


def _append_interrupt_message(stderr: str) -> str:
    prefix = str(stderr or "").rstrip("\n")
    return "\n".join(part for part in (prefix, _MESSAGE_OPERATION_INTERRUPTED) if part)


def _safe_command(command: list[str]) -> list[str]:
    return [str(item) for item in command]


def _emit(
    progress_sink: ToolProgressSink | None,
    *,
    operation: str,
    status: OperationStepStatus,
    detail: str = "",
    elapsed_seconds: float | None = None,
) -> None:
    if progress_sink is None:
        return
    progress_sink(
        OperationProgressEvent(
            step_id=operation,
            status=status,
            label=operation,
            detail=detail,
            elapsed_seconds=elapsed_seconds,
        )
    )


def _child_process_env(extra: dict[str, str]) -> dict[str, str]:
    safe = {
        key: value
        for key, value in _runtime_env.items()
        if key in _SAFE_ENV_KEYS and not _is_sensitive_env_key(key)
    }
    for key, value in extra.items():
        if value == "":
            continue
        if _is_sensitive_env_key(key):
            raise ValueError(
                f"Refusing to pass sensitive environment variable to child process: {key}"
            )
        safe[key] = value
    return safe


@contextlib.contextmanager
def _temporary_process_env(run_env: dict[str, str]):
    original = dict(_runtime_env)
    _runtime_env.clear()
    _runtime_env.update(run_env)
    try:
        yield
    finally:
        _runtime_env.clear()
        _runtime_env.update(original)


@contextlib.contextmanager
def _subprocess_environment_guard(extra: dict[str, str]):
    """Give nested child processes a scrubbed environment without mutating os.environ."""
    child_env = _child_process_env(extra)
    original_popen = subprocess.Popen

    class GuardedPopen(subprocess.Popen):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            explicit_env = kwargs.get("env")
            if explicit_env is None:
                kwargs["env"] = child_env
            else:
                kwargs["env"] = _scrub_explicit_env(explicit_env, fallback=child_env)
            super().__init__(*args, **kwargs)

    subprocess.Popen = GuardedPopen
    try:
        yield
    finally:
        subprocess.Popen = original_popen


def _scrub_explicit_env(value: object, *, fallback: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict):
        return dict(fallback)
    scrubbed: dict[str, str] = {}
    for key, item in value.items():
        text_key = str(key or "").strip()
        if not text_key or _is_sensitive_env_key(text_key):
            continue
        scrubbed[text_key] = str(item)
    for key, item in fallback.items():
        scrubbed.setdefault(key, item)
    return scrubbed


def _is_sensitive_env_key(key: str) -> bool:
    return bool(_SENSITIVE_ENV_KEY.search(str(key or "")))


def _system_exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return int(code)
    return 1


def _supports_process_groups() -> bool:
    return os.name != "nt" and hasattr(os, "setsid") and hasattr(os, "killpg")


def _become_process_group_leader() -> int | None:
    if not _supports_process_groups():
        return None
    with contextlib.suppress(OSError):
        os.setsid()
    with contextlib.suppress(OSError):
        return int(os.getpgrp())
    return None


def _is_process_alive(process: Any) -> bool:
    is_alive = getattr(process, "is_alive", None)
    if callable(is_alive):
        return bool(is_alive())
    poll = getattr(process, "poll", None)
    if callable(poll):
        return poll() is None
    return False


def _wait_for_process_exit(process: Any, timeout_seconds: float) -> None:
    join = getattr(process, "join", None)
    if callable(join):
        join(timeout=timeout_seconds)
        return
    wait = getattr(process, "wait", None)
    if callable(wait):
        with contextlib.suppress(subprocess.TimeoutExpired):
            wait(timeout=timeout_seconds)


def _process_group_id(process: Any) -> int | None:
    if not _supports_process_groups():
        return None
    pid = int(getattr(process, "pid", 0) or 0)
    if pid <= 0:
        return None
    try:
        return int(os.getpgid(pid))
    except ProcessLookupError:
        return pid
    except OSError:
        return None


def _current_process_group_id() -> int | None:
    if not _supports_process_groups() or not hasattr(os, "getpgrp"):
        return None
    try:
        return int(os.getpgrp())
    except OSError:
        return None


def _is_safe_child_process_group(process: Any, process_group_id: int | None) -> bool:
    if process_group_id is None:
        return False
    pid = int(getattr(process, "pid", 0) or 0)
    current_group_id = _current_process_group_id()
    return process_group_id == pid or (
        current_group_id is not None and process_group_id != current_group_id
    )


def _await_process_group_id(
    process: Any,
    *,
    timeout_seconds: float = 1.0,
    poll_seconds: float = 0.01,
) -> int | None:
    """Wait until the child is no longer in the parent's process group."""

    if not _supports_process_groups():
        return None
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    process_group_id: int | None = None
    while True:
        process_group_id = _process_group_id(process)
        if _is_safe_child_process_group(process, process_group_id):
            return process_group_id
        if not _is_process_alive(process):
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(0.0, float(poll_seconds)))


def _child_ready_process_group_id(process: Any, ready_queue: Any) -> int | None:
    get = getattr(ready_queue, "get", None)
    if callable(get):
        try:
            process_group_id = get(timeout=1.0)
            if _is_safe_child_process_group(process, process_group_id):
                return int(process_group_id)
        except Exception:
            pass
    return _await_process_group_id(process)


def _signal_process_group_id(pgid: int | None, signum: int) -> bool:
    if pgid is None or not _supports_process_groups():
        return False
    try:
        os.killpg(int(pgid), signum)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        return False


def _terminate_direct_process(process: Any, *, force: bool) -> None:
    method_name = "kill" if force else "terminate"
    method = getattr(process, method_name, None)
    if callable(method):
        with contextlib.suppress(ProcessLookupError, OSError):
            method()


def _terminate_process_tree(process: Any, *, process_group_id: int | None = None) -> None:
    """Terminate a process and descendants that stayed in its process group."""
    pgid = process_group_id if process_group_id is not None else _process_group_id(process)
    if not _is_safe_child_process_group(process, pgid):
        pgid = None
    alive = _is_process_alive(process)
    if not alive and pgid is None:
        return
    if not _signal_process_group_id(pgid, signal.SIGTERM):
        if alive:
            _terminate_direct_process(process, force=False)
        else:
            return
    _wait_for_process_exit(process, min(0.2, _TERMINATE_GRACE_SECONDS))
    # A root process may receive TERM while it is in the middle of spawning a
    # child; re-signal the captured process group after the root has had a chance
    # to exit so late-created descendants do not survive the timeout boundary.
    if alive:
        _signal_process_group_id(pgid, signal.SIGTERM)
        _wait_for_process_exit(process, _TERMINATE_GRACE_SECONDS)
    if not _is_process_alive(process):
        _signal_process_group_id(pgid, signal.SIGKILL)
        return
    if not _signal_process_group_id(pgid, signal.SIGKILL):
        _terminate_direct_process(process, force=True)
    _wait_for_process_exit(process, _TERMINATE_GRACE_SECONDS)


def _communicate_streaming(
    process: subprocess.Popen[str],
    *,
    operation: str,
    timeout_seconds: float | None,
    progress_sink: ToolProgressSink | None,
    started: float,
    process_group_id: int | None = None,
    capture: _StreamingCapture | None = None,
) -> subprocess.CompletedProcess[str]:
    capture = capture or _StreamingCapture()
    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def reader(name: str, stream: Any) -> None:
        try:
            for line in iter(stream.readline, ""):
                output_queue.put((name, line))
        finally:
            output_queue.put((name, None))

    threads = [
        threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    open_streams = 2
    next_heartbeat = started + _HEARTBEAT_SECONDS
    while open_streams:
        elapsed = time.monotonic() - started
        if timeout_seconds is not None and elapsed > timeout_seconds:
            _terminate_process_tree(process, process_group_id=process_group_id)
            _drain_streaming_output_queue(output_queue, capture)
            raise subprocess.TimeoutExpired(
                process.args,
                timeout_seconds,
                output=capture.stdout,
                stderr=capture.stderr,
            )
        timeout = max(0.1, min(0.5, next_heartbeat - time.monotonic()))
        try:
            stream_name, line = output_queue.get(timeout=timeout)
        except queue.Empty:
            if time.monotonic() >= next_heartbeat:
                elapsed = time.monotonic() - started
                _emit(
                    progress_sink,
                    operation=operation,
                    status=OperationStepStatus.RUNNING,
                    detail=f"waiting; {elapsed:.0f}s elapsed",
                    elapsed_seconds=elapsed,
                )
                next_heartbeat = time.monotonic() + _HEARTBEAT_SECONDS
            continue
        if line is None:
            open_streams -= 1
            continue
        capture.append(stream_name, line)
        _emit(
            progress_sink,
            operation=operation,
            status=OperationStepStatus.RUNNING,
            detail=line.strip()[:240],
            elapsed_seconds=time.monotonic() - started,
        )

    return_code = process.wait()
    for thread in threads:
        thread.join(timeout=1)
    return subprocess.CompletedProcess(
        args=process.args,
        returncode=return_code,
        stdout=capture.stdout,
        stderr=capture.stderr,
    )


def _drain_streaming_output_queue(
    output_queue: queue.Queue[tuple[str, str | None]], capture: _StreamingCapture
) -> None:
    while True:
        try:
            stream_name, line = output_queue.get_nowait()
        except queue.Empty:
            return
        if line is None:
            continue
        capture.append(stream_name, line)
