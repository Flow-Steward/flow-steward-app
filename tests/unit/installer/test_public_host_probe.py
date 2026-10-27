"""Real HTTP behavior behind the installer's disposable host-network boundary."""

from __future__ import annotations

import base64
import io
import json
import signal
import ssl
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core.application.installer.contracts import InstallConfig
from core.infrastructure.installer import gateway_health, public_probe
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome, SubprocessExecutor
from core.infrastructure.installer.state_store import InstallerStateStore

ROOT = Path(__file__).resolve().parents[3]
IMAGE_ID = "sha256:" + "a" * 64


class NativeHostProcess:
    """Replace only Docker isolation; execute the production HTTP probe unchanged."""

    def __init__(self, repo_root: Path, *, engine_overrides=None) -> None:
        self.repo_root = repo_root
        self.probes = 0
        self.engine_overrides = engine_overrides or {}

    def run(self, command, **kwargs):
        if command[:2] == ["docker", "info"]:
            return ProcessOutcome(
                command=tuple(command),
                returncode=0,
                stdout=json.dumps(
                    {
                        "OSType": "linux",
                        "OperatingSystem": "Ubuntu 24.04.5 LTS",
                        "KernelVersion": "6.8.0-139-generic",
                        "SecurityOptions": ["name=seccomp,profile=builtin"],
                        **self.engine_overrides,
                    }
                ),
            )
        if command[:3] == ["docker", "container", "inspect"]:
            return ProcessOutcome(command=tuple(command), returncode=0, stdout=IMAGE_ID)
        assert command[:7] == ["docker", "run", "--rm", "--pull", "never", "--network", "host"]
        assert "-i" in command
        assert "--privileged" not in command
        assert "--pid" not in command
        assert "--read-only" in command
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert command[command.index("--user") + 1] == "65534:65534"
        mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
        assert mounts == [
            f"type=bind,src={self.repo_root}/core/infrastructure/installer/public_probe.py,"
            "dst=/opt/fs-public-probe.py,readonly"
        ]
        assert command[-4:] == ["--entrypoint", "python3", IMAGE_ID, "/opt/fs-public-probe.py"]
        self.probes += 1
        return SubprocessExecutor().run(
            [sys.executable, str(ROOT / "core/infrastructure/installer/public_probe.py")],
            input_text=kwargs["input_text"],
            timeout_seconds=kwargs["timeout_seconds"],
            separate_stderr=True,
        )


@contextmanager
def listening_server(*, context=None, status=200, body=None):
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers["Host"]))
            response_body = (
                body
                if body is not None
                else json.dumps({"status": "ok", "auth": {"bootstrap_required": True}}).encode()
            )
            self.send_response(status)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def http_listener():
    with listening_server() as listener:
        yield listener


@pytest.fixture
def tls_context(tmp_path):
    certificate = tmp_path / ".ssl/cert.pem"
    key = tmp_path / ".ssl/key.pem"
    certificate.parent.mkdir()
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=flow.example.test",
            "-addext",
            "subjectAltName=DNS:flow.example.test",
            "-addext",
            "basicConstraints=critical,CA:FALSE",
        ],
        capture_output=True,
        check=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    return context


def host_gateway(tmp_path):
    process = NativeHostProcess(tmp_path)
    return DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward"),
        process=process,
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
        sleep=lambda _seconds: None,
    )


