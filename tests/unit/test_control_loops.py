"""Unit tests for control/ package — loops, config, scans, cron."""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
import threading
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.infrastructure.runtime.control.config import ControlConfig, _env_flag
from core.infrastructure.runtime.control.loops import (
    _run_generic_loop,
    run_dashboard_rollup_loop,
    run_scheduled_jobs_loop,
    run_watchdog_loop,
    start_control_threads,
    stop_control_threads,
)

# ---------------------------------------------------------------------------
# ControlConfig
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _only_sys_module(name: str, module: types.ModuleType):
    """Swap one ``sys.modules`` entry and restore exactly that entry."""
    missing = object()
    previous = sys.modules.get(name, missing)
    sys.modules[name] = module
    try:
        yield module
    finally:
        if previous is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


class TestControlConfig:
    def test_from_env_defaults(self):
        cfg = ControlConfig.from_env()
        assert cfg.watchdog_enabled is True
        assert cfg.watchdog_interval >= 5
        assert cfg.watchdog_auto_apply is True
        # AC17-R105 follow-up: 120 was the compiled-in default and no installation
        # ever used it. Compose declares ${FS_WATCHDOG_STUCK_SECONDS:-180}, and `:-`
        # sets the key whether or not the operator did, so 180 is what every
        # deployment has always run. The code now says what is actually in force.
        assert cfg.watchdog_stuck_seconds == 180
        assert cfg.scheduled_jobs_enabled is True
        assert cfg.scheduled_jobs_interval >= 10
        assert cfg.scheduled_jobs_batch_limit >= 1
        assert cfg.dashboard_rollup_enabled is True
        assert cfg.dashboard_rollup_interval >= 30
        assert cfg.dashboard_rollup_backfill_days >= 1
        assert cfg.dashboard_rollup_lookback_days >= 1
        assert cfg.workflow_knowledge_sync_enabled is True
        assert cfg.workflow_knowledge_sync_interval >= 30
        assert cfg.workflow_knowledge_sync_changelog_limit >= 1

    def test_from_env_overrides(self, monkeypatch):
        monkeypatch.setenv("FS_WATCHDOG_ENABLED", "0")
        monkeypatch.setenv("FS_WATCHDOG_INTERVAL_SECONDS", "60")
        monkeypatch.setenv("FS_SCHEDULED_JOBS_ENABLED", "false")
        monkeypatch.setenv("FS_DASHBOARD_ROLLUP_ENABLED", "off")
        monkeypatch.setenv("FS_WORKFLOW_KNOWLEDGE_SYNC_ENABLED", "0")
        monkeypatch.setenv("FS_EXTERNAL_EFFECT_MAINTENANCE_ENABLED", "0")
        monkeypatch.setenv("FS_EXTERNAL_EFFECT_MAINTENANCE_INTERVAL_SECONDS", "75")
        cfg = ControlConfig.from_env()
        assert cfg.watchdog_enabled is False
        assert cfg.watchdog_interval == 60
        assert cfg.scheduled_jobs_enabled is False
        assert cfg.dashboard_rollup_enabled is False
        assert cfg.workflow_knowledge_sync_enabled is False
        assert cfg.external_effect_maintenance_enabled is False
        assert cfg.external_effect_maintenance_interval == 75

    def test_env_flag_parsing(self):
        assert _env_flag("NONEXISTENT_VAR_12345", True) is True
        assert _env_flag("NONEXISTENT_VAR_12345", False) is False


class TestEnvFlag:
    def test_true_values(self, monkeypatch):
        for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
            monkeypatch.setenv("TEST_FLAG", val)
            assert _env_flag("TEST_FLAG", False) is True

    def test_false_values(self, monkeypatch):
        for val in ("0", "false", "no", "off", "FALSE", "No"):
            monkeypatch.setenv("TEST_FLAG", val)
            assert _env_flag("TEST_FLAG", True) is False


# ---------------------------------------------------------------------------
# Loop runners
# ---------------------------------------------------------------------------


