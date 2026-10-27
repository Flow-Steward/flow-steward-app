"""Exact Docker CLI inventory and removal adapter for candidate cleanup."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class DockerGatewayError(RuntimeError):
    """Docker CLI or response failure at the infrastructure boundary."""


@dataclass(frozen=True)
class DockerResource:
    """Infrastructure-owned Docker resource projection."""

    identity: str
    name: str
    labels: Mapping[str, str]


@dataclass(frozen=True)
class DockerInventory:
    """Infrastructure-owned point-in-time Docker inventory."""

    containers: tuple[DockerResource, ...] = ()
    networks: tuple[DockerResource, ...] = ()
    volumes: tuple[DockerResource, ...] = ()
    images: tuple[DockerResource, ...] = ()


class DockerCliGateway:
    """Inspect Docker resources and remove only caller-approved exact identities."""

    def inventory(self) -> DockerInventory:
        return DockerInventory(
            containers=self._container_inventory(),
            networks=self._network_inventory(),
            volumes=self._volume_inventory(),
            images=self._image_inventory(),
        )

    def remove_containers(self, identities: tuple[str, ...]) -> None:
        self._remove_each(("docker", "rm", "-f"), identities)

    def remove_networks(self, identities: tuple[str, ...]) -> None:
        self._remove_each(("docker", "network", "rm"), identities)

    def remove_volumes(self, identities: tuple[str, ...]) -> None:
        self._remove_each(("docker", "volume", "rm"), identities)

    def remove_images(self, identities: tuple[str, ...]) -> None:
        self._remove_each(("docker", "image", "rm"), identities)

    def _container_inventory(self) -> tuple[DockerResource, ...]:
        identities = self._list_identities("docker", "ps", "-aq", "--no-trunc")
        payloads = self._inspect(("docker", "inspect"), identities)
        return tuple(
            sorted(
                (
                    DockerResource(
                        identity=str(payload.get("Id") or ""),
                        name=str(payload.get("Name") or "").removeprefix("/"),
                        labels=_labels(payload.get("Config")),
                    )
                    for payload in payloads
                ),
                key=lambda resource: resource.identity,
            )
        )

    def _network_inventory(self) -> tuple[DockerResource, ...]:
        identities = self._list_identities("docker", "network", "ls", "-q", "--no-trunc")
        payloads = self._inspect(("docker", "network", "inspect"), identities)
        return tuple(
            sorted(
                (
                    DockerResource(
                        identity=str(payload.get("Id") or ""),
                        name=str(payload.get("Name") or ""),
                        labels=_direct_labels(payload.get("Labels")),
                    )
                    for payload in payloads
                ),
                key=lambda resource: resource.identity,
            )
        )

    def _volume_inventory(self) -> tuple[DockerResource, ...]:
        identities = self._list_identities("docker", "volume", "ls", "-q")
        payloads = self._inspect(("docker", "volume", "inspect"), identities)
        return tuple(
            sorted(
                (
                    DockerResource(
                        identity=str(payload.get("Name") or ""),
                        name=str(payload.get("Name") or ""),
                        labels=_direct_labels(payload.get("Labels")),
                    )
                    for payload in payloads
                ),
                key=lambda resource: resource.identity,
            )
        )

    def _image_inventory(self) -> tuple[DockerResource, ...]:
        identities = tuple(
            sorted(set(self._list_identities("docker", "image", "ls", "-q", "--no-trunc")))
        )
        payloads = self._inspect(("docker", "image", "inspect"), identities)
        resources: list[DockerResource] = []
        for payload in payloads:
            image_id = str(payload.get("Id") or "")
            labels = _labels(payload.get("Config"))
            tags = _string_list(payload.get("RepoTags"))
            for identity in tags or (image_id,):
                resources.append(DockerResource(identity=identity, name=identity, labels=labels))
        return tuple(sorted(resources, key=lambda resource: resource.identity))

    def _list_identities(self, *command: str) -> tuple[str, ...]:
        output = self._run(command).stdout
        return tuple(line.strip() for line in output.splitlines() if line.strip())

    def _inspect(
        self,
        command_prefix: tuple[str, ...],
        identities: Sequence[str],
    ) -> tuple[dict[str, object], ...]:
        if not identities:
            return ()
        payload = json.loads(self._run((*command_prefix, *identities)).stdout)
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise DockerGatewayError("Docker inspect returned an invalid inventory")
        return tuple(payload)

    def _remove_each(self, command_prefix: tuple[str, ...], identities: Sequence[str]) -> None:
        for identity in identities:
            if not identity or identity.startswith("-"):
                raise DockerGatewayError("Docker cleanup identity is invalid")
            self._run((*command_prefix, identity))

    def _run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerGatewayError(f"Docker command could not run: {command[1]}") from exc
        if result.returncode != 0:
            reason = result.stderr.strip() or result.stdout.strip() or "unknown Docker error"
            raise DockerGatewayError(f"Docker command failed: {command[1]}: {reason}")
        return result


def _labels(config: object) -> dict[str, str]:
    if not isinstance(config, dict):
        return {}
    return _direct_labels(config.get("Labels"))


def _direct_labels(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(label_value) for key, label_value in value.items() if label_value is not None
    }


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))
