"""Exact-identity Docker cleanup; never invokes global prune or forced image removal."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from core.application.tooling.resource_cleanup.models import (
    DISPOSABLE_LABEL,
    OWNER_LABEL,
    TOKEN_LABEL,
    Ledger,
    Resource,
)

DOCKER_KINDS = {
    "container": "container",
    "network": "network",
    "volume": "volume",
    "image_tag": "image",
}
PROTECTED_VOLUMES = {"flow-steward-quality-cache", "flow-steward-mongodb-go-cache"}


class DockerResources:
    def command(self, *arguments: str) -> str:
        """Run only argv commands; suppress potentially secret-bearing native diagnostics."""
        try:
            result = subprocess.run(
                ["docker", *arguments], capture_output=True, text=True, timeout=60, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Docker unavailable or timed out; resource retained") from exc
        if result.returncode:
            raise ValueError("Docker command failed; resource retained")
        return result.stdout

    def inspect(self, kind: str, identity: str) -> dict[str, Any] | None:
        category = DOCKER_KINDS[kind]
        try:
            result = subprocess.run(
                ["docker", category, "inspect", identity],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Docker inspection unavailable") from exc
        if result.returncode:
            # Distinguish absence from daemon/permission failures without exposing stderr.
            if re.search(
                r"No such (?:object|container|network|volume|image)", result.stderr, re.IGNORECASE
            ) or re.search(r"network [a-f0-9]{64} not found", result.stderr, re.IGNORECASE):
                return None
            raise ValueError("Docker inspection failed; resource retained")
        parsed = json.loads(result.stdout)
        if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
            raise ValueError("invalid Docker inspection response")
        return parsed[0]

    def capture(
        self, kind: str, identity: str, ledger: Ledger, *, borrowed: bool = False
    ) -> dict[str, str]:
        if kind == "volume" and identity in PROTECTED_VOLUMES and not borrowed:
            raise ValueError("protected shared volume")
        data = self.inspect(kind, identity)
        if data is None:
            raise ValueError("cannot register missing Docker resource")
        if kind in {"container", "network"} and identity != data.get("Id"):
            raise ValueError("full immutable Docker ID required")
        if kind == "image_tag" and (
            identity.startswith("sha256:") or identity not in (data.get("RepoTags") or [])
        ):
            raise ValueError("an exact image tag is required, never an image ID")
        if (
            kind == "image_tag"
            and not borrowed
            and not (
                identity.startswith("fs-owned/" + ledger.token + ":")
                or identity == "fs-owned-" + ledger.token + "-fs-audit:latest"
            )
        ):
            raise ValueError("owned image tag must use the exclusive task namespace")
        labels = (
            (data.get("Config") or {}).get("Labels")
            if kind in {"container", "image_tag"}
            else data.get("Labels")
        )
        labels = labels or {}
        if not borrowed and any(
            labels.get(k) != v
            for k, v in {
                OWNER_LABEL: ledger.owner,
                TOKEN_LABEL: ledger.token,
                DISPOSABLE_LABEL: "true",
            }.items()
        ):
            raise ValueError("Docker task/token/disposable ownership labels do not match")
        native = data.get("Id") if kind != "volume" else data.get("CreatedAt")
        if not native:
            raise ValueError("missing Docker native identity")
        daemon = self.command("info", "--format", "{{.ID}}").strip()
        if not daemon:
            raise ValueError("Docker daemon identity unavailable")
        return {"native_id": native, "daemon_id": daemon}

    def assert_not_verification(self, identity: str, data: dict[str, Any]) -> None:
        if (data.get("State") or {}).get("Running"):
            labels = (data.get("Config") or {}).get("Labels") or {}
            command = " ".join((data.get("Config") or {}).get("Cmd") or [])
            if labels.get("com.docker.compose.service") == "fs-audit" or re.search(
                r"pytest|lint\.sh|test-runner", command
            ):
                raise ValueError("active verification container")
            processes = self.command("top", identity, "-eo", "args")
            if re.search(r"pytest|lint\.sh|test-runner", processes):
                raise ValueError("active verification process")

    def assert_no_active_dependents(self, identity: str, data: dict[str, Any]) -> None:
        networks = {
            n.get("NetworkID")
            for n in (data.get("NetworkSettings") or {}).get("Networks", {}).values()
        } - {None, ""}
        if not networks:
            return
        for peer_id in self.command("container", "ls", "-q", "--no-trunc").split():
            if peer_id == identity:
                continue
            peer = self.inspect("container", peer_id)
            if peer is None:
                continue
            peer_networks = {
                n.get("NetworkID")
                for n in (peer.get("NetworkSettings") or {}).get("Networks", {}).values()
            }
            if networks & peer_networks:
                self.assert_not_verification(peer_id, peer)

    def validate(self, ledger: Ledger, resource: Resource) -> bool:
        data = self.inspect(resource.kind, resource.identity)
        if data is None:
            return False
        if self.capture(resource.kind, resource.identity, ledger) != resource.proof:
            raise ValueError("Docker identity or ownership changed")
        if resource.kind == "container":
            self.assert_not_verification(resource.identity, data)
            self.assert_no_active_dependents(resource.identity, data)
        if resource.kind == "network" and data.get("Containers"):
            raise ValueError("network still has active consumers")
        if resource.kind in {"volume", "image_tag"}:
            containers = self.command("container", "ls", "-aq", "--no-trunc").split()
            for identity in containers:
                container = self.inspect("container", identity)
                if container is None:
                    continue
                if resource.kind == "volume" and any(
                    m.get("Name") == resource.identity for m in container.get("Mounts", [])
                ):
                    raise ValueError("volume still has a container consumer")
                if (
                    resource.kind == "image_tag"
                    and container.get("Image") == data["Id"]
                    and (container.get("State") or {}).get("Running")
                ):
                    raise ValueError("image still has a running consumer")
        return True

    def remove(self, ledger: Ledger, resource: Resource) -> None:
        if not self.validate(ledger, resource):
            return
        if resource.kind == "container":
            data = self.inspect("container", resource.identity)
            if data is None:
                return
            self.assert_not_verification(resource.identity, data)
            self.assert_no_active_dependents(resource.identity, data)
            if data and (data.get("State") or {}).get("Running"):
                self.command("container", "stop", "--time", "10", resource.identity)
            self.command("container", "rm", resource.identity)
        else:
            if self.capture(resource.kind, resource.identity, ledger) != resource.proof:
                raise ValueError("Docker identity changed immediately before removal")
            self.command(DOCKER_KINDS[resource.kind], "rm", resource.identity)
        if self.inspect(resource.kind, resource.identity) is not None:
            raise ValueError("Docker resource remained after removal")
