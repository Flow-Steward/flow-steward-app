"""Host adapters used by the ownership policy; no broad filesystem/Docker sweeps."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from core.application.tooling.resource_cleanup.models import Ledger, Resource
from core.infrastructure.agent_resources import filesystem, process, quality_environment
from core.infrastructure.agent_resources.builders import PrivateBuilders
from core.infrastructure.agent_resources.docker import DockerResources


class NativeResources:
    def __init__(self) -> None:
        self.docker = DockerResources()
        self.builders = PrivateBuilders(self.docker)

    @staticmethod
    def process_group_alive(pgid: int) -> bool:
        return process.group_alive(pgid)

    @staticmethod
    def current_pid() -> int:
        return os.getpid()

    @staticmethod
    def execution_environment() -> dict[str, str]:
        return dict(os.environ)

    @staticmethod
    def execute_owned(
        command: list[str], environment: dict[str, str], on_start: Callable[[int], None]
    ) -> int:
        return process.run_command(command, environment, on_start)

    @staticmethod
    def process_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def free_bytes(path: Path) -> int:
        return shutil.disk_usage(path).free

    def finalize_workspace(self, ledger: Ledger) -> None:
        filesystem.finalize_workspace(ledger)

    def create_builder(self, ledger: Ledger) -> tuple[str, dict[str, str]]:
        return self.builders.name(ledger), self.builders.create(ledger)

    def inventory(self) -> dict[str, list[str]]:
        result = {
            "containers": self.docker.command("container", "ls", "-aq", "--no-trunc").split(),
            "networks": self.docker.command("network", "ls", "-q", "--no-trunc").split(),
            "volumes": self.docker.command("volume", "ls", "-q").split(),
            "builders": list(self.builders.names()),
            "images": [],
        }
        for line in self.docker.command(
            "image", "ls", "--no-trunc", "--format", "{{json .}}"
        ).splitlines():
            image = json.loads(line)
            result["images"].append(f"{image['Repository']}:{image['Tag']}|{image['ID']}")
        return {key: sorted(set(values)) for key, values in result.items()}

    def discover(self, ledger: Ledger) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        filters = [
            "--filter",
            f"label=org.flowsteward.task={ledger.owner}",
            "--filter",
            f"label=org.flowsteward.resource-token={ledger.token}",
            "--filter",
            "label=org.flowsteward.disposable=true",
        ]
        for kind in ("container", "network", "volume"):
            arguments = [kind, "ls", "-q", *filters]
            if kind == "container":
                arguments += ["-a", "--no-trunc"]
            if kind == "network":
                arguments += ["--no-trunc"]
            for identity in self.docker.command(*arguments).split():
                found.append((kind, identity))
        for identity in set(
            self.docker.command("image", "ls", "-q", "--no-trunc", *filters).split()
        ):
            data = self.docker.inspect("image_tag", identity)
            if data:
                for tag in data.get("RepoTags") or []:
                    if (
                        tag.startswith("fs-owned/" + ledger.token + ":")
                        or tag == "fs-owned-" + ledger.token + "-fs-audit:latest"
                    ):
                        found.append(("image_tag", tag))
        return found

    def directory_consumers(self, resource: Resource) -> bool:
        target = Path(resource.identity)
        for identity in self.docker.command("container", "ls", "-aq", "--no-trunc").split():
            data = self.docker.inspect("container", identity)
            if data is None:
                continue
            for mount in data.get("Mounts", []):
                if mount.get("Type") != "bind":
                    continue
                source = Path(mount["Source"].removeprefix("/host_mnt"))
                if target.is_relative_to(source) or source.is_relative_to(target):
                    return True
        return False

    @staticmethod
    def prepare_environment(
        ledger: Ledger, command: list[str], environment: dict[str, str], scratch: str, cache: str
    ) -> dict[str, str]:
        return quality_environment.prepare(ledger, command, environment, scratch, cache)

    def create_directory(self, ledger: Ledger, name: str) -> tuple[str, dict[str, str]]:
        return filesystem.create(ledger, name)

    def capture(
        self, ledger: Ledger, kind: str, identity: str, *, borrowed: bool = False
    ) -> dict[str, str]:
        if kind == "directory":
            if not borrowed:
                raise ValueError("owned directories must be created through mkdir, never adopted")
            return filesystem.capture(Path(identity))
        if kind == "builder":
            raise ValueError("builders must be created through private-builder")
        return self.docker.capture(kind, identity, ledger, borrowed=borrowed)

    def validate(self, ledger: Ledger, resource: Resource) -> bool:
        if resource.kind == "directory":
            filesystem.no_symlinks(Path(resource.identity))
            if not Path(resource.identity).exists() and not Path(resource.identity).is_symlink():
                return False
            filesystem.validate(ledger, resource)
            if self.directory_consumers(resource):
                raise ValueError("directory has a Docker bind-mount consumer")
            return True
        if resource.kind == "builder":
            return self.builders.validate(ledger, resource)
        return self.docker.validate(ledger, resource)

    def remove(self, ledger: Ledger, resource: Resource) -> None:
        if resource.kind == "directory":
            self.validate(ledger, resource)
            filesystem.remove(ledger, resource)
        elif resource.kind == "builder":
            self.builders.remove(ledger, resource)
        else:
            self.docker.remove(ledger, resource)
