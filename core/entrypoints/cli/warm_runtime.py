"""Signal-aware idle process for the reusable CLI container."""

from __future__ import annotations

import signal
import threading
from types import FrameType


def wait_for_shutdown(stop: threading.Event | None = None) -> None:
    event = stop or threading.Event()

    def request_shutdown(_signum: int, _frame: FrameType | None) -> None:
        event.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    event.wait()


def main() -> None:
    wait_for_shutdown()


if __name__ == "__main__":
    main()
