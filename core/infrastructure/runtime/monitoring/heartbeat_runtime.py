"""Stoppable technical scheduling; callbacks own policy and persistence."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Literal
from uuid import uuid4

from core.infrastructure.runtime.monitoring.observations import SourceObservation
from core.infrastructure.runtime.worker.consumer_observer import ConsumerState, ConsumerStateSink

logger = logging.getLogger(__name__)


class MonitoringRuntimeHandle:
    def __init__(
        self,
        *,
        stop: Event,
        threads: tuple[Thread, ...],
        timeout_seconds: float,
        wake: Event | None = None,
    ) -> None:
        self._stop = stop
        self.threads = threads
        self._timeout = timeout_seconds
        self._closed = False
        self._lock = Lock()
        self._wake = wake

    def request_publish(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            self.request_publish()
        deadline = time.monotonic() + self._timeout
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            logger.warning("Monitoring runtime did not stop within its shutdown budget")


def start_monitoring_threads(
    *,
    consumer: Callable[[Event], None],
    cleanup: Callable[[], None],
    interval_seconds: float,
    close_timeout_seconds: float,
) -> MonitoringRuntimeHandle:
    if interval_seconds <= 0 or close_timeout_seconds <= 0:
        raise ValueError("monitoring scheduling budgets must be positive")
    stop = Event()

    def retain() -> None:
        while not stop.is_set():
            try:
                cleanup()
            except Exception:
                logger.warning("Monitoring retention iteration failed")
            stop.wait(interval_seconds)

    threads = (
        Thread(target=consumer, args=(stop,), name="runtime-status-consumer", daemon=True),
        Thread(target=retain, name="runtime-status-retention", daemon=True),
    )
    handle = MonitoringRuntimeHandle(
        stop=stop, threads=threads, timeout_seconds=close_timeout_seconds
    )
    for thread in threads:
        thread.start()
    return handle


def start_monitoring_publisher(
    *,
    collect: Callable[[], tuple[bytes, ...]],
    publish: Callable[[bytes], None],
    idle: Callable[[], None],
    close: Callable[[], None],
    interval_seconds: float,
    close_timeout_seconds: float,
    farewell: Callable[[], tuple[bytes, ...]] | None = None,
) -> MonitoringRuntimeHandle:
    """Recollect latest facts each interval; never retain failed publication bytes.

    ``farewell`` builds the last reports when the handle closes, so a process
    that stops says so instead of fading into "stale".
    """
    if interval_seconds <= 0 or close_timeout_seconds <= 0:
        raise ValueError("monitoring scheduling budgets must be positive")
    stop = Event()
    wake = Event()

    def run() -> None:
        next_publish = time.monotonic()
        try:
            while not stop.is_set():
                now = time.monotonic()
                if now >= next_publish or wake.is_set():
                    wake.clear()
                    try:
                        for body in collect():
                            if stop.is_set():
                                break
                            publish(body)
                    except Exception:
                        logger.warning("Runtime status publication failed")
                    next_publish = time.monotonic() + interval_seconds
                else:
                    try:
                        idle()
                    except Exception:
                        logger.warning("Runtime status publisher connection unavailable")
                stop.wait(min(0.05, max(0.0, next_publish - time.monotonic())))
        finally:
            if farewell is not None:
                try:
                    for body in farewell():
                        publish(body)
                except Exception:
                    logger.warning("Runtime status final publication failed")
            try:
                close()
            except Exception:
                logger.warning("Runtime status publisher close failed")

    thread = Thread(target=run, name="runtime-status-publisher", daemon=True)
    handle = MonitoringRuntimeHandle(
        stop=stop, threads=(thread,), timeout_seconds=close_timeout_seconds, wake=wake
    )
    thread.start()
    return handle


class RuntimeObservationSource:
    """One process-owned cache, with original consumer-check age and no broker I/O."""

    def __init__(
        self,
        *,
        service_type: str,
        original_process_id: str | None = None,
        stale_seconds: float = 180,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if stale_seconds <= 0:
            raise ValueError("source freshness budget must be positive")
        self._identity = uuid4()
        self._service_type = service_type
        self._process_id = (
            original_process_id if original_process_id is not None else str(self._identity)
        )
        self._stale_seconds = stale_seconds
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._lock = Lock()
        self._state: ConsumerState | None = None
        self._checked_at: datetime | None = None
        self._checked_tick = monotonic()
        self._sequence = 0
        self._closed = False
        self._stopped = False
        self._started_at = utcnow()
        self._notify: Callable[[], None] = lambda: None

    def set_notification(self, notify: Callable[[], None]) -> None:
        with self._lock:
            self._notify = notify

    def record(self, state: ConsumerState) -> None:
        if state not in {"registered", "active", "maintenance_paused", "draining", "disconnected"}:
            raise ValueError("invalid consumer state")
        with self._lock:
            if self._closed:
                return
            changed = state != self._state
            self._state = state
            self._refresh()
            notify = self._notify
        if changed:
            notify()

    def _refresh(self) -> None:
        self._checked_at = self._utcnow()
        self._checked_tick = self._monotonic()
        self._sequence += 1

    def refresh(self) -> None:
        with self._lock:
            if not self._closed and self._state is not None:
                self._refresh()

    def observe(self) -> SourceObservation:
        with self._lock:
            # No consumer check means this callback establishes process liveness only.
            if self._state is None and not self._closed:
                self._refresh()
            age = max(0.0, self._monotonic() - self._checked_tick)
            lifecycle: Literal[
                "running", "maintenance_paused", "draining", "disconnected", "stopped"
            ] = "running"
            if self._stopped:
                lifecycle = "stopped"
            elif self._state == "maintenance_paused":
                lifecycle = "maintenance_paused"
            elif self._state == "draining":
                lifecycle = "draining"
            elif self._state == "disconnected":
                lifecycle = "disconnected"
            availability: Literal["retired", "expired", "observed"] = "observed"
            if self._stopped:
                availability = "retired"
            elif age > self._stale_seconds:
                availability = "expired"
            return SourceObservation(
                service_type=self._service_type,
                source="self",
                original_process_id=self._process_id,
                observer_id=self._identity,
                observation_kind="process",
                source_sequence=self._sequence or None,
                checked_at=self._checked_at,
                source_age_seconds=age,
                source_stale_seconds=self._stale_seconds,
                availability=availability,
                readiness=self._state in {"registered", "active", "maintenance_paused"}
                if self._state is not None and not self._stopped
                else None,
                consumer_state=self._state,
                lifecycle=lifecycle,
                started_at=self._started_at,
            )

    def mark_stopped(self) -> None:
        """The process is shutting down: its last report says so."""
        with self._lock:
            self._stopped = True
            self._refresh()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._state = "disconnected"
            self._refresh()
            self._closed = True
            notify = self._notify
        notify()


class CompositeConsumerSink:
    """Keep inherited Compact reporting while adding an independent local cache."""

    def __init__(
        self,
        local: ConsumerStateSink,
        inherited: ConsumerStateSink | None,
        *,
        interval_seconds: float = 60,
    ) -> None:
        self._local = local
        self._inherited = inherited
        self._closed = False
        self.refresh_interval_seconds = interval_seconds

    def record(self, state: ConsumerState) -> None:
        self._local.record(state)
        if self._inherited is not None:
            self._inherited.record(state)

    def refresh(self) -> None:
        self._local.refresh()
        if self._inherited is not None:
            self._inherited.refresh()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._local.close()
        finally:
            if self._inherited is not None:
                self._inherited.close()
