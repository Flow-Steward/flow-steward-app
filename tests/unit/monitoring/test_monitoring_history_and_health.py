"""Monitoring tells current from history, and health from silence.

A restart used to leave every old process behind as "stale" for a week, health
was never measured, and a Compact probe read "stale" most of each minute. These
tests pin what an operator should see instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from core.application.monitoring.contracts import (
    HeartbeatReport,
    RuntimeInstallationContext,
    StatusRecord,
    build_observation_reports,
)
from core.application.monitoring.queries import RuntimeStatusQueryService
from core.infrastructure.runtime.monitoring.heartbeat_runtime import RuntimeObservationSource
from core.infrastructure.runtime.monitoring.observations import SourceObservation

INSTALLATION = "4ba8a97c-722c-4ab7-9832-4129821ca88a"


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def load(self, _context):
        return self.rows


def _context(profile: str = "scalable", **changes) -> RuntimeInstallationContext:
    return RuntimeInstallationContext(
        installation_id=INSTALLATION, deployment_profile=profile, **changes
    )


def _stored(
    observation: SourceObservation, *, published: datetime, received: datetime, profile="scalable"
):
    (report,) = build_observation_reports(
        (observation,), context=_context(profile), published_at=published
    )
    return StatusRecord(**report.model_dump(), id=uuid4(), received_at=received)


def _process(*, started: datetime, checked: datetime, **changes) -> SourceObservation:
    values = {
        "service_type": "app",
        "source": "self",
        "original_process_id": str(uuid4()),
        "observer_id": uuid4(),
        "observation_kind": "process",
        "source_sequence": 1,
        "checked_at": checked,
        "source_age_seconds": 0,
        "source_stale_seconds": 180,
        "availability": "observed",
        "lifecycle": "running",
        "started_at": started,
    }
    values.update(changes)
    return SourceObservation(**values)


def _project(rows, now, profile="scalable", **context):
    return RuntimeStatusQueryService(repository=_Rows(rows)).get_components(
        _context(profile, **context), now=now
    )


NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


def test_a_restart_moves_the_old_process_to_history_instead_of_stale():
    old_start = NOW - timedelta(days=2)
    old = _stored(
        _process(started=old_start, checked=old_start + timedelta(hours=1)),
        published=old_start + timedelta(hours=1),
        received=old_start + timedelta(hours=1),
    )
    current = _stored(
        _process(started=NOW - timedelta(minutes=5), checked=NOW),
        published=NOW,
        received=NOW,
    )

    result = _project([old, current], NOW, expectations=(("app", None),))

    by_instance = {row.instance_id: row for row in result.rows}
    assert by_instance[old.instance_id].superseded is True
    assert by_instance[current.instance_id].superseded is False
    assert result.counts.instances_total == 1
    assert result.counts.instances_stale == 0
    assert result.counts.instances_superseded == 1
    assert result.expected_gaps == ()


def test_a_replica_that_stopped_reporting_while_others_run_stays_a_current_problem():
    """Two replicas started together; one went silent. That is not history."""
    start = NOW - timedelta(hours=3)
    silent = _stored(
        _process(started=start, checked=NOW - timedelta(hours=1), service_type="worker"),
        published=NOW - timedelta(hours=1),
        received=NOW - timedelta(hours=1),
    )
    running = _stored(
        _process(started=start, checked=NOW, service_type="worker"),
        published=NOW,
        received=NOW,
    )

    result = _project([silent, running], NOW, expectations=(("worker", 2),))

    stale = next(row for row in result.rows if row.instance_id == silent.instance_id)
    assert stale.superseded is False
    assert stale.freshness == "stale"
    assert result.counts.instances_stale == 1
    assert [gap.service_type for gap in result.expected_gaps] == ["worker"]


def test_a_process_that_says_it_stopped_is_history_at_once():
    source = RuntimeObservationSource(service_type="app", utcnow=lambda: NOW)
    source.mark_stopped()
    stopped = _stored(source.observe(), published=NOW, received=NOW)

    assert stopped.payload.lifecycle == "stopped"
    assert stopped.payload.health.state == "not_observed"
    result = _project([stopped], NOW)
    assert result.rows[0].superseded is True
    assert result.counts.instances_total == 0


def test_rows_from_before_start_times_give_way_to_a_fresh_sibling():
    legacy = _stored(
        _process(started=None, checked=NOW - timedelta(days=3)),
        published=NOW - timedelta(days=3),
        received=NOW - timedelta(days=3),
    )
    current = _stored(_process(started=None, checked=NOW), published=NOW, received=NOW)

    result = _project([legacy, current], NOW)

    assert next(r for r in result.rows if r.instance_id == legacy.instance_id).superseded


def test_a_running_process_reports_a_self_report_health_verdict():
    row = _stored(_process(started=NOW, checked=NOW), published=NOW, received=NOW)

    assert row.payload.health.state == "healthy"
    assert row.payload.health.evidence == "self_report"
    result = _project([row], NOW)
    assert result.counts.instances_evidenced_healthy == 1


def test_a_failed_dependency_probe_is_unhealthy_with_its_evidence():
    probe = SourceObservation(
        service_type="postgres",
        source="compact.postgres",
        original_process_id=None,
        observer_id=uuid4(),
        observation_kind="dependency",
        source_sequence=1,
        checked_at=NOW,
        source_age_seconds=0,
        source_stale_seconds=10,
        availability="observed",
        readiness=False,
    )
    (report,) = build_observation_reports((probe,), context=_context("compact"), published_at=NOW)

    assert report.payload.health.state == "unhealthy"
    assert report.payload.health.evidence == "dependency_probe"


def test_a_compact_probe_stays_fresh_between_publications():
    """A 10-second probe budget applies at publication, not for the whole minute after."""
    probe = SourceObservation(
        service_type="postgres",
        source="compact.postgres",
        original_process_id=None,
        observer_id=uuid4(),
        observation_kind="dependency",
        source_sequence=1,
        checked_at=NOW,
        source_age_seconds=1,
        source_stale_seconds=10,
        availability="observed",
        readiness=True,
        started_at=NOW - timedelta(hours=1),
    )
    stored = _stored(probe, published=NOW, received=NOW, profile="compact")

    result = _project([stored], NOW + timedelta(seconds=55), profile="compact")

    row = result.rows[0]
    assert row.source_freshness == "fresh"
    assert row.readiness.freshness == "fresh"
    assert row.health.state == "healthy"


def test_the_scalable_receiver_reports_the_broker_and_the_database():
    from core.application.monitoring.runtime_composition import _receiver_dependencies

    channel = SourceObservation(
        service_type="control",
        source="heartbeat.receiver",
        original_process_id=None,
        observer_id=uuid4(),
        observation_kind="channel",
        source_sequence=1,
        checked_at=NOW,
        source_age_seconds=0,
        source_stale_seconds=180,
        availability="observed",
        readiness=True,
        lifecycle="running",
        started_at=NOW,
    )

    import core.application.monitoring.runtime_composition as composition

    probed: list[str] = []
    original = composition.probe_http_role
    composition.probe_http_role = lambda url: probed.append(url) or True
    try:
        rows = _receiver_dependencies(channel, _context("scalable"))
    finally:
        composition.probe_http_role = original
    assert {(row.service_type, row.observation_kind, row.readiness) for row in rows} == {
        ("rabbitmq", "dependency", True),
        ("postgres", "dependency", True),
        ("mcp", "role_probe", True),
    }
    assert probed
    assert probed[0].endswith("/mcp")
    assert _receiver_dependencies(channel, _context("compact")) == []


def test_the_publisher_sends_a_last_report_when_it_closes():
    from threading import Event

    from core.infrastructure.runtime.monitoring.heartbeat_runtime import start_monitoring_publisher

    sent: list[bytes] = []
    first = Event()

    def publish(body: bytes) -> None:
        sent.append(body)
        first.set()

    handle = start_monitoring_publisher(
        collect=lambda: (b"running",),
        publish=publish,
        idle=lambda: None,
        close=lambda: None,
        interval_seconds=60,
        close_timeout_seconds=2,
        farewell=lambda: (b"stopped",),
    )
    assert first.wait(2)
    handle.close()

    assert sent[0] == b"running"
    assert sent[-1] == b"stopped"


def test_a_report_without_a_start_time_still_validates():
    body = HeartbeatReport.model_validate(
        {
            "schema_version": 1,
            "installation_id": INSTALLATION,
            "deployment_profile": "scalable",
            "service_type": "app",
            "instance_id": str(uuid4()),
            "incarnation_id": str(uuid4()),
            "sequence": 1,
            "observed_at": NOW.isoformat(),
            "source": "self",
            "payload": {
                "type": "runtime_status",
                "version": 1,
                "lifecycle": "running",
                "readiness": {"state": "not_observed"},
                "health": {"state": "not_observed"},
            },
        }
    )
    assert body.payload.started_at is None


def test_a_role_known_only_by_its_probe_counts_as_running():
    """Compact's router and MCP server report no process of their own."""
    probe = SourceObservation(
        service_type="mcp",
        source="compact.mcp",
        original_process_id=None,
        observer_id=uuid4(),
        observation_kind="role_probe",
        source_sequence=1,
        checked_at=NOW,
        source_age_seconds=0,
        source_stale_seconds=10,
        availability="observed",
        readiness=True,
        started_at=NOW,
    )
    stored = _stored(probe, published=NOW, received=NOW, profile="compact")

    result = _project([stored], NOW, profile="compact", expectations=(("mcp", 1),))

    assert result.expected_gaps == ()


def test_a_restarted_receiver_replaces_the_old_channel_at_once():
    def channel(started):
        return SourceObservation(
            service_type="control",
            source="heartbeat.receiver",
            original_process_id=None,
            observer_id=uuid4(),
            observation_kind="channel",
            source_sequence=1,
            checked_at=NOW,
            source_age_seconds=0,
            source_stale_seconds=180,
            availability="observed",
            readiness=True,
            lifecycle="running",
            started_at=started,
        )

    old = _stored(channel(NOW - timedelta(hours=1)), published=NOW, received=NOW)
    new = _stored(channel(NOW - timedelta(seconds=30)), published=NOW, received=NOW)

    result = _project([old, new], NOW)

    assert result.channel.state == "connected"
    superseded = {row.instance_id for row in result.rows if row.superseded}
    assert superseded == {old.instance_id}
