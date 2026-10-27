"""AC17-R148: readiness judges whether first-admin bootstrap is open, not the flag alone.

Every installation keeps FS_AUTH_BOOTSTRAP_ENABLED=1 after its first administrator
exists: the installer writes it, Compact and Railway run with it, and nothing ever
turns it off -- there is nothing that could on Railway, and a one-time setup link for
a restored empty database depends on it. What keeps bootstrap shut is elsewhere: the
endpoint refuses once any user exists (an atomic zero-user invariant), and until then
it requires a setup token only the operator holds. Scalable readiness nevertheless
demanded the flag be 0, so `status` on every Scalable installation said BLOCKED and
could never say anything else.

The check now asks the question the flag was standing in for -- is bootstrap reachable
without an operator-held secret -- and keeps failing exactly where it is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application.installer import InstallConfig
from core.application.tooling.ops.deployment_doctor_static import _check_auth_defaults
from core.infrastructure.installer import gateway as gateway_module
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore


class _Process:
    def run(self, command, **_kwargs):
        return ProcessOutcome(command=tuple(command), returncode=0)


def _installer_written_env(tmp_path: Path, config: InstallConfig) -> dict[str, str]:
    """The .env.prod the installer's own secrets step writes -- not a hand-built one."""
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
    )
    log = gateway.state_store.open_log("r148")
    try:
        gateway.ensure_secrets(config, log=log)
    finally:
        log.close()
    return gateway_module._read_env(tmp_path / ".env.prod")


@pytest.mark.parametrize(
    "config",
    (
        InstallConfig(environment="production", deployment_profile="scalable"),
        InstallConfig(
            environment="production", deployment_profile="scalable", file_storage="local_fs"
        ),
        InstallConfig(
            environment="production", deployment_profile="scalable", public_host="fs.example.test"
        ),
        InstallConfig(
            environment="production", deployment_profile="compact", file_storage="local_fs"
        ),
    ),
    ids=("scalable-bundled", "scalable-local-fs", "scalable-public-host", "compact"),
)
def test_the_environment_the_installer_writes_is_not_blocked_by_its_own_readiness(
    tmp_path: Path, config: InstallConfig
) -> None:
    env = _installer_written_env(tmp_path, config)
    # Non-vacuity: this is the state every audited installation is in.
    assert env["FS_AUTH_BOOTSTRAP_ENABLED"] == "1"

    result = _check_auth_defaults(env, stack_environment="production")

    assert result.status == "pass", result.detail


def _production_env(**overrides: str) -> dict[str, str]:
    env = {
        "FS_LOCAL_AUTH_ENABLED": "1",
        "FS_AUTH_COOKIE_SECURE": "1",
        "FS_AUTH_BOOTSTRAP_ENABLED": "1",
    }
    env.update(overrides)
    return {name: value for name, value in env.items() if value != "<unset>"}


@pytest.mark.parametrize(
    ("overrides", "gated"),
    (
        ({"FS_AUTH_BOOTSTRAP_STATE_FILE": "/run/flow-steward-runtime/state.json"}, True),
        ({"FS_AUTH_BOOTSTRAP_TOKEN": "a-token-only-the-operator-holds"}, True),
        ({"FS_AUTH_BOOTSTRAP_ENABLED": "0"}, True),
        # The cases the check exists for: reachable without a secret.
        ({}, False),
        ({"FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY": "0"}, False),
        ({"FS_AUTH_BOOTSTRAP_STATE_FILE": "  ", "FS_AUTH_BOOTSTRAP_TOKEN": ""}, False),
        ({"FS_AUTH_BOOTSTRAP_TOKEN": "CHANGE_ME"}, False),
    ),
    ids=(
        "state-file",
        "token",
        "disabled",
        "no-secret",
        "no-secret-remote",
        "blank-secret",
        "placeholder-token",
    ),
)
def test_production_bootstrap_passes_only_behind_an_operator_held_secret(
    overrides: dict[str, str], gated: bool
) -> None:
    result = _check_auth_defaults(_production_env(**overrides), stack_environment="production")

    assert (result.status == "pass") is gated, result.detail
    if not gated:
        assert "FS_AUTH_BOOTSTRAP_ENABLED" in result.detail


def test_the_other_auth_defaults_are_still_enforced_behind_a_token() -> None:
    env = _production_env(
        FS_AUTH_BOOTSTRAP_STATE_FILE="/run/flow-steward-runtime/state.json",
        FS_AUTH_COOKIE_SECURE="0",
    )

    result = _check_auth_defaults(env, stack_environment="production")

    assert result.status == "fail"
    assert result.detail == "FS_AUTH_COOKIE_SECURE should be 1"
