"""Reusable process control helpers."""

from __future__ import annotations

import time


def terminate_worker_pid(
    pid: int,
    *,
    grace_seconds: float = 3.0,
    pid_alive_fn,
    kill_fn,
    sigterm,
    sigkill,
    now_fn=time.time,
    sleep_fn=time.sleep,
) -> dict:
    """Terminate a worker deterministically with SIGTERM and SIGKILL fallback."""
    if pid <= 0:
        return {"attempted": False, "reason": "invalid_pid"}
    if not pid_alive_fn(pid):
        return {"attempted": False, "reason": "pid_not_found", "pid": pid}

    try:
        kill_fn(pid, sigterm)
    except ProcessLookupError:
        return {"attempted": False, "reason": "pid_not_found", "pid": pid}

    deadline = now_fn() + max(0.5, float(grace_seconds))
    while now_fn() < deadline:
        if not pid_alive_fn(pid):
            return {"attempted": True, "signal": "SIGTERM", "pid": pid}
        sleep_fn(0.2)

    if pid_alive_fn(pid):
        try:
            kill_fn(pid, sigkill)
        except ProcessLookupError:
            return {"attempted": True, "signal": "SIGTERM", "pid": pid}
        sleep_fn(0.2)
    return {"attempted": True, "signal": "SIGKILL", "pid": pid}


def build_terminate_worker_pid_handler(
    *,
    grace_seconds: float,
    pid_alive_fn,
    kill_fn,
    sigterm,
    sigkill,
    now_fn=time.time,
    sleep_fn=time.sleep,
):
    def _handler(pid: int) -> dict:
        return terminate_worker_pid(
            pid,
            grace_seconds=grace_seconds,
            pid_alive_fn=pid_alive_fn,
            kill_fn=kill_fn,
            sigterm=sigterm,
            sigkill=sigkill,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )

    return _handler
