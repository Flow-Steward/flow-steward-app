from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from core.infrastructure.runtime import app_lifecycle_runtime


def test_shutdown_app_warns_when_resume_threads_linger() -> None:
    logger = MagicMock()
    resume_shutdown_event = MagicMock()
    stop_control_threads = MagicMock()

    app_lifecycle_runtime.shutdown_app(
        resume_shutdown_event=resume_shutdown_event,
        join_resume_threads=lambda: 2,
        stop_control_threads=stop_control_threads,
        control_threads={"watchdog": ("thread", "event")},
        logger=logger,
        resume_join_timeout_seconds=15.0,
    )

    resume_shutdown_event.set.assert_called_once()
    stop_control_threads.assert_called_once()
    logger.warning.assert_called_once()


def test_startup_app_skips_control_threads_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.infrastructure.db.schema_contract.enforce_runtime_schema_contract_dsn",
        lambda *_args, **_kwargs: None,
    )

    async def _run():
        start_control_threads = MagicMock()
        init_sentry = MagicMock()
        prewarm_state_reader_graph = MagicMock()
        logger = MagicMock()
        fake_bl = MagicMock()
        fake_bl.conn = None
        sync_extension_scan = MagicMock()

        result = await app_lifecycle_runtime.startup_app(
            config={"fs_db_dsn": "postgresql://example"},
            env_flag=lambda _name, default=False: False,
            get_logger_only=lambda: fake_bl,
            init_sentry=init_sentry,
            prewarm_state_reader_graph=prewarm_state_reader_graph,
            start_control_threads=start_control_threads,
            control_config=object(),
            watchdog_scan_fn=MagicMock(),
            scheduled_jobs_scan_fn=MagicMock(),
            dashboard_rollup_scan_fn=MagicMock(),
            maintenance_active_fn=lambda: False,
            sync_extension_scan=sync_extension_scan,
            ensure_canonical_guardrails_seeded_fn=MagicMock(return_value={}),
            extension_install_is_installed_fn=MagicMock(return_value=False),
            logger=logger,
        )

        assert "app_event_loop" in result
        assert result["control_threads"] == {}
        start_control_threads.assert_not_called()

    asyncio.run(_run())


def test_sync_extensions_delegates_scanned_records_to_injected_coordinator() -> None:
    logger = MagicMock()
    bl = MagicMock()
    scanned = [MagicMock(), MagicMock()]
    sync_extension_scan = MagicMock()

    app_lifecycle_runtime._sync_extensions(
        bl=bl,
        logger=logger,
        default_extensions_root=lambda: "/extensions",
        scan_extensions=lambda root: scanned if root == "/extensions" else [],
        sync_extension_scan=sync_extension_scan,
    )

    sync_extension_scan.assert_called_once_with(bl, scanned)
    logger.info.assert_called_once()


def test_compact_app_refreshes_projection_without_writable_recovery(monkeypatch) -> None:
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "compact")
    monkeypatch.setattr(
        "core.infrastructure.db.schema_contract.enforce_runtime_schema_contract_dsn",
        lambda *_args, **_kwargs: None,
    )

    async def _run() -> None:
        recover = MagicMock()
        cleanup = MagicMock(return_value=True)
        sync = MagicMock()
        fake_bl = MagicMock()
        fake_bl.conn = None
        monkeypatch.setattr(app_lifecycle_runtime, "_recover_extension_publications", recover)
        monkeypatch.setattr(app_lifecycle_runtime, "_reconcile_extension_cleanup", cleanup)

        await app_lifecycle_runtime.startup_app(
            config={"fs_db_dsn": "postgresql://example"},
            env_flag=lambda _name, default=False: False,
            get_logger_only=lambda: fake_bl,
            init_sentry=MagicMock(),
            prewarm_state_reader_graph=MagicMock(),
            start_control_threads=MagicMock(),
            control_config=object(),
            watchdog_scan_fn=MagicMock(),
            scheduled_jobs_scan_fn=MagicMock(),
            dashboard_rollup_scan_fn=MagicMock(),
            maintenance_active_fn=lambda: False,
            sync_extension_scan=sync,
            ensure_canonical_guardrails_seeded_fn=MagicMock(return_value={}),
            extension_install_is_installed_fn=MagicMock(return_value=False),
            logger=MagicMock(),
        )

        recover.assert_not_called()
        cleanup.assert_not_called()
        sync.assert_called_once()

    asyncio.run(_run())


