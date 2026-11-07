"""Installed Compact roles recover through Docker, including daemon restart."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("compose_file", "service", "policy"),
    [
        ("docker-compose.compact.yaml", "rabbitmq-fs", "unless-stopped"),
        ("docker-compose.compact.yaml", "pg-fs", "unless-stopped"),
        ("docker-compose.compact.yaml", "compact-runtime", "unless-stopped"),
        ("compact/ingress-bundled_traefik.yaml", "traefik", "unless-stopped"),
        ("compact/storage-bundled.yaml", "seaweedfs", "unless-stopped"),
        ("docker-compose.compact.yaml", "memory-postgres-extensions", "no"),
        ("docker-compose.compact.yaml", "fs-cli", "no"),
        ("compact/storage-bundled.yaml", "seaweedfs-config", "no"),
    ],
)
def test_compact_role_restart_contract(compose_file: str, service: str, policy: str) -> None:
    # These declarations are the boundary Docker consumes: on-failure would
    # strand an installation after daemon reboot; always revives a manual stop.
    model = yaml.safe_load((ROOT / "infra/docker" / compose_file).read_text())
    assert model["services"][service]["restart"] == policy


def test_compact_daemon_restart_waits_for_dependencies_before_bootstrap() -> None:
    # Docker starts existing containers without Compose's dependency ordering.
    # The runtime must opt into its protocol probes on every container start.
    model = yaml.safe_load((ROOT / "infra/docker/docker-compose.compact.yaml").read_text())
    assert (
        str(model["services"]["compact-runtime"]["environment"]["FS_COMPACT_WAIT_FOR_DEPENDENCIES"])
        == "1"
    )
