from threading import Event
from types import SimpleNamespace

import pytest

from core.application.monitoring.contracts import IngestResult
from core.application.monitoring.ingestion import RuntimeStatusIngestionService
from core.application.monitoring.runtime_composition import delivery_decision
from core.infrastructure.config.monitoring_runtime import load_monitoring_runtime_config
from core.infrastructure.runtime.monitoring.heartbeat_transport import (
    RabbitHeartbeatConsumer,
    RabbitHeartbeatPublisher,
    status_namespace,
)


class BrokerConnection:
    def __init__(self, trace, body=b"{}"):
        self.trace = trace
        self.body = body
        self.is_open = True
        self.is_closed = False
        self._impl = self
        self.ioloop = self
        self.arguments = None
        self.exchange_arguments = None
        self.binding = None

    def call_later(self, _seconds, _callback):
        return 1

    def remove_timeout(self, _timer):
        pass

    def channel(self):
        return self

    def queue_declare(self, **kwargs):
        self.arguments = kwargs
        return SimpleNamespace(method=SimpleNamespace(message_count=0))

    def basic_qos(self, **_kwargs):
        pass

    def exchange_declare(self, **kwargs):
        self.exchange_arguments = kwargs

    def queue_bind(self, **kwargs):
        self.binding = kwargs

    def confirm_delivery(self):
        self.trace.append("confirm")

    def basic_publish(self, **kwargs):
        self.trace.append("publish")
        assert kwargs["exchange"] != ""
        assert kwargs["mandatory"] is True
        assert kwargs["properties"].delivery_mode == 2

    def consume(self, **_kwargs):
        yield SimpleNamespace(delivery_tag=1), None, self.body

    def basic_ack(self, _tag):
        self.trace.append("ack")

    def basic_nack(self, _tag, *, requeue):
        self.trace.append("retry" if requeue else "reject")

    def close(self):
        self.trace.append("close")
        self.is_open = False
        self.is_closed = True


@pytest.mark.parametrize("outcome", ["stored", "duplicate", "failure", "malformed", "unknown"])
def test_commit_is_confirmed_before_ack(outcome, context, now, report_body):
    trace = []

    class Repository:
        def store(self, _report, *, history_enabled):
            if outcome == "failure":
                raise ConnectionError("database unavailable")
            if outcome == "unknown":
                trace.append("unconfirmed")
                return SimpleNamespace(disposition="unknown")
            trace.append("commit" if outcome == "stored" else "duplicate")
            return IngestResult(disposition="stored" if outcome == "stored" else "duplicate")

    config = load_monitoring_runtime_config({})
    ingestion = RuntimeStatusIngestionService(
        repository=Repository(), context=context, config=config, clock=lambda: now
    )
    connection = BrokerConnection(trace, b"bad json" if outcome == "malformed" else report_body())
    stop = Event()
    namespace = status_namespace(context.installation_id, context.deployment_profile)
    consumer = RabbitHeartbeatConsumer(
        url="amqps://localhost/",
        namespace=namespace,
        expected_namespace=namespace,
        config=config,
        connection_factory=lambda _params: connection,
    )

    def accept(body):
        result = delivery_decision(ingestion, body)
        stop.set()
        return result

    observations = []
    consumer.run(stop, accept, observations.append)
    expected = {
        "stored": ["commit", "ack", "close"],
        "duplicate": ["duplicate", "ack", "close"],
        "failure": ["retry", "close"],
        "malformed": ["reject", "close"],
        "unknown": ["unconfirmed", "retry", "close"],
    }
    assert trace == expected[outcome]
    assert observations[0].readiness is True
    assert observations[0].observation_kind == "channel"
    assert observations[-1].readiness is False


def test_queue_policy_is_bounded_and_confirms_precede_publish(context):
    trace = []
    connection = BrokerConnection(trace)
    publisher = RabbitHeartbeatPublisher(
        url="amqps://localhost/",
        namespace=status_namespace(context.installation_id, context.deployment_profile),
        config=load_monitoring_runtime_config({}),
        connection_factory=lambda _params: connection,
    )
    publisher.publish(b"{}")
    publisher.close()
    publisher.close()
    assert trace == ["confirm", "publish", "close"]
    assert connection.exchange_arguments == {
        "exchange": publisher.namespace,
        "exchange_type": "direct",
        "durable": True,
    }
    assert connection.binding == {
        "exchange": publisher.namespace,
        "queue": publisher.namespace,
        "routing_key": "status",
    }
    assert connection.arguments["durable"] is True
    assert connection.arguments["arguments"] == {
        "x-message-ttl": 180000,
        "x-max-length": 10000,
        "x-max-length-bytes": 16777216,
        "x-overflow": "reject-publish",
    }


def test_foreign_namespace_fails_before_broker_io(context):
    foreign_namespace = status_namespace("other", "scalable")
    expected_namespace = status_namespace(context.installation_id, context.deployment_profile)
    config = load_monitoring_runtime_config({})
    with pytest.raises(ValueError, match="namespace"):
        RabbitHeartbeatConsumer(
            url="amqps://localhost/",
            namespace=foreign_namespace,
            expected_namespace=expected_namespace,
            config=config,
            connection_factory=lambda _params: pytest.fail("unexpected broker I/O"),
        )
