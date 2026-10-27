"""P1-3: config apply behind an external Nginx or Apache stopped at validation.

The dynamic reverse-proxy override was passed whenever TLS was reverse_proxy, while the
production target selects the static ingress overlays instead of the installer override
file, and the Compose command builder refuses an override the target does not select:

    StackGatewayError: Candidate reverse-proxy override has no selected Compose target.

Reproduced for Scalable behind a host proxy, Scalable behind a Docker proxy and Compact
behind a host proxy, through the real target derivation and the real command builder.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import DeploymentConfig
from core.infrastructure.lifecycle.configuration_gateway import ConfigurationLifecycleGateway
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore
from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway

ROOT = Path(__file__).resolve().parents[3]


class _RecordingProcess:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, command, **kwargs):
        self.calls.append((list(command), kwargs.get("input_text")))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _config(profile: str, placement: str, environment: str = "production") -> DeploymentConfig:
    return DeploymentConfig(
        environment=environment,
        deployment_profile=profile,
        ingress_mode="external_proxy",
        tls_mode="reverse_proxy",
        public_host="fs.example.test",
        proxy_placement=placement,
        proxy_kind="nginx",
        **({"proxy_network": "proxy"} if placement == "docker" else {}),
    )


def _validate(tmp_path: Path, config: DeploymentConfig) -> _RecordingProcess:
    store = LifecycleConfigurationStore(tmp_path)
    store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    store.compose_env_path.write_text("FS_COMPOSE_PROJECT_NAME=flow-steward\n", encoding="utf-8")
    target = ConfigurationService(store=store).derive_target(config)
    process = _RecordingProcess()
    gateway = ConfigurationLifecycleGateway(
        project_root=ROOT,
        docker=DockerLifecycleGateway(project_root=ROOT, process=process),
        preservation=MagicMock(),
        manifest_store=MagicMock(),
        readiness_service=MagicMock(),
        release_provider=lambda: "test",
        storage_probe=lambda _environment: True,
    )
    gateway._docker.port_available_for_transition = lambda *_a, **_k: True  # type: ignore[method-assign]
    gateway.validate_target(config, target, {})
    return process


@pytest.mark.parametrize(
    ("profile", "placement"),
    (("scalable", "host"), ("scalable", "docker"), ("compact", "host")),
)
def test_a_production_target_behind_an_external_proxy_validates(
    tmp_path: Path, profile: str, placement: str
) -> None:
    process = _validate(tmp_path, _config(profile, placement))

    (command, stdin), *_ = process.calls
    assert "-" not in command  # no override read from stdin
    assert stdin is None
    assert any("ingress-external_proxy" in part for part in command)


def test_development_behind_a_host_proxy_still_gets_its_override(tmp_path: Path) -> None:
    """The one target that selects the installer override file keeps receiving it."""
    process = _validate(tmp_path, _config("scalable", "host", environment="development"))

    (command, stdin), *_ = process.calls
    assert "-" in command
    assert stdin is not None
    assert "installer-disabled-traefik" in stdin
