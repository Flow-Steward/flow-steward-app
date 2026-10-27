"""Control-plane process entry point.

Runs watchdog and scheduled-job loops independently of the API and workers.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger("core.entrypoints.cli.control_process")


def _repair_canonical_guardrails(*, bl) -> None:
    dsn = str(getattr(bl, "dsn", "") or "").strip()
    if not dsn:
        return
    try:
        from core.application.tooling.db.bootstrap_seed import ensure_canonical_guardrails_seeded

        summary = ensure_canonical_guardrails_seeded(dsn=dsn, author="system")
    except Exception as exc:
        logger.warning("Control canonical guardrail check failed (non-fatal): %s", exc)
        return

    restored = int(summary.get("restored") or 0)
    scope_repaired = int(summary.get("scope_repaired") or 0)
    if restored or scope_repaired:
        logger.warning(
            "Control restored canonical guardrails (restored=%d scope_repaired=%d expected=%d)",
            restored,
            scope_repaired,
            int(summary.get("expected") or 0),
        )


def _control_process_metrics_lines(
    *, cfg, threads_resolver, posture_status_resolver=None
) -> list[str]:
    from core.infrastructure.observability.process_metrics_runtime import (
        build_control_process_metrics_lines,
    )

    return build_control_process_metrics_lines(
        loop_flags={
            "dispatch_outbox": True,
            "workflow_timer": True,
            "workflow_child": True,
            "invoke_child_cancel": True,
            "external_effect_maintenance": bool(cfg.external_effect_maintenance_enabled),
            "watchdog": bool(cfg.watchdog_enabled),
            "scheduled_jobs": bool(cfg.scheduled_jobs_enabled),
            "dashboard_rollup": bool(cfg.dashboard_rollup_enabled),
            "security_posture": bool(cfg.security_posture_enabled),
            "workflow_knowledge_sync": bool(cfg.workflow_knowledge_sync_enabled),
            "retention": bool(cfg.retention_enabled),
        },
        threads_resolver=threads_resolver,
        security_posture_status_resolver=posture_status_resolver,
    )


def main() -> None:
    from core.application.jobs.submission_admission import public_submission_pause_active

    config: dict = {}
    metrics_server = None
    stores = None
    status_runtime = None
    status_publisher = None
    try:
        from core.infrastructure.config.runtime_config import CONFIG
        from core.infrastructure.observability.runtime_logging import configure_runtime_logging
        from core.infrastructure.observability.sentry import init_sentry

        config = CONFIG
        configure_runtime_logging(component="control", config=CONFIG)
        init_sentry(config=CONFIG, component="control")
    except Exception as exc:
        logger.warning("Control-plane logging bootstrap failed (non-fatal): %s", exc)

    from core.infrastructure.runtime.entrypoint_db_adapters import (
        enforce_runtime_schema,
        resolve_platform_dsn,
    )

    dsn = resolve_platform_dsn(
        str(config.get("fs_observability_dsn") or config.get("fs_db_dsn") or "")
    )
    enforce_runtime_schema(dsn, component="control")

    from core.infrastructure.runtime.control.config import ControlConfig
    from core.infrastructure.runtime.control.loops import (
        start_control_threads,
        stop_control_threads,
    )

    cfg = ControlConfig.from_env()
    threads: dict[str, tuple[threading.Thread, threading.Event]] = {}
    logger.info(
        "Control-plane starting (watchdog=%s scheduled_jobs=%s dashboard_rollup=%s security_posture=%s workflow_knowledge_sync=%s)",
        cfg.watchdog_enabled,
        cfg.scheduled_jobs_enabled,
        cfg.dashboard_rollup_enabled,
        cfg.security_posture_enabled,
        cfg.workflow_knowledge_sync_enabled,
    )

    try:
        from core.infrastructure.observability.process_metrics_runtime import (
            start_process_metrics_server,
        )

        metrics_server = start_process_metrics_server(
            component="control",
            config=config,
            logger=logger,
            port_key="control_metrics_port",
            port_env_name="FS_CONTROL_METRICS_PORT",
            extra_metrics_fn=lambda: _control_process_metrics_lines(
                cfg=cfg,
                threads_resolver=lambda: threads,
                posture_status_resolver=lambda: (
                    stores["projects"].get_security_posture_loop_status() if stores else {}
                ),
            ),
        )
    except Exception as exc:
        logger.warning("Control-plane metrics exporter failed to start (non-fatal): %s", exc)

    stores = _create_stores(dsn)
    if not stores:
        logger.error("Cannot create Flow Steward logger — FS_DB_DSN not set?")
        if metrics_server is not None:
            metrics_server.close()
        sys.exit(1)
    _repair_canonical_guardrails(bl=stores["projects"])

    from core.application.monitoring.runtime_composition import start_configured_status_consumer

    try:
        status_runtime = start_configured_status_consumer(
            dsn=dsn, runtime_values=dict(_runtime_env)
        )
    except Exception:
        # A monitoring configuration/dependency failure must not change job admission.
        # Do not expose credential-bearing driver errors or untrusted configuration.
        logger.error(
            "Runtime status receiver unavailable; verify installation scope and monitoring configuration"
        )

    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import create_storage_adapter
    from core.infrastructure.runtime.artifact_storage_factory import build_artifact_storage

    artifact_plane = build_artifact_storage(
        environ=dict(_runtime_env),
        create_storage_adapter=create_storage_adapter,
        artifact_plane_factory=ArtifactDatasetPlane,
    )
    scan_fns = _build_control_scan_fns(
        stores=stores,
        cfg=cfg,
        artifact_plane=artifact_plane,
    )

    from core.application.jobs.workflow_timer_dispatcher import (
        WORKFLOW_TIMER_SCAN_INTERVAL_SECONDS,
    )

    threads = start_control_threads(
        scan_fns,
        cfg,
        maintenance_active=public_submission_pause_active,
        scan_intervals={
            "workflow_timer": WORKFLOW_TIMER_SCAN_INTERVAL_SECONDS,
            "workflow_child": WORKFLOW_TIMER_SCAN_INTERVAL_SECONDS,
        },
    )
    from core.application.monitoring.runtime_composition import start_optional_process_reporting

    status_publisher = start_optional_process_reporting(
        runtime_values=dict(_runtime_env), service_type="control"
    )
    if not threads and status_runtime is None:
        logger.warning(
            "No control threads started — check FS_WATCHDOG_ENABLED / FS_SCHEDULED_JOBS_ENABLED"
        )
        if metrics_server is not None:
            metrics_server.close()
        if status_publisher is not None:
            status_publisher.close()
        sys.exit(0)

    logger.info("Control-plane running (%d thread(s))", len(threads))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()

    logger.info("Control-plane shutting down …")
    stop_control_threads(threads)
    if status_runtime is not None:
        status_runtime.close()
    if status_publisher is not None:
        status_publisher.close()
    if metrics_server is not None:
        metrics_server.close()
    logger.info("Control-plane stopped")


def _create_stores(dsn: str | None = None):
    """Create responsibility-owned stores for independent control loops."""
    resolved_dsn = str(dsn or _runtime_env.get("FS_DB_DSN", "")).strip()
    if not resolved_dsn:
        return None
    try:
        from core.infrastructure.runtime.entrypoint_db_adapters import create_control_stores

        return create_control_stores(resolved_dsn)
    except Exception as exc:
        logger.exception("Failed to create Flow Steward logger: %s", exc)
        return None


def _build_control_scan_fns(*, stores, cfg, artifact_plane):
    """Compose all dedicated control-process scans from responsibility-owned ports."""
    from core.infrastructure.runtime.control import scans

    return {
        "dispatch_outbox": lambda: scans.dispatch_outbox_scan(stores["events"], cfg),
        "workflow_timer": lambda: scans.workflow_timer_scan(stores["worker"], cfg),
        "workflow_child": lambda: scans.workflow_child_scan(stores["worker"], cfg),
        "invoke_child_cancel": lambda: scans.invoke_child_cancel_scan(stores["worker"], cfg),
        "external_effect_maintenance": lambda: scans.external_effect_maintenance_scan(
            stores["projects"], cfg, artifact_plane=artifact_plane
        ),
        "watchdog": lambda: scans.watchdog_scan(stores["worker"], cfg),
        "scheduled_jobs": lambda: scans.scheduled_jobs_scan(stores["scheduler"], cfg),
        "dashboard_rollup": lambda: scans.dashboard_rollup_scan(stores["observability"], cfg),
        "security_posture": lambda: scans.security_posture_scan(stores["projects"], cfg),
        "workflow_knowledge_sync": lambda: scans.workflow_knowledge_sync_scan(
            stores["projects"], stores["events"], cfg
        ),
        "retention": _run_retention_pass,
    }


def _run_retention_pass() -> dict[str, int]:
    from core.application.tooling.ops.retention_cleanup import run_scheduled_retention

    return run_scheduled_retention()


if __name__ == "__main__":
    main()
