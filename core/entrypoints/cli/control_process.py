"""Control-plane process entry point.

Runs watchdog and scheduled-job loops independently of the API and workers.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

# Must stay the first import: fixes the OTEL_* environment before libraries read it.
import core.infrastructure.observability.otel_bootstrap  # noqa: F401  # isort: split
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger("core.entrypoints.cli.control_process")


#: Set by :func:`main` before modules register; ``None`` (nothing entitled) until then.
_CONTROL_ENTITLEMENT_SERVICE = None
#: The ``schedule`` port's scheduler; set while modules register, started by :func:`main`.
_CONTROL_MODULE_JOBS = None


def _log_platform_module_failures(failures: list[dict[str, str]]) -> None:
    """Say once, at start, what a partly loaded module set means for this role."""
    if not failures:
        return
    logger.warning(
        "Control role runs without %d platform module(s) that failed to load (%s); "
        "entitlement answers that only they could give stay unknown, so retention "
        "keeps the approval journal rows of accounts with a journal retention until "
        "the modules load",
        len(failures),
        ", ".join(f"{item.get('id')} {item.get('version')}" for item in failures),
    )


def _register_control_platform_modules(config, dsn: str) -> list[dict[str, str]]:
    """Load the control hooks of platform modules with the control role's ports (ADR 0082).

    The role's own entitlement decider, which only the module that declares
    ``entitlement_source`` registers into and which the retention pass asks whether
    an account keeps approval journal rows past their jobs; a ``db`` factory for the
    role's connection store on ``dsn``; the auth audit writer, Core's secret codec
    and the external sign-in revocation helpers (:func:`_control_module_ports`);
    ``schedule``, which runs a module's periodic jobs on the control loop
    (``core/infrastructure/platform_modules/module_jobs.py``); and the installation
    id. The decider and the scheduler are sealed afterwards. Returns the failure
    record of each module that failed to load (ADR 0082 §8); the control role
    carries them in every heartbeat.
    """
    from core.application.jobs.submission_admission import public_submission_pause_active
    from core.application.lifecycle.runtime_installation_context import (
        projected_installation_id,
    )
    from core.application.settings.platform_contracts import CoreEntitlementService
    from core.infrastructure.platform_modules.loader import register_platform_modules_for_role
    from core.infrastructure.platform_modules.module_jobs import (
        AdvisoryJobLease,
        ModuleJobScheduler,
        active_account_lister,
        postgres_connector,
    )
    from core.infrastructure.runtime.entrypoint_db_adapters import module_db_factory
    from core.infrastructure.security.secret_backends import SecretBackendPort

    global _CONTROL_ENTITLEMENT_SERVICE, _CONTROL_MODULE_JOBS
    _CONTROL_ENTITLEMENT_SERVICE = CoreEntitlementService()
    # Where a module registers its secret backends; sealed with the decider.
    secret_backends = SecretBackendPort()
    ports = {
        "entitlement_service": _CONTROL_ENTITLEMENT_SERVICE,
        "secret_backends": secret_backends,
        "db": module_db_factory(dsn),
        **_control_module_ports(dsn),
    }
    connect = postgres_connector(dsn)
    _CONTROL_MODULE_JOBS = ModuleJobScheduler(
        services=ports,
        lease=AdvisoryJobLease(connect, logger=logger) if connect is not None else None,
        accounts=active_account_lister(connect),
        maintenance_active=public_submission_pause_active,
        logger=logger,
    )
    module_failures: list[dict[str, str]] = []
    loaded_modules = register_platform_modules_for_role(
        "control",
        config=config,
        logger=logger,
        services={**ports, "schedule": _CONTROL_MODULE_JOBS},
        failures=module_failures,
        installation_id=projected_installation_id(dict(_runtime_env)),
    )
    _CONTROL_ENTITLEMENT_SERVICE.seal()
    secret_backends.seal()
    _CONTROL_MODULE_JOBS.seal(
        {module.module_id: module.version for module in loaded_modules},
        {module.module_id: module.requires for module in loaded_modules},
    )
    _log_platform_module_failures(module_failures)
    if loaded_modules or module_failures:
        from core.infrastructure.platform_modules.role_status import (
            module_status_records,
            write_role_module_status,
        )

        # Read by the release workflow after a module change (ADR 0082 §5).
        write_role_module_status(
            "control", loaded=module_status_records(loaded_modules), failures=module_failures
        )
    return module_failures


def _control_module_ports(dsn: str) -> dict[str, object]:
    """The control role's ports for platform modules beyond the decider and ``db``.

    ``log_auth_audit_event(bl=None, *, action, username="", actor="", status="ok",
    detail=None)`` writes the same ``auth_<action>`` audit event as the app role's
    ``auth_helpers["log_auth_audit_event"]``; without ``bl`` it writes through the
    role's own identity store. ``encrypt_secret``, ``decrypt_secret`` and
    ``reseal_legacy_secrets`` are Core's secret codec bound to each module by the
    loader, like ``container.external_sign_in`` in the app role: a module decrypts
    only values it, or a module it requires, encrypted (ADR 0082 §12; an empty value
    decrypts to ``""``). ``external_sign_in_revocation`` revokes what external
    sign-in issued, inside the caller's transaction (ADR 0083).
    """
    from core.application.auth.audit import log_auth_audit_event
    from core.application.auth.external_session_revocation import ExternalSignInRevocationPort
    from core.infrastructure.runtime.entrypoint_db_adapters import create_identity_store
    from core.infrastructure.security.module_secret_codec import ModuleSecretPort

    audit_store: list[object] = []

    def write_auth_audit_event(
        bl=None,
        *,
        action: str,
        username: str = "",
        actor: str = "",
        status: str = "ok",
        detail: dict | None = None,
    ) -> None:
        store = bl
        if store is None:
            if not dsn:
                return
            if not audit_store:
                audit_store.append(create_identity_store(dsn))
            store = audit_store[0]
        log_auth_audit_event(
            store,
            action=action,
            username=username,
            actor=actor,
            status=status,
            detail=dict(detail or {}),
        )

    return {
        "log_auth_audit_event": write_auth_audit_event,
        "encrypt_secret": ModuleSecretPort("encrypt"),
        "decrypt_secret": ModuleSecretPort("decrypt"),
        "reseal_legacy_secrets": ModuleSecretPort("reseal_legacy_secrets"),
        "external_sign_in_revocation": ExternalSignInRevocationPort(),
    }


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
            "agent_expiry_reminders": True,
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
    from core.infrastructure.config.session_identity import set_session_role

    set_session_role("control")

    config: dict = {}
    metrics_server = None
    stores = None
    status_runtime = None
    status_publisher = None
    try:
        from core.infrastructure.config.runtime_config import CONFIG
        from core.infrastructure.observability.otel_runtime import init_telemetry
        from core.infrastructure.observability.runtime_logging import configure_runtime_logging
        from core.infrastructure.observability.sentry import init_sentry

        config = CONFIG
        configure_runtime_logging(component="control", config=CONFIG)
        init_sentry(config=CONFIG, component="control")
        init_telemetry(role="control")
    except Exception as exc:
        logger.warning("Control-plane logging bootstrap failed (non-fatal): %s", exc)

    from core.infrastructure.runtime.entrypoint_db_adapters import (
        enforce_runtime_schema,
        resolve_platform_dsn,
    )

    dsn = resolve_platform_dsn(
        str(config.get("fs_observability_dsn") or config.get("fs_db_dsn") or "")
    )
    module_failures = _register_control_platform_modules(config, dsn)
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
    _register_installation_metrics(dsn)

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

    from core.application.auth.agent_connection_reminders import REMINDER_SCAN_INTERVAL_SECONDS
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
            "agent_expiry_reminders": REMINDER_SCAN_INTERVAL_SECONDS,
        },
    )
    module_jobs = _CONTROL_MODULE_JOBS
    if module_jobs is not None:
        jobs_thread = module_jobs.start()
        if jobs_thread is not None:
            threads["platform_module_jobs"] = (jobs_thread, module_jobs.stop_event)
    from core.application.monitoring.runtime_composition import start_optional_process_reporting

    # Every report names the modules this role skipped and the modules whose jobs
    # are failing, so administrators see them on the monitoring page (ADR 0082 §5).
    status_publisher = start_optional_process_reporting(
        runtime_values=dict(_runtime_env),
        service_type="control",
        platform_module_failures=lambda: [
            *module_failures,
            *(module_jobs.failures() if module_jobs is not None else ()),
        ],
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
    if module_jobs is not None:
        module_jobs.stop()
    stop_control_threads(threads)
    if status_runtime is not None:
        status_runtime.close()
    if status_publisher is not None:
        status_publisher.close()
    if metrics_server is not None:
        metrics_server.close()
    from core.infrastructure.observability.otel_runtime import shutdown_telemetry

    shutdown_telemetry()
    logger.info("Control-plane stopped")


def _register_installation_metrics(dsn: str) -> None:
    """Export installation metrics over OTLP; the control plane is their only owner."""
    try:
        from core.infrastructure.observability.otel_installation_metrics import (
            register_installation_metrics,
        )
        from core.infrastructure.runtime.entrypoint_db_adapters import create_observability_store
        from core.infrastructure.runtime.worker.queue_broker import get_ready_queue_depth

        register_installation_metrics(
            store_factory=lambda: create_observability_store(dsn),
            get_ready_queue_depth=get_ready_queue_depth,
        )
    except Exception as exc:
        logger.warning("Installation metrics unavailable: %s", type(exc).__name__)


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
        "agent_expiry_reminders": lambda: _run_agent_expiry_reminders(stores["connection"]),
    }


def _run_agent_expiry_reminders(bl) -> dict[str, int]:
    from core.application.auth.agent_connection_reminders import run_agent_expiry_reminders

    return run_agent_expiry_reminders(bl)


def _run_retention_pass() -> dict[str, int]:
    from core.application.tooling.ops.retention_cleanup import run_scheduled_retention

    return run_scheduled_retention(entitlement_service=_CONTROL_ENTITLEMENT_SERVICE)


if __name__ == "__main__":
    main()
