"""Docker inventory of one Compose project's containers and volumes.

Finds a project's service containers by Compose labels, stops or removes them
without rendering the project's Compose file, and lists the project's volumes.
"""

from __future__ import annotations

from collections.abc import Callable

from core.application.lifecycle.stack_service import (
    StackGatewayError,
    StackVolumeState,
)
from core.infrastructure.lifecycle import docker_runtime_support

from .docker_gateway_contracts import (
    _CONTAINER_ID,
    _DOCKER_TIMEOUT_SECONDS,
    _MESSAGE_JSON,
    DockerCommandRunner,
)


def remove_project_services(
    project_name: str,
    services: tuple[str, ...],
    *,
    run: DockerCommandRunner,
    timeout_seconds: int,
    output_sink: Callable[[str], None],
    heartbeat: Callable[[str], None],
) -> None:
    """Remove rollback-absent services even when the source Compose omits them."""
    docker_runtime_support.validate_project_name(project_name)
    docker_runtime_support.validate_services(services)
    container_ids = project_service_container_ids(
        project_name,
        services,
        run=run,
        timeout_seconds=timeout_seconds,
        action="restore_absent_inventory",
    )
    if container_ids:
        run(
            ("docker", "rm", "--force", *container_ids),
            action="restore_absent",
            timeout_seconds=timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
        )


def stop_project_services(
    project_name: str,
    services: tuple[str, ...],
    *,
    run: DockerCommandRunner,
    timeout_seconds: int,
    output_sink: Callable[[str], None],
    heartbeat: Callable[[str], None],
) -> None:
    """Stop retained cross-profile containers absent from the source Compose."""
    container_ids = project_service_container_ids(
        project_name,
        services,
        run=run,
        timeout_seconds=timeout_seconds,
        action="restore_stopped_inventory",
    )
    if container_ids:
        run(
            ("docker", "stop", *container_ids),
            action="restore_stopped",
            timeout_seconds=timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
        )


def project_service_container_ids(
    project_name: str,
    services: tuple[str, ...],
    *,
    run: DockerCommandRunner,
    timeout_seconds: float,
    action: str,
) -> list[str]:
    docker_runtime_support.validate_project_name(project_name)
    docker_runtime_support.validate_services(services)
    container_ids: list[str] = []
    for service in services:
        outcome = run(
            (
                "docker",
                "ps",
                "--all",
                "--filter",
                f"label=com.docker.compose.project={project_name}",
                "--filter",
                f"label=com.docker.compose.service={service}",
                "--format",
                "{{.ID}}",
            ),
            action=action,
            timeout_seconds=timeout_seconds,
        )
        for line in str(outcome.stdout or "").splitlines():
            container_id = line.strip().lower()
            if not _CONTAINER_ID.fullmatch(container_id):
                raise StackGatewayError(
                    action=action,
                    detail="Docker returned an invalid rollback container identity.",
                )
            container_ids.append(container_id)
    return container_ids


def project_volumes(project_name: str, *, run: DockerCommandRunner) -> tuple[StackVolumeState, ...]:
    docker_runtime_support.validate_project_name(project_name)
    listing = run(
        (
            "docker",
            "volume",
            "ls",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
            "--format",
            "{{.Name}}",
        ),
        action="volume_status",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
    )
    names = tuple(sorted(line.strip() for line in str(listing.stdout).splitlines() if line.strip()))
    if not names:
        return ()
    inspected = run(
        (
            "docker",
            "volume",
            "inspect",
            "--format",
            _MESSAGE_JSON,
            *names,
        ),
        action="volume_status",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
    )
    volumes = tuple(
        sorted(
            (
                docker_runtime_support.stack_volume_state(item)
                for item in docker_runtime_support.json_objects(inspected.stdout)
            ),
            key=lambda item: item.name,
        )
    )
    if tuple(volume.name for volume in volumes) != names:
        raise StackGatewayError(
            action="volume_status",
            detail="Docker volume inventory did not match the selected project.",
        )
    return volumes
