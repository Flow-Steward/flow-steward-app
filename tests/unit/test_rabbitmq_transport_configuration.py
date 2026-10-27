"""Canonical deployment generation and diagnostics share transport ownership."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.infrastructure.config.connection_strings import build_rabbitmq_url
from core.infrastructure.config.env_registry import StackEnvSelection, build_installer_env
from core.infrastructure.config.rabbitmq_transport import validate_rabbitmq_endpoint
from tests.unit.test_compose_environment_markers import _services

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_installer_generates_matching_owned_local_broker(profile):
    env = build_installer_env(
        selection=StackEnvSelection(environment="production", file_storage="local_fs"),
        existing={},
        secret_factory=lambda: "generated-secret",
        deployment_profile=profile,
    )
    endpoint = validate_rabbitmq_endpoint(env["RABBITMQ_URL"], env)
    assert env["FS_RABBITMQ_TRANSPORT_MODE"] == "compose-local"
    assert (endpoint.host, endpoint.port, endpoint.tls) == ("rabbitmq-fs", 5672, False)


@pytest.mark.parametrize(
    "name",
    [
        "docker-compose.yaml",
        "docker-compose.prod.yaml",
        "docker-compose.compact.yaml",
        "docker-compose.oauth-browser.yaml",
    ],
)
def test_all_canonical_broker_consumers_declare_their_deployment_context(name):
    services = _services(ROOT / "infra/docker" / name)
    found = False
    for service in services.values():
        environment = service.get("environment", {})
        if not isinstance(environment, dict) or "RABBITMQ_URL" not in environment:
            continue
        found = True
        mode = environment["FS_RABBITMQ_TRANSPORT_MODE"]
        assert mode == (
            "audit-local" if "rabbitmq-audit" in environment["RABBITMQ_URL"] else "compose-local"
        )
    assert found


def test_component_urls_default_to_verified_remote_tls_and_preserve_ipv6():
    env = {"RABBITMQ_HOST": "2001:db8::1", "RABBITMQ_USER": "user", "RABBITMQ_PASS": "p@ss"}
    url = build_rabbitmq_url(env)
    assert url == "amqps://user:p%40ss@[2001:db8::1]:5671/"
    assert validate_rabbitmq_endpoint(url, env).tls is True


def test_doctor_refuses_remote_plaintext_before_opening_a_socket(monkeypatch):
    from core.application.tooling.ops import deployment_doctor_live as doctor

    monkeypatch.setattr(
        doctor.socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("unsafe broker socket"),
    )
    result = doctor._check_queue_connectivity(
        {"RABBITMQ_URL": "amqp://user:private-credential@remote.example.test:5672/"}, timeout=1
    )
    assert result.status == "fail"
    assert "transport" in result.detail
    assert "private-credential" not in result.detail


def test_doctor_preserves_verified_tls_socket_probe(monkeypatch):
    from contextlib import nullcontext

    from core.application.tooling.ops import deployment_doctor_live as doctor

    peers = []

    def connect(peer, *, timeout):
        peers.append(peer)
        return nullcontext(SimpleNamespace(getpeername=lambda: peer))

    monkeypatch.setattr(doctor.socket, "create_connection", connect)
    result = doctor._check_queue_connectivity(
        {"RABBITMQ_URL": "amqps://user:encoded%40credential@remote.example.test:5671/"}, timeout=1
    )
    assert result.status == "pass"
    assert peers == [("remote.example.test", 5671)]