def test_native_linux_public_checks_reach_host_loopback_without_exposing_ingress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, http_listener
) -> None:
    """The Docker bridge route cannot reach a host-only backend; host probing must."""
    port, requests = http_listener
    process = NativeHostProcess(tmp_path)
    gateway = DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward"),
        process=process,
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
        sleep=lambda _seconds: None,
    )
    config = InstallConfig(
        public_host="flow.example.test",
        tls_mode="reverse_proxy",
        reverse_proxy_backend_port=port,
    )

    def unreachable_bridge(*_args, **_kwargs):
        raise ConnectionRefusedError("bridge cannot reach host-only loopback")

    monkeypatch.setattr(gateway_health, "_routed_urlopen", unreachable_bridge)
    log = gateway.state_store.open_log("host-loopback")
    try:
        gateway.verify_public_route(config, log=log)
        bootstrap = gateway.verify_bootstrap(config, log=log)
    finally:
        log.close()
    assert bootstrap.bootstrap_required is True
    assert gateway.public_route_problem(config) == ("", "")
    assert requests == [
        ("/health", "flow.example.test:8099"),
        ("/", "flow.example.test:8099"),
        ("/api/app/bootstrap", "flow.example.test:8099"),
        ("/health", "flow.example.test:8099"),
    ]
    assert process.probes == 4
    assert config.reverse_proxy_backend_host == "127.0.0.1"


def test_host_probe_preserves_tls_authority_and_explicit_certificate_trust(tmp_path, tls_context):
    with listening_server(context=tls_context) as (port, requests):
        gateway = host_gateway(tmp_path)
        config = InstallConfig(
            public_host="flow.example.test", public_port=port, tls_mode="provided"
        )
        assert gateway.public_route_problem(config) == ("", "")
        assert requests == [("/health", f"flow.example.test:{port}")]


def test_host_probe_rejects_wrong_tls_hostname(tmp_path, tls_context):
    with listening_server(context=tls_context) as (port, requests):
        gateway = host_gateway(tmp_path)
        config = InstallConfig(
            public_host="wrong.example.test", public_port=port, tls_mode="provided"
        )
        context = gateway._public_tls_context(config)
        url = gateway.product_url(config) + "/health"
        with pytest.raises(ssl.SSLCertVerificationError, match="Hostname mismatch"):
            gateway._open_public_probe(url, config=config, context=context)
        assert requests == []


def test_host_probe_rejects_untrusted_certificate(tmp_path, tls_context):
    with listening_server(context=tls_context) as (port, requests):
        gateway = host_gateway(tmp_path)
        config = InstallConfig(
            public_host="flow.example.test",
            public_port=port,
            tls_mode="acme",
            acme_contact="audit@example.test",
            acme_directory_url="https://acme.example.test/directory",
        )
        context = gateway._public_tls_context(config)
        url = gateway.product_url(config) + "/health"
        with pytest.raises(ssl.SSLCertVerificationError, match="self-signed"):
            gateway._open_public_probe(url, config=config, context=context)
        assert requests == []


def test_host_probe_keeps_http_failure_visible_to_doctor(tmp_path):
    with listening_server(status=503) as (port, _requests):
        gateway = host_gateway(tmp_path)
        config = InstallConfig(tls_mode="reverse_proxy", reverse_proxy_backend_port=port)
        cause, _remediation = gateway.public_route_problem(config)
        assert "HTTP 503" in cause


