from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core.infrastructure.runtime.compact.router import CompactRouterConfig, render_router_config


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Upstream:
    def __init__(self, role: str) -> None:
        self.role = role
        self.release_stream = threading.Event()

        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                if self.headers.get("upgrade", "").lower() == "websocket":
                    self.send_response(101)
                    self.send_header("upgrade", "websocket")
                    self.send_header("connection", "Upgrade")
                    self.end_headers()
                    return
                if self.path.startswith("/mcp/stream"):
                    self.send_response(200)
                    self.send_header("content-length", "10")
                    self.end_headers()
                    self.wfile.write(b"first")
                    self.wfile.flush()
                    assert owner.release_stream.wait(timeout=5)
                    self.wfile.write(b"last!")
                    return
                payload = json.dumps(
                    {
                        "role": owner.role,
                        "path": self.path,
                        "authorization": self.headers.get("authorization"),
                        "trace": self.headers.get("x-test-trace"),
                    }
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_port)

    def __enter__(self) -> _Upstream:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release_stream.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _wait_for_router(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"Traefik exited early: {stdout!r} {stderr!r}")
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
            connection.request("GET", "/mcpx")
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                return
        except OSError:
            pass
        time.sleep(0.02)
    raise AssertionError("Traefik did not listen before the integration deadline")


def test_real_compact_router_preserves_http_streaming_and_websocket_contracts(
    tmp_path: Path,
) -> None:
    traefik = Path(os.environ.get("FS_TEST_TRAEFIK_BINARY", "/usr/local/bin/traefik"))
    if not traefik.is_file():
        pytest.skip("pinned Traefik binary is unavailable")
    public_port = _free_port()
    with _Upstream("app") as app, _Upstream("mcp") as mcp:
        config_path = tmp_path / "router.yaml"
        config_path.write_text(
            render_router_config(
                CompactRouterConfig(
                    public_port=public_port,
                    app_origin=f"http://127.0.0.1:{app.port}",
                    mcp_origin=f"http://127.0.0.1:{mcp.port}",
                )
            ),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [
                str(traefik),
                f"--entryPoints.compact.address=127.0.0.1:{public_port}",
                "--entryPoints.compact.transport.respondingTimeouts.writeTimeout=0s",
                f"--providers.file.filename={config_path}",
                "--providers.file.watch=false",
                "--api.dashboard=false",
                "--log.level=ERROR",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for_router(public_port, process)

            connection = http.client.HTTPConnection("127.0.0.1", public_port, timeout=5)
            connection.request(
                "GET",
                "/mcp?project=one",
                headers={"Authorization": "Bearer exact-token", "X-Test-Trace": "mcp"},
            )
            mcp_payload = json.loads(connection.getresponse().read())
            assert mcp_payload == {
                "role": "mcp",
                "path": "/mcp?project=one",
                "authorization": "Bearer exact-token",
                "trace": "mcp",
            }

            connection.request("GET", "/mcpx?project=two", headers={"X-Test-Trace": "app"})
            app_payload = json.loads(connection.getresponse().read())
            assert app_payload["role"] == "app"
            assert app_payload["path"] == "/mcpx?project=two"
            assert app_payload["trace"] == "app"

            stream = http.client.HTTPConnection("127.0.0.1", public_port, timeout=5)
            stream.request("GET", "/mcp/stream")
            response = stream.getresponse()
            assert response.read(5) == b"first"
            mcp.release_stream.set()
            assert response.read() == b"last!"

            websocket = socket.create_connection(("127.0.0.1", public_port), timeout=5)
            websocket.sendall(
                b"GET /socket HTTP/1.1\r\n"
                + f"Host: 127.0.0.1:{public_port}\r\n".encode()
                + b"Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
            )
            assert b" 101 " in websocket.recv(1024).split(b"\r\n", 1)[0]
            websocket.close()
        finally:
            process.terminate()
            process.wait(timeout=5)
