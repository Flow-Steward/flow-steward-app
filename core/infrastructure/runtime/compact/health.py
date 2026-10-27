"""Loopback health endpoint and worker observation reader for Compact."""

from __future__ import annotations

import json
import socket
import ssl
import struct
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from core.infrastructure.runtime.compact.process_spec import ProcessSpec
from core.infrastructure.runtime.compact.readiness import ReadinessRegistry
from core.infrastructure.runtime.worker.consumer_observer import ConsumerState

_PATH_HEALTH_LIVE = "/health/live"

_CONSUMER_STATES = frozenset(
    {"registered", "active", "maintenance_paused", "draining", "disconnected"}
)


def consume_observations(descriptor: int, registry: ReadinessRegistry) -> None:
    """Consume newline-delimited worker observations until the pipe closes."""
    with open(descriptor, "rb", closefd=True) as stream:
        for line in stream:
            try:
                payload = json.loads(line)
                state = str(payload["state"])
                if state not in _CONSUMER_STATES:
                    continue
                registry.record_worker(
                    role=str(payload["role"]),
                    worker_incarnation=str(payload["worker_incarnation"]),
                    state=cast(ConsumerState, state),
                    sequence=int(payload["sequence"]),
                )
            except (KeyError, TypeError, ValueError):
                continue


def _payload(registry: ReadinessRegistry, *, detailed: bool) -> tuple[int, dict[str, Any]]:
    snapshot = registry.snapshot()
    status = HTTPStatus.OK if snapshot.ready else HTTPStatus.SERVICE_UNAVAILABLE
    result: dict[str, Any] = {"ready": snapshot.ready}
    if detailed:
        result.update(
            {
                "bootstrap_ready": snapshot.bootstrap_ready,
                "schema_ready": snapshot.schema_ready,
                "dependencies": snapshot.dependencies,
                "roles": snapshot.roles,
                "worker_states": snapshot.worker_states,
                "workers_complete": snapshot.workers_complete,
            }
        )
    return int(status), result


class CompactHealthServer:
    """Expose aggregate public readiness and loopback-only details."""

    def __init__(
        self,
        registry: ReadinessRegistry,
        *,
        host: str = "127.0.0.1",
        port: int = 9100,
    ) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == _PATH_HEALTH_LIVE:
                    body = b'{"live":true}'
                    self.send_response(HTTPStatus.OK)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path not in {"/health/ready", "/health/details"}:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                status, payload = _payload(
                    registry,
                    detailed=path == "/health/details",
                )
                body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@dataclass(frozen=True, slots=True)
class RabbitMQProbeTarget:
    """Canonical broker endpoint with its already-validated TLS client context."""

    address: tuple[str, int]
    ssl_context: ssl.SSLContext | None = None
    server_hostname: str | None = None


def monitor_dependencies(
    registry: ReadinessRegistry,
    *,
    postgres: tuple[str, int],
    rabbitmq: RabbitMQProbeTarget,
    stop: threading.Event,
    interval_seconds: float,
    connect: Any = socket.create_connection,
    postgres_user: str = "",
) -> None:
    """Continuously reflect whether each dependency would serve a client.

    P2: this was TCP connect alone. Postgres accepts TCP while it is still starting up
    and refuses queries, so readiness said "ready" exactly when it was not. Each probe
    now speaks the dependency's own protocol far enough to hear it answer as a server
    that is up, and needs no credential to do so.
    """
    while not stop.is_set():
        timeout = min(interval_seconds, 2.0)
        registry.set_dependency(
            "postgres", _postgres_ready(postgres, postgres_user, timeout, connect=connect)
        )
        registry.set_dependency("rabbitmq", _rabbitmq_ready(rabbitmq, timeout, connect=connect))
        stop.wait(interval_seconds)


_PG_PROTOCOL_3_0 = 196608
# SQLSTATE cannot_connect_now: the database system is starting up, shutting down, or in
# recovery mode. Every other answer comes from a server that is up.
_PG_CANNOT_CONNECT_NOW = "57P03"


def _probe_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("dependency probe deadline exceeded")
    return remaining


