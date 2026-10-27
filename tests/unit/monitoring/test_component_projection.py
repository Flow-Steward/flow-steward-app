from datetime import timedelta
from uuid import uuid4

import pytest

from core.application.monitoring.contracts import HeartbeatReport, StatusRecord


class StoredRows:
    def __init__(self, rows):
        self.rows = rows

    def load(self, _context):
        return self.rows


def record(report_body, now, **changes):
    report = HeartbeatReport.model_validate_json(report_body(**changes))
    return StatusRecord(**report.model_dump(), id=uuid4(), received_at=now)


def project(rows, context, now):
    from core.application.monitoring.queries import RuntimeStatusQueryService

    return RuntimeStatusQueryService(repository=StoredRows(rows)).get_components(context, now=now)


@pytest.mark.parametrize("age,freshness", [(180, "fresh"), (181, "stale")])
def test_report_age_boundary_and_delayed_receipt(report_body, context, now, age, freshness):
    row = record(report_body, now, observed_at=(now - timedelta(seconds=age)).isoformat())
    result = project([row], context, now)
    assert result.rows[0].freshness == freshness
    assert result.rows[0].report_age_seconds == age
    assert result.rows[0].receive_age_seconds == 0
    assert result.counts.instances_fresh == (freshness == "fresh")
    assert result.completeness is None


def test_dynamic_names_scope_and_incarnations_are_not_slot_replacements(report_body, context, now):
    rows = [
        record(report_body, now),
        record(report_body, now, instance_id=str(uuid4()), incarnation_id=str(uuid4())),
        record(report_body, now, service_type="image-indexer"),
        record(report_body, now, installation_id="another"),
        record(report_body, now, deployment_profile="compact"),
    ]
    result = project(rows, context, now)
    assert {row.service_type for row in result.rows} == {"worker", "image-indexer"}
    assert result.counts.roles_total == 2
    assert result.counts.instances_total == 3
    assert result.completeness is None


@pytest.mark.parametrize("retirement_age", [0, 181])
@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_retired_incarnation_cannot_be_ready_through_cached_self_report(
    report_body, context, now, retirement_age, profile
):
    context = context.model_copy(
        update={"deployment_profile": profile, "expectations": (("worker", 2),)}
    )
    dead = record(report_body, now, deployment_profile=profile)
    dead = dead.model_copy(
        update={
            "payload": dead.payload.model_copy(
                update={
                    "readiness": dead.payload.readiness.model_copy(
                        update={"state": "ready", "checked_at": now, "evidence": "consumer_state"}
                    ),
                    "health": dead.payload.health.model_copy(
                        update={"state": "healthy", "checked_at": now, "evidence": "probe"}
                    ),
                }
            )
        }
    )
    retired = dead.model_copy(
        update={
            "id": uuid4(),
            "source": "compact.worker-0" if profile == "compact" else "process.observer",
            "observed_at": now - timedelta(seconds=retirement_age),
            "payload": dead.payload.model_copy(update={"source_availability": "retired"}),
        }
    )
    # A different incarnation of the same instance and a genuinely independent
    # replica must survive retirement; neither is a global "latest worker" slot.
    replacement = dead.model_copy(update={"id": uuid4(), "incarnation_id": uuid4()})
    replica = dead.model_copy(
        update={"id": uuid4(), "instance_id": uuid4(), "incarnation_id": uuid4()}
    )
    result = project([dead, retired, replacement, replica], context, now)

    # The retired incarnation is history: shown, but not a current instance.
    assert result.counts.instances_total == 2
    assert result.counts.instances_superseded == 1
    assert result.counts.instances_fresh == 2
    assert result.counts.instances_stale == 0
    assert result.counts.instances_ready == 2
    assert result.counts.instances_evidenced_healthy == 2
    assert result.expected_gaps == ()
    historical = [row for row in result.rows if row.incarnation_id == dead.incarnation_id]
    assert len(historical) == 2
    assert all(row.source_freshness == "retired" for row in historical)
    assert all(row.superseded for row in historical)
    cached = next(row for row in historical if row.source == "self")
    assert cached.source_availability == "observed"
    assert cached.lifecycle == "running"
    assert cached.readiness.state == "ready"
    assert dead.payload.source_availability == "observed"


