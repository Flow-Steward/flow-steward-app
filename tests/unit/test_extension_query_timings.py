"""Host timings of an extension query and the process wait that wakes at exit."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from core.application.extensions.operation_query import _PhaseTimer
from core.infrastructure.extension_runtime.extension_runtime_invocation import _wait_for_exit


def test_the_wait_returns_when_the_process_exits() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    started = time.perf_counter()

    _wait_for_exit(proc, 30)

    assert proc.returncode == 0
    assert time.perf_counter() - started < 10


def test_the_wait_times_out_on_a_process_that_keeps_running() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            _wait_for_exit(proc, 0.2)
    finally:
        proc.kill()
        proc.wait()


def test_phase_timer_reports_each_phase_and_the_total() -> None:
    timer = _PhaseTimer()
    timer.lap("resolve_extension")
    timer.lap("run")

    result = timer.result()
    assert list(result) == ["resolve_extension", "run", "total"]
    assert result["total"] == pytest.approx(result["resolve_extension"] + result["run"], abs=0.2)
