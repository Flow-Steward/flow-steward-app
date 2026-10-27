"""Reusable watchdog adapter helpers for transport/runtime layers."""

from __future__ import annotations

from dataclasses import replace as dataclass_replace
from datetime import datetime
from typing import Any


def build_watchdog_runtime_config(
    *,
    control_config,
    watchdog_stuck_seconds: int,
    watchdog_orphan_grace_seconds: int,
    watchdog_pickup_grace_seconds: int,
    watchdog_max_journal_entries: int,
):
    def _config(*, auto_apply: bool | None = None):
        cfg = dataclass_replace(
            control_config,
            watchdog_stuck_seconds=int(watchdog_stuck_seconds),
            watchdog_orphan_grace_seconds=int(watchdog_orphan_grace_seconds),
            watchdog_pickup_grace_seconds=int(watchdog_pickup_grace_seconds),
            watchdog_max_journal_entries=int(watchdog_max_journal_entries),
        )
        if auto_apply is not None:
            cfg = dataclass_replace(cfg, watchdog_auto_apply=bool(auto_apply))
        return cfg

    return _config


def run_watchdog_scan(
    bl,
    *,
    apply: bool,
    watchdog_runtime_config,
    control_scans_module,
    control_watchdog_scan,
    detect_retryable_interrupted_step_snapshot,
    retryable_interrupted_step_status: str,
    append_runtime_journal,
    update_job_status_with_reason,
    maintenance_activated_at: datetime | None = None,
) -> dict[str, Any]:
    cfg = watchdog_runtime_config(auto_apply=apply)

    def _append_runtime_journal_proxy(
        bl_proxy,
        job_id_proxy: int,
        *,
        event: str,
        reason: str,
        detail: dict | None = None,
        max_entries: int = 200,
    ) -> dict[str, Any]:
        _ = max_entries
        return append_runtime_journal(
            bl_proxy,
            job_id_proxy,
            event=event,
            reason=reason,
            detail=detail,
        )

    detect_fn = control_scans_module._detect_retryable_interrupted_step_snapshot
    status_name = control_scans_module._RETRYABLE_INTERRUPTED_STEP_STATUS
    append_fn = control_scans_module._append_runtime_journal
    update_fn = control_scans_module._update_job_status_with_reason
    try:
        control_scans_module._detect_retryable_interrupted_step_snapshot = (
            detect_retryable_interrupted_step_snapshot
        )
        control_scans_module._RETRYABLE_INTERRUPTED_STEP_STATUS = retryable_interrupted_step_status
        control_scans_module._append_runtime_journal = _append_runtime_journal_proxy
        control_scans_module._update_job_status_with_reason = update_job_status_with_reason
        return control_watchdog_scan(
            bl,
            cfg,
            maintenance_activated_at=maintenance_activated_at,
        )
    finally:
        control_scans_module._detect_retryable_interrupted_step_snapshot = detect_fn
        control_scans_module._RETRYABLE_INTERRUPTED_STEP_STATUS = status_name
        control_scans_module._append_runtime_journal = append_fn
        control_scans_module._update_job_status_with_reason = update_fn
