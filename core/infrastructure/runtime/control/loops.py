"""Generic loop runners for control-plane background threads.

Each runner accepts a *scan callback* so the actual business logic (DB queries,
status updates, queue publishing) can live in a dedicated runtime module
without coupling the loop runner to transport code.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime.control.config import ControlConfig

logger = logging.getLogger(__name__)

# Type alias for scan callbacks: no args, returns a summary dict.
ScanFn = Callable[[], dict]


def _maintenance_inactive() -> bool:
    return False


# ---------------------------------------------------------------------------
# Individual loop runners
# ---------------------------------------------------------------------------


def run_watchdog_loop(
    stop_event: threading.Event,
    interval: float,
    scan_fn: ScanFn,
    maintenance_active: Callable[[], bool] = _maintenance_inactive,
) -> None:
    """Periodic watchdog scan loop; marks stale jobs when auto-apply is enabled."""
    logger.info("Watchdog loop started (interval=%ss)", interval)
    while not stop_event.is_set():
        if not maintenance_active():
            try:
                scan = scan_fn()
                if int((scan or {}).get("detection_count") or 0) > 0:
                    logger.warning(
                        "Watchdog detected %s unhealthy jobs (%s applied)",
                        scan.get("detection_count"),
                        scan.get("applied_count"),
                    )
            except Exception as e:
                logger.warning("Watchdog loop iteration failed: %s", e)
        if stop_event.wait(interval):
            break
    logger.info("Watchdog loop stopped")


def run_scheduled_jobs_loop(
    stop_event: threading.Event,
    interval: float,
    scan_fn: ScanFn,
    maintenance_active: Callable[[], bool] = _maintenance_inactive,
) -> None:
    """Periodic scheduled-jobs scan loop; enqueues due jobs."""
    logger.info("Scheduled jobs loop started (interval=%ss)", interval)
    while not stop_event.is_set():
        if not maintenance_active():
            try:
                scan = scan_fn()
                enqueued = int((scan or {}).get("enqueued") or 0)
                if enqueued > 0:
                    logger.info("Scheduled jobs loop enqueued %d job(s)", enqueued)
            except Exception as e:
                logger.warning("Scheduled jobs loop iteration failed: %s", e)
        if stop_event.wait(interval):
            break
    logger.info("Scheduled jobs loop stopped")


def run_dashboard_rollup_loop(
    stop_event: threading.Event,
    interval: float,
    scan_fn: ScanFn,
    maintenance_active: Callable[[], bool] = _maintenance_inactive,
) -> None:
    """Periodic dashboard rollup scan loop; refreshes facts/aggregates."""
    logger.info("Dashboard rollup loop started (interval=%ss)", interval)
    while not stop_event.is_set():
        if not maintenance_active():
            try:
                scan = scan_fn()
                refreshed = int((scan or {}).get("refreshed_accounts") or 0)
                if refreshed > 0:
                    logger.info("Dashboard rollup loop refreshed %d account(s)", refreshed)
            except Exception as e:
                logger.warning("Dashboard rollup loop iteration failed: %s", e)
        if stop_event.wait(interval):
            break
    logger.info("Dashboard rollup loop stopped")


def _run_generic_loop(
    stop_event: threading.Event,
    interval: float,
    scan_fn: ScanFn,
    label: str,
    maintenance_active: Callable[[], bool] = _maintenance_inactive,
) -> None:
    """Generic periodic scan loop."""
    logger.info("%s loop started (interval=%ss)", label, interval)
    while not stop_event.is_set():
        if not maintenance_active():
            try:
                scan_fn()
            except Exception as e:
                logger.exception("%s loop iteration failed: %s", label, e)
        if stop_event.wait(interval):
            break
    logger.info("%s loop stopped", label)


# ---------------------------------------------------------------------------
# Thread lifecycle helpers
# ---------------------------------------------------------------------------


def start_control_threads(
    scan_fns: dict[str, ScanFn],
    config: ControlConfig,
    *,
    maintenance_active: Callable[[], bool] = _maintenance_inactive,
    scan_intervals: Mapping[str, float] | None = None,
) -> dict[str, tuple[threading.Thread, threading.Event]]:
    """Start enabled control loops as daemon threads.

    *scan_fns* maps loop names to zero-arg callables that perform one scan
    iteration and return a summary dict. *scan_intervals* carries the cadence
    of loops whose interval is application policy (the workflow timer and
    child loops), so the composition root injects it instead of this adapter
    importing it; a loop without an injected interval uses the watchdog one.

    Returns ``{name: (thread, stop_event)}`` for each started loop.
    """
    if _runtime_env.get("PYTEST_CURRENT_TEST"):
        logger.debug("Control threads skipped under pytest")
        return {}

    started: dict[str, tuple[threading.Thread, threading.Event]] = {}
    intervals = dict(scan_intervals or {})

    if config.external_effect_maintenance_enabled and "external_effect_maintenance" in scan_fns:
        stop = threading.Event()
        t = threading.Thread(
            target=_run_generic_loop,
            args=(
                stop,
                config.external_effect_maintenance_interval,
                scan_fns["external_effect_maintenance"],
                "external-effect-maintenance",
                maintenance_active,
            ),
            name="control-external-effect-maintenance",
            daemon=True,
        )
        t.start()
        started["external_effect_maintenance"] = (t, stop)
    elif not config.external_effect_maintenance_enabled:
        logger.info(
            "External-effect maintenance loop disabled by FS_EXTERNAL_EFFECT_MAINTENANCE_ENABLED=0"
        )

    if config.retention_enabled and "retention" in scan_fns:
        stop = threading.Event()
        t = threading.Thread(
            target=_run_generic_loop,
            args=(
                stop,
                config.retention_interval,
                scan_fns["retention"],
                "retention",
                maintenance_active,
            ),
            name="control-retention",
            daemon=True,
        )
        t.start()
        started["retention"] = (t, stop)
    elif not config.retention_enabled:
        logger.info("Retention loop disabled by FS_RETENTION_ENABLED=0")

    if "dispatch_outbox" in scan_fns:
        stop = threading.Event()
        t = threading.Thread(
            target=_run_generic_loop,
            args=(
                stop,
                config.watchdog_interval,
                scan_fns["dispatch_outbox"],
                "dispatch-outbox",
                maintenance_active,
            ),
            name="control-dispatch-outbox",
            daemon=True,
        )
        t.start()
        started["dispatch_outbox"] = (t, stop)

    if "workflow_timer" in scan_fns:
        stop = threading.Event()
        t = threading.Thread(
            target=_run_generic_loop,
            args=(
                stop,
                intervals.get("workflow_timer", config.watchdog_interval),
                scan_fns["workflow_timer"],
                "workflow-timer",
                maintenance_active,
            ),
            name="control-workflow-timer",
            daemon=True,
        )
        t.start()
        started["workflow_timer"] = (t, stop)

    if "workflow_child" in scan_fns:
        stop = threading.Event()
        t = threading.Thread(
            target=_run_generic_loop,
            args=(
                stop,
                intervals.get("workflow_child", config.watchdog_interval),
                scan_fns["workflow_child"],
                "workflow-child",
                maintenance_active,
            ),
            name="control-workflow-child",
            daemon=True,
        )
        t.start()
        started["workflow_child"] = (t, stop)

    if "invoke_child_cancel" in scan_fns:
        stop = threading.Event()
        t = threading.Thread(
            target=_run_generic_loop,
            args=(
                stop,
                config.watchdog_interval,
                scan_fns["invoke_child_cancel"],
                "invoke-child-cancel",
                maintenance_active,
            ),
            name="control-invoke-child-cancel",
            daemon=True,
        )
        t.start()
        started["invoke_child_cancel"] = (t, stop)

    if config.watchdog_enabled and "watchdog" in scan_fns:
        stop = threading.Event()
        t = threading.Thread(
            target=run_watchdog_loop,
            args=(stop, config.watchdog_interval, scan_fns["watchdog"], maintenance_active),
            name="control-watchdog",
            daemon=True,
        )
        t.start()
        started["watchdog"] = (t, stop)
    elif not config.watchdog_enabled:
        logger.info("Watchdog loop disabled by FS_WATCHDOG_ENABLED=0")

    if config.scheduled_jobs_enabled and "scheduled_jobs" in scan_fns:
        stop = threading.Event()
        t = threading.Thread(
            target=run_scheduled_jobs_loop,
            args=(
                stop,
                config.scheduled_jobs_interval,
                scan_fns["scheduled_jobs"],
                maintenance_active,
            ),
            name="control-scheduled-jobs",
            daemon=True,
        )
        t.start()
        started["scheduled_jobs"] = (t, stop)
    elif not config.scheduled_jobs_enabled:
        logger.info("Scheduled jobs loop disabled by FS_SCHEDULED_JOBS_ENABLED=0")

    if config.dashboard_rollup_enabled and "dashboard_rollup" in scan_fns:
        stop = threading.Event()
        t = threading.Thread(
            target=run_dashboard_rollup_loop,
            args=(
                stop,
                config.dashboard_rollup_interval,
                scan_fns["dashboard_rollup"],
                maintenance_active,
            ),
            name="control-dashboard-rollup",
            daemon=True,
        )
        t.start()
        started["dashboard_rollup"] = (t, stop)
    elif not config.dashboard_rollup_enabled:
        logger.info("Dashboard rollup loop disabled by FS_DASHBOARD_ROLLUP_ENABLED=0")

    if config.security_posture_enabled and "security_posture" in scan_fns:
        stop = threading.Event()
        t = threading.Thread(
            target=_run_generic_loop,
            args=(
                stop,
                config.security_posture_interval,
                scan_fns["security_posture"],
                "security-posture",
                maintenance_active,
            ),
            name="control-security-posture",
            daemon=True,
        )
        t.start()
        started["security_posture"] = (t, stop)
    elif not config.security_posture_enabled:
        logger.info("Security posture loop disabled by FS_SECURITY_POSTURE_ENABLED=0")

    if config.workflow_knowledge_sync_enabled and "workflow_knowledge_sync" in scan_fns:
        stop = threading.Event()
        t = threading.Thread(
            target=_run_generic_loop,
            args=(
                stop,
                config.workflow_knowledge_sync_interval,
                scan_fns["workflow_knowledge_sync"],
                "workflow-knowledge-sync",
                maintenance_active,
            ),
            name="control-workflow-knowledge-sync",
            daemon=True,
        )
        t.start()
        started["workflow_knowledge_sync"] = (t, stop)
    elif not config.workflow_knowledge_sync_enabled:
        logger.info("Workflow knowledge sync loop disabled by FS_WORKFLOW_KNOWLEDGE_SYNC_ENABLED=0")

    return started


def stop_control_threads(
    threads: dict[str, tuple[threading.Thread, threading.Event]],
    timeout: float = 5.0,
) -> None:
    """Signal all control threads to stop and join them."""
    timeout = max(0.5, float(timeout))
    for _name, (_thread, stop_event) in threads.items():
        stop_event.set()
    for name, (thread, _stop_event) in threads.items():
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning("Control thread '%s' did not stop within %.1fs", name, timeout)
