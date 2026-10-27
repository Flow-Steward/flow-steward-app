"""Actual source → AMQP → committed PostgreSQL → admin API, in both profiles."""

from __future__ import annotations

import socket
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pika
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.monitoring.contracts import (
    RuntimeInstallationContext,
    build_observation_reports,
    encode_heartbeat,
)
from core.application.monitoring.ingestion import RuntimeStatusIngestionService
from core.application.monitoring.repository import RuntimeStatusRepository
from core.application.monitoring.runtime_composition import start_status_consumer
from core.entrypoints.api.system_monitoring import create_api_system_monitoring_router
from core.infrastructure.config.monitoring_runtime import load_monitoring_runtime_config
from core.infrastructure.runtime.compact.readiness import ReadinessRegistry
from core.infrastructure.runtime.monitoring.compact_observations import read_compact_observations
from core.infrastructure.runtime.monitoring.heartbeat_runtime import (
    CompositeConsumerSink,
    RuntimeObservationSource,
)
from core.infrastructure.runtime.monitoring.heartbeat_transport import (
    RabbitHeartbeatPublisher,
    status_namespace,
)
from core.infrastructure.runtime.monitoring.observations import SourceObservation
from tests.integration.test_runtime_status_rabbitmq import BrokerProxy, wait_until
from tests.integration.test_runtime_status_rabbitmq import broker_url as broker_url
from tests.integration.test_runtime_status_rabbitmq import database as database

pytestmark = pytest.mark.integration


class AcceptanceStack:
    def __init__(self, *, profile, broker_url, dsn, monkeypatch):
        self.dsn = dsn
        self.context = RuntimeInstallationContext(
            installation_id=str(uuid4()),
            deployment_profile=profile,
            expectations=(("worker", None),),
            dependencies=("postgres",),
        )
        self.config = replace(load_monitoring_runtime_config({}), interval_seconds=1)
        self.repository = RuntimeStatusRepository(dsn, config=self.config)
        monkeypatch.setenv("FS_RABBITMQ_TRANSPORT_MODE", "loopback-local")
        self.proxy = BrokerProxy(broker_url)
        self.namespace = status_namespace(self.context.installation_id, profile)
        self.namespaces = {self.namespace}
        self.sequence = 0
        self.tick = 0.0
        self.worker_id = "opaque/audit-worker:" + uuid4().hex
        self.registry = ReadinessRegistry(
            worker_processes=1, stale_seconds=180, monotonic=lambda: self.tick
        )
        self.source = RuntimeObservationSource(
            service_type="worker", original_process_id=self.worker_id, monotonic=lambda: self.tick
        )
        self.sink = CompositeConsumerSink(self.source, None)
        monkeypatch.setattr(
            "core.application.monitoring.runtime_composition.resolve_rabbitmq_url",
            lambda: self.proxy.url,
        )
        ingestion = RuntimeStatusIngestionService(
            repository=self.repository,
            context=self.context,
            config=self.config,
            clock=lambda: datetime.now(UTC),
        )
        self.consumer = start_status_consumer(
            context=self.context,
            config=self.config,
            ingestion=ingestion,
            repository=self.repository,
        )
        self.publisher = RabbitHeartbeatPublisher(
            url=self.proxy.url, namespace=self.namespace, config=self.config
        )
        values = {
            "FS_INSTALLATION_ID": self.context.installation_id,
            "FS_DEPLOYMENT_PROFILE": profile,
            "FS_MONITORING_EXPECTATIONS": '{"worker":null}',
            "FS_MONITORING_DEPENDENCIES": '["postgres"]',
            "FS_DB_DSN": dsn,
        }
        monkeypatch.setattr(
            "core.application.monitoring.system.runtime_env", SimpleNamespace(copy=lambda: values)
        )
        # Only unrelated legacy storage/readiness fixtures are replaced. The
        # router's default component query, AMQP ingestion and DB reads are real.
        monkeypatch.setattr("core.entrypoints.api.system_monitoring.deps.get_logger_only", object)
        monkeypatch.setattr(
            "core.entrypoints.api.system_monitoring.deps.compute_readiness",
            lambda _bl: {"ready": True, "checks": {}},
        )
        monkeypatch.setattr(
            "core.entrypoints.api.system_monitoring.build_monitoring_meta", lambda *_a, **_k: {}
        )
        app = FastAPI()
        app.include_router(
            create_api_system_monitoring_router(
                local_auth_enabled=lambda: True,
                external_auth_enabled=False,
                require_authenticated_user=lambda *_a, **_k: {"roles": ["admin"]},
                auth_admin_roles={"admin"},
            )
        )
        self.client = TestClient(app)

    def record_worker(self, state, *, worker_id=None):
        if worker_id is not None:
            self.worker_id = worker_id
            self.source = RuntimeObservationSource(
                service_type="worker", original_process_id=worker_id, monotonic=lambda: self.tick
            )
            self.sink = CompositeConsumerSink(self.source, None)
        if self.context.deployment_profile == "compact":
            self.sequence += 1
            self.registry.record_worker(
                role="worker-0",
                worker_incarnation=self.worker_id,
                state=state,
                sequence=self.sequence,
            )
        else:
            self.sink.record(state)

    def observe(self):
        if self.context.deployment_profile == "compact":
            return read_compact_observations(self.registry)
        return (self.source.observe(),)

    def publish(self, observations=None):
        reports = build_observation_reports(
            self.observe() if observations is None else observations,
            context=self.context,
            published_at=datetime.now(UTC),
        )
        for report in reports:
            self.sequence += 1
            report = report.model_copy(update={"sequence": self.sequence})
            self.publisher.publish(encode_heartbeat(report))
            wait_until(
                lambda report=report: any(
                    row.source == report.source
                    and row.incarnation_id == report.incarnation_id
                    and row.sequence == report.sequence
                    for row in self.repository.load(self.context)
                )
            )

    def summary(self):
        response = self.client.get("/api/system/monitoring/summary")
        assert response.status_code == 200, response.text
        return response.json()["components"]

    def worker(self):
        return next(row for row in self.summary()["rows"] if row["service_type"] == "worker")

    def close(self, broker_url):
        self.publisher.close()
        self.consumer.close()
        self.client.close()
        self.proxy.close()
        assert all(not thread.is_alive() for thread in self.consumer.threads)
        connection = pika.BlockingConnection(pika.URLParameters(broker_url))
        try:
            for namespace in self.namespaces:
                connection.channel().queue_delete(queue=namespace)
                connection.channel().exchange_delete(exchange=namespace)
        finally:
            connection.close()


