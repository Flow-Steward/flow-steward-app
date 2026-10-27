"""Application boundary for scoped, idempotent runtime-status ingestion."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from core.application.monitoring.contracts import (
    HeartbeatReport,
    IngestResult,
    RuntimeInstallationContext,
    decode_heartbeat,
)
from core.infrastructure.config.monitoring_runtime import MonitoringRuntimeConfig


class StatusWriter(Protocol):
    def store(self, report: HeartbeatReport, *, history_enabled: bool) -> IngestResult: ...


class RuntimeStatusIngestionService:
    """Validate before persistence; storage errors remain retryable, never success."""

    def __init__(
        self,
        *,
        repository: StatusWriter,
        context: RuntimeInstallationContext,
        config: MonitoringRuntimeConfig,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._context = context
        self._config = config
        self._clock = clock

    def accept(self, body: bytes) -> IngestResult:
        try:
            report = decode_heartbeat(
                body,
                context=self._context,
                received_at=self._clock(),
                stale_seconds=self._config.stale_seconds,
            )
        except ValueError:
            # Validation input/details may contain sensitive untrusted data; do not log it.
            return IngestResult(disposition="rejected")
        return self._repository.store(report, history_enabled=self._config.history_enabled)