def _poll_until(predicate, timeout=2.0, interval=0.01):
    """Tight-loop poll — deterministic replacement for time.sleep."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestRunWatchdogLoop:
    def test_calls_scan_fn_and_stops(self):
        calls = []

        def scan_fn():
            calls.append(1)
            return {"detection_count": 0}

        stop = threading.Event()
        t = threading.Thread(
            target=run_watchdog_loop,
            args=(stop, 0.05, scan_fn),
            daemon=True,
        )
        t.start()
        assert _poll_until(lambda: len(calls) >= 2), f"expected >=2 calls, got {len(calls)}"
        stop.set()
        t.join(timeout=2)
        assert not t.is_alive()

    def test_handles_scan_exception(self):
        calls = []

        def scan_fn():
            calls.append(1)
            raise RuntimeError("boom")

        stop = threading.Event()
        t = threading.Thread(
            target=run_watchdog_loop,
            args=(stop, 0.05, scan_fn),
            daemon=True,
        )
        t.start()
        assert _poll_until(lambda: len(calls) >= 1), f"expected >=1 calls, got {len(calls)}"
        stop.set()
        t.join(timeout=2)
        assert not t.is_alive()


class TestRunScheduledJobsLoop:
    def test_calls_scan_fn_and_stops(self):
        calls = []

        def scan_fn():
            calls.append(1)
            return {"enqueued": 0}

        stop = threading.Event()
        t = threading.Thread(
            target=run_scheduled_jobs_loop,
            args=(stop, 0.05, scan_fn),
            daemon=True,
        )
        t.start()
        assert _poll_until(lambda: len(calls) >= 2), f"expected >=2 calls, got {len(calls)}"
        stop.set()
        t.join(timeout=2)
        assert not t.is_alive()


@pytest.mark.parametrize(
    ("runner", "scan_result"),
    [
        (run_watchdog_loop, {"detection_count": 0}),
        (run_scheduled_jobs_loop, {"enqueued": 0}),
        (run_dashboard_rollup_loop, {"refreshed_accounts": 0}),
    ],
)
def test_control_scan_does_not_run_while_new_work_is_paused(runner, scan_result) -> None:
    stop = threading.Event()
    scan = MagicMock(return_value=scan_result)

    def paused() -> bool:
        stop.set()
        return True

    runner(stop, 0, scan, maintenance_active=paused)

    scan.assert_not_called()


@pytest.mark.parametrize(
    "label",
    ("dispatch-outbox", "security-posture", "workflow-knowledge-sync"),
)
def test_generic_control_scan_does_not_run_while_new_work_is_paused(label: str) -> None:
    stop = threading.Event()
    scan = MagicMock(return_value={})

    def paused() -> bool:
        stop.set()
        return True

    _run_generic_loop(stop, 0, scan, label, maintenance_active=paused)

    scan.assert_not_called()


class TestRunDashboardRollupLoop:
    def test_calls_scan_fn_and_stops(self):
        calls = []

        def scan_fn():
            calls.append(1)
            return {"refreshed_accounts": 0}

        stop = threading.Event()
        t = threading.Thread(
            target=run_dashboard_rollup_loop,
            args=(stop, 0.05, scan_fn),
            daemon=True,
        )
        t.start()
        assert _poll_until(lambda: len(calls) >= 2), f"expected >=2 calls, got {len(calls)}"
        stop.set()
        t.join(timeout=2)
        assert not t.is_alive()


class TestRunGenericLoop:
    def test_logs_exceptions_with_traceback(self):
        stop = threading.Event()

        def scan_fn():
            stop.set()
            raise RuntimeError("boom")

        with patch("core.infrastructure.runtime.control.loops.logger") as mock_logger:
            _run_generic_loop(stop, 0.01, scan_fn, "workflow-knowledge-sync")

        mock_logger.exception.assert_called_once()


# ---------------------------------------------------------------------------
# Thread lifecycle
# ---------------------------------------------------------------------------


class TestStartStopControlThreads:
    def test_start_and_stop(self):
        calls = {"watchdog": 0, "sched": 0, "rollup": 0}

        def wd_scan():
            calls["watchdog"] += 1
            return {}

        def sj_scan():
            calls["sched"] += 1
            return {}

        def rollup_scan():
            calls["rollup"] += 1
            return {}

        def workflow_knowledge_scan():
            calls["workflow_knowledge"] = calls.get("workflow_knowledge", 0) + 1
            return {}

        cfg = ControlConfig(
            watchdog_enabled=True,
            watchdog_interval=0.05,
            watchdog_auto_apply=True,
            watchdog_stuck_seconds=120,
            watchdog_orphan_grace_seconds=120,
            watchdog_pickup_grace_seconds=45,
            watchdog_max_journal_entries=200,
            scheduled_jobs_enabled=True,
            scheduled_jobs_interval=0.05,
            scheduled_jobs_batch_limit=20,
            dashboard_rollup_enabled=True,
            dashboard_rollup_interval=0.05,
            dashboard_rollup_backfill_days=30,
            dashboard_rollup_lookback_days=2,
            security_posture_enabled=False,
            security_posture_interval=600,
            workflow_knowledge_sync_enabled=True,
            workflow_knowledge_sync_interval=0.05,
            workflow_knowledge_sync_changelog_limit=200,
        )
        # start_control_threads skips when PYTEST_CURRENT_TEST is set
        env = dict(os.environ)
        env.pop("PYTEST_CURRENT_TEST", None)
        with patch.dict(os.environ, env, clear=True):
            threads = start_control_threads(
                {
                    "dispatch_outbox": lambda: calls.__setitem__(
                        "dispatch_outbox", calls.get("dispatch_outbox", 0) + 1
                    ),
                    "watchdog": wd_scan,
                    "scheduled_jobs": sj_scan,
                    "dashboard_rollup": rollup_scan,
                    "workflow_knowledge_sync": workflow_knowledge_scan,
                },
                cfg,
            )
        assert "watchdog" in threads
        assert "dispatch_outbox" in threads
        assert "scheduled_jobs" in threads
        assert "dashboard_rollup" in threads
        assert "workflow_knowledge_sync" in threads
        assert threads["watchdog"][0].name == "control-watchdog"
        assert threads["dispatch_outbox"][0].name == "control-dispatch-outbox"
        assert threads["scheduled_jobs"][0].name == "control-scheduled-jobs"
        assert threads["dashboard_rollup"][0].name == "control-dashboard-rollup"
        assert threads["workflow_knowledge_sync"][0].name == "control-workflow-knowledge-sync"
        assert _poll_until(
            lambda: (
                calls["watchdog"] >= 1
                and calls.get("dispatch_outbox", 0) >= 1
                and calls["sched"] >= 1
                and calls["rollup"] >= 1
                and calls.get("workflow_knowledge", 0) >= 1
            )
        )
        stop_control_threads(threads)
        for name, (t, _) in threads.items():
            assert not t.is_alive(), f"{name} thread still alive"

    def test_disabled_loops_not_started(self):
        cfg = ControlConfig(
            watchdog_enabled=False,
            watchdog_interval=30,
            watchdog_auto_apply=True,
            watchdog_stuck_seconds=120,
            watchdog_orphan_grace_seconds=120,
            watchdog_pickup_grace_seconds=45,
            watchdog_max_journal_entries=200,
            scheduled_jobs_enabled=False,
            scheduled_jobs_interval=30,
            scheduled_jobs_batch_limit=20,
            dashboard_rollup_enabled=False,
            dashboard_rollup_interval=300,
            dashboard_rollup_backfill_days=180,
            dashboard_rollup_lookback_days=3,
            security_posture_enabled=False,
            security_posture_interval=600,
            workflow_knowledge_sync_enabled=False,
            workflow_knowledge_sync_interval=120,
            workflow_knowledge_sync_changelog_limit=200,
        )
        threads = start_control_threads(
            {"watchdog": dict, "scheduled_jobs": dict, "dashboard_rollup": dict},
            cfg,
        )
        assert threads == {}

    def test_external_effect_maintenance_is_independent_of_dashboard_rollups(self):
        calls = {"maintenance": 0}
        cfg = ControlConfig(
            watchdog_enabled=False,
            watchdog_interval=30,
            watchdog_auto_apply=True,
            watchdog_stuck_seconds=120,
            watchdog_orphan_grace_seconds=120,
            watchdog_pickup_grace_seconds=45,
            watchdog_max_journal_entries=200,
            scheduled_jobs_enabled=False,
            scheduled_jobs_interval=30,
            scheduled_jobs_batch_limit=20,
            dashboard_rollup_enabled=False,
            dashboard_rollup_interval=300,
            dashboard_rollup_backfill_days=180,
            dashboard_rollup_lookback_days=3,
            security_posture_enabled=False,
            security_posture_interval=600,
            workflow_knowledge_sync_enabled=False,
            workflow_knowledge_sync_interval=120,
            workflow_knowledge_sync_changelog_limit=200,
            external_effect_maintenance_enabled=True,
            external_effect_maintenance_interval=0.05,
        )
        env = dict(os.environ)
        env.pop("PYTEST_CURRENT_TEST", None)
        with patch.dict(os.environ, env, clear=True):
            threads = start_control_threads(
                {
                    "external_effect_maintenance": lambda: calls.__setitem__(
                        "maintenance", calls["maintenance"] + 1
                    )
                },
                cfg,
            )

        assert "external_effect_maintenance" in threads
        assert "dashboard_rollup" not in threads
        assert _poll_until(lambda: calls["maintenance"] >= 1)
        stop_control_threads(threads)

    def test_external_effect_reconciliation_pauses_during_maintenance(self, monkeypatch):
        monkeypatch.setenv("FS_WATCHDOG_ENABLED", "0")
        monkeypatch.setenv("FS_SCHEDULED_JOBS_ENABLED", "0")
        monkeypatch.setenv("FS_DASHBOARD_ROLLUP_ENABLED", "0")
        monkeypatch.setenv("FS_SECURITY_POSTURE_ENABLED", "0")
        monkeypatch.setenv("FS_WORKFLOW_KNOWLEDGE_SYNC_ENABLED", "0")
        monkeypatch.setenv("FS_EXTERNAL_EFFECT_MAINTENANCE_ENABLED", "1")
        monkeypatch.setenv("FS_EXTERNAL_EFFECT_MAINTENANCE_INTERVAL_SECONDS", "1")
        cfg = ControlConfig.from_env()
        scan = MagicMock(return_value={"resolved": 0})
        env = dict(os.environ)
        env.pop("PYTEST_CURRENT_TEST", None)
        with patch.dict(os.environ, env, clear=True):
            threads = start_control_threads(
                {"external_effect_maintenance": scan},
                cfg,
                maintenance_active=lambda: True,
            )

        try:
            assert not _poll_until(lambda: scan.call_count >= 1, timeout=0.15)
        finally:
            stop_control_threads(threads)

    def test_dispatch_outbox_loop_starts_when_optional_control_loops_are_disabled(self):
        cfg = ControlConfig(
            watchdog_enabled=False,
            watchdog_interval=0.05,
            watchdog_auto_apply=True,
            watchdog_stuck_seconds=120,
            watchdog_orphan_grace_seconds=120,
            watchdog_pickup_grace_seconds=45,
            watchdog_max_journal_entries=200,
            scheduled_jobs_enabled=False,
            scheduled_jobs_interval=30,
            scheduled_jobs_batch_limit=20,
            dashboard_rollup_enabled=False,
            dashboard_rollup_interval=300,
            dashboard_rollup_backfill_days=180,
            dashboard_rollup_lookback_days=3,
            security_posture_enabled=False,
            security_posture_interval=600,
            workflow_knowledge_sync_enabled=False,
            workflow_knowledge_sync_interval=120,
            workflow_knowledge_sync_changelog_limit=200,
        )
        calls: list[bool] = []
        env = dict(os.environ)
        env.pop("PYTEST_CURRENT_TEST", None)
        with patch.dict(os.environ, env, clear=True):
            threads = start_control_threads(
                {"dispatch_outbox": lambda: calls.append(True) or {}},
                cfg,
            )

        assert set(threads) == {"dispatch_outbox"}
        assert _poll_until(lambda: bool(calls))
        stop_control_threads(threads)

    def test_missing_scan_fn_skipped(self):
        cfg = ControlConfig(
            watchdog_enabled=True,
            watchdog_interval=0.05,
            watchdog_auto_apply=True,
            watchdog_stuck_seconds=120,
            watchdog_orphan_grace_seconds=120,
            watchdog_pickup_grace_seconds=45,
            watchdog_max_journal_entries=200,
            scheduled_jobs_enabled=True,
            scheduled_jobs_interval=0.05,
            scheduled_jobs_batch_limit=20,
            dashboard_rollup_enabled=True,
            dashboard_rollup_interval=0.05,
            dashboard_rollup_backfill_days=180,
            dashboard_rollup_lookback_days=3,
            security_posture_enabled=False,
            security_posture_interval=600,
            workflow_knowledge_sync_enabled=False,
            workflow_knowledge_sync_interval=120,
            workflow_knowledge_sync_changelog_limit=200,
        )
        # Only provide watchdog, not scheduled_jobs
        env = dict(os.environ)
        env.pop("PYTEST_CURRENT_TEST", None)
        with patch.dict(os.environ, env, clear=True):
            threads = start_control_threads({"watchdog": dict}, cfg)
        assert "watchdog" in threads
        assert "scheduled_jobs" not in threads
        stop_control_threads(threads)


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------


class TestCronNextRunAt:
    def test_basic_cron(self):
        from core.infrastructure.runtime.control._cron import cron_next_run_at

        ref = datetime(2026, 3, 23, 10, 0, 0, tzinfo=UTC)
        result = cron_next_run_at("30 * * * *", ref)
        assert result.minute == 30
        assert result >= ref

    def test_daily_at_9(self):
        from core.infrastructure.runtime.control._cron import cron_next_run_at

        ref = datetime(2026, 3, 23, 10, 0, 0, tzinfo=UTC)
        result = cron_next_run_at("0 9 * * *", ref)
        assert result.hour == 9
        assert result.minute == 0
        assert result.day == 24  # next day

    def test_invalid_cron_raises(self):
        from core.infrastructure.runtime.control._cron import cron_next_run_at

        with pytest.raises(ValueError):
            cron_next_run_at("invalid")


# ---------------------------------------------------------------------------
# Watchdog scan (standalone)
# ---------------------------------------------------------------------------


class TestWatchdogScan:
    def _make_cfg(self, **overrides):
        defaults = {
            "watchdog_enabled": True,
            "watchdog_interval": 30,
            "watchdog_auto_apply": True,
            "watchdog_stuck_seconds": 120,
            "watchdog_orphan_grace_seconds": 120,
            "watchdog_pickup_grace_seconds": 45,
            "watchdog_max_journal_entries": 200,
            "scheduled_jobs_enabled": True,
            "scheduled_jobs_interval": 30,
            "scheduled_jobs_batch_limit": 20,
            "dashboard_rollup_enabled": True,
            "dashboard_rollup_interval": 300,
            "dashboard_rollup_backfill_days": 180,
            "dashboard_rollup_lookback_days": 3,
            "security_posture_enabled": False,
            "security_posture_interval": 600,
            "workflow_knowledge_sync_enabled": False,
            "workflow_knowledge_sync_interval": 120,
            "workflow_knowledge_sync_changelog_limit": 200,
        }
        defaults.update(overrides)
        return ControlConfig(**defaults)

    def test_no_active_jobs(self):
        from core.infrastructure.runtime.control.scans import watchdog_scan

        bl = MagicMock()
        bl.get_active_jobs.return_value = []
        bl.list_project_execution_leases.return_value = []
        bl.list_jobs_for_attention.return_value = []
        cfg = self._make_cfg()
        result = watchdog_scan(bl, cfg)
        assert result["active_count"] == 0
        assert result["detection_count"] == 0

    def test_stuck_job_detected(self):
        from core.infrastructure.runtime.control.scans import watchdog_scan

        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_active_jobs.return_value = [
            {
                "id": 1,
                "project_id": 1,
                "status": "in_progress",
                "started_at": now - timedelta(seconds=300),
                "heartbeat_at": now - timedelta(seconds=200),
                "worker_id": "worker-stale",
                "queued_at": None,
            }
        ]
        bl.list_project_execution_leases.return_value = []
        bl.list_jobs_for_attention.return_value = []
        bl.get_job.return_value = {
            "id": 1,
            "project_id": 1,
            "status": "in_progress",
            "started_at": now - timedelta(seconds=300),
            "heartbeat_at": now - timedelta(seconds=200),
            "worker_id": "",
            "meta": {},
        }
        cfg = self._make_cfg(watchdog_stuck_seconds=120)
        result = watchdog_scan(bl, cfg)
        assert result["detection_count"] >= 1
        assert result["applied_count"] >= 1

    def test_healthy_job_not_detected(self):
        from core.infrastructure.runtime.control.scans import watchdog_scan

        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_active_jobs.return_value = [
            {
                "id": 1,
                "project_id": 1,
                "status": "in_progress",
                "started_at": now - timedelta(seconds=30),
                "heartbeat_at": now - timedelta(seconds=5),
                "worker_id": "worker-stale",
                "queued_at": None,
            }
        ]
        bl.list_project_execution_leases.return_value = []
        bl.list_jobs_for_attention.return_value = []
        cfg = self._make_cfg()
        result = watchdog_scan(bl, cfg)
        assert result["detection_count"] == 0

    def test_watchdog_scan_reaps_stale_worker_subprocess_logs(self):
        from core.infrastructure.runtime.control.scans import watchdog_scan

        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_active_jobs.return_value = [
            {
                "id": 1,
                "project_id": 1,
                "status": "in_progress",
                "started_at": now - timedelta(seconds=30),
                "heartbeat_at": now - timedelta(seconds=5),
                "worker_id": "worker-stale",
                "queued_at": None,
            }
        ]
        bl.list_project_execution_leases.return_value = []
        bl.list_jobs_for_attention.return_value = []
        cfg = self._make_cfg()

        with patch(
            "core.infrastructure.runtime.control.scans.reap_stale_worker_subprocesses",
            return_value=1,
        ) as reap:
            result = watchdog_scan(bl, cfg)

        reap.assert_called_once()
        assert reap.call_args.args == (bl,)
        assert reap.call_args.kwargs["job_id"] == "1"
        assert reap.call_args.kwargs["stale_worker_ids"] == {"worker-stale"}
        assert result["reaped_worker_subprocess_count"] == 1

    def test_watchdog_scan_marks_retryable_interrupted_step(self):
        from core.infrastructure.runtime.control.scans import watchdog_scan

        now = datetime.now(UTC)
        snapshot = {
            "agent_id": "writer",
            "skill": "write_section",
            "phase_id": "implementation",
            "step_id": "developer:write_section",
            "correlation_id": "corr-1",
        }
        bl = MagicMock()
        bl.get_active_jobs.return_value = [
            {
                "id": 1,
                "project_id": 1,
                "status": "in_progress",
                "started_at": now - timedelta(seconds=300),
                "heartbeat_at": now - timedelta(seconds=200),
                "worker_id": "worker-999999",
                "thread_id": "thread-1",
                "current_agent": "writer",
                "current_phase": "implementation",
            }
        ]
        bl.list_project_execution_leases.return_value = []
        bl.list_jobs_for_attention.return_value = []
        bl.get_job.return_value = {
            "id": 1,
            "project_id": 1,
            "status": "in_progress",
            "started_at": now - timedelta(seconds=300),
            "heartbeat_at": now - timedelta(seconds=200),
            "worker_id": "worker-999999",
            "thread_id": "thread-1",
            "current_agent": "writer",
            "current_phase": "implementation",
            "meta": {},
        }

        cfg = self._make_cfg(watchdog_stuck_seconds=120)
        with patch(
            "core.infrastructure.runtime.control.scans._detect_retryable_interrupted_step_snapshot",
            return_value=snapshot,
        ):
            result = watchdog_scan(bl, cfg)

        assert result["applied_count"] == 1
        status_calls = [
            call.kwargs["status"]
            for call in bl.update_job.call_args_list
            if "status" in call.kwargs
        ]
        assert "retryable_interrupted_step" in status_calls
        meta_calls = [
            call.kwargs["meta"]
            for call in bl.update_job.call_args_list
            if "meta" in call.kwargs and isinstance(call.kwargs["meta"], dict)
        ]
        assert meta_calls[-1]["retryable_interrupted_step"]["skill"] == "write_section"


# ---------------------------------------------------------------------------
# Scheduled-jobs scan (standalone)
# ---------------------------------------------------------------------------


class TestScheduledJobsScan:
    def _make_cfg(self, **overrides):
        defaults = {
            "watchdog_enabled": True,
            "watchdog_interval": 30,
            "watchdog_auto_apply": True,
            "watchdog_stuck_seconds": 120,
            "watchdog_orphan_grace_seconds": 120,
            "watchdog_pickup_grace_seconds": 45,
            "watchdog_max_journal_entries": 200,
            "scheduled_jobs_enabled": True,
            "scheduled_jobs_interval": 30,
            "scheduled_jobs_batch_limit": 20,
            "dashboard_rollup_enabled": True,
            "dashboard_rollup_interval": 300,
            "dashboard_rollup_backfill_days": 180,
            "dashboard_rollup_lookback_days": 3,
            "security_posture_enabled": False,
            "security_posture_interval": 600,
            "workflow_knowledge_sync_enabled": False,
            "workflow_knowledge_sync_interval": 120,
            "workflow_knowledge_sync_changelog_limit": 200,
        }
        defaults.update(overrides)
        return ControlConfig(**defaults)

    def test_disabled_returns_early(self):
        from core.infrastructure.runtime.control.scans import scheduled_jobs_scan

        bl = MagicMock()
        cfg = self._make_cfg(scheduled_jobs_enabled=False)
        result = scheduled_jobs_scan(bl, cfg)
        assert result.get("disabled") is True

    def test_no_due_jobs(self):
        from core.infrastructure.runtime.control.scans import scheduled_jobs_scan

        bl = MagicMock()
        bl.list_due_scheduled_jobs.return_value = []
        cfg = self._make_cfg()
        result = scheduled_jobs_scan(bl, cfg)
        assert result["checked"] == 0
        assert result["enqueued"] == 0

    def test_enqueue_task_wrapper_preserves_snapshots_and_test_mode(self):
        from core.infrastructure.runtime.control.scans import _enqueue_task_standalone

        bl = MagicMock()
        transaction_conn = object()
        unit_of_work = MagicMock()
        unit_of_work.transaction.return_value.__enter__.return_value = transaction_conn
        bl.job_lifecycle_unit_of_work.return_value = unit_of_work
        submission_repository = MagicMock()
        submission_repository.create.return_value = 77
        fake_queue_module = types.ModuleType("core.infrastructure.runtime.worker.queue_publisher")
        fake_queue_module.enqueue_task = MagicMock()
        project_config = {
            "project_policy": {"autonomy_mode": "supervised"},
            "project_workflows": [{"workflow_id": "wf_demo", "enabled": True, "is_default": True}],
            "provider_token": "secret-token",
        }
        import core.infrastructure.runtime.worker as worker_pkg

        with (
            # Replace only the queue module: patch.dict(sys.modules) would also
            # drop every module first imported inside the block, leaving their
            # packages pointing at copies later tests can no longer patch.
            _only_sys_module(
                "core.infrastructure.runtime.worker.queue_publisher", fake_queue_module
            ),
            patch.object(worker_pkg, "queue_publisher", fake_queue_module, create=True),
            patch(
                "core.application.jobs.job_submission_repository.JobSubmissionRepository",
                return_value=submission_repository,
            ) as repository_cls,
            patch(
                "core.application.notifications.dispatch.dispatch_project_notification_event"
            ) as notify_mock,
        ):
            result = _enqueue_task_standalone(
                bl,
                task_id="scheduled-1",
                task_summary="Summary",
                task_source="schedule",
                project_id=7,
                project_name="demo",
                project_config=project_config,
                workflow_id="wf_demo",
                extra_state={"workflow_id": "wf_demo", "test_mode": True},
                meta={"scheduled_job_id": 5},
            )

        assert result == 77
        repository_cls.assert_called_once_with(bl, transaction_conn)
        create_kwargs = submission_repository.create.call_args.kwargs
        assert create_kwargs["project_id"] == 7
        assert create_kwargs["task_id"] == "scheduled-1"
        assert create_kwargs["status"] == "pending"
        assert create_kwargs["meta"]["scheduled_job_id"] == 5
        assert create_kwargs["meta"]["test_mode"] is True
        assert create_kwargs["meta"]["project_policy_snapshot"] == {"autonomy_mode": "supervised"}
        assert create_kwargs["meta"]["project_config_snapshot"]["provider_token"] == "[REDACTED]"
        runtime_kwargs = bl.update_job_runtime.call_args.kwargs
        assert runtime_kwargs["selected_workflow"] == "wf_demo"
        enqueue_kwargs = fake_queue_module.enqueue_task.call_args.kwargs
        assert enqueue_kwargs["project_id"] == 7
        assert enqueue_kwargs["project_config"] == project_config
        assert enqueue_kwargs["extra_state"]["workflow_id"] == "wf_demo"
        assert enqueue_kwargs["extra_state"]["test_mode"] is True
        assert not notify_mock.called

    def test_run_scheduled_job_once_preserves_validation_and_enqueue_guardrails(self):
        from core.infrastructure.runtime.control.scans import _run_scheduled_job_once

        now = datetime.now(UTC)
        scheduled_job = {
            "id": 9,
            "project_id": 7,
            "account_id": "acc-demo",
            "workflow_id": "wf_demo",
            "cron_expr": "0 * * * *",
            "next_run_at": now,
            "enabled": True,
            "inputs": {"title": "Nightly refresh"},
            "admission_allowed": True,
            "admission_workflow_revision": 1,
            "current_workflow_revision": 1,
        }
        run_cfg = {
            "project_name": "demo",
            "project_policy": {"autonomy_mode": "supervised"},
            "project_workflows": [{"workflow_id": "wf_demo", "enabled": True, "is_default": True}],
            "provider_token": "secret-token",
        }
        bl = MagicMock()
        bl.get_project_run_config.return_value = run_cfg
        with (
            patch(
                "core.infrastructure.runtime.control.scans._enqueue_scheduled_job",
                return_value={"status": "enqueued", "job_id": 88},
            ) as enqueue_mock,
            patch(
                "core.infrastructure.runtime.control.scans_scheduled_jobs._materialize_scheduled_job_launch",
                side_effect=lambda _workflow_id, inputs: (dict(inputs), []),
            ),
        ):
            result = _run_scheduled_job_once(
                bl,
                scheduled_job,
                self._make_cfg(),
                scheduled_for=now,
                advance_schedule=False,
                now_utc=now,
            )

        assert result["status"] == "enqueued"
        assert result["job_id"] == "88"
        bl.get_project_run_config.assert_called_once_with("7")
        enqueue_kwargs = enqueue_mock.call_args.kwargs
        assert enqueue_kwargs["project_config"] == run_cfg
        assert enqueue_kwargs["workflow_id"] == "wf_demo"
        assert enqueue_kwargs["meta"]["scheduled_job_id"] == "9"
        assert enqueue_kwargs["meta"]["workflow_selection_source"] == "schedule_binding"
        assert enqueue_kwargs["extra_state"]["scheduled_run_type"] == "scheduled"
        assert enqueue_kwargs["task_summary"] == "Workflow run: wf_demo"

    def test_run_scheduled_job_once_fails_closed_on_stale_admission(self):
        from core.infrastructure.runtime.control.scans import _run_scheduled_job_once

        now = datetime.now(UTC)
        scheduled_job = {
            "id": 9,
            "project_id": 7,
            "account_id": "acc-demo",
            "workflow_id": "wf_demo",
            "cron_expr": "0 * * * *",
            "next_run_at": now,
            "enabled": True,
            "admission_allowed": True,
            "admission_reason": "validated",
            "admission_workflow_revision": 4,
            "current_workflow_revision": 5,
        }
        bl = MagicMock()
        record = MagicMock()

        with patch(
            "core.infrastructure.runtime.control.scans._record_scheduled_job_outcome",
            record,
        ):
            result = _run_scheduled_job_once(
                bl,
                scheduled_job,
                self._make_cfg(),
                scheduled_for=now,
                advance_schedule=False,
                now_utc=now,
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "workflow_admission_stale"
        bl.get_project_run_config.assert_not_called()
        record.assert_called_once()

    def test_control_scans_module_does_not_import_serve(self):
        source = (
            Path(__file__)
            .resolve()
            .parents[2]
            .joinpath("core/infrastructure/runtime/control/scans.py")
            .read_text(encoding="utf-8")
        )
        assert "import serve" not in source
        assert "from serve import" not in source
        assert "_temporary_serve_scan_config" not in source
        assert "api_runtime." not in source
        sys.modules.pop("core.infrastructure.runtime.control.scans", None)
        sys.modules.pop("serve", None)
        importlib.import_module("core.infrastructure.runtime.control.scans")
        assert "serve" not in sys.modules


class TestDashboardRollupScan:
    def _make_cfg(self, **overrides):
        defaults = {
            "watchdog_enabled": True,
            "watchdog_interval": 30,
            "watchdog_auto_apply": True,
            "watchdog_stuck_seconds": 120,
            "watchdog_orphan_grace_seconds": 120,
            "watchdog_pickup_grace_seconds": 45,
            "watchdog_max_journal_entries": 200,
            "scheduled_jobs_enabled": True,
            "scheduled_jobs_interval": 30,
            "scheduled_jobs_batch_limit": 20,
            "dashboard_rollup_enabled": True,
            "dashboard_rollup_interval": 300,
            "dashboard_rollup_backfill_days": 180,
            "dashboard_rollup_lookback_days": 3,
            "security_posture_enabled": False,
            "security_posture_interval": 600,
            "workflow_knowledge_sync_enabled": False,
            "workflow_knowledge_sync_interval": 120,
            "workflow_knowledge_sync_changelog_limit": 200,
        }
        defaults.update(overrides)
        return ControlConfig(**defaults)

    def test_disabled_returns_early(self):
        from core.infrastructure.runtime.control.scans import dashboard_rollup_scan

        bl = MagicMock()
        cfg = self._make_cfg(dashboard_rollup_enabled=False)
        result = dashboard_rollup_scan(bl, cfg)
        assert result.get("disabled") is True

    def test_calls_logger_refresh(self):
        from core.infrastructure.runtime.control.scans import dashboard_rollup_scan

        bl = MagicMock()
        bl.refresh_dashboard_rollups.return_value = {
            "refreshed_accounts": 1,
            "facts_rows_deleted": 3,
            "facts_rows_inserted": 7,
            "daily_rows_deleted": 1,
            "daily_rows_inserted": 2,
            "details": [{"account_id": "acc_default"}],
        }
        cfg = self._make_cfg()
        result = dashboard_rollup_scan(bl, cfg)
        bl.refresh_dashboard_rollups.assert_called_once()
        assert result["refreshed_accounts"] == 1
        assert result["facts_rows_inserted"] == 7
        assert result["daily_rows_inserted"] == 2
        assert result["details"][0]["account_id"] == "acc_default"


class TestRuntimeTopologyCompose:
    def test_external_effect_scan_rotates_past_failing_first_hundred_tenants(self):
        from core.infrastructure.runtime.control.scans import external_effect_maintenance_scan

        first_page = [f"account-{index:03d}" for index in range(100)]
        second_page = ["account-100"]
        processed: list[str] = []

        def reconcile(_bl, *, account_id: str, **_kwargs):
            processed.append(account_id)
            if account_id == "account-000":
                raise RuntimeError("persistent tenant storage failure")
            return 0

        with (
            patch(
                "core.infrastructure.db.external_effect_store.list_external_effect_maintenance_accounts",
                side_effect=[first_page, second_page],
            ),
            patch(
                "core.application.extensions.external_effects.cleanup_stale_external_effects",
                return_value={},
            ),
            patch(
                "core.application.extensions.external_effects.reconcile_external_effect_outputs",
                side_effect=reconcile,
            ),
        ):
            external_effect_maintenance_scan(
                MagicMock(), ControlConfig.from_env(), artifact_plane=object()
            )
            external_effect_maintenance_scan(
                MagicMock(), ControlConfig.from_env(), artifact_plane=object()
            )

        assert "account-000" in processed
        assert "account-100" in processed

    def test_external_effect_scan_expires_lease_before_reconciling_without_request_traffic(self):
        from core.infrastructure.runtime.control.scans import external_effect_maintenance_scan

        calls: list[str] = []
        with (
            patch(
                "core.infrastructure.db.external_effect_store.list_external_effect_maintenance_accounts",
                return_value=["account-a"],
            ),
            patch(
                "core.application.extensions.external_effects.cleanup_stale_external_effects",
                side_effect=lambda *_args, **_kwargs: calls.append("expire") or {},
            ),
            patch(
                "core.application.extensions.external_effects.reconcile_external_effect_outputs",
                side_effect=lambda *_args, **_kwargs: calls.append("reconcile") or 1,
            ),
        ):
            result = external_effect_maintenance_scan(
                MagicMock(), ControlConfig.from_env(), artifact_plane=object()
            )

        assert calls == ["expire", "reconcile"]
        assert result["resolved"] == 1

    def test_external_effect_scan_isolates_one_tenant_failure(self):
        from core.infrastructure.runtime.control.scans import external_effect_maintenance_scan

        cfg = ControlConfig.from_env()
        rows = [
            {
                "effect_id": "effect-a",
                "account_id": "account-a",
                "claim_token": "claim-a",
                "claim_version": 1,
            },
            {
                "effect_id": "effect-b",
                "account_id": "account-b",
                "claim_token": "claim-b",
                "claim_version": 1,
            },
        ]
        with (
            patch(
                "core.infrastructure.db.external_effect_store.list_external_effect_maintenance_accounts",
                return_value=[row["account_id"] for row in rows],
            ),
            patch(
                "core.application.extensions.external_effects.reconcile_external_effect_outputs",
                side_effect=[RuntimeError("tenant-a storage unavailable"), 1],
            ) as reconcile,
        ):
            result = external_effect_maintenance_scan(MagicMock(), cfg, artifact_plane=object())

        assert reconcile.call_count == 2
        assert result == {"scanned_accounts": 2, "failed_accounts": 1, "resolved": 1}

    def test_control_process_owns_external_effect_reconciliation_with_real_plane(self):
        from core.entrypoints.cli.control_process import _build_control_scan_fns

        stores = {
            "events": MagicMock(),
            "worker": MagicMock(),
            "scheduler": MagicMock(),
            "observability": MagicMock(),
            "projects": MagicMock(),
        }
        plane = object()
        cfg = ControlConfig.from_env()
        with patch(
            "core.infrastructure.runtime.control.scans.external_effect_maintenance_scan",
            create=True,
            return_value={"scanned_accounts": 1, "resolved": 1},
        ) as scan:
            scan_fns = _build_control_scan_fns(stores=stores, cfg=cfg, artifact_plane=plane)
            result = scan_fns["external_effect_maintenance"]()

        assert result == {"scanned_accounts": 1, "resolved": 1}
        scan.assert_called_once_with(stores["projects"], cfg, artifact_plane=plane)

    def test_compose_assigns_control_loop_ownership_explicitly(self):
        import yaml

        compose_text = (
            Path(__file__)
            .resolve()
            .parents[2]
            .joinpath("infra", "docker", "docker-compose.yaml")
            .read_text(encoding="utf-8")
        )

        assert "fs-app:" in compose_text
        assert 'CONTROL_LOOPS_ENABLED: "0"' in compose_text
        assert "control-plane:" in compose_text
        services = yaml.safe_load(compose_text)["services"]
        for service_name in ("fs-app", "fs-extension-installer"):
            assert services[service_name]["environment"]["CONTROL_LOOPS_ENABLED"] == "0"
        assert 'CONTROL_LOOPS_ENABLED: "1"' not in compose_text
        assert 'command: ["python", "-m", "core.entrypoints.cli.control_process"]' in compose_text
        assert compose_text.count("core.entrypoints.cli.control_process") == 1
        assert 'profiles: ["control"]' not in compose_text

    def test_production_control_plane_can_reach_s3_artifact_storage(self):
        import yaml

        compose_path = (
            Path(__file__)
            .resolve()
            .parents[2]
            .joinpath("infra", "docker", "docker-compose.prod.yaml")
        )
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        services = compose["services"]
        control_networks = set(services["control-plane"].get("networks") or [])
        storage_networks = set(services["seaweedfs"].get("networks") or [])

        assert "artifact-storage" in control_networks
        assert control_networks & storage_networks


def test_retention_runs_daily_by_default_and_never_more_than_hourly(monkeypatch) -> None:
    monkeypatch.delenv("FS_RETENTION_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("FS_RETENTION_ENABLED", raising=False)
    cfg = ControlConfig.from_env()
    assert cfg.retention_enabled is True
    assert cfg.retention_interval == 86400

    monkeypatch.setenv("FS_RETENTION_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("FS_RETENTION_ENABLED", "0")
    cfg = ControlConfig.from_env()
    assert cfg.retention_interval == 3600
    assert cfg.retention_enabled is False


def test_retention_loop_runs_a_pass_as_soon_as_the_control_plane_starts() -> None:
    passes = {"count": 0}
    cfg = ControlConfig(
        watchdog_enabled=False,
        watchdog_interval=30,
        watchdog_auto_apply=True,
        watchdog_stuck_seconds=120,
        watchdog_orphan_grace_seconds=120,
        watchdog_pickup_grace_seconds=45,
        watchdog_max_journal_entries=200,
        scheduled_jobs_enabled=False,
        scheduled_jobs_interval=30,
        scheduled_jobs_batch_limit=20,
        dashboard_rollup_enabled=False,
        dashboard_rollup_interval=300,
        dashboard_rollup_backfill_days=180,
        dashboard_rollup_lookback_days=3,
        security_posture_enabled=False,
        security_posture_interval=600,
        workflow_knowledge_sync_enabled=False,
        workflow_knowledge_sync_interval=120,
        workflow_knowledge_sync_changelog_limit=200,
        external_effect_maintenance_enabled=False,
        retention_enabled=True,
        retention_interval=86400,
    )
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    with patch.dict(os.environ, env, clear=True):
        threads = start_control_threads(
            {"retention": lambda: passes.__setitem__("count", passes["count"] + 1)},
            cfg,
        )

    assert "retention" in threads
    assert _poll_until(lambda: passes["count"] == 1)
    stop_control_threads(threads)
