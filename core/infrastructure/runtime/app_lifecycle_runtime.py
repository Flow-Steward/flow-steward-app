# mypy: ignore-errors
"""Reusable app startup and shutdown helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def _has_live_db_runtime_context(bl: Any) -> bool:
    dsn = getattr(bl, "dsn", None)
    return isinstance(dsn, str) and bool(dsn.strip())


def _repair_canonical_guardrails(*, bl, logger, ensure_canonical_guardrails_seeded_fn) -> None:
    dsn = str(getattr(bl, "dsn", "") or "").strip()
    if not dsn:
        return
    try:
        summary = ensure_canonical_guardrails_seeded_fn(dsn=dsn, author="system")
    except Exception as exc:
        logger.warning("Startup canonical guardrail check failed (non-fatal): %s", exc)
        return

    restored = int(summary.get("restored") or 0)
    scope_repaired = int(summary.get("scope_repaired") or 0)
    if restored or scope_repaired:
        logger.warning(
            "Startup restored canonical guardrails (restored=%d scope_repaired=%d expected=%d)",
            restored,
            scope_repaired,
            int(summary.get("expected") or 0),
        )


def _sync_extensions(
    *,
    bl,
    logger,
    default_extensions_root,
    scan_extensions,
    sync_extension_scan,
) -> set[str]:
    ext_root = default_extensions_root()
    scanned = scan_extensions(ext_root)
    if not bl or not hasattr(bl, "sync_extension_scan_records"):
        raise RuntimeError("Startup extension scan synchronization is unavailable")
    sync_extension_scan(bl, scanned)
    logger.info(
        "Startup extension rescan: %d bundle(s) synced from %s",
        len(scanned),
        ext_root,
    )
    return {
        str(item.get("extension_key") or "").strip()
        for item in scanned
        if isinstance(item, dict) and str(item.get("extension_key") or "").strip()
    }


def _reconcile_extension_cleanup(
    *,
    bl,
    logger,
    default_extensions_root,
    discovered_extension_ids,
    extension_install_is_installed_fn,
) -> bool:
    """Finish committed cleanup using database install state as authoritative."""
    try:
        from core.infrastructure.extension_runtime.bundle_cleanup import reconcile_cleanup_journal

        result = reconcile_cleanup_journal(
            extensions_root=Path(default_extensions_root()),
            discard_installed_intents=True,
            discovered_extension_ids=discovered_extension_ids,
            is_installed=lambda extension_id, account_id: extension_install_is_installed_fn(
                bl.conn,
                extension_id=extension_id,
                account_id=account_id,
            ),
        )
        if result.pending:
            logger.warning(
                "Startup extension cleanup has %d pending intent(s): %s",
                result.pending,
                "; ".join(result.errors),
            )
            return False
        if result.completed or result.discarded:
            logger.info(
                "Startup extension cleanup reconciled (completed=%d discarded=%d)",
                result.completed,
                result.discarded,
            )
        return True
    except Exception as exc:
        logger.warning(
            "Startup extension cleanup reconciliation failed: %s",
            exc,
        )
        return False


def _recover_extension_publications(*, bl, logger, default_extensions_root) -> None:
    from core.infrastructure.extension_runtime.publication_recovery import (
        recover_managed_release_publications,
    )

    if not bl or not hasattr(bl, "list_extensions"):
        raise RuntimeError("Startup extension publication recovery is unavailable")
    extensions = bl.list_extensions(enabled_only=False) or []
    recovered = recover_managed_release_publications(
        extensions_root=Path(default_extensions_root()),
        extensions=extensions,
    )
    if recovered:
        logger.info("Startup recovered %d committed extension publication(s)", recovered)


async def startup_app(
    *,
    config: dict[str, Any],
    env_flag,
    get_logger_only,
    init_sentry,
    prewarm_state_reader_graph,
    start_control_threads,
    control_config,
    watchdog_scan_fn,
    scheduled_jobs_scan_fn,
    dashboard_rollup_scan_fn,
    maintenance_active_fn,
    sync_extension_scan,
    ensure_canonical_guardrails_seeded_fn,
    extension_install_is_installed_fn,
    logger,
    external_effect_maintenance_scan_fn=None,
    start_monitoring_fn=None,
) -> dict[str, Any]:
    from core.infrastructure.observability.runtime_logging import (
        configure_runtime_logging,
        install_asyncio_exception_logging,
    )

    await asyncio.sleep(0)
    configure_runtime_logging(component="serve", config=config)
    app_event_loop = asyncio.get_running_loop()
    install_asyncio_exception_logging(app_event_loop)

    from core.infrastructure.db.platform_logger import resolve_observability_dsn
    from core.infrastructure.db.schema_contract import enforce_runtime_schema_contract_dsn

    dsn = resolve_observability_dsn(
        str(config.get("fs_observability_dsn") or config.get("fs_db_dsn") or "")
    )
    enforce_runtime_schema_contract_dsn(dsn, component="serve")

    init_sentry(config=config, component="serve", framework="fastapi")

    bl = get_logger_only()

    if not bl:
        raise RuntimeError("Startup database runtime is unavailable after schema preflight")

    if bl:
        _repair_canonical_guardrails(
            bl=bl,
            logger=logger,
            ensure_canonical_guardrails_seeded_fn=ensure_canonical_guardrails_seeded_fn,
        )

    if _has_live_db_runtime_context(bl):
        try:
            from core.infrastructure.runtime.memory_preflight import enforce_memory_preflight

            enforce_memory_preflight(bl=bl)
        except Exception as exc:
            logger.error("Startup memory backend preflight failed: %s", exc)
            raise
    else:
        logger.info("Startup memory backend preflight skipped without live DB runtime context")

    from core.infrastructure.extension_runtime.extension_runtime import (
        default_extensions_root,
        scan_extensions,
    )
    from core.infrastructure.extension_runtime.lifecycle_lock import (
        extension_lifecycle_lock,
    )

    compact_profile = (
        str(
            config.get("deployment_profile")
            or config.get("fs_deployment_profile")
            or _runtime_env.get("FS_DEPLOYMENT_PROFILE")
            or ""
        ).strip()
        == "compact"
    )
    # AC17-R150: wait, on every profile. This used to wait only on Compact and elsewhere
    # raise at once when anything else held the lock -- another process starting at the
    # same moment, or an extension operation in flight -- so on Scalable the application
    # died at startup on every update and every `config apply`, came back on its restart
    # policy seconds later, and a readiness probe landing in between got HTTP 500. The
    # holders are other reconciliations and extension operations, each bounded; the work
    # under the lock is idempotent, so the process that waits simply finds less to do.
    with extension_lifecycle_lock(connection=getattr(bl, "conn", None), blocking=True) as acquired:
        if not acquired:
            raise RuntimeError("Startup extension lifecycle reconciliation is already in progress")
        if not compact_profile:
            _recover_extension_publications(
                bl=bl,
                logger=logger,
                default_extensions_root=default_extensions_root,
            )
        discovered_extension_ids = _sync_extensions(
            bl=bl,
            logger=logger,
            default_extensions_root=default_extensions_root,
            scan_extensions=scan_extensions,
            sync_extension_scan=sync_extension_scan,
        )
        if not compact_profile and not _reconcile_extension_cleanup(
            bl=bl,
            logger=logger,
            default_extensions_root=default_extensions_root,
            discovered_extension_ids=discovered_extension_ids,
            extension_install_is_installed_fn=extension_install_is_installed_fn,
        ):
            raise RuntimeError("Startup extension cleanup reconciliation did not complete")

    prewarm_state_reader_graph()

    control_threads: dict[str, Any] = {}
    if env_flag("CONTROL_LOOPS_ENABLED", True):
        scan_fns = {
            "watchdog": lambda: watchdog_scan_fn(get_logger_only(), apply=True),
            "scheduled_jobs": lambda: scheduled_jobs_scan_fn(get_logger_only()),
            "dashboard_rollup": lambda: dashboard_rollup_scan_fn(get_logger_only()),
        }
        if callable(external_effect_maintenance_scan_fn):
            scan_fns["external_effect_maintenance"] = lambda: external_effect_maintenance_scan_fn(
                get_logger_only()
            )
        control_threads = start_control_threads(
            scan_fns=scan_fns,
            config=control_config,
            maintenance_active=maintenance_active_fn,
        )
    else:
        logger.info(
            "Control loops disabled by CONTROL_LOOPS_ENABLED=0 — "
            "expecting a dedicated control-plane process"
        )

    monitoring_handle = None
    if start_monitoring_fn is not None:
        try:
            monitoring_handle = start_monitoring_fn()
        except Exception:
            logger.warning("Runtime reporting unavailable; verify monitoring configuration")
    return {
        "app_event_loop": app_event_loop,
        "control_threads": control_threads,
        "monitoring_handle": monitoring_handle,
    }


def build_startup_handler(**kwargs):
    async def _handler() -> dict[str, Any]:
        return await startup_app(**kwargs)

    return _handler


def build_lifespan_handler(
    *,
    startup_handler,
    shutdown_app_fn,
    resume_shutdown_event,
    join_resume_threads,
    stop_control_threads,
    get_control_threads,
    logger,
    resume_join_timeout_seconds: float,
    get_monitoring_handle=lambda: None,
):
    async def _lifespan(_app):
        await startup_handler()
        try:
            yield
        finally:
            try:
                shutdown_app_fn(
                    resume_shutdown_event=resume_shutdown_event,
                    join_resume_threads=join_resume_threads,
                    stop_control_threads=stop_control_threads,
                    control_threads=get_control_threads(),
                    logger=logger,
                    resume_join_timeout_seconds=resume_join_timeout_seconds,
                )
            finally:
                monitoring_handle = get_monitoring_handle()
                if monitoring_handle is not None:
                    monitoring_handle.close()

    return _lifespan


def shutdown_app(
    *,
    resume_shutdown_event,
    join_resume_threads,
    stop_control_threads,
    control_threads,
    logger,
    resume_join_timeout_seconds: float,
) -> None:
    resume_shutdown_event.set()
    lingering = join_resume_threads()
    if lingering:
        logger.warning(
            "Shutdown: %d resume thread(s) still alive after %.1fs",
            lingering,
            resume_join_timeout_seconds,
        )
    stop_control_threads(control_threads)
