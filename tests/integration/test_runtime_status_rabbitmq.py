"""Real AMQP/Postgres replay and bounded confirms in test-owned resources."""

from __future__ import annotations

import socket
import struct
import threading
import time
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import pika
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from core.application.monitoring.contracts import (
    HeartbeatReport,
    RuntimeInstallationContext,
    encode_heartbeat,
)
from core.application.monitoring.ingestion import RuntimeStatusIngestionService
from core.application.monitoring.repository import RuntimeStatusRepository
from core.application.monitoring.runtime_composition import start_configured_status_consumer
from core.application.tooling.db.init_db import bootstrap_canonical_schema_if_empty
from core.infrastructure.config.connection_strings import resolve_rabbitmq_url
from core.infrastructure.config.monitoring_runtime import load_monitoring_runtime_config
from core.infrastructure.runtime.monitoring.heartbeat_runtime import start_monitoring_publisher
from core.infrastructure.runtime.monitoring.heartbeat_transport import (
    RabbitHeartbeatConsumer,
    RabbitHeartbeatPublisher,
    status_namespace,
)
from tests._db_env import resolve_test_db_dsn

pytestmark = pytest.mark.integration


@pytest.fixture
def broker_url():
    url = resolve_rabbitmq_url()
    parsed = urlparse(url)
    assert parsed.hostname == "rabbitmq-audit", "refuse shared broker"
    assert parsed.path == "/flow_steward_audit", "refuse shared broker"
    return url


@pytest.fixture
def database():
    admin = psycopg.connect(resolve_test_db_dsn(), autocommit=True)
    assert admin.execute("SELECT current_database()").fetchone()[0] == "flow_steward_audit"
    name = "fs_status_transport_" + uuid4().hex
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(resolve_test_db_dsn(), dbname=name)
    try:
        bootstrap_canonical_schema_if_empty(dsn=dsn, root=Path(__file__).resolve().parents[2])
        yield dsn
    finally:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def context():
    return RuntimeInstallationContext(installation_id=str(uuid4()), deployment_profile="scalable")


@pytest.fixture
def namespace(broker_url, context):
    name = status_namespace(context.installation_id, context.deployment_profile)
    yield name
    connection = pika.BlockingConnection(pika.URLParameters(broker_url))
    try:
        connection.channel().queue_delete(queue=name)
        connection.channel().exchange_delete(exchange=name)
    finally:
        connection.close()


def report(context):
    identity = uuid4()
    return HeartbeatReport.model_validate(
        {
            "schema_version": 1,
            "installation_id": context.installation_id,
            "deployment_profile": context.deployment_profile,
            "service_type": "worker",
            "instance_id": identity,
            "incarnation_id": identity,
            "sequence": 1,
            "observed_at": datetime.now(UTC),
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


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.05)
    pytest.fail("monitoring condition did not arrive within test budget")


class BrokerProxy:
    """A disposable TCP endpoint; can drop confirms or restart without Docker."""

    def __init__(self, url, *, drop_confirms=False):
        self.upstream = urlparse(url)
        self.drop_confirms = drop_confirms
        self.port = 0
        self.start()

    def start(self):
        self.stop = threading.Event()
        self.sockets = []
        self.threads = []
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", self.port))
        self.port = self.listener.getsockname()[1]
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.acceptor = threading.Thread(target=self._accept, daemon=True)
        self.acceptor.start()

    @property
    def url(self):
        userinfo = self.upstream.netloc.rsplit("@", 1)[0]
        return urlunparse(self.upstream._replace(netloc=f"{userinfo}@127.0.0.1:{self.port}"))

    def _accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            server = socket.create_connection(
                (self.upstream.hostname, self.upstream.port or 5672), timeout=2
            )
            self.sockets.extend((client, server))
            for source, target, filter_ack in (
                (client, server, False),
                (server, client, self.drop_confirms),
            ):
                source.settimeout(0.1)
                thread = threading.Thread(
                    target=self._forward, args=(source, target, filter_ack), daemon=True
                )
                self.threads.append(thread)
                thread.start()

    def _forward(self, source, target, filter_ack):
        pending = b""
        try:
            while not self.stop.is_set():
                try:
                    data = source.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    return
                if not filter_ack:
                    target.sendall(data)
                    continue
                pending += data
                while len(pending) >= 7:
                    size = struct.unpack("!I", pending[3:7])[0] + 8
                    if len(pending) < size:
                        break
                    frame, pending = pending[:size], pending[size:]
                    if not (frame[0] == 1 and frame[7:11] == struct.pack("!HH", 60, 80)):
                        target.sendall(frame)
        except OSError:
            pass
        finally:
            for connection in (source, target):
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)

    def close(self):
        self.stop.set()
        self.listener.close()
        self.acceptor.join(2)
        for connection in self.sockets:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        for thread in self.threads:
            thread.join(2)
        assert not self.acceptor.is_alive()
        assert all(not thread.is_alive() for thread in self.threads)


