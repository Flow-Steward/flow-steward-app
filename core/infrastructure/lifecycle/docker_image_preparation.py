"""Prepare installed production images without a source-build fallback."""

from __future__ import annotations

import re
from collections.abc import Mapping

from core.application.lifecycle.stack_service import StackGatewayError
from core.application.release.images import all_release_roles

from .docker_gateway_contracts import (
    _DOCKER_TIMEOUT_SECONDS,
    _FORMAT_IMAGE_ID,
    _IMAGE_ID,
    DockerCommandRunner,
)


def prepare_production_images(
    *,
    images: tuple[str, ...],
    services: tuple[str, ...],
    environment: Mapping[str, str],
    run: DockerCommandRunner,
    timeout_seconds: int,
) -> None:
    """Validate published roles, reuse exact images, or pull declared references."""
    role_images = {role.role: role.environment_variable for role in all_release_roles()}
    service_roles = {
        "pg-fs": "postgres",
        "memory-postgres-extensions": "postgres",
        "fs-extension-installer": "fs-app",
    }
    selected_role_images = tuple(
        str(environment.get(role_images[service_roles.get(service, service)]) or "")
        for service in services
        if service_roles.get(service, service) in role_images
    )
    if not images or any(
        image not in images
        or not (_IMAGE_ID.fullmatch(image) or re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image))
        for image in selected_role_images
    ):
        raise StackGatewayError(
            action="target_image_inventory",
            detail=(
                "Production requires a complete immutable image set; repair the installation "
                "before applying configuration. Local source builds are disabled."
            ),
        )
    inspect_command = ("docker", "image", "inspect", "--format", _FORMAT_IMAGE_ID, *images)
    try:
        inspected = run(
            inspect_command, action="target_image_reuse", timeout_seconds=_DOCKER_TIMEOUT_SECONDS
        )
    except StackGatewayError:
        pass
    else:
        if _complete_image_ids(inspected.stdout, len(images)):
            return
    # A local-only ID cannot be downloaded. An incomplete set containing one
    # requires installer repair instead of guessing replacement identities.
    if any(_IMAGE_ID.fullmatch(image) for image in images):
        raise StackGatewayError(
            action="target_image_reuse",
            detail=(
                "An exact production image is unavailable locally. Repair the installation; "
                "local source builds are disabled."
            ),
        )
    for image in images:
        run(("docker", "pull", image), action="target_image_pull", timeout_seconds=timeout_seconds)
    inspected = run(
        inspect_command, action="target_image_verify", timeout_seconds=_DOCKER_TIMEOUT_SECONDS
    )
    if not _complete_image_ids(inspected.stdout, len(images)):
        raise StackGatewayError(
            action="target_image_verify",
            detail=(
                "The exact production image set could not be verified after pulling. "
                "Local source builds are disabled."
            ),
        )


def _complete_image_ids(output: str, expected_count: int) -> bool:
    image_ids = tuple(line.strip() for line in output.splitlines() if line.strip())
    return len(image_ids) == expected_count and all(
        _IMAGE_ID.fullmatch(value) for value in image_ids
    )