def _recv_exactly(connection: Any, size: int, *, deadline: float | None = None) -> bytes:
    data = b""
    while len(data) < size:
        if deadline is not None:
            connection.settimeout(_probe_timeout(deadline))
        chunk = connection.recv(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _postgres_ready(
    address: tuple[str, int],
    user: str,
    timeout: float,
    *,
    connect: Any = socket.create_connection,
) -> bool:
    """True when Postgres would accept a session, judged by its first protocol answer.

    A StartupMessage is sent and nothing more. An authentication request (`R`) is a
    server that is up; so is an error such as an unknown role or database, which says
    only that these particular credentials would be refused. `57P03` is a server that
    is starting or stopping. Silence, a closed connection or anything else is not
    ready. The connection is closed before any credential would be asked for.
    """
    params = b"user\x00" + (user or "flow_steward").encode("utf-8") + b"\x00\x00"
    body = struct.pack("!I", _PG_PROTOCOL_3_0) + params
    try:
        connection = connect(address, timeout=timeout)
    except OSError:
        return False
    try:
        connection.settimeout(timeout)
        connection.sendall(struct.pack("!I", len(body) + 4) + body)
        head = _recv_exactly(connection, 5)
        if len(head) < 5:
            return False
        kind = head[:1]
        if kind == b"R":
            return True
        if kind != b"E":
            return False
        length = struct.unpack("!I", head[1:5])[0]
        payload = _recv_exactly(connection, max(0, min(length - 4, 8192)))
        for field in payload.split(b"\x00"):
            if field[:1] == b"C":
                return field[1:].decode("ascii", "replace") != _PG_CANNOT_CONNECT_NOW
        return False
    except OSError:
        return False
    finally:
        with suppress(OSError):
            connection.close()


_AMQP_0_9_1_HEADER = b"AMQP\x00\x00\x09\x01"
_AMQP_FRAME_METHOD = 1


def _rabbitmq_ready(
    target: RabbitMQProbeTarget,
    timeout: float,
    *,
    connect: Any = socket.create_connection,
) -> bool:
    """Read Connection.Start over the broker's validated transport, without login."""
    deadline = time.monotonic() + timeout
    try:
        connection = connect(target.address, timeout=_probe_timeout(deadline))
    except OSError:
        return False
    try:
        connection.settimeout(_probe_timeout(deadline))
        if target.ssl_context is not None:
            connection = target.ssl_context.wrap_socket(
                connection, server_hostname=target.server_hostname
            )
        connection.settimeout(_probe_timeout(deadline))
        connection.sendall(_AMQP_0_9_1_HEADER)
        frame = _recv_exactly(connection, 3, deadline=deadline)
        return len(frame) == 3 and frame[0] == _AMQP_FRAME_METHOD and frame[1:3] == b"\x00\x00"
    except OSError:
        return False
    finally:
        with suppress(OSError):
            connection.close()


def monitor_roles(
    registry: ReadinessRegistry,
    *,
    specs: tuple[ProcessSpec, ...],
    stop: threading.Event,
    interval_seconds: float,
    probe: Any = None,
) -> None:
    """Mark fixed services ready only while their HTTP protocol responds."""
    role_probe = probe or _role_protocol_ready
    roles = tuple(
        (spec.role, spec.port)
        for spec in specs
        if not spec.role.startswith("worker-") and spec.port is not None
    )
    while not stop.is_set():
        for role, port in roles:
            registry.record_role_probe(
                role,
                bool(role_probe(role, port, min(interval_seconds, 2.0))),
            )
        stop.wait(interval_seconds)


# Roles probed on an endpoint that needs credentials rather than on a health endpoint:
# the server answering at all is the signal, and a 401 or 406 is the expected answer.
_PROTOCOL_PROBE_ROLES = frozenset({"mcp", "installer"})


def _status_means_ready(role: str, status: int) -> bool:
    """P2: `100 <= status <= 599` made a role answering 500 or 503 ready.

    A health endpoint is ready on 2xx only; the router's /health/ready answers 503
    precisely to say it is not. A protocol probe is ready below 500. A server error is
    never ready.
    """
    if role in _PROTOCOL_PROBE_ROLES:
        return 100 <= status < 500
    return 200 <= status < 300


_ROLE_PROBE_PATHS = {
    "app": _PATH_HEALTH_LIVE,
    "control": _PATH_HEALTH_LIVE,
    "installer": "/api/extensions/capabilities/archive-lifecycle",
    "mcp": "/mcp",
    # The router's own /health/ready is the supervisor's aggregate readiness, which
    # includes the router: probing it asked "is everything ready" of a question whose
    # answer depended on this one, and once 503 stopped counting as ready (P2) Compact
    # could never become ready. /health/live through the router reaches the app's
    # liveness, which proves the router listens and routes and depends on nothing the
    # router's readiness feeds.
    "router": _PATH_HEALTH_LIVE,
}


def _role_probe_path(role: str) -> str | None:
    return _PATH_HEALTH_LIVE if role.startswith("worker-") else _ROLE_PROBE_PATHS.get(role)


def _role_protocol_ready(role: str, port: int, timeout: float) -> bool:
    path = _role_probe_path(role)
    if path is None:
        return False
    connection = HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return _status_means_ready(role, int(response.status))
    except (OSError, HTTPException):
        return False
    finally:
        connection.close()