@pytest.fixture(params=["compact", "scalable"])
def acceptance_stack(request, broker_url, database, monkeypatch):
    stack = AcceptanceStack(
        profile=request.param, broker_url=broker_url, dsn=database, monkeypatch=monkeypatch
    )
    try:
        yield stack
    finally:
        stack.close(broker_url)


@pytest.mark.parametrize("state", ["registered", "active"])
def test_idle_and_busy_consumer_are_ready_and_healthy_by_consumer_state(acceptance_stack, state):
    stack = acceptance_stack
    stack.record_worker(state)
    stack.publish()
    body = stack.summary()
    assert stack.worker()["consumer_state"] == state
    assert stack.worker()["readiness"]["state"] == "ready"
    # A connected consumer is the worker's health evidence, and says so.
    assert stack.worker()["health"]["state"] == "healthy"
    assert stack.worker()["health"]["evidence"] == "consumer_state"
    assert body["counts"]["instances_ready"] == 1
    assert body["counts"]["instances_evidenced_healthy"] == 1
    assert body["completeness"] is None


def test_original_source_expiry_survives_transport(acceptance_stack):
    stack = acceptance_stack
    stack.record_worker("registered")
    stack.publish()
    original = stack.worker()["readiness"]["checked_at"]
    stack.tick = 181
    stack.publish()
    row = stack.worker()
    assert row["readiness"]["checked_at"] == original
    assert row["source_age_seconds"] >= 181
    assert row["source_freshness"] == "stale"
    assert row["readiness"]["freshness"] == "stale"
    assert row["health"]["freshness"] == "stale"
    assert stack.summary()["counts"]["instances_ready"] == 0
    assert stack.summary()["counts"]["instances_evidenced_healthy"] == 0


