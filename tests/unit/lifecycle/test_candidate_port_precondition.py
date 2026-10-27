"""P2: config apply behind an existing Traefik demanded a free public port.

validate_target probed config.public_port for every mode but reverse_proxy. Behind an
external Traefik that port belongs to the Traefik -- Flow Steward publishes none -- so
the check could only pass on a host where the operator's own Traefik was not running.
The same held for an external proxy in Docker, whose overlay publishes nothing either.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application.lifecycle.contracts import DeploymentConfig
from tests.unit.lifecycle.test_reverse_proxy_target_override import (
    ConfigurationLifecycleGateway,
    ConfigurationService,
    DockerLifecycleGateway,
    LifecycleConfigurationStore,
    MagicMock,
    ROOT,
    _RecordingProcess,
)


def _validate(tmp_path: Path, config: DeploymentConfig, *, busy: bool) -> list[tuple]:
    store = LifecycleConfigurationStore(tmp_path)
    store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    store.compose_env_path.write_text("FS_COMPOSE_PROJECT_NAME=flow-steward\n", encoding="utf-8")
    target = ConfigurationService(store=store).derive_target(config)
    gateway = ConfigurationLifecycleGateway(
        project_root=ROOT,
        docker=DockerLifecycleGateway(project_root=ROOT, process=_RecordingProcess()),
        preservation=MagicMock(),
        manifest_store=MagicMock(),
        readiness_service=MagicMock(),
        release_provider=lambda: "test",
        storage_probe=lambda _environment: True,
    )
    probed: list[tuple] = []

    def port_available(_target, host, port, *, protocol):
        probed.append((host, port, protocol))
        return not busy

    gateway._docker.port_available_for_transition = port_available  # type: ignore[method-assign]
    gateway.validate_target(config, target, {})
    return probed


_BEHIND_EXTERNAL_TRAEFIK = DeploymentConfig(
    environment="production",
    deployment_profile="scalable",
    ingress_mode="external_traefik",
    tls_mode="external_resolver",
    public_host="flow.example.com",
    public_port=443,
    traefik_network="traefik",
    traefik_cert_resolver="myresolver",
)
_BEHIND_A_DOCKER_PROXY = DeploymentConfig(
    environment="production",
    deployment_profile="scalable",
    ingress_mode="external_proxy",
    tls_mode="reverse_proxy",
    public_host="fs.example.test",
    proxy_placement="docker",
    proxy_kind="nginx",
    proxy_network="proxy",
)


@pytest.mark.parametrize("config", (_BEHIND_EXTERNAL_TRAEFIK, _BEHIND_A_DOCKER_PROXY))
def test_an_ingress_that_publishes_no_host_port_is_not_asked_for_one(
    tmp_path: Path, config: DeploymentConfig
) -> None:
    probed = _validate(tmp_path, config, busy=True)

    assert probed == []


@pytest.mark.parametrize(
    "config",
    (
        DeploymentConfig(environment="production", deployment_profile="scalable", public_port=8443),
        DeploymentConfig(
            environment="production",
            deployment_profile="scalable",
            ingress_mode="external_proxy",
            tls_mode="reverse_proxy",
            public_host="fs.example.test",
            proxy_placement="host",
            proxy_kind="nginx",
        ),
    ),
)
def test_an_ingress_that_publishes_a_port_still_needs_it_free(
    tmp_path: Path, config: DeploymentConfig
) -> None:
    """The green direction: bundled Traefik and a host proxy do bind, and must be refused."""
    with pytest.raises(ValueError, match="port is unavailable"):
        _validate(tmp_path, config, busy=True)
