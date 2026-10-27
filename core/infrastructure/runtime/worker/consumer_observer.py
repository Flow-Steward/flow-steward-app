"""Supervisor-owned worker consumer state observations."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from typing import Literal, Protocol

ConsumerState = Literal[
    "registered",
    "active",
    "maintenance_paused",
    "draining",
    "disconnected",
]
_ALLOWED_STATES = frozenset(
    {"registered", "active", "maintenance_paused", "draining", "disconnected"}
)


class _Waitable(Protocol):
    def wait(self, timeout: float | None = None) -> bool: ...


class ConsumerStateSink(Protocol):
    def record(self, state: ConsumerState) -> None: ...
    def refresh(self) -> None: ...
    def close(self) -> None: ...


class ConsumerObservationSink:
    """Write bounded newline-delimited state records to an inherited descriptor."""

    def __init__(
        self,
        *,
        descriptor: int,
        role: str,
        worker_incarnation: str,
    ) -> None:
        if descriptor < 0:
            raise ValueError("observation descriptor must be non-negative")
        if not role.strip() or not worker_incarnation.strip():
            raise ValueError("observation role and worker incarnation are required")
        self._descriptor = descriptor
        self._role = role.strip()
        self._worker_incarnation = worker_incarnation.strip()
        self._sequence = 0
        self._state: ConsumerState | None = None
        self._closed = False
        self._lock = threading.Lock()

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        *,
        role: str,
        worker_incarnation: str,
    ) -> ConsumerObservationSink | None:
        if "FS_COMPACT_OBSERVATION_FD" not in environ:
            return None
        raw = environ["FS_COMPACT_OBSERVATION_FD"]
        if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
            raise ValueError("FS_COMPACT_OBSERVATION_FD must be a decimal descriptor")
        return cls(
            descriptor=int(raw),
            role=role,
            worker_incarnation=worker_incarnation,
        )

    def record(self, state: ConsumerState) -> None:
        if state not in _ALLOWED_STATES:
            raise ValueError(f"unsupported consumer observation state: {state}")
        with self._lock:
            if self._closed:
                raise RuntimeError("consumer observation descriptor is closed")
            self._state = state
            self._write(state)

    def refresh(self) -> None:
        """Refresh the current state without inventing a state transition."""
        with self._lock:
            if self._closed or self._state is None:
                return
            self._write(self._state)

    def _write(self, state: ConsumerState) -> None:
        self._sequence += 1
        payload = (
            json.dumps(
                {
                    "role": self._role,
                    "worker_incarnation": self._worker_incarnation,
                    "state": state,
                    "sequence": self._sequence,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )
        view = memoryview(payload)
        while view:
            written = os.write(self._descriptor, view)
            if written <= 0:
                raise OSError("consumer observation descriptor write failed")
            view = view[written:]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._state = "disconnected"
            self._write("disconnected")
            self._closed = True
            os.close(self._descriptor)


def create_observation_sink(
    environ: Mapping[str, str],
    *,
    role: str,
    worker_incarnation: str,
) -> ConsumerObservationSink | None:
    """Create a supervised worker sink without claiming queue readiness."""
    return ConsumerObservationSink.from_environment(
        environ,
        role=role,
        worker_incarnation=worker_incarnation,
    )


def run_observation_heartbeat(
    sink: ConsumerStateSink,
    *,
    stop: _Waitable,
    interval_seconds: float,
) -> None:
    """Keep the supervisor observation fresh while the worker remains alive."""
    if interval_seconds <= 0:
        raise ValueError("observation heartbeat interval must be positive")
    while not stop.wait(interval_seconds):
        sink.refresh()
