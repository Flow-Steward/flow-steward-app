"""Dedicated, installation-scoped AMQP byte transport with bounded operations."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from threading import Event
from typing import Any, Literal
from uuid import uuid4

import pika

from core.infrastructure.config.monitoring_runtime import MonitoringRuntimeConfig
from core.infrastructure.config.rabbitmq_transport import build_rabbitmq_parameters
from core.infrastructure.runtime.monitoring.observations import SourceObservation

Settlement = Literal["ack", "reject", "retry"]
_NAMESPACE = re.compile(r"^fs\.runtime-status\.v1\.[a-f0-9]{64}$")


def status_namespace(installation_id: str, deployment_profile: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", installation_id):
        raise ValueError("invalid monitoring installation namespace")
    if deployment_profile not in {"compact", "scalable"}:
        raise ValueError("invalid monitoring profile namespace")
    scope = json.dumps([installation_id, deployment_profile], separators=(",", ":")).encode()
    return "fs.runtime-status.v1." + hashlib.sha256(scope).hexdigest()


@contextmanager
def _operation_deadline(connection: Any, seconds: float) -> Iterator[None]:
    """Abort the owned socket, not a job channel, if an AMQP RPC/confirm stalls.

    Pika 1.3.2's BlockingConnection.call_later defers user callbacks during RPCs.
    Its underlying I/O-loop timer is processed inside _flush_output instead.
    This pinned adapter boundary also aborts the stream so no timed-out thread
    or socket is retained. A real-broker timeout regression protects this seam.
    """
    expired = False
    implementation = connection._impl

    def abort() -> None:
        nonlocal expired
        expired = True
        implementation._terminate_stream(
            TimeoutError("monitoring broker operation deadline exceeded")
        )

    timer = implementation.ioloop.call_later(max(0.0, seconds), abort)
    try:
        yield
    finally:
        if expired:
            raise TimeoutError("monitoring broker operation deadline exceeded")
        if not connection.is_closed:
            implementation.ioloop.remove_timeout(timer)


class _BrokerOwner:
    def __init__(
        self,
        *,
        url: str,
        namespace: str,
        config: MonitoringRuntimeConfig,
        connection_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        if not _NAMESPACE.fullmatch(namespace):
            raise ValueError("invalid monitoring namespace")
        self.namespace = namespace
        self._config = config
        self._parameters = build_rabbitmq_parameters(url, parameter_factory=pika.URLParameters)
        budget = config.operation_timeout_seconds
        self._parameters.socket_timeout = budget
        self._parameters.stack_timeout = budget
        self._parameters.blocked_connection_timeout = budget
        self._parameters.connection_attempts = 1
        self._parameters.retry_delay = 0
        self._parameters.heartbeat = max(2, budget)
        self._factory = connection_factory or pika.BlockingConnection
        self._connection: Any = None
        self._channel: Any = None

    def _declare(self) -> None:
        self._channel.exchange_declare(
            exchange=self.namespace, exchange_type="direct", durable=True
        )
        self._channel.queue_declare(
            queue=self.namespace,
            durable=True,
            arguments={
                "x-message-ttl": self._config.stale_seconds * 1000,
                "x-max-length": self._config.queue_max_messages,
                "x-max-length-bytes": self._config.queue_max_bytes,
                "x-overflow": "reject-publish",
            },
        )
        self._channel.queue_bind(
            exchange=self.namespace, queue=self.namespace, routing_key="status"
        )

    def _connect(self) -> None:
        started = time.monotonic()
        self._connection = self._factory(self._parameters)
        remaining = self._config.operation_timeout_seconds - (time.monotonic() - started)
        with _operation_deadline(self._connection, remaining):
            self._channel = self._connection.channel()
            self._declare()

    def close(self) -> None:
        connection, self._connection = self._connection, None
        self._channel = None
        if connection is not None and connection.is_open:
            with (
                suppress(Exception),
                _operation_deadline(connection, self._config.operation_timeout_seconds),
            ):
                connection.close()


class RabbitHeartbeatPublisher(_BrokerOwner):
    """Use only the caller's publisher thread; never borrow a queue consumer."""

    def poll(self) -> None:
        """Service AMQP heartbeats on the publisher's own thread between reports."""
        if self._connection is None or not self._connection.is_open:
            return
        try:
            with _operation_deadline(self._connection, self._config.operation_timeout_seconds):
                self._connection.process_data_events(time_limit=0)
        except Exception:
            self.close()
            raise

    def publish(self, body: bytes) -> None:
        if not isinstance(body, bytes) or len(body) > 16384:
            raise ValueError("invalid monitoring envelope size")
        try:
            if self._connection is None or not self._connection.is_open:
                self._connect()
                with _operation_deadline(self._connection, self._config.operation_timeout_seconds):
                    self._channel.confirm_delivery()
            with _operation_deadline(self._connection, self._config.operation_timeout_seconds):
                self._channel.basic_publish(
                    exchange=self.namespace,
                    routing_key="status",
                    body=body,
                    mandatory=True,
                    properties=pika.BasicProperties(
                        content_type="application/json",
                        delivery_mode=2,
                        expiration=str(self._config.stale_seconds * 1000),
                    ),
                )
        except Exception:
            self.close()
            raise


