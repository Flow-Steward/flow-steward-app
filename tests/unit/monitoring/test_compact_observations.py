from datetime import UTC, datetime

import pytest

from core.application.monitoring.contracts import (
    RuntimeInstallationContext,
    build_observation_reports,
)
from core.infrastructure.runtime.compact.readiness import ReadinessRegistry
from core.infrastructure.runtime.monitoring.compact_observations import read_compact_observations


def test_expired_worker_provenance_is_not_refreshed_by_bridge():
    tick = [0.0]
    accepted = datetime(2026, 10, 1, tzinfo=UTC)
    registry = ReadinessRegistry(
        worker_processes=2, monotonic=lambda: tick[0], utcnow=lambda: accepted
    )
    registry.record_worker(
        role="worker-0", worker_incarnation="host-token-1", state="registered", sequence=2
    )
    before = registry.snapshot()
    tick[0] = 120
    rows = read_compact_observations(registry)
    worker = next(row for row in rows if row.original_process_id == "host-token-1")
    assert worker.availability == "expired"
    assert worker.checked_at == accepted
    assert worker.source_sequence == 2
    assert worker.source_age_seconds == 120
    missing = next(row for row in rows if row.source == "compact.worker-1")
    assert missing.original_process_id is None
    assert missing.availability == "missing"
    context = RuntimeInstallationContext(installation_id="test", deployment_profile="compact")
    reports = build_observation_reports(rows, context=context, published_at=accepted)
    report = next(row for row in reports if row.service_type == "worker")
    assert report.payload.source_availability == "expired"
    assert report.payload.source_age_seconds == 120
    assert report.payload.readiness.checked_at == accepted
    # The consumer state is the worker's health evidence; that it has since
    # expired is the report's freshness, judged separately downstream.
    assert report.payload.health.state == "healthy"
    assert report.payload.health.evidence == "consumer_state"
    assert report.payload.health.checked_at == accepted
    assert not registry.snapshot().worker_states
    assert before.worker_states == {"worker-0": "registered"}
    assert len(rows) == 9
    assert read_compact_observations(registry) == rows


def test_probe_identity_stable_and_role_reset_not_a_new_check():
    registry = ReadinessRegistry(worker_processes=1)
    registry.record_role_probe("app", True)
    row = next(row for row in registry.observations() if row.service_type == "app")
    registry.reset_role("app")
    reset = next(row for row in registry.observations() if row.service_type == "app")
    assert reset.checked_at == row.checked_at
    assert reset.availability == "retired"
    assert reset.observer_id == row.observer_id
    assert reset.original_process_id is None
    assert reset.observation_kind == "role_probe"


def test_restart_retires_source_without_fake_missing_identity():
    registry = ReadinessRegistry(worker_processes=1, monotonic=lambda: 0)
    registry.record_worker(role="worker-0", worker_incarnation="first", state="active", sequence=1)
    registry.reset_role("worker-0")
    retired = next(row for row in registry.observations() if row.service_type == "worker")
    assert retired.original_process_id == "first"
    assert retired.availability == "retired"
    registry.record_worker(
        role="worker-0", worker_incarnation="second", state="registered", sequence=1
    )
    current = next(row for row in registry.observations() if row.service_type == "worker")
    assert current.original_process_id == "second"
    registry.record_worker(role="worker-0", worker_incarnation="first", state="active", sequence=99)
    assert next(row for row in registry.observations() if row.service_type == "worker") == current


@pytest.mark.parametrize(
    "state", ["registered", "active", "maintenance_paused", "draining", "disconnected"]
)
def test_source_refresh_retains_actual_state_and_identity(state):
    tick = [0.0]
    registry = ReadinessRegistry(worker_processes=1, monotonic=lambda: tick[0])
    registry.record_worker(role="worker-0", worker_incarnation=" raw-id ", state=state, sequence=1)
    tick[0] = 20
    registry.record_worker(role="worker-0", worker_incarnation=" raw-id ", state=state, sequence=2)
    row = next(row for row in registry.observations() if row.service_type == "worker")
    assert row.availability == "observed"
    assert row.source_age_seconds == 0
    assert row.original_process_id == " raw-id "
    assert row.consumer_state == state
    assert row.source_sequence == 2
    registry.record_worker(
        role="worker-0", worker_incarnation=" raw-id ", state="active", sequence=1
    )
    assert next(row for row in registry.observations() if row.service_type == "worker") == row


def test_fractional_compact_ttl_survives_mapping_without_rounding():
    registry = ReadinessRegistry(worker_processes=1, stale_seconds=0.5)
    registry.record_worker(
        role="worker-0", worker_incarnation="process", state="active", sequence=1
    )
    reports = build_observation_reports(
        registry.observations(),
        context=RuntimeInstallationContext(installation_id="test", deployment_profile="compact"),
        published_at=datetime.now(UTC),
    )
    assert reports[0].payload.source_stale_seconds == 0.5
    assert reports[0].payload.readiness.stale_seconds == 0.5
