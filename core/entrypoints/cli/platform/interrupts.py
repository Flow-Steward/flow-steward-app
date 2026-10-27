"""Scoped CLI signal handling for lifecycle mutations only."""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType

from core.application.lifecycle.contracts import CancellationToken
from core.application.lifecycle.errors import ForcedLifecycleInterrupt

FORCE_INTERRUPT_WINDOW_SECONDS = 5.0


@contextmanager
def lifecycle_interrupt_scope(
    cancellation: CancellationToken,
    *,
    enabled: bool = True,
    reporter: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    force_window_seconds: float = FORCE_INTERRUPT_WINDOW_SECONDS,
) -> Iterator[None]:
    """Install cooperative-then-forced SIGINT behavior for one invocation."""
    if not enabled or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGINT)
    last_interrupt_at: float | None = None

    def handle_interrupt(_signum: int, _frame: FrameType | None) -> None:
        nonlocal last_interrupt_at
        now = clock()
        if last_interrupt_at is not None and now - last_interrupt_at <= force_window_seconds:
            cancellation.force()
            _report(
                reporter,
                "Forced interrupt requested; recovery is required. "
                f"Run: {cancellation.recovery_command}",
            )
            raise ForcedLifecycleInterrupt
        last_interrupt_at = now
        cancellation.request()
        _report(
            reporter,
            "Cancellation requested; waiting for the next safe boundary. "
            "Press Ctrl+C again within 5 seconds to force interruption.",
        )

    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _report(reporter: Callable[[str], None] | None, message: str) -> None:
    if reporter is None:
        return
    try:
        reporter(message)
    except Exception:
        return
