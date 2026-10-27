"""Read-only stored observations; freshness never performs discovery or publication."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from core.application.monitoring.contracts import (
    CheckStatus,
    RuntimeInstallationContext,
    StatusRecord,
    utc_time,
)
from core.infrastructure.config.monitoring_runtime import (
    MonitoringRuntimeConfig,
    load_monitoring_runtime_config,
)


class _Projection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DimensionProjection(CheckStatus):
    age_seconds: float | None
    freshness: Literal["fresh", "stale", "not_observed"]


class ComponentRow(_Projection):
    service_type: str
    instance_id: UUID
    incarnation_id: UUID
    source: str
    observation_kind: Literal["process", "dependency", "role_probe", "channel"]
    observed_at: datetime
    received_at: datetime
    report_age_seconds: float
    receive_age_seconds: float
    effective_age_seconds: float
    freshness: Literal["fresh", "stale"]
    source_age_seconds: float | None
    source_stale_seconds: float | None
    source_availability: Literal["observed", "missing", "expired", "retired"]
    source_freshness: Literal["fresh", "stale", "missing", "retired"]
    lifecycle: str
    readiness: DimensionProjection
    health: DimensionProjection
    consumer_state: str | None
    conflicting: bool = False
    started_at: datetime | None = None
    #: A newer start of the same role and source replaced this one, or it said
    #: it stopped. History: kept for diagnosis, left out of current counts.
    superseded: bool = False


class ComponentCounts(_Projection):
    roles_total: int
    instances_superseded: int = 0
    roles_observed: int
    instances_total: int
    instances_fresh: int
    instances_stale: int
    instances_ready: int
    instances_evidenced_healthy: int
    dependencies_total: int


class ExpectedGap(_Projection):
    service_type: str
    desired_count: int | None
    observed_count: int
    observation_kind: Literal["process", "dependency"] = "process"
    reason: Literal["missing", "stale", "replica_target_unobserved", "not_observed"]


class ChannelProjection(_Projection):
    state: Literal[
        "connected",
        "disconnected",
        "stale",
        "missing",
        "not_observed",
        "unavailable",
        "conflicting",
    ]
    observed_at: datetime | None = None
    age_seconds: float | None = None


class ComponentsProjection(_Projection):
    installation_id: str
    deployment_profile: Literal["compact", "scalable"]
    available: bool
    channel: ChannelProjection
    rows: tuple[ComponentRow, ...] = ()
    expected_gaps: tuple[ExpectedGap, ...] = ()
    counts: ComponentCounts | None = None
    completeness: bool | None = None


class StatusReader(Protocol):
    def load(self, context: RuntimeInstallationContext) -> list[StatusRecord]: ...


def _age(now: datetime, checked_at: datetime) -> float:
    return max(0.0, (now - checked_at).total_seconds())


def _dimension(
    check: CheckStatus,
    *,
    now: datetime,
    observed_at: datetime,
    report_fresh: bool,
    source_fresh: bool,
    stale_seconds: int,
) -> DimensionProjection:
    """A check is fresh when it was fresh at publication and the report still is.

    A Compact probe runs every few seconds but is published once a minute.
    Measuring its age from now against its own few-second budget made every
    probe read "stale" for most of each minute; the probe's budget applies
    when it is published, the report's own budget after that.
    """
    if check.checked_at is None:
        return DimensionProjection(**check.model_dump(), age_seconds=None, freshness="not_observed")
    age = _age(now, check.checked_at)
    age_at_publish = max(0.0, (observed_at - check.checked_at).total_seconds())
    fresh_at_publish = age_at_publish <= (check.stale_seconds or stale_seconds)
    freshness: Literal["fresh", "stale"] = (
        "fresh" if fresh_at_publish and report_fresh and source_fresh else "stale"
    )
    return DimensionProjection(**check.model_dump(), age_seconds=age, freshness=freshness)


def _row(record: StatusRecord, *, now: datetime, config: MonitoringRuntimeConfig) -> ComponentRow:
    report_age = _age(now, record.observed_at)
    receive_age = _age(now, record.received_at)
    payload = record.payload
    source_age = (
        payload.source_age_seconds + report_age if payload.source_age_seconds is not None else None
    )
    availability = payload.source_availability
    report_fresh = max(report_age, receive_age) <= config.stale_seconds
    source_freshness: Literal["fresh", "stale", "missing", "retired"] = "fresh"
    if availability == "missing":
        source_freshness = "missing"
    elif availability == "retired":
        source_freshness = "retired"
    elif (
        availability == "expired"
        or not report_fresh
        or (
            payload.source_age_seconds is not None
            and payload.source_age_seconds > (payload.source_stale_seconds or config.stale_seconds)
        )
    ):
        # The source's own budget applies at publication; the report's after.
        source_freshness = "stale"
    return ComponentRow(
        service_type=record.service_type,
        instance_id=record.instance_id,
        incarnation_id=record.incarnation_id,
        source=record.source,
        observation_kind=payload.observation_kind,
        observed_at=record.observed_at,
        received_at=record.received_at,
        report_age_seconds=report_age,
        receive_age_seconds=receive_age,
        effective_age_seconds=max(report_age, receive_age),
        freshness="stale" if max(report_age, receive_age) > config.stale_seconds else "fresh",
        source_age_seconds=source_age,
        source_stale_seconds=payload.source_stale_seconds,
        source_availability=availability,
        source_freshness=source_freshness,
        lifecycle=payload.lifecycle,
        consumer_state=payload.consumer_state,
        readiness=_dimension(
            payload.readiness,
            now=now,
            observed_at=record.observed_at,
            report_fresh=report_fresh,
            source_fresh=source_freshness == "fresh",
            stale_seconds=config.stale_seconds,
        ),
        health=_dimension(
            payload.health,
            now=now,
            observed_at=record.observed_at,
            report_fresh=report_fresh,
            source_fresh=source_freshness == "fresh",
            stale_seconds=config.stale_seconds,
        ),
        started_at=payload.started_at,
    )


def _fresh(row: ComponentRow) -> bool:
    return row.freshness == "fresh" and row.source_freshness == "fresh"


def _conflict(rows: list[ComponentRow]) -> bool:
    fresh = [row for row in rows if _fresh(row)]
    for dimension in ("readiness", "health"):
        states = {
            check.state
            for row in fresh
            if (check := getattr(row, dimension)).freshness == "fresh"
            and check.state != "not_observed"
        }
        if len(states) > 1:
            return True
    states = {row.lifecycle for row in fresh if row.lifecycle != "not_observed"}
    return len(states) > 1


def _replaced_channel(
    key: tuple[str, UUID, UUID, str],
    group: list[ComponentRow],
    groups: dict[tuple[str, UUID, UUID, str], list[ComponentRow]],
) -> bool:
    """An installation has one status receiver: a later start replaces it at once.

    The receiver sends no last report when it stops, so its old row stays
    "fresh" for minutes after a restart and two receivers read as conflicting.
    """
    started = [row.started_at for row in group if row.started_at is not None]
    if not started:
        return False
    mine = max(started)
    sources = {row.source for row in group}
    return any(
        other_key != key
        and other_key[3] == "channel"
        and sources & {row.source for row in other}
        and any(row.started_at is not None and row.started_at > mine for row in other)
        for other_key, other in groups.items()
    )


def _superseded_groups(
    groups: dict[tuple[str, UUID, UUID, str], list[ComponentRow]],
) -> set[tuple[str, UUID, UUID, str]]:
    """Groups that are history: stopped, or replaced by a newer start.

    Every process start reports under a new identity, so a restart used to
    leave the old one behind as "stale" for a week. An old group is history
    when it said it stopped, or when a group of the same role and source is
    fresh and started after the old one last reported. A replica that stopped
    reporting while its siblings run (no newer start) stays current and stale:
    that one is a real problem.
    """
    latest: dict[tuple[str, UUID, UUID, str], datetime] = {}
    for key, group in groups.items():
        latest[key] = max(row.observed_at for row in group)
    superseded: set[tuple[str, UUID, UUID, str]] = set()
    for key, group in groups.items():
        if key[3] not in {"process", "channel", "dependency", "role_probe"}:
            continue
        if any(row.source_availability == "retired" or row.lifecycle == "stopped" for row in group):
            superseded.add(key)
            continue
        if key[3] == "channel" and _replaced_channel(key, group, groups):
            superseded.add(key)
            continue
        if any(_fresh(row) for row in group):
            continue
        sources = {row.source for row in group}
        for other_key, other in groups.items():
            if other_key == key or other_key[0] != key[0] or other_key[3] != key[3]:
                continue
            if not sources & {row.source for row in other}:
                continue
            if not any(_fresh(row) for row in other):
                continue
            started = [row.started_at for row in other if row.started_at is not None]
            # A row from before start times were reported cannot be ordered;
            # a fresh sibling of the same source is then the best evidence.
            if (
                not started
                or min(started) >= latest[key]
                or all(row.started_at is None for row in group)
            ):
                superseded.add(key)
                break
    return superseded


def _channel(rows: list[ComponentRow]) -> ChannelProjection:
    rows = [row for row in rows if row.observation_kind == "channel"]
    if not rows:
        return ChannelProjection(state="missing")
    latest = max(rows, key=lambda row: row.observed_at)
    fresh = [row for row in rows if _fresh(row) and row.readiness.freshness == "fresh"]
    states = {row.readiness.state for row in fresh if row.readiness.state != "not_observed"}
    state: Literal["connected", "disconnected", "stale", "not_observed", "conflicting"] = (
        "not_observed"
    )
    if len(states) > 1:
        state = "conflicting"
    elif states:
        state = "connected" if "ready" in states else "disconnected"
    elif not _fresh(latest) or latest.readiness.freshness == "stale":
        state = "stale"
    return ChannelProjection(
        state=state, observed_at=latest.observed_at, age_seconds=latest.effective_age_seconds
    )


class RuntimeStatusQueryService:
    def __init__(
        self, *, repository: StatusReader, config: MonitoringRuntimeConfig | None = None
    ) -> None:
        self._repository = repository
        self._config = config or load_monitoring_runtime_config({})

    def get_components(
        self, context: RuntimeInstallationContext, *, now: datetime
    ) -> ComponentsProjection:
        now = utc_time(now)
        try:
            records = self._repository.load(context)
        except Exception:
            return ComponentsProjection(
                installation_id=context.installation_id,
                deployment_profile=context.deployment_profile,
                available=False,
                channel=ChannelProjection(state="unavailable"),
            )
        rows = [
            _row(record, now=now, config=self._config)
            for record in records
            if record.installation_id == context.installation_id
            and record.deployment_profile == context.deployment_profile
        ]
        groups: dict[tuple[str, UUID, UUID, str], list[ComponentRow]] = defaultdict(list)
        for row in rows:
            groups[
                (row.service_type, row.instance_id, row.incarnation_id, row.observation_kind)
            ].append(row)
        for key, group in groups.items():
            if key[3] == "process" and any(row.source_availability == "retired" for row in group):
                # Retirement is terminal for this exact process incarnation, not
                # another replica or a later incarnation of the same instance.
                # Keep each source's historical facts, but no cached report can
                # make a retired process current again through its longer TTL.
                groups[key] = [
                    row.model_copy(update={"source_freshness": "retired"}) for row in group
                ]
        history = _superseded_groups(groups)
        rows = [
            row.model_copy(update={"conflicting": _conflict(group), "superseded": key in history})
            for key, group in groups.items()
            for row in group
        ]
        processes = [
            group for key, group in groups.items() if key[3] == "process" and key not in history
        ]
        fresh_processes = [group for group in processes if any(_fresh(row) for row in group)]
        roles = {
            row.service_type for row in rows if row.observation_kind in {"process", "role_probe"}
        }
        roles.update(name for name, count in context.expectations if count != 0)
        observed_roles = {
            row.service_type
            for row in rows
            if row.observation_kind in {"process", "role_probe"} and _fresh(row)
        }
        per_role: dict[str, int] = defaultdict(int)
        for group in fresh_processes:
            per_role[group[0].service_type] += 1
        # A role nothing reports as a process (Compact's router and MCP server)
        # is known only by its probe: a fresh answering probe is its instance.
        process_roles = {group[0].service_type for group in processes}
        for row in rows:
            if (
                row.observation_kind == "role_probe"
                and row.service_type not in process_roles
                and not row.superseded
                and _fresh(row)
            ):
                per_role[row.service_type] = max(per_role[row.service_type], 1)
        gaps = []
        for name, target in context.expectations:
            if target == 0:
                continue
            missing = name not in observed_roles if target is None else per_role[name] < target
            if missing:
                reason: Literal["missing", "stale", "replica_target_unobserved"]
                if not any(row.service_type == name for row in rows):
                    reason = "missing"
                elif name not in observed_roles:
                    reason = "stale"
                else:
                    reason = "replica_target_unobserved"
                gaps.append(
                    ExpectedGap(
                        service_type=name,
                        desired_count=target,
                        observed_count=per_role[name],
                        reason=reason,
                    )
                )

        def evidenced(group: list[ComponentRow], dimension: str, state: str) -> bool:
            return not _conflict(group) and any(
                _fresh(row)
                and (check := getattr(row, dimension)).freshness == "fresh"
                and check.state == state
                for row in group
            )

        dependency_names = {
            row.service_type for row in rows if row.observation_kind == "dependency"
        } | set(context.dependencies)
        for name in context.dependencies:
            if not any(
                row.service_type == name and row.observation_kind == "dependency" for row in rows
            ):
                gaps.append(
                    ExpectedGap(
                        service_type=name,
                        desired_count=None,
                        observed_count=0,
                        observation_kind="dependency",
                        reason="not_observed",
                    )
                )

        counts = ComponentCounts(
            roles_total=len(roles),
            roles_observed=len(observed_roles),
            instances_total=len(processes),
            instances_superseded=sum(1 for key in history if key[3] == "process"),
            instances_fresh=len(fresh_processes),
            instances_stale=len(processes) - len(fresh_processes),
            instances_ready=sum(evidenced(group, "readiness", "ready") for group in processes),
            instances_evidenced_healthy=sum(
                evidenced(group, "health", "healthy") for group in processes
            ),
            dependencies_total=len(dependency_names),
        )
        known_targets = bool(context.expectations) and all(
            target is not None for _, target in context.expectations
        )
        return ComponentsProjection(
            installation_id=context.installation_id,
            deployment_profile=context.deployment_profile,
            available=True,
            channel=_channel([row for row in rows if not row.superseded]),
            rows=tuple(
                sorted(rows, key=lambda row: (row.service_type, str(row.instance_id), row.source))
            ),
            expected_gaps=tuple(gaps),
            counts=counts,
            completeness=not any(gap.observation_kind == "process" for gap in gaps)
            if known_targets
            else None,
        )
