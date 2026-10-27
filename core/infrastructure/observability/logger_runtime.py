"""Reusable singleton logger helpers."""

from __future__ import annotations

from typing import Any


def get_logger_singleton(
    *,
    config: dict[str, Any],
    singleton,
    singleton_dsn: str,
    create_logger_fn,
    sleep_fn,
    logger,
    attempts: int = 3,
):
    dsn = config.get("fs_observability_dsn", "") or config.get("fs_db_dsn", "")
    if not dsn:
        return None, singleton, singleton_dsn

    current_singleton = singleton
    current_singleton_dsn = singleton_dsn
    last_error = None
    for attempt in range(attempts):
        if current_singleton is not None and current_singleton_dsn == dsn:
            try:
                with current_singleton.conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                return current_singleton, current_singleton, current_singleton_dsn
            except Exception as exc:
                last_error = exc
                logger.debug("Flow Steward logger liveness check failed; reconnecting: %s", exc)
                current_singleton = None

        try:
            current_singleton = create_logger_fn(dsn)
            current_singleton_dsn = dsn
            return current_singleton, current_singleton, current_singleton_dsn
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Flow Steward logger creation failed (attempt %d/%d); retrying: %s",
                attempt + 1,
                attempts,
                exc,
            )
            sleep_fn(0.1 * (attempt + 1))

    logger.error("Flow Steward logger unavailable after retries: %s", last_error)
    return None, current_singleton, current_singleton_dsn


def build_get_logger_singleton_handler(
    *,
    get_config,
    get_singleton_state,
    set_singleton_state,
    get_create_logger_fn,
    get_sleep_fn,
    logger,
):
    def _get_logger_only():
        singleton, singleton_dsn = get_singleton_state()
        result, next_singleton, next_singleton_dsn = get_logger_singleton(
            config=get_config(),
            singleton=singleton,
            singleton_dsn=singleton_dsn,
            create_logger_fn=get_create_logger_fn(),
            sleep_fn=get_sleep_fn(),
            logger=logger,
        )
        set_singleton_state(next_singleton, next_singleton_dsn)
        return result

    return _get_logger_only


def dashboard_rollup_scan(
    bl,
    *,
    control_config,
    dataclass_replace,
    dashboard_rollup_scan_impl=None,
) -> dict:
    if dashboard_rollup_scan_impl is None:
        from core.infrastructure.runtime.control.scans_rollups import (
            dashboard_rollup_scan as dashboard_rollup_scan_impl,
        )

    compat_bl = bl
    refresh_fn = getattr(bl, "refresh_dashboard_rollups", None) if bl else None
    if callable(refresh_fn):

        class _DashboardRollupCompat:
            @staticmethod
            def refresh_dashboard_rollups(**kwargs):
                try:
                    return refresh_fn(**kwargs)
                except TypeError:
                    return refresh_fn()

        compat_bl = _DashboardRollupCompat()

    return dashboard_rollup_scan_impl(
        compat_bl,
        dataclass_replace(control_config, dashboard_rollup_enabled=True),
    )
