"""A dead required security capability must be named, not swallowed.

RC7-001 / RA-001: the posture loop lives in control-plane and _run_generic_loop
catches every exception and continues. With the production salt missing from
that service the loop failed on import every iteration, for the life of the
process, while /health/ready stayed green and nothing recorded that the
capability had never worked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.infrastructure.runtime import readiness_runtime


class _Cursor:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, *_args, **_kwargs) -> None:
        return None

    def fetchone(self):
        return (self._value,)


class _Conn:
    def __init__(self, value):
        self._value = value

    def cursor(self):
        return _Cursor(self._value)


class _Logger:
    def __init__(self, newest):
        self.conn = _Conn(newest)
        self._newest = newest

    def get_security_posture_loop_status(self):
        timestamp = self._newest.isoformat() if isinstance(self._newest, datetime) else ""
        return {
            "last_attempt_at": timestamp,
            "last_success_at": timestamp,
            "last_failure_reason": "",
            "consecutive_failures": 0,
        }


class _StatusLogger(_Logger):
    def __init__(self, newest, status):
        super().__init__(newest)
        self._status = status

    def get_security_posture_loop_status(self):
        return self._status


def test_a_capability_that_never_completed_fails_readiness_by_name() -> None:
    result = readiness_runtime._security_posture_check(_Logger(None))

    assert result["ok"] is False
    assert result["never_completed"] is True
    assert "SECURITY_PII_SALT" in result["detail"], (
        "the failure must name the recovery action, not just report unhealthy"
    )


def test_a_recent_successful_iteration_is_ready() -> None:
    result = readiness_runtime._security_posture_check(
        _Logger(datetime.now(UTC) - timedelta(seconds=30))
    )

    assert result["ok"] is True
    assert result["never_completed"] is False


def test_a_recent_successful_empty_project_iteration_is_ready() -> None:
    timestamp = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    result = readiness_runtime._security_posture_check(
        _StatusLogger(
            None,
            {
                "last_attempt_at": timestamp,
                "last_success_at": timestamp,
                "last_failure_reason": "",
                "consecutive_failures": 0,
            },
        )
    )

    assert result["ok"] is True
    assert result["never_completed"] is False
    assert "no project snapshots" in result["detail"]


def test_a_persistently_failing_loop_is_caught_by_staleness() -> None:
    """The loop keeps running after a failure, so silence is the only symptom."""
    result = readiness_runtime._security_posture_check(
        _Logger(datetime.now(UTC) - timedelta(days=2))
    )

    assert result["ok"] is False
    assert result["age_seconds"] > result["max_age_seconds"]


def test_latest_failed_iteration_is_visible_even_when_previous_snapshot_is_recent() -> None:
    result = readiness_runtime._security_posture_check(
        _StatusLogger(
            datetime.now(UTC) - timedelta(seconds=30),
            {
                "last_attempt_at": "2026-08-10T12:00:00+00:00",
                "last_success_at": "2026-08-10T11:59:00+00:00",
                "last_failure_reason": "projects repository unavailable",
                "consecutive_failures": 2,
            },
        )
    )

    assert result["ok"] is False
    assert result["consecutive_failures"] == 2
    assert result["last_failure_reason"] == "projects repository unavailable"
    assert result["last_attempt_at"] == "2026-08-10T12:00:00+00:00"
    assert result["last_success_at"] == "2026-08-10T11:59:00+00:00"


def test_a_deployment_with_posture_scanning_off_is_not_reported_broken(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness_runtime._runtime_env,
        "get",
        lambda name, default=None: (
            "0" if name == "FS_SECURITY_POSTURE_ENABLED" else (default or "")
        ),
    )

    result = readiness_runtime._security_posture_check(_Logger(None))

    assert result["ok"] is True
    assert result["enabled"] is False


def test_an_unreadable_history_fails_closed() -> None:
    class _Broken:
        @property
        def conn(self):
            raise RuntimeError("database gone")

        @staticmethod
        def get_security_posture_loop_status():
            timestamp = datetime.now(UTC).isoformat()
            return {
                "last_attempt_at": timestamp,
                "last_success_at": timestamp,
                "last_failure_reason": "",
                "consecutive_failures": 0,
            }

    result = readiness_runtime._security_posture_check(_Broken())

    assert result["ok"] is False
    assert "database gone" in result["error"]


@pytest.mark.parametrize("value", [None, datetime.now(UTC) - timedelta(days=5)])
def test_readiness_reports_the_check_under_a_stable_name(value) -> None:
    """Operators and CLI doctor agree only if the key does not drift."""
    result = readiness_runtime._security_posture_check(_Logger(value))

    assert set(result) >= {"ok", "enabled", "detail"}
