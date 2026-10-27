from __future__ import annotations

from unittest.mock import MagicMock

from core.infrastructure.observability import logger_runtime


def test_get_logger_singleton_retries_and_reuses_singleton() -> None:
    fresh_logger = MagicMock()
    fresh_logger.conn.cursor.return_value.__enter__.return_value = MagicMock()
    create_calls = {"count": 0}

    def _create(_dsn: str):
        create_calls["count"] += 1
        if create_calls["count"] == 1:
            raise RuntimeError("temporary db outage")
        return fresh_logger

    result, singleton, singleton_dsn = logger_runtime.get_logger_singleton(
        config={"fs_db_dsn": "postgresql://example"},
        singleton=None,
        singleton_dsn="",
        create_logger_fn=_create,
        sleep_fn=lambda _seconds: None,
        logger=MagicMock(),
    )

    assert result is fresh_logger
    assert singleton is fresh_logger
    assert singleton_dsn == "postgresql://example"
    assert create_calls["count"] == 2


def test_dashboard_rollup_scan_supports_zero_arg_refresh_function() -> None:
    class _BL:
        @staticmethod
        def refresh_dashboard_rollups():
            return {"refreshed_accounts": 2}

    result = logger_runtime.dashboard_rollup_scan(
        _BL(),
        control_config=type("Cfg", (), {"dashboard_rollup_enabled": False})(),
        dataclass_replace=lambda cfg, dashboard_rollup_enabled: type(
            "Cfg",
            (),
            {"dashboard_rollup_enabled": dashboard_rollup_enabled},
        )(),
        dashboard_rollup_scan_impl=lambda bl, cfg: {
            "payload": bl.refresh_dashboard_rollups(),
            "enabled": cfg.dashboard_rollup_enabled,
        },
    )

    assert result == {"payload": {"refreshed_accounts": 2}, "enabled": True}


def test_dashboard_rollup_scan_preserves_original_logger_without_refresh_method() -> None:
    logger_ref = object()

    result = logger_runtime.dashboard_rollup_scan(
        logger_ref,
        control_config=type("Cfg", (), {"dashboard_rollup_enabled": False})(),
        dataclass_replace=lambda cfg, dashboard_rollup_enabled: type(
            "Cfg",
            (),
            {"dashboard_rollup_enabled": dashboard_rollup_enabled},
        )(),
        dashboard_rollup_scan_impl=lambda bl, cfg: {
            "same_logger": bl is logger_ref,
            "enabled": cfg.dashboard_rollup_enabled,
        },
    )

    assert result == {"same_logger": True, "enabled": True}