class RabbitHeartbeatConsumer(_BrokerOwner):
    def __init__(self, *, expected_namespace: str, **kwargs: Any) -> None:
        if kwargs.get("namespace") != expected_namespace:
            raise ValueError("monitoring namespace conflicts with trusted context")
        super().__init__(**kwargs)
        self._observer_id = uuid4()
        self._started_at = datetime.now(UTC)
        self._sequence = 0

    def _observation(self, ready: bool) -> SourceObservation:
        self._sequence += 1
        return SourceObservation(
            service_type="control",
            source="heartbeat.receiver",
            original_process_id=None,
            observer_id=self._observer_id,
            observation_kind="channel",
            source_sequence=self._sequence,
            checked_at=datetime.now(UTC),
            source_age_seconds=0,
            source_stale_seconds=self._config.stale_seconds,
            availability="observed",
            readiness=ready,
            lifecycle="running",
            started_at=self._started_at,
        )

    def run(
        self,
        stop: Event,
        accept: Callable[[bytes], Settlement],
        record_channel: Callable[[SourceObservation], None],
    ) -> None:
        def record(ready: bool) -> None:
            # Direct database reporting can fail while both dependencies are down.
            # Keep retrying; never imply that the receiver observation was stored.
            with suppress(Exception):
                record_channel(self._observation(ready))

        while not stop.is_set():
            try:
                self._connect()
                with _operation_deadline(self._connection, self._config.operation_timeout_seconds):
                    self._channel.basic_qos(prefetch_count=1)
                record(True)
                next_probe = time.monotonic() + self._config.interval_seconds
                for method, _properties, body in self._channel.consume(
                    queue=self.namespace,
                    auto_ack=False,
                    inactivity_timeout=0.2,
                ):
                    if stop.is_set():
                        break
                    if method is not None:
                        try:
                            decision = accept(body)
                        except Exception:
                            decision = "retry"
                        with _operation_deadline(
                            self._connection, self._config.operation_timeout_seconds
                        ):
                            if decision == "ack":
                                self._channel.basic_ack(method.delivery_tag)
                            else:
                                self._channel.basic_nack(
                                    method.delivery_tag, requeue=decision != "reject"
                                )
                        if decision not in {"ack", "reject"}:
                            break
                    if time.monotonic() >= next_probe:
                        with _operation_deadline(
                            self._connection, self._config.operation_timeout_seconds
                        ):
                            self._channel.queue_declare(queue=self.namespace, passive=True)
                        record(True)
                        next_probe = time.monotonic() + self._config.interval_seconds
            except Exception:
                # No untrusted envelope, credential-bearing URL or driver detail logs.
                pass
            finally:
                self.close()
                record(False)
            stop.wait(self._config.operation_timeout_seconds)
