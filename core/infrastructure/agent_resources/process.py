"""Owned foreground process execution with signal forwarding and process-group leases."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable
from contextlib import suppress


def run_command(
    command: list[str], environment: dict[str, str], on_start: Callable[[int], None]
) -> int:
    """Wait for the foreground command; never signal processes outside its new session."""
    if not command:
        raise ValueError("run requires a command")
    child = subprocess.Popen(command, env=environment, start_new_session=True)
    previous = {}

    def forward(signum: int, _frame: object) -> None:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signum)

    try:
        on_start(child.pid)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, forward)
        code = child.wait()
        return code if code >= 0 else 128 - code
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait()


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
