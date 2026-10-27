"""W4: a container a killed recreate left renamed gets its Compose name back.

Measured by the auditor: after `config apply` was killed mid-recreate, fs-app kept
Compose's temporary name "ac63e40dd8df_fs-ext-fs-app-1", and `docker exec
fs-ext-fs-app-1` answered "No such container" until an unrelated later run happened to
recreate it. Compose names the replaced container "<its own short id>_<name>".
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.application.lifecycle.contracts import ComposeTarget
from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway

_APP_ID = "ac63e40dd8df"
_WORKER_ID = "0b1c2d3e4f5a"


class _Process:
    def __init__(self, listing: str) -> None:
        self.listing = listing
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs):
        self.commands.append(tuple(command))
        stdout = self.listing if command[:2] == ["docker", "ps"] else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def renames(self) -> list[tuple[str, ...]]:
        return [command[2:] for command in self.commands if command[:2] == ("docker", "rename")]

    def removals(self) -> list[tuple[str, ...]]:
        return [command[2:] for command in self.commands if command[:2] == ("docker", "rm")]


def _target() -> ComposeTarget:
    return ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        env_file=".env.prod",
        compose_files=("infra/docker/docker-compose.prod.yaml",),
        compose_profiles=(),
        required_services=("fs-app", "fs-worker"),
        runtime_services=("fs-app", "fs-worker"),
        rebuild_services=("fs-app", "fs-worker"),
        project_name="fs-ext",
    )


def _start(tmp_path: Path, listing: str) -> _Process:
    process = _Process(listing)
    DockerLifecycleGateway(project_root=tmp_path, process=process).start(
        _target(),
        ("fs-app", "fs-worker"),
        timeout_seconds=30,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )
    return process


def test_a_container_left_under_its_temporary_name_gets_its_name_back(tmp_path: Path) -> None:
    process = _start(
        tmp_path,
        f"{_APP_ID}\t{_APP_ID}_fs-ext-fs-app-1\n{_WORKER_ID}\tfs-ext-fs-worker-1\n",
    )

    assert process.renames() == [(_APP_ID, "fs-ext-fs-app-1")]


def test_a_taken_name_is_left_alone(tmp_path: Path) -> None:
    """Killed after the new container took the name: two containers, not a rename."""
    process = _start(
        tmp_path,
        f"{_APP_ID}\t{_APP_ID}_fs-ext-fs-app-1\n{_WORKER_ID}\tfs-ext-fs-app-1\n",
    )

    assert process.renames() == []


def test_only_the_exact_temporary_form_is_touched(tmp_path: Path) -> None:
    process = _start(
        tmp_path,
        # a prefix that is not the container's own id, and a name outside the project
        f"{_APP_ID}\t{_WORKER_ID}_fs-ext-fs-app-1\n"
        f"{_WORKER_ID}\t{_WORKER_ID}_someone-elses-app-1\n",
    )

    assert process.renames() == []
    assert any(command[:2] == ("docker", "ps") for command in process.commands)  # it looked


# Measured on Compose 5.5.1 (2026-09-25, docker events): a recreate CREATES the new
# container as "<the old container's short id>_<name>", stops and removes the old one,
# then renames the new one. So the temporary name is on the new container, and its
# prefix is not its own id -- the rule above never fired under current Compose (live on
# fs-acc: the runtime kept running as "ad9cfacdf781_fs-acc-compact-runtime-1").
_NEW_ID = "2d589e15b668"
_OLD_ID = "9c7b7a7eead0"


def test_killed_after_the_old_container_went_the_new_one_gets_the_name(tmp_path: Path) -> None:
    process = _start(
        tmp_path,
        f"{_NEW_ID}\t{_OLD_ID}_fs-ext-fs-app-1\tcreated\n{_WORKER_ID}\tfs-ext-fs-worker-1\trunning\n",
    )

    assert process.removals() == []
    assert process.renames() == [(_NEW_ID, "fs-ext-fs-app-1")]


def test_killed_before_the_stopped_old_container_went_the_recreate_is_finished(
    tmp_path: Path,
) -> None:
    """The fs-acc state: the old runtime exited under the name, the new one created."""
    process = _start(
        tmp_path,
        f"{_NEW_ID}\t{_OLD_ID}_fs-ext-fs-app-1\tcreated\n{_OLD_ID}\tfs-ext-fs-app-1\texited\n",
    )

    assert process.removals() == [(_OLD_ID,)]
    assert process.renames() == [(_NEW_ID, "fs-ext-fs-app-1")]


def test_an_old_container_that_still_runs_is_never_removed(tmp_path: Path) -> None:
    """The green side of finishing: it still serves, so nothing is taken from under it."""
    process = _start(
        tmp_path,
        f"{_NEW_ID}\t{_OLD_ID}_fs-ext-fs-app-1\tcreated\n{_OLD_ID}\tfs-ext-fs-app-1\trunning\n",
    )

    assert process.removals() == []
    assert process.renames() == []
