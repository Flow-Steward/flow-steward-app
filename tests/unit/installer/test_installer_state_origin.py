"""Report orphaned deployment state before choosing official versus source images."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from core.application.installer import InstallationKind
from core.application.installer.service import InstallerService
from core.entrypoints.cli import installer
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore

_VOLUMES = (
    "flow-steward_fs-compact-data",
    "flow-steward_fs-postgres",
    "flow-steward_fs-rabbitmq",
    "flow-steward_seaweedfs-config",
    "flow-steward_seaweedfs-data",
)
_CONTAINER = "flow-steward-seaweedfs-config-1"


class Inventory:
    def __init__(self, *, orphaned: bool = False, available: bool = True) -> None:
        self.orphaned = orphaned
        self.available = available
        self.calls: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs):
        command = tuple(command)
        self.calls.append(command)
        if command[:3] == ("docker", "volume", "ls"):
            output = "\n".join(_VOLUMES) if self.orphaned else ""
        elif command[:3] == ("docker", "ps", "-a"):
            output = _CONTAINER if self.orphaned else ""
        else:
            pytest.fail(f"Unexpected mutation or remote command: {command}")
        return ProcessOutcome(command=command, returncode=0 if self.available else 1, stdout=output)


def _service(root: Path, inventory: Inventory):
    state = InstallerStateStore(root / ".flow-steward")
    gateway = DockerInstallerGateway(
        repo_root=root, state_store=state, process=inventory, environment={}
    )
    return InstallerService(gateway=gateway, state=state, project_identity="flow-steward"), state


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("source_flag", [False, True])
def test_new_clone_with_orphaned_docker_state_reports_ambiguity_before_image_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, json_output: bool, source_flag: bool
) -> None:
    inventory = Inventory(orphaned=True)
    service, state = _service(tmp_path, inventory)
    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "_runtime_env", {})
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(installer, "installer_state_root", lambda _root: state.root)
    arguments = ["--json"] if json_output else ["--plain", "--non-interactive"]
    arguments += ["--confirm"]
    if source_flag:
        arguments.append("--from-source")

    result = CliRunner().invoke(installer.installer_command, arguments)

    assert result.exit_code == 9, result.output
    if json_output:
        assert json.loads(result.output)["error"]["code"] == "ambiguous_installation_state"
    else:
        assert "ambiguous_installation_state" in result.output
    assert _CONTAINER in result.output
    assert all(name in result.output for name in _VOLUMES)
    assert "source_install_requires_flag" not in result.output
    assert "building product images" not in result.output
    assert "preserved" in result.output
    assert "FS_INSTALL_MODE" not in installer._runtime_env
    assert len(inventory.calls) == 2
    assert not state.root.exists()


def test_unreadable_docker_inventory_is_not_a_source_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = Inventory(available=False)
    service, state = _service(tmp_path, inventory)
    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "_runtime_env", {})
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(installer, "installer_state_root", lambda _root: state.root)

    result = CliRunner().invoke(installer.installer_command, ["--json", "--confirm"])

    assert result.exit_code == 9, result.output
    assert json.loads(result.output)["error"]["code"] == "ambiguous_installation_state"
    assert "inventory" in result.output.lower()
    assert "source_install_requires_flag" not in result.output
    assert not state.root.exists()


def test_bootstrap_files_alone_do_not_turn_a_clean_clone_into_a_source_resume(tmp_path: Path):
    inventory = Inventory()
    service, state = _service(tmp_path, inventory)
    (state.root / "runtime").mkdir(parents=True)
    (state.root / "runtime/installer-bootstrap-image").write_text("management dependency cache")
    (state.root / "logs").mkdir()
    (state.root / "installer-host.pid").write_text("12345")

    assert service.inspect().kind is InstallationKind.FRESH
    assert len(inventory.calls) == 2
