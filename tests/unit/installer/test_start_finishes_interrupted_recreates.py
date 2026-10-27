"""AC17-R177-adjacent F2 (live on fs-acc, 2026-09-25): an update killed mid-recreate,
then `./install.sh` -- exit 0, "already installed", 200 -- and the runtime ran on as
"ad9cfacdf781_fs-acc-compact-runtime-1": the installer never finished the recreate, so
every later Compose command on the canonical name broke. The installer's own starts now
finish it first, by the same rule lifecycle operations use (W4)."""

from __future__ import annotations

from pathlib import Path

from core.application.installer import InstallConfig
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore

_NEW = "2d589e15b668"  # pragma: allowlist secret -- a Docker container ID
_OLD = "9c7b7a7eead0"


class _Docker:
    def __init__(self, listing: str) -> None:
        self.listing = listing
        self.commands: list[list[str]] = []

    def run(self, command, **_kwargs):
        command = list(command)
        self.commands.append(command)
        out = self.listing if command[:3] == ["docker", "ps", "--all"] else ""
        return ProcessOutcome(command=tuple(command), returncode=0, stdout=out)

    def index(self, prefix: list[str]) -> int:
        return next(i for i, c in enumerate(self.commands) if c[: len(prefix)] == prefix)


def _start(tmp_path: Path, listing: str) -> _Docker:
    (tmp_path / "infra/docker").mkdir(parents=True)
    (tmp_path / ".env.prod").write_text("", encoding="utf-8")
    docker = _Docker(listing)
    gateway = DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward", lifecycle_state_root=tmp_path),
        process=docker,
        environment={"FS_COMPOSE_PROJECT_NAME": "fs-acc"},
    )
    config = InstallConfig(
        environment="production", deployment_profile="compact", data_placement="compact_volume"
    )
    gateway.start_runtime(config, log=gateway.state_store.open_log("start-test"))
    return docker


def test_the_runtime_start_finishes_an_interrupted_recreate_before_up(tmp_path: Path) -> None:
    docker = _start(
        tmp_path,
        f"{_NEW}\t{_OLD}_fs-acc-compact-runtime-1\tcreated\n"
        f"{_OLD}\tfs-acc-compact-runtime-1\texited\n",
    )

    removed = docker.index(["docker", "rm", _OLD])
    renamed = docker.index(["docker", "rename", _NEW, "fs-acc-compact-runtime-1"])
    up = next(i for i, c in enumerate(docker.commands) if "up" in c and "compact-runtime" in c)
    assert removed < renamed < up


def test_a_start_with_nothing_interrupted_renames_nothing(tmp_path: Path) -> None:
    docker = _start(tmp_path, f"{_OLD}\tfs-acc-compact-runtime-1\texited\n")

    assert not any(c[:2] in (["docker", "rm"], ["docker", "rename"]) for c in docker.commands)
    assert any("up" in c and "compact-runtime" in c for c in docker.commands)
