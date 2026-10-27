"""Canonical runtime monitoring policy; transport limits are operator settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


def _positive(values: Mapping[str, str], name: str, default: int, maximum: int) -> int:
    raw = values.get(name, "").strip() or str(default)
    if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= maximum:
        raise ValueError(f"{name} must be a positive bounded decimal integer")
    return int(raw)


@dataclass(frozen=True, slots=True)
class MonitoringRuntimeConfig:
    interval_seconds: int
    stale_seconds: int
    history_enabled: bool
    current_retention_days: int
    history_retention_days: int
    cleanup_batch_size: int
    queue_max_messages: int
    queue_max_bytes: int
    operation_timeout_seconds: int


def load_monitoring_runtime_config(values: Mapping[str, str]) -> MonitoringRuntimeConfig:
    """Fail closed on invalid budgets without parsing settings in feature code."""
    history = (values.get("FS_MONITORING_HISTORY_ENABLED", "").strip() or "0").lower()
    if history not in {"0", "1", "false", "true"}:
        raise ValueError("FS_MONITORING_HISTORY_ENABLED must be a boolean")
    result = MonitoringRuntimeConfig(
        interval_seconds=_positive(values, "FS_MONITORING_INTERVAL_SECONDS", 60, 86400),
        stale_seconds=_positive(values, "FS_MONITORING_STALE_SECONDS", 180, 86400),
        history_enabled=history in {"1", "true"},
        current_retention_days=_positive(values, "FS_MONITORING_CURRENT_RETENTION_DAYS", 7, 3650),
        history_retention_days=_positive(values, "FS_MONITORING_HISTORY_RETENTION_DAYS", 7, 3650),
        cleanup_batch_size=_positive(values, "FS_MONITORING_CLEANUP_BATCH_SIZE", 500, 5000),
        queue_max_messages=_positive(values, "FS_MONITORING_QUEUE_MAX_MESSAGES", 10000, 1000000),
        queue_max_bytes=_positive(values, "FS_MONITORING_QUEUE_MAX_BYTES", 16777216, 1073741824),
        operation_timeout_seconds=_positive(
            values, "FS_MONITORING_OPERATION_TIMEOUT_SECONDS", 2, 30
        ),
    )
    if result.interval_seconds >= result.stale_seconds:
        raise ValueError("monitoring interval must be below stale threshold")
    return result
