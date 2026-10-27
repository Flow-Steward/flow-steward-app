"""Prepare infrastructure images before Compose starts with pulling disabled."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from core.infrastructure.config.env_registry import registry_default_value
from core.infrastructure.installer.process import ProcessOutcome


class DependencyImageConfig(Protocol):
    @property
    def ingress_mode(self) -> str: ...

    @property
    def file_storage(self) -> str: ...


def core_service_names(config: DependencyImageConfig) -> list[str]:
    services = ["rabbitmq-fs", "pg-fs"]
    if config.ingress_mode == "bundled_traefik":
        services.insert(0, "traefik")
    if config.file_storage == "bundled":
        services.append("seaweedfs")
    return services


def _resolved_images(
    *,
    config: DependencyImageConfig,
    compose_prefix: Sequence[str],
    repo_root: Path,
    run: Callable[..., ProcessOutcome],
    failure: Callable[[str], Exception],
) -> dict[str, str]:
    # Compose JSON includes interpolated secrets. Read it privately, never through
    # the installer's command transcript or an output sink.
    try:
        outcome = run(
            [*compose_prefix, "config", "--format", "json"],
            cwd=repo_root,
            separate_stderr=True,
        )
        if outcome.returncode:
            raise failure("Docker Compose could not resolve infrastructure images.")
        services = json.loads(outcome.stdout)["services"]
        if not isinstance(services, dict):
            raise ValueError("invalid services")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise failure(
            "Docker Compose returned no usable infrastructure image configuration."
        ) from exc

    required = core_service_names(config)
    if config.file_storage == "bundled":
        required.append("seaweedfs-config")
    resolved: dict[str, str] = {}
    for name in required:
        service = services.get(name)
        image = service.get("image") if isinstance(service, dict) else None
        if not isinstance(image, str) or not image.strip():
            raise failure(f"Docker Compose did not resolve an image for {name}.")
        resolved[name] = image
    return resolved


def core_dependency_images_present(
    *,
    config: DependencyImageConfig,
    compose_prefix: Sequence[str],
    repo_root: Path,
    run: Callable[..., ProcessOutcome],
    failure: Callable[[str], Exception],
) -> bool:
    images = _resolved_images(
        config=config, compose_prefix=compose_prefix, repo_root=repo_root, run=run, failure=failure
    )
    return all(
        run(["docker", "image", "inspect", image], cwd=repo_root).returncode == 0
        for image in set(images.values())
    )


def prepare_core_dependency_images(
    *,
    config: DependencyImageConfig,
    compose_prefix: Sequence[str],
    repo_root: Path,
    environment: Mapping[str, str],
    run: Callable[..., ProcessOutcome],
    checked: Callable[[Sequence[str]], ProcessOutcome],
    failure: Callable[[str], Exception],
) -> None:
    images = _resolved_images(
        config=config, compose_prefix=compose_prefix, repo_root=repo_root, run=run, failure=failure
    )
    prepared: set[str] = set()
    for name, image in images.items():
        if image in prepared:
            continue
        try:
            present = run(["docker", "image", "inspect", image], cwd=repo_root)
        except OSError as exc:
            raise failure(f"Docker could not inspect the image for {name}.") from exc
        if present.returncode:
            if image.startswith("sha256:"):
                raise failure(
                    f"The pinned local image for {name} is missing ({image}); "
                    "restore that exact image before resuming."
                )
            if name == "pg-fs" and image == registry_default_value("POSTGRES_IMAGE"):
                command = [
                    "docker",
                    "buildx",
                    "build",
                    "--load",
                    "--tag",
                    image,
                    "--file",
                    str(repo_root / "infra/docker/postgres-extended/Dockerfile"),
                ]
                if environment.get("AGE_REF"):
                    command.extend(("--build-arg", f"AGE_REF={environment['AGE_REF']}"))
                checked([*command, str(repo_root)])
            else:
                checked(["docker", "pull", image])
        prepared.add(image)
