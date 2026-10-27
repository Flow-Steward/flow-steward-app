"""Streaming process execution with retained output and quiet-process heartbeat."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, cast

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

HEARTBEAT_SECONDS = 5.0
_SIGNAL_GRACE_SECONDS = 5.0
_SIGNAL_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class ProcessOutcome:
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class SubprocessExecutor:
    """Run one process while preserving output for the operation log."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
        output_sink: Callable[[str], None] | None = None,
        heartbeat_sink: Callable[[float], None] | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        separate_stderr: bool = False,
        inherited_fds: Sequence[int] = (),
    ) -> ProcessOutcome:
        started = time.monotonic()
        process_environment = _controlled_process_environment(environment)
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd) if cwd else None,
            env=process_environment,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if separate_stderr else subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
            pass_fds=tuple(inherited_fds),
        )
        if separate_stderr:
            return self._run_with_separate_stderr(
                command,
                process=process,
                started=started,
                timeout_seconds=timeout_seconds,
                output_sink=output_sink,
                heartbeat_sink=heartbeat_sink,
                input_text=input_text,
            )
        if input_text is not None:
            assert process.stdin is not None
            process.stdin.write(input_text)
            process.stdin.close()
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        stdout_fd = process.stdout.fileno()
        os.set_blocking(stdout_fd, False)
        chunks: list[bytes] = []
        pending = bytearray()

        def capture_bytes(data: bytes) -> None:
            chunks.append(data)
            pending.extend(data)
            while b"\n" in pending:
                line, _, remainder = pending.partition(b"\n")
                pending.clear()
                pending.extend(remainder)
                if output_sink is not None:
                    output_sink(line.rstrip(b"\r").decode("utf-8", errors="replace"))

        next_heartbeat = started + HEARTBEAT_SECONDS
        try:
            while process.poll() is None:
                now = time.monotonic()
                if timeout_seconds is not None and now - started >= timeout_seconds:
                    _stop_process_group(process)
                    raise TimeoutError(f"Process exceeded {timeout_seconds:.0f} seconds")
                timeout = max(0.0, min(1.0, next_heartbeat - now))
                for key, _mask in selector.select(timeout):
                    try:
                        data = os.read(cast(IO[str], key.fileobj).fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if data:
                        capture_bytes(data)
                    else:
                        selector.unregister(key.fileobj)
                now = time.monotonic()
                if now >= next_heartbeat:
                    if heartbeat_sink is not None:
                        heartbeat_sink(now - started)
                    next_heartbeat = now + HEARTBEAT_SECONDS
            while True:
                try:
                    trailing = os.read(stdout_fd, 65536)
                except BlockingIOError:
                    break
                if not trailing:
                    break
                capture_bytes(trailing)
            if pending and output_sink is not None:
                output_sink(bytes(pending).decode("utf-8", errors="replace"))
        except KeyboardInterrupt:
            _stop_process_group(process)
            raise
        finally:
            selector.close()
            process.stdout.close()
        return ProcessOutcome(
            command=tuple(str(part) for part in command),
            returncode=int(process.returncode or 0),
            stdout=b"".join(chunks).decode("utf-8", errors="replace"),
        )

    def _run_with_separate_stderr(
        self,
        command: Sequence[str],
        *,
        process: subprocess.Popen[str],
        started: float,
        timeout_seconds: float | None,
        output_sink: Callable[[str], None] | None,
        heartbeat_sink: Callable[[float], None] | None,
        input_text: str | None,
    ) -> ProcessOutcome:
        assert process.stdout is not None
        assert process.stderr is not None
        next_heartbeat = started + HEARTBEAT_SECONDS
        pending_input = input_text
        try:
            while True:
                now = time.monotonic()
                if timeout_seconds is not None and now - started >= timeout_seconds:
                    _stop_process_group(process)
                    raise TimeoutError(f"Process exceeded {timeout_seconds:.0f} seconds")
                wait_seconds = min(1.0, max(0.01, next_heartbeat - now))
                if timeout_seconds is not None:
                    wait_seconds = min(wait_seconds, timeout_seconds - (now - started))
                try:
                    stdout, stderr = process.communicate(
                        input=pending_input,
                        timeout=wait_seconds,
                    )
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        if heartbeat_sink is not None:
                            heartbeat_sink(now - started)
                        next_heartbeat = now + HEARTBEAT_SECONDS
        except KeyboardInterrupt:
            _stop_process_group(process)
            raise
        finally:
            process.stdout.close()
            process.stderr.close()
        if output_sink is not None:
            for line in stdout.splitlines():
                output_sink(line)
        return ProcessOutcome(
            command=tuple(str(part) for part in command),
            returncode=int(process.returncode or 0),
            stdout=stdout,
            stderr=stderr,
        )


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    for process_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, process_signal)
        except ProcessLookupError:
            process.poll()
            return
        deadline = time.monotonic() + _SIGNAL_GRACE_SECONDS
        try:
            while time.monotonic() < deadline:
                process.poll()
                if not _process_group_running(process.pid):
                    return
                time.sleep(_SIGNAL_POLL_SECONDS)
        except KeyboardInterrupt:
            continue


def _process_group_running(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False

    proc_root = Path("/proc")
    try:
        if not proc_root.is_dir():
            return True
        # An unreaped orphan keeps its group present in Docker without an init,
        # but only live members can continue the interrupted operation.
        for entry in proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if int(fields[2]) == group_id and fields[0] not in {"Z", "X"}:
                return True
    except (OSError, ValueError, IndexError):
        # Unknown visibility cannot prove termination; keep escalating against
        # the original group within the existing signal deadlines.
        return True
    return False


def _controlled_process_environment(
    environment: Mapping[str, str] | None,
) -> dict[str, str] | None:
    if environment is None:
        return None

    operational_names = {
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "HOME",
        "PATH",
        "TMPDIR",
        "XDG_CONFIG_HOME",
    }
    controlled = {name: _runtime_env[name] for name in operational_names if name in _runtime_env}
    controlled.update((str(name), str(value)) for name, value in environment.items())
    return controlled
