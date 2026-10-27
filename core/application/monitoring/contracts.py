"""Versioned runtime observations, independent of provider and service inventory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from core.infrastructure.runtime.monitoring.observations import SourceObservation

ServiceName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]
SourceName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.:/-]{0,127}$")]
PositiveSequence = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]
ExpectationCount = Annotated[int, Field(strict=True, ge=0, le=65535)]


class _ClosedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def utc_time(value: datetime) -> datetime:
    """Reject missing/non-UTC timezone information instead of guessing a timezone."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("monitoring timestamps must be UTC")
    return value.astimezone(UTC)


class RuntimeInstallationContext(_ClosedValue):
    installation_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"
    )
    deployment_profile: Literal["compact", "scalable"]
    expectations: tuple[tuple[ServiceName, ExpectationCount | None], ...] = ()
    dependencies: tuple[ServiceName, ...] = ()

    @model_validator(mode="after")
    def unique_expectations(self) -> RuntimeInstallationContext:
        if len(self.expectations) > 128 or len({name for name, _ in self.expectations}) != len(
            self.expectations
        ):
            raise ValueError("expected inventory must be bounded and unique")
        if len(self.dependencies) > 128 or len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependency inventory must be bounded and unique")
        return self


class CheckStatus(_ClosedValue):
    """A status dimension requires its own check provenance, not report freshness."""

    state: Literal["not_observed", "ready", "not_ready", "healthy", "unhealthy"]
    checked_at: datetime | None = None
    evidence: ServiceName | None = None
    stale_seconds: (
        Annotated[float, Field(strict=True, gt=0, le=86400, allow_inf_nan=False)] | None
    ) = None

    @field_validator("checked_at")
    @classmethod
    def check_utc(cls, value: datetime | None) -> datetime | None:
        return utc_time(value) if value is not None else None

    @model_validator(mode="after")
    def require_provenance(self) -> CheckStatus:
        if self.state == "not_observed":
            if (
                self.checked_at is not None
                or self.evidence is not None
                or self.stale_seconds is not None
            ):
                raise ValueError("unobserved checks cannot carry evidence")
        elif self.checked_at is None or self.evidence is None:
            raise ValueError("observed checks require timestamp and evidence")
        return self


class RuntimeStatusPayload(_ClosedValue):
    type: Literal["runtime_status"]
    version: Annotated[int, Field(strict=True, ge=1, le=1)]
    lifecycle: Literal[
        "not_observed",
        "starting",
        "running",
        "stopping",
        "stopped",
        "maintenance_paused",
        "draining",
        "disconnected",
    ]
    readiness: CheckStatus
    health: CheckStatus
    consumer_state: (
        Literal["registered", "active", "maintenance_paused", "draining", "disconnected"] | None
    ) = None
    source_availability: Literal["observed", "missing", "expired", "retired"] = "observed"
    source_age_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    source_stale_seconds: (
        Annotated[float, Field(strict=True, gt=0, le=86400, allow_inf_nan=False)] | None
    ) = None
    source_sequence: PositiveSequence | None = None
    observation_kind: Literal["process", "dependency", "role_probe", "channel"] = "process"
    #: When the reporting process or observer started; lets a newer start of the
    #: same role replace an older one instead of leaving it behind as "stale".
    started_at: datetime | None = None

    @field_validator("started_at")
    @classmethod
    def started_utc(cls, value: datetime | None) -> datetime | None:
        return utc_time(value) if value is not None else None

    @model_validator(mode="after")
    def distinct_dimensions(self) -> RuntimeStatusPayload:
        if self.readiness.state not in {"not_observed", "ready", "not_ready"}:
            raise ValueError("invalid readiness dimension")
        if self.health.state not in {"not_observed", "healthy", "unhealthy"}:
            raise ValueError("invalid health dimension")
        return self


class HeartbeatReport(_ClosedValue):
    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)]
    installation_id: str = Field(min_length=1, max_length=128)
    deployment_profile: Literal["compact", "scalable"]
    service_type: ServiceName
    instance_id: UUID
    incarnation_id: UUID
    sequence: PositiveSequence
    observed_at: datetime
    source: SourceName
    payload: RuntimeStatusPayload

    @field_validator("observed_at")
    @classmethod
    def observation_utc(cls, value: datetime) -> datetime:
        return utc_time(value)


class StatusRecord(HeartbeatReport):
    """A committed observation; receipt time is assigned by the database."""

    id: UUID
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def receipt_utc(cls, value: datetime) -> datetime:
        return utc_time(value)


@dataclass(frozen=True, slots=True)
class IngestResult:
    disposition: Literal["stored", "duplicate", "rejected"]
    record_id: UUID | None = None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate monitoring field")
        result[key] = value
    return result


