"""Compact probes must use the same verified transport as its broker clients."""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from core.entrypoints.cli import compact_runtime
from core.infrastructure.runtime.compact import health
from core.infrastructure.runtime.compact.readiness import ReadinessRegistry


def _certificate(tmp_path: Path, *, matching_host: bool) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Compact test broker")])
    now = datetime.now(UTC)
    alternative = (
        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
        if matching_host
        else x509.DNSName("other-broker.example.test")
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([alternative]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "broker.pem", tmp_path / "broker-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@contextmanager
def _broker(context: ssl.SSLContext | None):
    received: list[bytes] = []
    stopped = threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    address = listener.getsockname()

    def serve() -> None:
        while not stopped.is_set():
            try:
                connection, _peer = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                connection.settimeout(0.5)
                if context is not None:
                    connection = context.wrap_socket(connection, server_side=True)
                header = connection.recv(8)
                received.append(header)
                if header == b"AMQP\x00\x00\x09\x01":
                    # Connection.Start's frame prefix is enough for this probe.
                    connection.sendall(b"\x01\x00\x00")
            except OSError:
                pass
            finally:
                connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield address, received
    finally:
        stopped.set()
        listener.close()
        thread.join(timeout=2)
        assert not thread.is_alive(), "owned broker listener must finish after every case"


@pytest.mark.parametrize("surface", ["probe", "monitor"])
@pytest.mark.parametrize(
    "transport,expected_ready",
    [("verified-tls", True), ("plain-local", True), ("untrusted-ca", False), ("wrong-host", False)],
)
def test_compact_readiness_obeys_canonical_broker_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    transport: str,
    expected_ready: bool,
) -> None:
    context = None
    query = ""
    if transport != "plain-local":
        cert, key = _certificate(tmp_path, matching_host=transport != "wrong-host")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        if transport != "untrusted-ca":
            query = "?ssl_options=" + quote(repr({"cafile": str(cert)}))
    monkeypatch.setenv("FS_RABBITMQ_TRANSPORT_MODE", "loopback-local" if context is None else "tls")
    with _broker(context) as (address, received):
        scheme = "amqp" if context is None else "amqps"
        url = f"{scheme}://{address[0]}:{address[1]}/{query}"
        target = compact_runtime._rabbitmq_probe_target(url)
        if surface == "probe":
            ready = health._rabbitmq_ready(target, 0.5)
        else:
            registry = ReadinessRegistry(worker_processes=1)
            stop = threading.Event()
            monkeypatch.setattr(stop, "wait", lambda _timeout: stop.set() or True)
            monkeypatch.setattr(health, "_postgres_ready", lambda *_args, **_kwargs: True)
            health.monitor_dependencies(
                registry,
                postgres=("127.0.0.1", 1),
                rabbitmq=target,
                stop=stop,
                interval_seconds=0.5,
            )
            ready = registry.snapshot().dependencies["rabbitmq"]
        assert ready is expected_ready
        if expected_ready:
            assert received == [b"AMQP\x00\x00\x09\x01"]
        else:
            assert received == [], "unverified TLS must not carry an AMQP header"


@pytest.mark.parametrize("wait_for_dependencies", ["0", "1"])
def test_compact_resolves_canonical_transport_before_bootstrap_even_without_startup_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_for_dependencies: str,
) -> None:
    canonical_url = "amqps://canonical-broker.example.test:5671/flow"
    observed: list[str] = []
    resolve = compact_runtime._rabbitmq_probe_target

    def record_target(url: str):
        observed.append(url)
        return resolve(url)

    def stop_before_role_start(_root: Path) -> None:
        raise RuntimeError("test stops before runtime processes start")

    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "compact")
    monkeypatch.setenv("FS_RABBITMQ_TRANSPORT_MODE", "tls")
    monkeypatch.setenv("FS_COMPACT_WAIT_FOR_DEPENDENCIES", wait_for_dependencies)
    monkeypatch.setenv("FS_COMPACT_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("FS_DB_DSN", "postgresql://canonical-db.example.test:5432/flow")
    monkeypatch.setenv("RABBITMQ_URL", canonical_url)
    monkeypatch.setenv("RABBITMQ_HOST", "ignored-component-broker.example.test")
    monkeypatch.setenv("RABBITMQ_PORT", "2222")
    monkeypatch.setattr(compact_runtime, "verify_compact_environment_identity", lambda _env: None)
    monkeypatch.setattr(compact_runtime, "_HELD_VOLUME_LOCKS", [])
    monkeypatch.setattr(compact_runtime, "acquire_compact_volume_lock", lambda _root: object())
    monkeypatch.setattr(compact_runtime, "_rabbitmq_probe_target", record_target)
    monkeypatch.setattr(compact_runtime, "_wait_for_bootstrap_dependencies", lambda **_kwargs: None)
    monkeypatch.setattr(compact_runtime, "_bootstrap", stop_before_role_start)

    assert compact_runtime.main() == 78
    assert observed == [canonical_url]
