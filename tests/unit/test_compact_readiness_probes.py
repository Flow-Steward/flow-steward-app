"""P2: Compact readiness counted a failing role and a starting database as ready.

  - `_role_protocol_ready` returned `100 <= status <= 599`: a role answering 500 or 503
    on its health endpoint was ready.
  - Dependencies were probed by TCP connect alone. Postgres accepts TCP while it is
    still starting up and refuses queries, so readiness said "ready" exactly when it
    was not.

Real sockets on loopback throughout: an HTTP server answering a chosen status, and
dependency servers that accept TCP and then answer the way a starting -- or ready --
service does.
"""

from __future__ import annotations

import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.infrastructure.runtime.compact import health


def _http_server(status: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *_args) -> None:  # pragma: no cover - quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize(
    ("role", "status", "ready"),
    (
        ("app", 200, True),
        ("app", 204, True),
        ("app", 500, False),
        ("app", 503, False),
        ("router", 503, False),
        ("worker-1", 500, False),
        # A protocol probe on an endpoint that needs credentials: the server answering is
        # the signal, a 401 is expected, and a server error is still a failure.
        ("mcp", 401, True),
        ("mcp", 406, True),
        ("mcp", 502, False),
        ("installer", 401, True),
        ("installer", 500, False),
    ),
)
def test_a_role_is_ready_only_when_its_probe_says_so(role: str, status: int, ready: bool) -> None:
    server = _http_server(status)
    try:
        assert health._role_protocol_ready(role, server.server_address[1], 2.0) is ready
    finally:
        server.shutdown()


def _tcp_server(reply) -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def serve() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(2)
                    request = connection.recv(4096)
                    answer = reply(request)
                    if answer:
                        connection.sendall(answer)
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    return listener, listener.getsockname()[1]


def _pg_error(sqlstate: str, message: str) -> bytes:
    fields = (
        b"SFATAL\x00" + b"C" + sqlstate.encode() + b"\x00" + b"M" + message.encode() + b"\x00\x00"
    )
    return b"E" + struct.pack("!I", len(fields) + 4) + fields


_PG_AUTH_REQUEST = b"R" + struct.pack("!II", 12, 10) + b"SCRAM"  # AuthenticationSASL, abridged


@pytest.mark.parametrize(
    ("reply", "ready"),
    (
        (lambda _req: _PG_AUTH_REQUEST, True),
        # Wrong credentials or database still means the server is up and serving.
        (lambda _req: _pg_error("28000", 'role "x" does not exist'), True),
        (lambda _req: _pg_error("57P03", "the database system is starting up"), False),
        (lambda _req: _pg_error("57P03", "the database system is shutting down"), False),
        (lambda _req: b"", False),  # accepts TCP, says nothing: the case TCP alone passed
    ),
)
def test_postgres_is_ready_only_when_it_would_accept_a_session(reply, ready: bool) -> None:
    listener, port = _tcp_server(reply)
    try:
        assert health._postgres_ready(("127.0.0.1", port), "flow_steward", 2.0) is ready
    finally:
        listener.close()


_AMQP_CONNECTION_START = b"\x01\x00\x00" + struct.pack("!I", 4) + b"\x00\x0a\x00\x0a" + b"\xce"


@pytest.mark.parametrize(
    ("reply", "ready"),
    (
        (lambda req: _AMQP_CONNECTION_START if req.startswith(b"AMQP") else b"", True),
        (lambda _req: b"", False),
        (lambda _req: b"HTTP/1.1 400 Bad Request\r\n\r\n", False),
    ),
)
def test_rabbitmq_is_ready_only_when_it_speaks_amqp(reply, ready: bool) -> None:
    listener, port = _tcp_server(reply)
    try:
        assert health._rabbitmq_ready(health.RabbitMQProbeTarget(("127.0.0.1", port)), 2.0) is ready
    finally:
        listener.close()


def test_the_dependency_monitor_does_not_take_an_open_port_for_a_ready_server() -> None:
    """The behavioural red: a listener that accepts TCP and says nothing was 'ready'."""
    from core.infrastructure.runtime.compact.readiness import ReadinessRegistry

    silent_pg, pg_port = _tcp_server(lambda _req: b"")
    ready_mq, mq_port = _tcp_server(
        lambda req: _AMQP_CONNECTION_START if req.startswith(b"AMQP") else b""
    )
    registry = ReadinessRegistry(worker_processes=1)
    stop = threading.Event()
    stop.wait = lambda _seconds: stop.set() or True  # type: ignore[method-assign]
    try:
        health.monitor_dependencies(
            registry,
            postgres=("127.0.0.1", pg_port),
            rabbitmq=health.RabbitMQProbeTarget(("127.0.0.1", mq_port)),
            stop=stop,
            interval_seconds=0.5,
        )
    finally:
        silent_pg.close()
        ready_mq.close()

    assert registry.snapshot().dependencies == {"postgres": False, "rabbitmq": True}


def test_no_role_is_probed_through_the_readiness_it_feeds() -> None:
    """The regression 4beb151de shipped, caught where it lives: in the router's routes.

    The router's /health/ready is routed to the supervisor's aggregate readiness, which
    includes the router. Probing the router there made its readiness depend on itself;
    while any status counted as ready the loop never showed, and once 503 did not,
    no Compact runtime could become ready. Derived from the real router template, so
    moving a route or a probe path cannot quietly close the loop again.
    """
    import re
    from pathlib import Path

    template = (Path(health.__file__).resolve().parent / "router.yaml.tmpl").read_text(
        encoding="utf-8"
    )
    routers = re.findall(
        r"^    (\w[\w-]*):\n(?:      .*\n)*?      rule: \"(.+?)\"\n      priority: (\d+)\n      service: (\w+)",
        template,
        re.MULTILINE,
    )
    assert routers, "no routes parsed from router.yaml.tmpl; this test would be vacuous"
    aggregate = "health"
    assert any(service == aggregate for *_rest, service in routers), "no aggregate route found"

    def serving(path: str) -> str:
        matching = []
        for _name, rule, priority, service in routers:
            exact = re.findall(r"Path\(`([^`]+)`\)", rule)
            prefixes = re.findall(r"PathPrefix\(`([^`]+)`\)", rule)
            if path in exact or any(path.startswith(prefix) for prefix in prefixes):
                matching.append((int(priority), service))
        assert matching, f"no route serves {path}"
        return max(matching)[1]

    probe_path = health._role_probe_path("router")
    assert serving(probe_path) != aggregate, (
        f"the router is probed on {probe_path}, which the router hands to the aggregate "
        "readiness that includes the router"
    )
