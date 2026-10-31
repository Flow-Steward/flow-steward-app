"""ADR 0047 decision 19 (checklist S6): the Scalable MCP -> app forward secret.

A Scalable installation gets FS_MCP_FORWARD_SECRET from the installer. The runtime step
is where it is completed, because install, resume and an update of a completed
installation all run that step before fs-app and fs-mcp start, while an update never
runs the secrets step -- so a .env.prod from the previous release, which has no such
secret, gets one on its first update. Compact never writes it: its supervisor mints one
per container start.
"""

from __future__ import annotations

from pathlib import Path

from core.application.installer import InstallConfig
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore

_GENERATED = "generated-forward-secret-0123456789abcdef"  # pragma: allowlist secret


class _Docker:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, command, **_kwargs):
        command = list(command)
        self.commands.append(command)
        return ProcessOutcome(command=tuple(command), returncode=0, stdout="")


def _start(tmp_path: Path, *, profile: str, env_text: str | None) -> tuple[Path, str]:
    (tmp_path / "infra/docker").mkdir(parents=True)
    env_path = tmp_path / ".env.prod"
    if env_text is not None:
        env_path.write_text(env_text, encoding="utf-8")
    gateway = DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward", lifecycle_state_root=tmp_path),
        process=_Docker(),
        environment={"FS_COMPOSE_PROJECT_NAME": "fs-acc"},
        secret_factory=lambda: _GENERATED,
    )
    config = InstallConfig(
        environment="production",
        deployment_profile=profile,
        data_placement="compact_volume",
    )
    log = gateway.state_store.open_log("forward-secret")
    gateway.start_runtime(config, log=log)
    log.close()
    return env_path, log.path.read_text(encoding="utf-8")


def _value(env_path: Path, name: str) -> str | None:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip("'\"")
    return None


def test_an_update_of_a_previous_release_env_generates_the_forward_secret(tmp_path: Path) -> None:
    env_path, log_text = _start(
        tmp_path,
        profile="scalable",
        env_text="FS_PUBLIC_BASE_URL=https://fs.example.test\nFS_METRICS_TOKEN=kept\n",
    )

    assert _value(env_path, "FS_MCP_FORWARD_SECRET") == _GENERATED
    assert _value(env_path, "FS_METRICS_TOKEN") == "kept"
    assert "Generated FS_MCP_FORWARD_SECRET" in log_text
    assert _GENERATED not in log_text, "the secret value must never be logged"


def test_an_existing_forward_secret_is_kept(tmp_path: Path) -> None:
    existing = "operator-or-earlier-install-forward-secret-value"  # pragma: allowlist secret
    env_path, log_text = _start(
        tmp_path, profile="scalable", env_text=f"FS_MCP_FORWARD_SECRET={existing}\n"
    )

    assert _value(env_path, "FS_MCP_FORWARD_SECRET") == existing
    assert "Generated FS_MCP_FORWARD_SECRET" not in log_text


def test_a_placeholder_forward_secret_is_replaced(tmp_path: Path) -> None:
    env_path, _log = _start(tmp_path, profile="scalable", env_text="FS_MCP_FORWARD_SECRET=\n")

    assert _value(env_path, "FS_MCP_FORWARD_SECRET") == _GENERATED


def test_compact_never_writes_the_forward_secret_to_the_env_file(tmp_path: Path) -> None:
    env_path, _log = _start(tmp_path, profile="compact", env_text="FS_METRICS_TOKEN=kept\n")

    assert _value(env_path, "FS_MCP_FORWARD_SECRET") is None


def test_no_env_file_is_created_just_for_the_forward_secret(tmp_path: Path) -> None:
    env_path, _log = _start(tmp_path, profile="scalable", env_text=None)

    assert not env_path.exists()


def test_the_forward_secret_is_not_a_lifecycle_required_durable_secret() -> None:
    """`config apply` of an installation without it must keep working (OAuth reports
    forward_secret_missing instead), so it is outside the required-secret lists."""
    from core.infrastructure.config.env_registry import (
        StackEnvSelection,
        generated_secret_env_keys_for_selection,
        installer_generated_secret_names,
    )
    from core.infrastructure.installer.gateway_support import _INSTALLER_SECRET_NAMES

    selection = StackEnvSelection(environment="production")
    assert "FS_MCP_FORWARD_SECRET" not in generated_secret_env_keys_for_selection(selection)
    assert "FS_MCP_FORWARD_SECRET" not in installer_generated_secret_names()
    assert "FS_MCP_FORWARD_SECRET" not in _INSTALLER_SECRET_NAMES
