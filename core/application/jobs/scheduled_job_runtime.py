"""Reusable wrappers for scheduled job execution from transport layers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any


def run_scheduled_job_once(
    bl,
    scheduled_job: dict[str, Any],
    *,
    scheduled_jobs_config,
    control_run_scheduled_job_once,
    scheduled_job_next_run_at_fn,
    record_scheduled_job_outcome_fn,
    as_utc_datetime_fn,
    run_type: str = "scheduled",
    scheduled_for: datetime | None = None,
    advance_schedule: bool = True,
    now_utc: datetime | None = None,
    get_project_run_config_fn=None,
) -> dict[str, Any]:
    scheduled_job_tz = (
        str(
            scheduled_job.get("schedule_timezone")
            or scheduled_job.get("project_schedule_timezone")
            or ""
        ).strip()
        or None
    )

    def _scheduled_next_run_cb(
        cron_expr: str,
        *,
        scheduled_for: datetime,
        now_utc: datetime,
        schedule_tz: str | None = None,
    ) -> datetime:
        effective_tz = schedule_tz if schedule_tz is not None else scheduled_job_tz
        return scheduled_job_next_run_at_fn(
            cron_expr,
            scheduled_for=scheduled_for,
            now_utc=now_utc,
            schedule_tz=effective_tz,
        )

    return control_run_scheduled_job_once(
        bl,
        scheduled_job,
        scheduled_jobs_config,
        run_type=run_type,
        scheduled_for=scheduled_for,
        advance_schedule=advance_schedule,
        now_utc=now_utc,
        _as_utc_datetime_fn=as_utc_datetime_fn,
        _record_scheduled_job_outcome_fn=record_scheduled_job_outcome_fn,
        _scheduled_job_next_run_at_fn=_scheduled_next_run_cb,
        _get_project_run_config_fn=get_project_run_config_fn,
    )


def build_transport_helpers(
    *,
    scheduled_jobs_config_fn,
    control_run_scheduled_job_once,
    control_record_scheduled_job_outcome,
    scheduled_job_next_run_at_fn,
    as_utc_datetime_fn,
    get_project_run_config_fn=None,
) -> tuple[Callable[..., None], Callable[..., dict[str, Any]]]:
    def _record_scheduled_job_outcome(bl, **kwargs) -> None:
        control_record_scheduled_job_outcome(
            bl,
            **kwargs,
        )

    def _run_scheduled_job_once(
        bl,
        scheduled_job: dict[str, Any],
        *,
        run_type: str = "scheduled",
        scheduled_for: datetime | None = None,
        advance_schedule: bool = True,
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        return run_scheduled_job_once(
            bl,
            scheduled_job,
            scheduled_jobs_config=scheduled_jobs_config_fn(),
            control_run_scheduled_job_once=control_run_scheduled_job_once,
            scheduled_job_next_run_at_fn=scheduled_job_next_run_at_fn,
            record_scheduled_job_outcome_fn=_record_scheduled_job_outcome,
            as_utc_datetime_fn=as_utc_datetime_fn,
            get_project_run_config_fn=get_project_run_config_fn,
            run_type=run_type,
            scheduled_for=scheduled_for,
            advance_schedule=advance_schedule,
            now_utc=now_utc,
        )

    return _record_scheduled_job_outcome, _run_scheduled_job_once


def build_scheduled_jobs_scan_runner(
    *,
    scheduled_jobs_runtime_config_fn,
    control_scheduled_jobs_scan,
    as_utc_datetime_fn,
    run_scheduled_job_once_fn,
    scheduled_job_next_run_at_fn,
    record_scheduled_job_outcome_fn,
):
    def _scan(bl, *, now_utc: datetime | None = None) -> dict:
        cfg = scheduled_jobs_runtime_config_fn()
        return control_scheduled_jobs_scan(
            bl,
            cfg,
            now_utc=now_utc,
            _as_utc_datetime_fn=as_utc_datetime_fn,
            _run_scheduled_job_once_fn=lambda bl_arg, scheduled_job_arg, _cfg_arg, **kwargs: (
                run_scheduled_job_once_fn(
                    bl_arg,
                    scheduled_job_arg,
                    **kwargs,
                )
            ),
            _scheduled_job_next_run_at_fn=scheduled_job_next_run_at_fn,
            _record_scheduled_job_outcome_fn=record_scheduled_job_outcome_fn,
        )

    return _scan
