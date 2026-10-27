"""AC17-R152: `config apply` must keep the host identity the containers run as.

Measured by the auditor on a Scalable installation whose operator is uid 1001: every
apply started its candidates as 1000:1000, they could not open their own log or the
runtime state (both 1001, owner-only), /health/ready answered 500, and the change rolled
back. Before and after -- the rollback restores the file -- they ran as 1001.

The installer records FS_HOST_UID/FS_HOST_GID inside its managed block of .env.prod,
and every service runs as `${FS_HOST_UID:-1000}:${FS_HOST_GID:-1000}`. The lifecycle
rewrites that block from its own set of keys, which did not include them, so the
rendered target fell back to 1000. On a host whose operator is uid 1000 nothing shows.

Driven through the installer's real secrets step and the lifecycle store's real
rewrite; only the uid is chosen.
"""

from __future__ import annotations

from pathlib import Path

from core.application.installer import InstallConfig
from core.infrastructure.installer import gateway as gateway_module
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore


class _Process:
    def run(self, command, **_kwargs):
        return ProcessOutcome(command=tuple(command), returncode=0)


def _installed_env(tmp_path: Path) -> Path:
    (tmp_path / "infra/docker").mkdir(parents=True)
    for name in ("docker-compose.yaml", "docker-compose.prod.yaml"):
        (tmp_path / "infra/docker" / name).write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    gateway = DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward"),
        process=_Process(),
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment={},
        sleep=lambda _seconds: None,
        secret_factory=lambda: "generated-secret",
        host_identity=lambda: (1001, 1002),
    )
    log = gateway.state_store.open_log("r152")
    try:
        gateway.ensure_secrets(
            InstallConfig(environment="production", deployment_profile="scalable"), log=log
        )
    finally:
        log.close()
    return tmp_path / ".env.prod"


def _values(path: Path) -> dict[str, str]:
    return gateway_module._read_env(path)


def test_the_lifecycle_rewrite_keeps_the_identity_the_installer_recorded(tmp_path: Path) -> None:
    env_path = _installed_env(tmp_path)
    # Non-vacuity: the installer did record the operator's identity.
    assert (_values(env_path)["FS_HOST_UID"], _values(env_path)["FS_HOST_GID"]) == (
        "1001",
        "1002",
    )

    store = LifecycleConfigurationStore(tmp_path)
    store.write_runtime_environment(env_path, {"FS_WORKER_PROCESSES": "2", "APP_PORT": "8099"})

    values = _values(env_path)
    assert (values.get("FS_HOST_UID"), values.get("FS_HOST_GID")) == ("1001", "1002")
    assert values["APP_PORT"] == "8099"  # the rewrite itself still happened
