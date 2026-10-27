from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from core.application.jobs import scheduled_job_runtime


def test_run_scheduled_job_once_uses_project_schedule_timezone() -> None:
    next_run_calls: list[dict] = []

    def _control_run_once(_bl, _scheduled_job, _cfg, **kwargs):
        kwargs["_scheduled_job_next_run_at_fn"](
            "0 * * * *",
            scheduled_for=datetime(2026, 3, 30, 10, 0, tzinfo=UTC),
            now_utc=datetime(2026, 3, 30, 10, 0, tzinfo=UTC),
        )
        return {"status": "ok"}

    result = scheduled_job_runtime.run_scheduled_job_once(
        MagicMock(),
        {"project_schedule_timezone": "Europe/Vilnius"},
        scheduled_jobs_config=object(),
        control_run_scheduled_job_once=_control_run_once,
        scheduled_job_next_run_at_fn=lambda cron_expr, **kwargs: (
            next_run_calls.append({"cron_expr": cron_expr, **kwargs})
            or datetime(2026, 3, 30, 11, 0, tzinfo=UTC)
        ),
        record_scheduled_job_outcome_fn=MagicMock(),
        as_utc_datetime_fn=MagicMock(),
    )

    assert result == {"status": "ok"}
    assert next_run_calls[0]["schedule_tz"] == "Europe/Vilnius"


def test_run_scheduled_job_once_does_not_override_control_enqueue_path() -> None:
    def _control_run_once(_bl, _scheduled_job, _cfg, **kwargs):
        assert "_enqueue_task_standalone_fn" not in kwargs
        return {"job_id": 42}

    result = scheduled_job_runtime.run_scheduled_job_once(
        MagicMock(),
        {},
        scheduled_jobs_config=object(),
        control_run_scheduled_job_once=_control_run_once,
        scheduled_job_next_run_at_fn=MagicMock(),
        record_scheduled_job_outcome_fn=MagicMock(),
        as_utc_datetime_fn=MagicMock(),
    )

    assert result == {"job_id": 42}


def test_build_transport_helpers_binds_shared_scheduled_job_dependencies() -> None:
    control_run_once = MagicMock(return_value={"status": "ok"})
    control_record = MagicMock()

    record_outcome, run_once = scheduled_job_runtime.build_transport_helpers(
        scheduled_jobs_config_fn=lambda: {"enabled": True},
        control_run_scheduled_job_once=control_run_once,
        control_record_scheduled_job_outcome=control_record,
        scheduled_job_next_run_at_fn=MagicMock(),
        as_utc_datetime_fn=MagicMock(),
    )

    record_outcome(MagicMock(), scheduled_job={"id": 1}, run_type="scheduled", status="ok")
    result = run_once(MagicMock(), {"id": 1})

    assert result == {"status": "ok"}
    control_record.assert_called_once()
    assert control_run_once.call_args.args[2] == {"enabled": True}