@pytest.mark.parametrize("status", [200, 503])
def test_one_shot_probe_reports_real_http_response(status, monkeypatch, capsys):
    with listening_server(status=status, body=b"public health response") as (port, requests):
        payload = {
            "url": "http://flow.example.test/health",
            "authority": "flow.example.test",
            "dial_host": "127.0.0.1",
            "dial_port": port,
            "server_hostname": None,
            "ca_certificates": None,
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        previous_handler = signal.getsignal(signal.SIGALRM)
        try:
            public_probe.main()
        finally:
            signal.signal(signal.SIGALRM, previous_handler)
        output = capsys.readouterr()
        response = json.loads(output.out)
        assert response["status"] == status
        assert base64.b64decode(response["body"], validate=True) == b"public health response"
        assert output.err == ""
        assert requests == [("/health", "flow.example.test")]


def test_host_bootstrap_probe_accepts_schema_projection_larger_than_health_budget(tmp_path):
    body = json.dumps(
        {"auth": {"bootstrap_required": True}, "schemas": "x" * (556_562 - 100)}
    ).encode()
    with listening_server(body=body) as (port, requests):
        gateway = host_gateway(tmp_path)
        config = InstallConfig(
            public_host="flow.example.test",
            public_port=443,
            tls_mode="reverse_proxy",
            reverse_proxy_backend_port=port,
        )
        url = gateway.product_url(config) + "/api/app/bootstrap"
        with gateway._open_public_probe(url, config=config, context=None) as response:
            assert response.status == 200
            assert response.read() == body
        assert requests == [("/api/app/bootstrap", "flow.example.test")]


@pytest.mark.parametrize("path", ["/api/app/bootstrap", "/api/app/bootstrap/"])
def test_host_bootstrap_probe_keeps_a_finite_response_budget(tmp_path, path):
    with listening_server(body=b"x" * (2 * 1024 * 1024 + 1)) as (port, _requests):
        gateway = host_gateway(tmp_path)
        config = InstallConfig(tls_mode="reverse_proxy", reverse_proxy_backend_port=port)
        url = gateway.product_url(config) + path
        with pytest.raises(OSError, match="size limit"):
            gateway._open_public_probe(url, config=config, context=None)


def test_host_probe_refuses_an_oversized_http_body(tmp_path):
    with listening_server(body=b"x" * (512 * 1024 + 1)) as (port, _requests):
        gateway = host_gateway(tmp_path)
        config = InstallConfig(tls_mode="reverse_proxy", reverse_proxy_backend_port=port)
        url = gateway.product_url(config) + "/health"
        with pytest.raises(OSError, match="size limit"):
            gateway._open_public_probe(url, config=config, context=None)


@pytest.mark.parametrize(
    "engine_overrides",
    [
        {"OperatingSystem": "Docker Desktop"},
        {"KernelVersion": "7.0.12-linuxkit"},
        {"SecurityOptions": ["name=rootless"]},
        {"OperatingSystem": ""},
        {"OSType": "windows"},
    ],
)
def test_non_native_engine_keeps_existing_bridge_route(
    tmp_path, monkeypatch, http_listener, engine_overrides
):
    port, requests = http_listener
    process = NativeHostProcess(tmp_path, engine_overrides=engine_overrides)
    gateway = DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward"),
        process=process,
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
    )
    config = InstallConfig(tls_mode="reverse_proxy", reverse_proxy_backend_port=port)
    from core.infrastructure.installer.public_probe import _routed_urlopen

    def mapped_bridge(request, **kwargs):
        assert kwargs["dial_host"] == "host.docker.internal"
        return _routed_urlopen(request, **{**kwargs, "dial_host": "127.0.0.1"})

    monkeypatch.setattr(gateway_health, "_routed_urlopen", mapped_bridge)
    assert gateway.public_route_problem(config) == ("", "")
    assert requests == [("/health", "localhost:8099")]
    assert process.probes == 0


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (1, "", "probe failed"),
        (0, "not JSON", ""),
        (0, "[]", ""),
        (0, '{"status":true,"body":""}', ""),
        (0, '{"status":200,"body":"invalid_base64"}', ""),
        (0, '{"status":200,"body":""}', "unexpected diagnostic"),
    ],
)
def test_host_probe_refuses_failed_or_invalid_child_results(tmp_path, returncode, stdout, stderr):
    class InvalidProbeProcess(NativeHostProcess):
        def run(self, command, **kwargs):
            if command[:2] == ["docker", "run"]:
                return ProcessOutcome(tuple(command), returncode, stdout, stderr)
            return super().run(command, **kwargs)

    gateway = DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward"),
        process=InvalidProbeProcess(tmp_path),
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
    )
    config = InstallConfig(tls_mode="reverse_proxy")
    url = gateway.product_url(config) + "/health"
    with pytest.raises(OSError, match="public route probe"):
        gateway._open_public_probe(url, config=config, context=None)