def test_commit_before_ack_replays_without_freshness_or_history_change(
    broker_url, namespace, database, context
):
    config = replace(load_monitoring_runtime_config({}), history_enabled=True)
    repository = RuntimeStatusRepository(database, config=config)
    ingestion = RuntimeStatusIngestionService(
        repository=repository, context=context, config=config, clock=lambda: datetime.now(UTC)
    )
    publisher = RabbitHeartbeatPublisher(url=broker_url, namespace=namespace, config=config)
    consumer = RabbitHeartbeatConsumer(
        url=broker_url, namespace=namespace, expected_namespace=namespace, config=config
    )
    stop = threading.Event()
    results = []
    first = []
    errors = []

    def accept(body):
        result = ingestion.accept(body)
        results.append(result.disposition)
        if len(results) == 1:
            first.extend(repository.load(context))
            consumer.close()  # actual socket loss after commit, before Basic.Ack
        else:
            if repository.load(context) != first:
                errors.append("duplicate refreshed current")
            stop.set()
        return "ack"

    thread = threading.Thread(
        target=consumer.run, args=(stop, accept, lambda _row: None), daemon=True
    )
    try:
        publisher.publish(encode_heartbeat(report(context)))
        thread.start()
        wait_until(lambda: len(results) >= 2)
        assert results == ["stored", "duplicate"]
        assert not errors
        with psycopg.connect(database) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM public.fs_runtime_status_history"
                ).fetchone()[0]
                == 1
            )
    finally:
        stop.set()
        thread.join(5)
        publisher.close()
    assert not thread.is_alive()


def test_missing_confirm_times_out_and_releases_owned_connection(
    broker_url, namespace, monkeypatch
):
    monkeypatch.setenv("FS_RABBITMQ_TRANSPORT_MODE", "loopback-local")
    proxy = BrokerProxy(broker_url, drop_confirms=True)
    connections = []

    def connect(parameters):
        connection = pika.BlockingConnection(parameters)
        connections.append(connection)
        return connection

    publisher = RabbitHeartbeatPublisher(
        url=proxy.url,
        namespace=namespace,
        config=load_monitoring_runtime_config({}),
        connection_factory=connect,
    )
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="deadline"):
            publisher.publish(b"{}")
        assert time.monotonic() - started < 3
        assert len(connections) == 1
        assert connections[0].is_closed
    finally:
        publisher.close()
        proxy.close()


def test_queue_overflow_rejects_publication(broker_url, namespace):
    config = replace(load_monitoring_runtime_config({}), queue_max_messages=1)
    publisher = RabbitHeartbeatPublisher(url=broker_url, namespace=namespace, config=config)
    try:
        publisher.publish(b"first")
        with pytest.raises(pika.exceptions.NackError):
            publisher.publish(b"second")
    finally:
        publisher.close()


def test_control_composition_records_idle_connection_and_endpoint_recovery(
    broker_url, namespace, database, context, monkeypatch
):
    proxy = BrokerProxy(broker_url)
    values = {
        "FS_INSTALLATION_ID": context.installation_id,
        "FS_DEPLOYMENT_PROFILE": "scalable",
        "FS_MONITORING_INTERVAL_SECONDS": "1",
        "FS_MONITORING_STALE_SECONDS": "4",
    }
    monkeypatch.setenv("FS_RABBITMQ_TRANSPORT_MODE", "loopback-local")
    monkeypatch.setattr(
        "core.application.monitoring.runtime_composition.resolve_rabbitmq_url", lambda: proxy.url
    )
    repository = RuntimeStatusRepository(database)
    old = report(context)
    repository.store(old, history_enabled=False)
    with psycopg.connect(database) as connection:
        connection.execute(
            "UPDATE public.fs_runtime_status_current SET received_at=received_at-interval '8 days' WHERE instance_id=%s",
            (old.instance_id,),
        )
    handle = start_configured_status_consumer(dsn=database, runtime_values=values)

    def channel_rows():
        return [row for row in repository.load(context) if row.source == "heartbeat.receiver"]

    try:
        wait_until(lambda: channel_rows() and channel_rows()[0].payload.readiness.state == "ready")
        wait_until(
            lambda: all(row.instance_id != old.instance_id for row in repository.load(context))
        )
        initial = channel_rows()[0]
        wait_until(lambda: channel_rows()[0].sequence > initial.sequence)
        proxy.close()
        wait_until(lambda: channel_rows()[0].payload.readiness.state == "not_ready")
        disconnected = channel_rows()[0]
        assert disconnected.payload.health.state == "unhealthy"
        assert disconnected.payload.health.evidence == "broker_connection"
        proxy.start()
        wait_until(lambda: channel_rows()[0].payload.readiness.state == "ready")
        recovered = channel_rows()[0]
        assert recovered.incarnation_id == initial.incarnation_id
        assert recovered.sequence > disconnected.sequence
        assert recovered.payload.observation_kind == "channel"
        assert recovered.payload.health.state == "healthy"
        assert recovered.payload.health.evidence == "broker_connection"
        assert not handle._stop.is_set()
    finally:
        handle.close()
        proxy.close()
    assert all(not thread.is_alive() for thread in handle.threads)


def test_publisher_services_amqp_heartbeats_between_reports(broker_url, namespace):
    connections = []
    published = []
    complete = threading.Event()

    def connect(parameters):
        connection = pika.BlockingConnection(parameters)
        connections.append(connection)
        return connection

    publisher = RabbitHeartbeatPublisher(
        url=broker_url,
        namespace=namespace,
        config=load_monitoring_runtime_config({}),
        connection_factory=connect,
    )

    def publish(body):
        publisher.publish(body)
        published.append(body)
        if len(published) == 2:
            complete.set()

    handle = start_monitoring_publisher(
        collect=lambda: (b"{}",),
        publish=publish,
        idle=publisher.poll,
        close=publisher.close,
        interval_seconds=5,
        close_timeout_seconds=5,
    )
    try:
        assert complete.wait(10)
        assert len(connections) == 1
    finally:
        handle.close()
    assert connections[0].is_closed
    assert not handle.threads[0].is_alive()
