"""Synchronous MCP calls preserve failures and daemon lifetime inside async callers."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap

import pytest

from core.application.orchestration.tools.mcp_tools_client import _run_mcp_coro_sync


@pytest.mark.parametrize("value,expected", [("result", "result"), (None, ""), (0, "")])
def test_running_loop_relay_preserves_result_contract(value, expected):
    async def operation():
        return value

    async def caller():
        return _run_mcp_coro_sync(operation())

    assert asyncio.run(caller()) == expected


@pytest.mark.parametrize(
    "failure_type", [RuntimeError, TimeoutError, asyncio.CancelledError, KeyboardInterrupt]
)
def test_running_loop_relay_returns_the_original_failure_to_caller(failure_type):
    failure = failure_type("operation interrupted")

    async def operation():
        raise failure

    async def caller():
        pending = operation()
        with pytest.raises(failure_type) as caught:
            _run_mcp_coro_sync(pending)
        assert caught.value is failure

    asyncio.run(caller())


def test_interrupted_caller_can_exit_while_daemon_operation_is_still_blocked():
    # A real subprocess proves interpreter shutdown. A non-daemon executor's
    # implicit atexit join would hang here despite shutdown(wait=False).
    script = textwrap.dedent(
        """
        import asyncio
        import os
        import signal
        import threading
        from core.application.orchestration.tools.mcp_tools_client import _run_mcp_coro_sync

        started = threading.Event()
        def interrupt(_signal, _frame):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGINT, interrupt)

        def cancel_caller():
            if started.wait(5):
                os.kill(os.getpid(), signal.SIGINT)
        threading.Thread(target=cancel_caller, daemon=True).start()

        async def operation():
            started.set()
            await asyncio.sleep(3600)
        async def caller():
            _run_mcp_coro_sync(operation())
        try:
            asyncio.run(caller())
        except KeyboardInterrupt:
            print("caller interrupted; daemon does not block exit", flush=True)
        else:
            raise AssertionError("caller was not interrupted")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert "caller interrupted; daemon does not block exit" in completed.stdout
    assert "Exception in thread" not in completed.stderr
