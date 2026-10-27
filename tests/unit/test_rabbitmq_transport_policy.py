"""Both AMQP sinks enforce the deployment-owned plaintext/TLS boundary."""

from __future__ import annotations

import ssl
from urllib.parse import quote

import pytest

from core.infrastructure.config.monitoring_runtime import load_monitoring_runtime_config
from core.infrastructure.runtime.monitoring.heartbeat_transport import (
    RabbitHeartbeatPublisher,
    status_namespace,
)
from core.infrastructure.runtime.worker import queue_broker as queue


def _parameters_at_sink(monkeypatch, sink: str, url: str, mode: str, *, forbid_socket=False):
    monkeypatch.setenv("FS_RABBITMQ_TRANSPORT_MODE", mode)
    if sink == "heartbeat":
        publisher = RabbitHeartbeatPublisher(
            url=url,
            namespace=status_namespace("transport-policy-test", "scalable"),
            config=load_monitoring_runtime_config({"FS_RABBITMQ_TRANSPORT_MODE": mode}),
            connection_factory=lambda _params: pytest.fail("constructor must not open a socket"),
        )
        return publisher._parameters
    captured = []

    def connect(params):
        if forbid_socket:
            pytest.fail("unsafe transport reached the broker connection factory")
        captured.append(params)

    monkeypatch.setattr(queue.pika, "BlockingConnection", connect)
    queue._connect(url, retries=1, delay=0)
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize("sink", ["queue", "heartbeat"])
@pytest.mark.parametrize(
    "mode,host",
    [
        ("tls", "broker.example.test"),
        ("tls", "rabbitmq-fs"),
        ("compose-local", "broker.example.test"),
        ("compose-local", "10.4.3.2"),
        ("compose-local", "[fd00::1234]"),
        ("compose-local", "rabbitmq-fs.attacker.test"),
        ("audit-local", "rabbitmq-fs"),
        ("loopback-local", "localhost"),
    ],
)
def test_plaintext_remote_or_mismatched_local_context_refuses_before_connection(
    monkeypatch, sink, mode, host
):
    with pytest.raises(ValueError, match="RabbitMQ transport"):
        _parameters_at_sink(
            monkeypatch, sink, f"amqp://user:password@{host}:5672/", mode, forbid_socket=True
        )


@pytest.mark.parametrize("sink", ["queue", "heartbeat"])
@pytest.mark.parametrize(
    "mode,host,port",
    [
        ("compose-local", "rabbitmq-fs", 5672),
        ("audit-local", "rabbitmq-audit", 5672),
        ("loopback-local", "127.0.0.1", 5673),
        ("loopback-local", "[::1]", 5673),
    ],
)
def test_matching_local_deployment_context_preserves_plaintext_control(
    monkeypatch, sink, mode, host, port
):
    parameters = _parameters_at_sink(
        monkeypatch, sink, f"amqp://user:password@{host}:{port}/", mode
    )
    assert parameters.ssl_options is None
    assert parameters.port == port


@pytest.mark.parametrize("sink", ["queue", "heartbeat"])
@pytest.mark.parametrize(
    "query",
    [
        "",
        "?ssl_options=" + quote("{'protocol': 2}"),
        "?ssl_options="
        + quote(repr({"protocol": 2, "cafile": ssl.get_default_verify_paths().cafile})),
    ],
    ids=["default", "malformed-options", "unverified-context"],
)
def test_tls_effective_context_cannot_disable_certificate_or_hostname_verification(
    monkeypatch, sink, query
):
    url = f"amqps://user:credential%3Asentinel@broker.example.test:5671/{query}"
    try:
        parameters = _parameters_at_sink(monkeypatch, sink, url, "tls")
    except ValueError as error:
        assert query, "ordinary remote TLS must remain supported"
        assert "RabbitMQ transport" in str(error)
        assert url not in str(error)
    else:
        assert parameters.ssl_options is not None
        context = parameters.ssl_options.context
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert parameters.ssl_options.server_hostname == "broker.example.test"


@pytest.mark.parametrize("sink", ["queue", "heartbeat"])
@pytest.mark.parametrize(
    "url",
    [
        "amqp://user:credential%3Asentinel@rabbitmq-fs:5673/",
        "amqp://user:credential%3Asentinel@[::1]:not-a-port/",
        "amqp://user:credential%3Asentinel@/",
    ],
)
def test_rejected_transport_diagnostics_never_echo_credentials(monkeypatch, sink, url):
    with pytest.raises(ValueError, match="RabbitMQ transport") as caught:
        _parameters_at_sink(monkeypatch, sink, url, "compose-local", forbid_socket=True)
    assert "credential" not in str(caught.value)
    assert url not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("sink", ["queue", "heartbeat"])
@pytest.mark.parametrize("mode", ["insecure", "COMPOSE-LOCAL"])
def test_unknown_transport_modes_refuse_even_tls(monkeypatch, sink, mode):
    with pytest.raises(ValueError, match="RabbitMQ transport"):
        _parameters_at_sink(monkeypatch, sink, "amqps://broker.example.test:5671/", mode)


@pytest.mark.parametrize("sink", ["queue", "heartbeat"])
@pytest.mark.parametrize("fault", ["missing-tls", "different-peer", "different-server-name"])
def test_effective_client_parameters_must_match_authorized_peer(monkeypatch, sink, fault):
    from types import SimpleNamespace

    context = ssl.create_default_context()
    parameters = SimpleNamespace(
        host="other.example.test" if fault == "different-peer" else "broker.example.test",
        port=5671,
        ssl_options=None
        if fault == "missing-tls"
        else SimpleNamespace(
            context=context,
            server_hostname="other.example.test"
            if fault == "different-server-name"
            else "broker.example.test",
        ),
    )
    monkeypatch.setattr(queue.pika, "URLParameters", lambda _url: parameters)
    with pytest.raises(ValueError, match="RabbitMQ transport"):
        _parameters_at_sink(
            monkeypatch, sink, "amqps://broker.example.test:5671/", "tls", forbid_socket=True
        )


@pytest.mark.parametrize("sink", ["queue", "heartbeat"])
def test_verified_custom_ca_query_and_percent_encoded_credentials_remain_supported(
    monkeypatch, sink
):
    options = {"cafile": ssl.get_default_verify_paths().cafile}
    parameters = _parameters_at_sink(
        monkeypatch,
        sink,
        "amqps://user%3Aname:encoded%40password@broker.example.test:5671/%2F"
        + "?ssl_options="
        + quote(repr(options)),
        "tls",
    )
    assert parameters.credentials.username == "user:name"
    assert parameters.credentials.password == "encoded@password"
    assert parameters.virtual_host == "/"
    assert parameters.ssl_options.context.verify_mode == ssl.CERT_REQUIRED
    assert parameters.ssl_options.context.check_hostname is True