def test_retired_only_worker_leaves_expected_gap_without_hiding_history(report_body, context, now):
    context = context.model_copy(update={"expectations": (("worker", 1),)})
    cached = record(report_body, now)
    retired = cached.model_copy(
        update={
            "id": uuid4(),
            "source": "process.observer",
            "payload": cached.payload.model_copy(update={"source_availability": "retired"}),
        }
    )
    result = project([cached, retired], context, now)
    assert len(result.rows) == 2
    assert result.counts.roles_observed == 0
    assert result.counts.instances_fresh == 0
    assert result.counts.instances_ready == 0
    assert result.completeness is False
    assert len(result.expected_gaps) == 1
    assert result.expected_gaps[0].observed_count == 0
    assert result.expected_gaps[0].reason == "stale"


def test_expected_gaps_survive_cleanup_and_disabled_roles_are_not_missing(context, now):
    context = context.model_copy(
        update={"expectations": (("worker", 2), ("optional", 0), ("image-indexer", None))}
    )
    result = project([], context, now)
    assert {gap.service_type for gap in result.expected_gaps} == {"worker", "image-indexer"}
    assert result.available is True
    assert result.completeness is None


def test_duplicate_sources_conflict_without_extra_replica(report_body, context, now):
    first = record(report_body, now)
    ready = {"state": "ready", "checked_at": now, "evidence": "consumer_state"}
    healthy = {"state": "healthy", "checked_at": now, "evidence": "probe"}
    first = first.model_copy(
        update={
            "payload": first.payload.model_copy(
                update={
                    "readiness": first.payload.readiness.model_validate(ready),
                    "health": first.payload.health.model_validate(healthy),
                }
            )
        }
    )
    second = first.model_copy(
        update={
            "id": uuid4(),
            "source": "observer",
            "payload": first.payload.model_copy(
                update={"health": first.payload.health.model_copy(update={"state": "unhealthy"})}
            ),
        }
    )
    result = project([first, second], context, now)
    assert result.counts.instances_total == 1
    assert result.counts.instances_evidenced_healthy == 0
    assert all(row.conflicting for row in result.rows)


def test_fresh_publish_does_not_refresh_cached_check(report_body, context, now):
    row = record(report_body, now)
    check = row.payload.health.model_validate(
        {
            "state": "healthy",
            "checked_at": now - timedelta(seconds=181),
            "evidence": "probe",
            "stale_seconds": 180.0,
        }
    )
    row = row.model_copy(
        update={
            "payload": row.payload.model_copy(
                update={"health": check, "source_age_seconds": 181, "source_stale_seconds": 180.0}
            )
        }
    )
    result = project([row], context, now)
    assert result.rows[0].freshness == "fresh"
    assert result.rows[0].health.freshness == "stale"
    assert result.rows[0].source_age_seconds == 181
    assert result.counts.instances_evidenced_healthy == 0


def test_dependency_probe_and_receiver_never_count_as_processes(report_body, context, now):
    row = record(report_body, now)
    dependency = row.model_copy(
        update={
            "service_type": "postgres",
            "payload": row.payload.model_copy(update={"observation_kind": "dependency"}),
        }
    )
    channel = row.model_copy(
        update={
            "service_type": "control",
            "source": "heartbeat.receiver",
            "payload": row.payload.model_copy(update={"observation_kind": "channel"}),
        }
    )
    result = project([dependency, channel], context, now)
    assert result.counts.instances_total == 0
    assert result.counts.roles_total == 0
    assert result.counts.dependencies_total == 1
    assert result.channel.state == "not_observed"


def test_paused_consumer_is_not_synthetic_failure(report_body, context, now):
    row = record(report_body, now)
    row = row.model_copy(
        update={"payload": row.payload.model_copy(update={"consumer_state": "maintenance_paused"})}
    )
    result = project([row], context, now)
    assert result.rows[0].consumer_state == "maintenance_paused"
    assert result.rows[0].health.state == "not_observed"
    assert result.counts.instances_evidenced_healthy == 0


def test_storage_unavailable_is_not_empty_green(context, now):
    class Unavailable:
        def load(self, _context):
            raise ConnectionError("credential-bearing database detail")

    from core.application.monitoring.queries import RuntimeStatusQueryService

    result = RuntimeStatusQueryService(repository=Unavailable()).get_components(context, now=now)
    assert result.available is False
    assert result.counts is None
    assert result.completeness is None
    assert "credential" not in result.model_dump_json()


def test_unprobed_configured_dependencies_are_not_fictional_processes(context, now):
    context = context.model_copy(update={"dependencies": ("object-store", "external-ingress")})
    result = project([], context, now)
    assert result.counts.dependencies_total == 2
    assert result.counts.instances_total == result.counts.roles_total == 0
    assert {
        gap.service_type for gap in result.expected_gaps if gap.observation_kind == "dependency"
    } == {"object-store", "external-ingress"}
    assert all(gap.reason == "not_observed" for gap in result.expected_gaps)
