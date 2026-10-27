from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.infrastructure.runtime import watchdog_runtime


def test_run_watchdog_scan_swaps_and_restores_control_hooks() -> None:
    original_detect = object()
    original_append = object()
    original_update = object()
    control_scans_module = SimpleNamespace(
        _detect_retryable_interrupted_step_snapshot=original_detect,
        _RETRYABLE_INTERRUPTED_STEP_STATUS="retryable_interrupted_step",
        _append_runtime_journal=original_append,
        _update_job_status_with_reason=original_update,
    )

    captured = {}

    def _control_watchdog_scan(_bl, cfg, *, maintenance_activated_at=None):
        captured["cfg"] = cfg
        captured["detect"] = control_scans_module._detect_retryable_interrupted_step_snapshot
        captured["status"] = control_scans_module._RETRYABLE_INTERRUPTED_STEP_STATUS
        captured["append"] = control_scans_module._append_runtime_journal
        captured["update"] = control_scans_module._update_job_status_with_reason
        captured["maintenance_activated_at"] = maintenance_activated_at
        return {"ok": True}

    maintenance_activated_at = datetime(2026, 9, 18, 14, 39, tzinfo=UTC)
    result = watchdog_runtime.run_watchdog_scan(
        MagicMock(),
        apply=True,
        watchdog_runtime_config=lambda auto_apply=None: {"auto_apply": auto_apply},
        control_scans_module=control_scans_module,
        control_watchdog_scan=_control_watchdog_scan,
        detect_retryable_interrupted_step_snapshot=lambda *_args, **_kwargs: {},
        retryable_interrupted_step_status="retryable_interrupted_step",
        append_runtime_journal=lambda *args, **kwargs: {"args": args, "kwargs": kwargs},
        update_job_status_with_reason=MagicMock(),
        maintenance_activated_at=maintenance_activated_at,
    )

    assert result == {"ok": True}
    assert captured["cfg"] == {"auto_apply": True}
    assert captured["detect"] is not original_detect
    assert captured["append"] is not original_append
    assert captured["update"] is not original_update
    assert captured["maintenance_activated_at"] == maintenance_activated_at
    assert control_scans_module._detect_retryable_interrupted_step_snapshot is original_detect
    assert control_scans_module._append_runtime_journal is original_append
    assert control_scans_module._update_job_status_with_reason is original_update