def test_startup_app_repairs_canonical_guardrails(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.infrastructure.db.schema_contract.enforce_runtime_schema_contract_dsn",
        lambda *_args, **_kwargs: None,
    )

    async def _run():
        start_control_threads = MagicMock()
        init_sentry = MagicMock()
        prewarm_state_reader_graph = MagicMock()
        logger = MagicMock()
        fake_bl = MagicMock()
        fake_bl.conn = None
        repair = MagicMock()
        repair_guardrails = MagicMock(return_value={})
        sync_extension_scan = MagicMock()

        monkeypatch.setattr(app_lifecycle_runtime, "_repair_canonical_guardrails", repair)

        await app_lifecycle_runtime.startup_app(
            config={"fs_db_dsn": "postgresql://example"},
            env_flag=lambda _name, default=False: default,
            get_logger_only=lambda: fake_bl,
            init_sentry=init_sentry,
            prewarm_state_reader_graph=prewarm_state_reader_graph,
            start_control_threads=start_control_threads,
            control_config=object(),
            watchdog_scan_fn=MagicMock(),
            scheduled_jobs_scan_fn=MagicMock(),
            dashboard_rollup_scan_fn=MagicMock(),
            maintenance_active_fn=lambda: False,
            sync_extension_scan=sync_extension_scan,
            ensure_canonical_guardrails_seeded_fn=repair_guardrails,
            extension_install_is_installed_fn=MagicMock(return_value=False),
            logger=logger,
        )

        repair.assert_called_once_with(
            bl=fake_bl,
            logger=logger,
            ensure_canonical_guardrails_seeded_fn=repair_guardrails,
        )

    asyncio.run(_run())


def test_startup_app_wires_watchdog_and_external_effect_maintenance_scans(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.infrastructure.db.schema_contract.enforce_runtime_schema_contract_dsn",
        lambda *_args, **_kwargs: None,
    )

    async def _run():
        start_control_threads = MagicMock(return_value={})
        watchdog = MagicMock(return_value={"detection_count": 0})
        external_effect_maintenance = MagicMock(return_value={"resolved": 0})
        fake_bl = MagicMock()
        fake_bl.conn = None

        await app_lifecycle_runtime.startup_app(
            config={"fs_db_dsn": "postgresql://example"},
            env_flag=lambda _name, default=False: default,
            get_logger_only=lambda: fake_bl,
            init_sentry=MagicMock(),
            prewarm_state_reader_graph=MagicMock(),
            start_control_threads=start_control_threads,
            control_config=object(),
            watchdog_scan_fn=watchdog,
            scheduled_jobs_scan_fn=MagicMock(),
            dashboard_rollup_scan_fn=MagicMock(),
            external_effect_maintenance_scan_fn=external_effect_maintenance,
            maintenance_active_fn=lambda: False,
            sync_extension_scan=MagicMock(),
            ensure_canonical_guardrails_seeded_fn=MagicMock(return_value={}),
            extension_install_is_installed_fn=MagicMock(return_value=False),
            logger=MagicMock(),
        )

        scan_fns = start_control_threads.call_args.kwargs["scan_fns"]
        scan_fns["watchdog"]()
        scan_fns["external_effect_maintenance"]()
        watchdog.assert_called_once_with(fake_bl, apply=True)
        external_effect_maintenance.assert_called_once_with(fake_bl)

    asyncio.run(_run())
