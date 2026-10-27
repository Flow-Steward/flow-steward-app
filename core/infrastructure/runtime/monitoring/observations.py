"""Immutable source facts, independent of application and transport contracts."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from core.infrastructure.runtime.worker.consumer_observer import ConsumerState


@dataclass(frozen=True, slots=True)
class SourceObservation:
    service_type: str
    source: str
    original_process_id: str | None
    observer_id: UUID
    observation_kind: Literal["process", "dependency", "role_probe", "channel"]
    source_sequence: int | None = None
    checked_at: datetime | None = None
    source_age_seconds: float | None = None
    source_stale_seconds: float | None = None
    availability: Literal["observed", "missing", "expired", "retired"] = "missing"
    readiness: bool | None = None
    #: A health verdict with what it rests on. ``None`` lets the report derive
    #: it from the readiness probe or consumer state when one exists.
    health: bool | None = None
    health_evidence: str | None = None
    #: When this process (or observer) started: a newer start of the same role
    #: and source replaces an older one that stopped reporting.
    started_at: datetime | None = None
    consumer_state: ConsumerState | None = None
    lifecycle: Literal[
        "not_observed",
        "starting",
        "running",
        "stopping",
        "stopped",
        "maintenance_paused",
        "draining",
        "disconnected",
    ] = "not_observed"