def test_failed_dependency_is_not_counted_as_a_process(acceptance_stack):
    stack = acceptance_stack
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    endpoint = listener.getsockname()
    listener.close()
    with pytest.raises(OSError):
        socket.create_connection(endpoint, timeout=1)
    if stack.context.deployment_profile == "compact":
        stack.registry.set_dependency("postgres", False)
        rows = stack.observe()
    else:
        rows = (
            SourceObservation(
                service_type="postgres",
                source="dependency.tcp",
                original_process_id=None,
                observer_id=uuid4(),
                observation_kind="dependency",
                source_sequence=1,
                checked_at=datetime.now(UTC),
                source_age_seconds=0,
                source_stale_seconds=180,
                availability="observed",
                readiness=False,
            ),
        )
    stack.publish(rows)
    body = stack.summary()
    failed_source = (
        "compact.postgres" if stack.context.deployment_profile == "compact" else "dependency.tcp"
    )
    failed_rows = [
        row
        for row in body["rows"]
        if row["service_type"] == "postgres" and row["source"] == failed_source
    ]
    assert len(failed_rows) == 1
    row = failed_rows[0]
    assert row["readiness"]["state"] == "not_ready"
    assert row["health"]["state"] == "unhealthy"
    assert row["health"]["evidence"] == "dependency_probe"
    assert row["observation_kind"] == "dependency"
    if stack.context.deployment_profile == "scalable":
        receiver_rows = [
            row
            for row in body["rows"]
            if row["service_type"] == "postgres" and row["source"] == "control.postgres"
        ]
        assert len(receiver_rows) == 1
        receiver = receiver_rows[0]
        assert receiver["readiness"]["state"] == "ready"
        assert receiver["health"]["state"] == "healthy"
        assert receiver["health"]["evidence"] == "dependency_probe"
        assert receiver["observation_kind"] == "dependency"
    # The receiver also reports RabbitMQ in Scalable. The two PostgreSQL
    # sources remain one dependency; neither dependency is counted as a process.
    expected_dependencies = 2 if stack.context.deployment_profile == "scalable" else 1
    assert body["counts"]["dependencies_total"] == expected_dependencies
    assert body["counts"]["instances_total"] == 0


def test_broker_outage_keeps_last_status_readable_and_consumer_recovers(acceptance_stack):
    stack = acceptance_stack
    stack.record_worker("registered")
    stack.publish()
    original = stack.worker()["observed_at"]
    wait_until(lambda: stack.summary()["channel"]["state"] == "connected")
    stack.proxy.close()
    wait_until(lambda: stack.summary()["channel"]["state"] == "disconnected")
    assert stack.worker()["observed_at"] == original
    stack.proxy.start()
    wait_until(lambda: stack.summary()["channel"]["state"] == "connected")
    stack.record_worker("active")
    # Reset only the publisher's test-owned connection after proxy socket loss.
    stack.publisher.close()
    stack.publish()
    assert stack.worker()["consumer_state"] == "active"
    assert stack.worker()["observed_at"] != original


def test_stored_old_report_is_stale_without_an_on_request_broker_probe(acceptance_stack):
    stack = acceptance_stack
    stack.record_worker("registered")
    stack.publish()
    with psycopg.connect(stack.dsn) as connection:
        connection.execute(
            "UPDATE public.fs_runtime_status_current SET observed_at=observed_at-interval '181 seconds' "
            "WHERE installation_id=%s AND service_type='worker'",
            (stack.context.installation_id,),
        )
    assert stack.worker()["freshness"] == "stale"
    assert stack.summary()["counts"]["instances_ready"] == 0


def test_restart_incarnation_and_generic_service_have_distinct_stored_identity(acceptance_stack):
    stack = acceptance_stack
    stack.record_worker("registered")
    stack.publish()
    original = stack.worker()["incarnation_id"]
    stack.record_worker("active", worker_id="opaque/restarted:" + uuid4().hex)
    stack.publish()
    dynamic = RuntimeObservationSource(service_type="image-indexer")
    stack.publish((dynamic.observe(),))
    body = stack.summary()
    workers = [row for row in body["rows"] if row["service_type"] == "worker"]
    assert len(workers) == 2
    assert len({row["incarnation_id"] for row in workers}) == 2
    assert original in {row["incarnation_id"] for row in workers}
    assert any(row["service_type"] == "image-indexer" for row in body["rows"])
    assert body["counts"]["instances_total"] == 3
    assert body["completeness"] is None  # no authoritative fixed-slot replacement claim


def test_clone_scope_cannot_pollute_the_original_installation(acceptance_stack):
    stack = acceptance_stack
    stack.record_worker("registered")
    stack.publish()
    clone = stack.context.model_copy(update={"installation_id": str(uuid4())})
    namespace = status_namespace(clone.installation_id, clone.deployment_profile)
    stack.namespaces.add(namespace)
    publisher = RabbitHeartbeatPublisher(
        url=stack.proxy.url, namespace=namespace, config=stack.config
    )
    report = build_observation_reports(
        stack.observe(), context=clone, published_at=datetime.now(UTC)
    )[0]
    try:
        publisher.publish(encode_heartbeat(report))
    finally:
        publisher.close()
    # Even a wrong-routing producer is rejected by the original consumer's scope guard.
    stack.publisher.publish(encode_heartbeat(report))
    stack.publish()  # FIFO committed barrier after the rejected message
    assert stack.repository.load(clone) == []
    assert stack.summary()["installation_id"] == stack.context.installation_id
    assert stack.summary()["counts"]["instances_total"] == 1
