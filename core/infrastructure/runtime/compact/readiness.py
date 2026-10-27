"""Aggregate readiness state for the fixed Compact process inventory."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from core.infrastructure.runtime.monitoring.observations import SourceObservation
from core.infrastructure.runtime.worker.consumer_observer import ConsumerState

_REQUIRED_ROLES = frozenset({"router", "app", "installer", "mcp", "control"})
_READY_WORKER_STATES = frozenset({"registered", "active", "maintenance_paused"})


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    ready: bool
    bootstrap_ready: bool
    schema_ready: bool
    dependencies: dict[str, bool]
    roles: dict[str, bool]
    worker_states: dict[str, ConsumerState]
    workers_complete: bool


class ReadinessRegistry:
    def __init__(
        self,
        *,
        worker_processes: int,
        stale_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if worker_processes < 1:
            raise ValueError("worker_processes must be positive")
        self._expected_workers = tuple(f"worker-{index}" for index in range(worker_processes))
        self._bootstrap_ready = False
        self._schema_ready = False
        self._dependencies = {"postgres": False, "rabbitmq": False}
        self._roles = dict.fromkeys(_REQUIRED_ROLES, False)
        if stale_seconds <= 0:
            raise ValueError("stale_seconds must be positive")
        self._stale_seconds = stale_seconds
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._observer_id = uuid4()
        self._started_at = utcnow()
        self._source_rows: dict[str, tuple[SourceObservation, float | None]] = {}
        self._workers: dict[str, tuple[str, int, ConsumerState, float]] = {}
        self._retired_incarnations: set[str] = set()
        self._lock = threading.RLock()
        self._observation_notify: Callable[[], None] = lambda: None
        for name in self._dependencies:
            self._missing_source(name, "dependency")
        for name in sorted(self._roles):
            self._missing_source(name, "role_probe")
        for name in self._expected_workers:
            self._missing_source(name, "process")

    def _missing_source(
        self, name: str, kind: Literal["process", "dependency", "role_probe"]
    ) -> None:
        self._source_rows[name] = (
            SourceObservation(
                service_type="worker" if kind == "process" else name,
                source=f"compact.{name}",
                original_process_id=None,
                observer_id=self._observer_id,
                observation_kind=kind,
                started_at=self._started_at,
            ),
            None,
        )

    def _record_probe(self, name: str, ready: bool) -> None:
        row, _ = self._source_rows[name]
        self._source_rows[name] = (
            replace(
                row,
                checked_at=self._utcnow(),
                readiness=bool(ready),
                source_sequence=(row.source_sequence or 0) + 1,
                availability="observed",
                source_stale_seconds=self._stale_seconds,
            ),
            self._monotonic(),
        )
        if row.readiness != bool(ready) or row.availability != "observed":
            self._observation_notify()

    def set_observation_notification(self, notify: Callable[[], None]) -> None:
        """Inject a coalescing notification only; no transport work under the lock."""
        with self._lock:
            self._observation_notify = notify

    def record_role_probe(self, role: str, ready: bool) -> None:
        with self._lock:
            self.set_role_ready(role, ready)
            self._record_probe(role, ready)

    def observations(self) -> tuple[SourceObservation, ...]:
        with self._lock:
            now = self._monotonic()
            return tuple(
                replace(
                    row,
                    source_age_seconds=max(0.0, now - seen) if seen is not None else None,
                    availability="expired"
                    if row.availability == "observed"
                    and seen is not None
                    and now - seen > self._stale_seconds
                    else row.availability,
                )
                for row, seen in self._source_rows.values()
            )

    def retire_all(self) -> None:
        """Mark every observed source retired: the supervisor is stopping."""
        with self._lock:
            for name, (row, seen) in self._source_rows.items():
                if row.checked_at is not None:
                    self._source_rows[name] = (
                        replace(row, availability="retired", lifecycle="stopped"),
                        seen,
                    )

    def _retire_source(self, role: str) -> None:
        if role in self._source_rows:
            row, seen = self._source_rows[role]
            if row.checked_at is not None:
                self._source_rows[role] = (replace(row, availability="retired"), seen)
                if row.availability != "retired":
                    self._observation_notify()

    def set_bootstrap_ready(self, ready: bool) -> None:
        with self._lock:
            self._bootstrap_ready = bool(ready)

    def set_schema_ready(self, ready: bool) -> None:
        with self._lock:
            self._schema_ready = bool(ready)

    def set_dependency(self, name: str, ready: bool) -> None:
        if name not in self._dependencies:
            raise ValueError(f"unsupported Compact dependency: {name}")
        with self._lock:
            self._dependencies[name] = bool(ready)
            self._record_probe(name, ready)

    def set_role_ready(self, role: str, ready: bool) -> None:
        if role not in self._roles:
            raise ValueError(f"unsupported Compact role: {role}")
        with self._lock:
            self._roles[role] = bool(ready)

    def reset_role(self, role: str) -> None:
        """Forget one role's readiness while only that role restarts.

        A service role reads not-ready until its monitor sees it answer again. A worker's
        last observation is retired with its incarnation, so a dead worker cannot keep
        the pool looking complete for the stale window.
        """
        with self._lock:
            self._retire_source(role)
            if role in self._roles:
                self._roles[role] = False
                return
            current = self._workers.pop(role, None)
            if current is not None:
                self._retired_incarnations.add(current[0])

    def reset_runtime(self) -> None:
        """Clear readiness before a new supervised process generation starts."""
        with self._lock:
            self._roles = dict.fromkeys(_REQUIRED_ROLES, False)
            self._retired_incarnations.update(row[0] for row in self._workers.values())
            self._workers.clear()
            for role in (*self._roles, *self._expected_workers):
                self._retire_source(role)

    def record_worker(
        self,
        *,
        role: str,
        worker_incarnation: str,
        state: ConsumerState,
        sequence: int,
    ) -> None:
        if (
            role not in self._expected_workers
            or sequence < 1
            or not worker_incarnation
            or state
            not in {"registered", "active", "maintenance_paused", "draining", "disconnected"}
        ):
            return
        with self._lock:
            current = self._workers.get(role)
            if worker_incarnation in self._retired_incarnations:
                return
            if current is not None:
                current_incarnation, current_sequence, _current_state, _seen_at = current
                if current_incarnation == worker_incarnation:
                    if sequence <= current_sequence:
                        return
                else:
                    self._retired_incarnations.add(current_incarnation)
            self._workers[role] = (
                worker_incarnation,
                sequence,
                state,
                self._monotonic(),
            )
            row, _ = self._source_rows[role]
            self._source_rows[role] = (
                replace(
                    row,
                    original_process_id=worker_incarnation,
                    source_sequence=sequence,
                    checked_at=self._utcnow(),
                    availability="observed",
                    readiness=state in _READY_WORKER_STATES,
                    consumer_state=state,
                    source_stale_seconds=self._stale_seconds,
                ),
                self._workers[role][3],
            )
            if (
                row.original_process_id != worker_incarnation
                or row.consumer_state != state
                or row.availability != "observed"
            ):
                self._observation_notify()

    def snapshot(self) -> ReadinessSnapshot:
        with self._lock:
            now = self._monotonic()
            worker_states: dict[str, ConsumerState] = {
                role: row[2]
                for role, row in self._workers.items()
                if now - row[3] <= self._stale_seconds
            }
            # Public readiness asks whether this installation can serve and run a job:
            # every service role and one worker that would take it. Not the whole pool --
            # the supervisor stops every role once readiness stays false past its budget,
            # and one worker crash-looping under its restart backoff must not do that.
            workers_ready = any(state in _READY_WORKER_STATES for state in worker_states.values())
            workers_complete = set(worker_states) == set(self._expected_workers) and all(
                state in _READY_WORKER_STATES for state in worker_states.values()
            )
            ready = bool(
                self._bootstrap_ready
                and self._schema_ready
                and all(self._dependencies.values())
                and all(self._roles.values())
                and workers_ready
            )
            return ReadinessSnapshot(
                ready=ready,
                bootstrap_ready=self._bootstrap_ready,
                schema_ready=self._schema_ready,
                dependencies=dict(self._dependencies),
                roles=dict(self._roles),
                worker_states=worker_states,
                workers_complete=workers_complete,
            )
