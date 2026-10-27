from __future__ import annotations

from core.infrastructure.runtime import process_runtime


def test_terminate_worker_pid_returns_invalid_pid_for_non_positive_values() -> None:
    result = process_runtime.terminate_worker_pid(
        0,
        pid_alive_fn=lambda _pid: False,
        kill_fn=lambda _pid, _sig: None,
        sigterm="TERM",
        sigkill="KILL",
    )

    assert result == {"attempted": False, "reason": "invalid_pid"}


def test_terminate_worker_pid_stops_after_sigterm_when_process_exits() -> None:
    clock = {"now": 0.0}
    alive = {"value": True}
    signals: list[str] = []

    def _kill(_pid: int, sig: str) -> None:
        signals.append(sig)
        if sig == "TERM":
            alive["value"] = False

    result = process_runtime.terminate_worker_pid(
        101,
        pid_alive_fn=lambda _pid: alive["value"],
        kill_fn=_kill,
        sigterm="TERM",
        sigkill="KILL",
        now_fn=lambda: clock["now"],
        sleep_fn=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    assert result == {"attempted": True, "signal": "SIGTERM", "pid": 101}
    assert signals == ["TERM"]


def test_terminate_worker_pid_escalates_to_sigkill_after_grace_period() -> None:
    clock = {"now": 0.0}
    alive = {"value": True}
    signals: list[str] = []

    def _kill(_pid: int, sig: str) -> None:
        signals.append(sig)
        if sig == "KILL":
            alive["value"] = False

    result = process_runtime.terminate_worker_pid(
        202,
        pid_alive_fn=lambda _pid: alive["value"],
        kill_fn=_kill,
        sigterm="TERM",
        sigkill="KILL",
        grace_seconds=0.5,
        now_fn=lambda: clock["now"],
        sleep_fn=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    assert result == {"attempted": True, "signal": "SIGKILL", "pid": 202}
    assert signals == ["TERM", "KILL"]
