"""`stack start` runs Compose inside the CLI container, where the installer state
directory is a mount. Bind sources must name the Docker host's path: the container
path made the daemon create an empty directory in place of
`runtime/seaweedfs-s3.json`, and bundled storage never came back up."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.lifecycle.contracts import ComposeTarget
from core.application.lifecycle.stack_models import StackGatewayError
from core.infrastructure.installer.process import (
    CONTAINER_STATE_MOUNT,
    DockerHostStateUnavailable,
    docker_host_state_dir,
)
from core.infrastructure.lifecycle import docker_gateway
from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway

# A host path this process cannot see, as the host project is from a CLI container.
_HOST_STATE = "/srv/operator/flow-steward/.flow-steward"


class _Process:
    def __init__(self) -> None:
        self.invocations: list[dict[str, object]] = []

    def run(self, command: list[str], **kwargs: object):
        self.invocations.append(dict(kwargs))
        return SimpleNamespace(returncode=0, stdout="seaweedfs\n", stderr="")


def _target() -> ComposeTarget:
    return ComposeTarget(
        stack_environment="production",
        file_storage="bundled",
        env_file=".env.prod",
        compose_files=("infra/docker/docker-compose.prod.yaml",),
        compose_profiles=("storage-seaweedfs",),
        required_services=("fs-app", "seaweedfs"),
        runtime_services=("fs-app", "seaweedfs"),
        rebuild_services=("fs-app",),
    )


def _env_prod(root: Path, host_state: str | None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".env.prod"
    lines = ["# installed production stack"]
    if host_state:
        lines.append(f"FS_CANDIDATE_STATE_DIR={host_state}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_container_mount_uses_the_recorded_host_path_it_cannot_see(tmp_path: Path) -> None:
    env_file = _env_prod(tmp_path, _HOST_STATE)

    assert docker_host_state_dir(CONTAINER_STATE_MOUNT, env_file) == _HOST_STATE


def test_container_mount_reads_the_first_env_file_that_records_the_host_path(
    tmp_path: Path,
) -> None:
    unreadable = tmp_path / "host-project-not-mounted" / ".env.prod"
    image_copy = _env_prod(tmp_path / "app", _HOST_STATE)

    assert docker_host_state_dir(CONTAINER_STATE_MOUNT, unreadable, image_copy) == _HOST_STATE


def test_container_mount_without_a_recorded_host_path_stops_instead_of_guessing(
    tmp_path: Path,
) -> None:
    env_file = _env_prod(tmp_path, None)

    with pytest.raises(DockerHostStateUnavailable, match="FS_CANDIDATE_STATE_DIR"):
        docker_host_state_dir(CONTAINER_STATE_MOUNT, env_file)


def test_host_runs_keep_their_own_state_path(tmp_path: Path) -> None:
    state_root = tmp_path / ".flow-steward"
    state_root.mkdir()

    assert docker_host_state_dir(state_root, _env_prod(tmp_path, str(state_root))) is None
    assert docker_host_state_dir(state_root, _env_prod(tmp_path, None)) is None


def test_stack_compose_gets_the_host_state_path_from_the_cli_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(CONTAINER_STATE_MOUNT))
    monkeypatch.setattr(docker_gateway, "_IMAGE_ROOT", tmp_path / "image")
    project_root = tmp_path / "app"
    _env_prod(project_root, _HOST_STATE)
    process = _Process()
    gateway = DockerLifecycleGateway(
        project_root=project_root,
        docker_host_project_root=tmp_path / "host-project-not-mounted",
        process=process,
    )

    gateway.services(_target())

    environment = process.invocations[-1]["environment"]
    assert isinstance(environment, dict)
    assert environment["FS_INSTALLER_STATE_DIR"] == _HOST_STATE


def test_stack_compose_refuses_a_container_state_path_it_cannot_translate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(CONTAINER_STATE_MOUNT))
    monkeypatch.setattr(docker_gateway, "_IMAGE_ROOT", tmp_path / "image")
    project_root = tmp_path / "app"
    _env_prod(project_root, None)
    process = _Process()
    gateway = DockerLifecycleGateway(
        project_root=project_root,
        docker_host_project_root=project_root,
        process=process,
    )

    with pytest.raises(StackGatewayError, match=r"rerun \./install\.sh"):
        gateway.services(_target())
    assert process.invocations == []
