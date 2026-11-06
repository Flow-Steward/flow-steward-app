"""Task-created docker-container builders own their cache; shared builders stay untouched."""

from __future__ import annotations

import hashlib
import json
import re
import shlex

from core.application.tooling.resource_cleanup.models import Ledger, Resource
from core.infrastructure.agent_resources.docker import DockerResources


class PrivateBuilders:
    def __init__(self, docker: DockerResources) -> None:
        self.docker = docker

    @staticmethod
    def name(ledger: Ledger) -> str:
        return "fs-owned-" + ledger.token

    def names(self) -> set[str]:
        records = self.docker.command("buildx", "ls", "--format", "json")
        return {json.loads(line)["Name"].rstrip("*") for line in records.splitlines()}

    def create(self, ledger: Ledger) -> dict[str, str]:
        name = self.name(ledger)
        if name in self.names():
            raise ValueError("private builder already exists; refusing to modify it")
        self.docker.command(
            "buildx",
            "create",
            "--name",
            name,
            "--node",
            name + "0",
            "--driver",
            "docker-container",
            "--driver-opt",
            "default-load=true",
            "--driver-opt",
            f"env.FS_RESOURCE_OWNER={ledger.owner}",
            "--driver-opt",
            f"env.FS_RESOURCE_TOKEN={ledger.token}",
        )
        self.docker.command("buildx", "inspect", "--bootstrap", name)
        return self.capture(ledger, name)

    def capture(self, ledger: Ledger, name: str) -> dict[str, str]:
        if name != self.name(ledger):
            raise ValueError("only the task-created private builder is eligible")
        text = self.docker.command("buildx", "inspect", name)
        if not re.search(r"^Driver:\s+docker-container\s*$", text, re.MULTILINE):
            raise ValueError("shared or unsupported builder driver")
        options = re.findall(r"^Driver Options:\s*(.*)$", text, re.MULTILINE)
        endpoints = re.findall(r"^Endpoint:\s*(.*)$", text, re.MULTILINE)
        if len(options) != 1 or len(endpoints) != 1:
            raise ValueError("unexpected private builder topology")
        opts = dict(part.split("=", 1) for part in shlex.split(options[0]) if "=" in part)
        if (
            opts.get("env.FS_RESOURCE_OWNER") != ledger.owner
            or opts.get("env.FS_RESOURCE_TOKEN") != ledger.token
        ):
            raise ValueError("private builder owner options changed")
        container = self.docker.inspect("container", "buildx_buildkit_" + name + "0")
        if container is None:
            raise ValueError("private builder backing container missing")
        environment = (container.get("Config") or {}).get("Env") or []
        if any(
            f"{key}={value}" not in environment
            for key, value in {
                "FS_RESOURCE_OWNER": ledger.owner,
                "FS_RESOURCE_TOKEN": ledger.token,
            }.items()
        ):
            raise ValueError("private builder backing ownership changed")
        mounts = [
            m
            for m in container.get("Mounts", [])
            if m.get("Type") == "volume" and m.get("Destination") == "/var/lib/buildkit"
        ]
        if len(mounts) != 1 or not container.get("Id"):
            raise ValueError("unexpected private builder cache topology")
        volume = self.docker.inspect("volume", mounts[0]["Name"])
        if not volume or not volume.get("CreatedAt"):
            raise ValueError("private builder cache identity unavailable")
        return {
            "endpoint": hashlib.sha256(endpoints[0].strip().encode()).hexdigest(),
            "owner": ledger.owner,
            "token": ledger.token,
            "container_id": container["Id"],
            "volume_name": mounts[0]["Name"],
            "volume_created": volume["CreatedAt"],
            "daemon_id": self.docker.command("info", "--format", "{{.ID}}").strip(),
        }

    def validate(self, ledger: Ledger, resource: Resource) -> bool:
        if resource.identity != self.name(ledger):
            raise ValueError("not the task-created builder")
        if resource.identity not in self.names():
            if not resource.proof.get("container_id") or not resource.proof.get("volume_name"):
                raise ValueError("builder backing identities unavailable")
            if (
                self.docker.inspect("container", resource.proof["container_id"]) is not None
                or self.docker.inspect("volume", resource.proof["volume_name"]) is not None
            ):
                raise ValueError("builder configuration absent but backing cache remains")
            return False
        if self.capture(ledger, resource.identity) != resource.proof:
            raise ValueError("builder identity changed")
        for identity in self.docker.command("container", "ls", "-aq", "--no-trunc").split():
            if identity == resource.proof["container_id"]:
                continue
            peer = self.docker.inspect("container", identity)
            if peer and any(
                m.get("Name") == resource.proof["volume_name"] for m in peer.get("Mounts", [])
            ):
                raise ValueError("private builder cache has another consumer")
        text = self.docker.command("buildx", "inspect", resource.identity)
        if re.search(r"^Status:\s+running", text, re.MULTILINE):
            records = self.docker.command(
                "buildx", "history", "ls", "--builder", resource.identity, "--format", "json"
            )
            for line in records.splitlines():
                if json.loads(line).get("status", "").lower() not in {
                    "completed",
                    "error",
                    "canceled",
                    "cancelled",
                }:
                    raise ValueError("private builder has an active build")
        return True

    def remove(self, ledger: Ledger, resource: Resource) -> None:
        if not self.validate(ledger, resource):
            return
        self.docker.command("buildx", "rm", resource.identity)
        # The configured name must disappear; never remove the current/default builder.
        if resource.identity in self.names():
            raise ValueError("private builder remained after removal")
        if (
            self.docker.inspect("container", resource.proof["container_id"]) is not None
            or self.docker.inspect("volume", resource.proof["volume_name"]) is not None
        ):
            raise ValueError("private builder backing resources remained after removal")
