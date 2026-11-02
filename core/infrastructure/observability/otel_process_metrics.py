"""Process metrics each long-lived process exports about itself.

They mirror the generic part of the process ``/metrics`` endpoints
(``process_metrics_runtime``) under the pinned process semantic conventions.
A forked agent child exports none: the owner check runs before every
collection, and the parent's own series already covers the process tree it
manages. Identity (``platform_process_info``) travels as resource attributes.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

from core.infrastructure.observability import otel_instruments as instruments

try:
    # The same start time ``platform_process_start_time_seconds`` reports.
    from core.infrastructure.observability.process_metrics_runtime import (
        _PROCESS_START_TIME_SECONDS,
    )
except ImportError:  # the fs-mcp image ships only the otel_* modules
    _PROCESS_START_TIME_SECONDS = time.time()

PROCESS_CPU_TIME = "process.cpu.time"
PROCESS_THREAD_COUNT = "process.thread.count"
PROCESS_UPTIME = "process.uptime"


def _cpu_time() -> Iterator[tuple[float, dict[str, Any]]]:
    yield time.process_time(), {}


def _thread_count() -> Iterator[tuple[float, dict[str, Any]]]:
    yield threading.active_count(), {}


def _uptime() -> Iterator[tuple[float, dict[str, Any]]]:
    yield max(time.time() - _PROCESS_START_TIME_SECONDS, 0.0), {}


def register_process_metrics() -> bool:
    """Register this process's instruments with the installed meter provider."""
    registered = [
        instruments.register_observable(
            PROCESS_CPU_TIME,
            kind="counter",
            unit="s",
            description="Total CPU seconds of this process.",
            observe=_cpu_time,
        ),
        instruments.register_observable(
            PROCESS_THREAD_COUNT,
            kind="up_down_counter",
            unit="{thread}",
            description="Threads currently running in this process.",
            observe=_thread_count,
        ),
        instruments.register_observable(
            PROCESS_UPTIME,
            kind="gauge",
            unit="s",
            description="Time since this process started.",
            observe=_uptime,
        ),
    ]
    return all(registered)


__all__ = [
    "PROCESS_CPU_TIME",
    "PROCESS_THREAD_COUNT",
    "PROCESS_UPTIME",
    "register_process_metrics",
]