def decode_heartbeat(
    body: bytes,
    *,
    context: RuntimeInstallationContext,
    received_at: datetime,
    stale_seconds: int,
    max_future_seconds: int = 30,
    max_bytes: int = 16384,
) -> HeartbeatReport:
    """Validate a bounded report against the receiver's trusted scope and clock."""
    received_at = utc_time(received_at)
    if len(body) > max_bytes:
        raise ValueError("monitoring envelope exceeds size limit")
    try:
        decoded = json.loads(body, object_pairs_hook=_unique_object)
        report = HeartbeatReport.model_validate(decoded)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid monitoring envelope") from exc
    if (
        report.installation_id != context.installation_id
        or report.deployment_profile != context.deployment_profile
    ):
        raise ValueError("monitoring scope mismatch")
    age = (received_at - report.observed_at).total_seconds()
    if age < -max_future_seconds or age > stale_seconds:
        raise ValueError("monitoring report outside freshness window")
    for check in (report.payload.readiness, report.payload.health):
        if (
            check.checked_at is not None
            and (check.checked_at - received_at).total_seconds() > max_future_seconds
        ):
            raise ValueError("monitoring check timestamp is in the future")
    return report


def monitoring_worker_incarnation(installation_id: str, service_type: str, worker_id: str) -> UUID:
    """Map the entire opaque worker identity; never alter queue or lease owners."""
    name = "urn:flow-steward:monitoring:worker-incarnation:v1:" + json.dumps(
        [installation_id, service_type, worker_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return uuid5(NAMESPACE_URL, name)


#: What a derived health verdict rests on, by observation kind.
_DERIVED_HEALTH_EVIDENCE = {
    "dependency": "dependency_probe",
    "role_probe": "role_probe",
    "channel": "broker_connection",
}


def _health(row: SourceObservation) -> CheckStatus:
    """The health of one observation, and what it rests on.

    A dependency or role probe that answered is healthy; one that failed is not.
    A worker is healthy while its consumer is connected. A process with no
    other check is healthy while it keeps reporting: the evidence says so, so a
    reader can tell a liveness signal from a deeper check. A stopped or retired
    source has no current health.
    """
    if row.checked_at is None or row.lifecycle == "stopped" or row.availability == "retired":
        return CheckStatus(state="not_observed")
    verdict = row.health
    evidence = row.health_evidence
    if verdict is None and row.observation_kind in _DERIVED_HEALTH_EVIDENCE:
        if row.readiness is not None:
            verdict = row.readiness
            evidence = _DERIVED_HEALTH_EVIDENCE[row.observation_kind]
    elif verdict is None and row.observation_kind == "process":
        if row.consumer_state is not None:
            verdict = row.consumer_state != "disconnected"
            evidence = "consumer_state"
        elif row.lifecycle in {"running", "disconnected"}:
            verdict = row.lifecycle == "running"
            evidence = "self_report"
    if verdict is None:
        return CheckStatus(state="not_observed")
    return CheckStatus(
        state="healthy" if verdict else "unhealthy",
        checked_at=row.checked_at,
        evidence=evidence or "self_report",
        stale_seconds=row.source_stale_seconds,
    )


def build_observation_reports(
    observations: tuple[SourceObservation, ...],
    *,
    context: RuntimeInstallationContext,
    published_at: datetime,
) -> tuple[HeartbeatReport, ...]:
    published_at = utc_time(published_at)
    reports = []
    for row in observations:
        if row.checked_at is None or (
            row.observation_kind == "process" and row.original_process_id is None
        ):
            continue
        identity = (
            monitoring_worker_incarnation(
                context.installation_id, row.service_type, row.original_process_id
            )
            if row.original_process_id is not None
            else row.observer_id
        )
        readiness = CheckStatus(state="not_observed")
        if row.readiness is not None:
            readiness = CheckStatus(
                state="ready" if row.readiness else "not_ready",
                checked_at=row.checked_at,
                evidence="consumer_state"
                if row.observation_kind == "process"
                else "protocol_probe",
                stale_seconds=row.source_stale_seconds,
            )
        health = _health(row)
        reports.append(
            HeartbeatReport(
                schema_version=1,
                installation_id=context.installation_id,
                deployment_profile=context.deployment_profile,
                service_type=row.service_type,
                instance_id=identity,
                incarnation_id=identity,
                sequence=row.source_sequence or 1,
                observed_at=published_at,
                source=row.source,
                payload=RuntimeStatusPayload(
                    type="runtime_status",
                    version=1,
                    lifecycle=row.lifecycle,
                    readiness=readiness,
                    health=health,
                    consumer_state=row.consumer_state,
                    source_availability=row.availability,
                    source_age_seconds=row.source_age_seconds,
                    source_stale_seconds=row.source_stale_seconds,
                    source_sequence=row.source_sequence,
                    observation_kind=row.observation_kind,
                    started_at=row.started_at,
                ),
            )
        )
    return tuple(reports)


def encode_heartbeat(report: HeartbeatReport) -> bytes:
    body = report.model_dump_json().encode("utf-8")
    if len(body) > 16384:
        raise ValueError("monitoring envelope exceeds size limit")
    return body
