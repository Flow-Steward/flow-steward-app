from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from core.application.lifecycle.contracts import (
    ComposeTarget,
)
from core.application.lifecycle.stack_service import (
    StackContainerRollbackState,
    StackContainerState,
    StackGatewayError,
    StackImageTransition,
    StackRollbackSnapshot,
)
from core.infrastructure.lifecycle import docker_runtime_support
from core.infrastructure.lifecycle.docker_runtime_support import (
    restore_scale_args,
)

from .docker_gateway_contracts import (
    _DOCKER_TIMEOUT_SECONDS,
    _FORMAT_IMAGE_ID,
    _IMAGE_ID,
    _MESSAGE_JSON,
    _PATH_FLOW_STEWARD,
    _SAFE_OPERATION_ID,
    ComposeCommandRunner,
    DockerCommandRunner,
    DockerRollbackCapture,
    DockerRollbackDependencies,
    DockerStatusReader,
)


def _restore_mismatches(
    snapshot: StackRollbackSnapshot,
    observed: StackRollbackSnapshot,
) -> tuple[str, ...]:
    """Describe every managed service that left its recorded topology or image."""
    selected = set(snapshot.selected_services)
    observed_by_service = {item.service: item for item in observed.services}
    mismatches: list[str] = []
    for before in snapshot.services:
        after = observed_by_service.get(before.service)
        if after is None:
            mismatches.append(
                f"{before.service}: expected topology {before.topology}, observed no container"
            )
        elif after.topology != before.topology:
            mismatches.append(
                f"{before.service}: expected topology {before.topology}, observed {after.topology}"
            )
        elif before.topology != "absent" and after.replicas != before.replicas:
            mismatches.append(
                f"{before.service}: expected {before.replicas} containers, "
                f"observed {after.replicas}"
            )
        elif (
            before.service in selected
            and before.topology != "absent"
            and after.image_id != before.image_id
        ):
            mismatches.append(
                f"{before.service}: expected image {before.image_id}, observed {after.image_id}"
            )
    return tuple(mismatches)


def capture_rollback(
    target: ComposeTarget,
    *,
    operation_id: str,
    selected_services: tuple[str, ...],
    managed_services: tuple[str, ...],
    run: DockerCommandRunner,
    status: DockerStatusReader,
) -> StackRollbackSnapshot:
    if not _SAFE_OPERATION_ID.fullmatch(operation_id):
        raise StackGatewayError(
            action="rollback_capture",
            detail="Lifecycle operation id contains unsafe characters.",
        )
    docker_runtime_support.validate_services((*selected_services, *managed_services))
    state = status(target)
    # Review P2: a service may run as several replicas (Scalable workers). This used to
    # refuse any second container, so no lifecycle operation could start once an
    # operator had scaled the workers. Replicas are recorded as one entry with their
    # count; a restore recreates them from one definition, so they must agree.
    by_service: dict[str, list[StackContainerState]] = {}
    for container in state.services:
        by_service.setdefault(container.service, []).append(container)
    container_ids = tuple(
        container.container_id
        for service in managed_services
        for container in by_service.get(service, ())
        if container.container_id.strip()
    )
    images: dict[str, set[str]] = {}
    if container_ids:
        inspected = run(
            ("docker", "inspect", "--format", _MESSAGE_JSON, *container_ids),
            action="rollback_capture",
            timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
        )
        for item in docker_runtime_support.json_objects(inspected.stdout):
            service = docker_runtime_support.inspection_service(item)
            image_id = str(item.get("Image") or "").strip().lower()
            if not service or not _IMAGE_ID.fullmatch(image_id):
                raise StackGatewayError(
                    action="rollback_capture",
                    detail="Docker inspection did not prove an exact prior image identity.",
                )
            images.setdefault(service, set()).add(image_id)
    services: list[StackContainerRollbackState] = []
    for service in managed_services:
        containers = by_service.get(service, [])
        if not containers:
            services.append(StackContainerRollbackState(service=service, topology="absent"))
            continue
        service_images = images.get(service, set())
        if not service_images:
            raise StackGatewayError(
                action="rollback_capture",
                detail="Rollback capture could not map a managed service to its image.",
            )
        if len(service_images) > 1:
            raise StackGatewayError(
                action="rollback_capture",
                detail=(
                    f"The {len(containers)} {service} containers run more than one image; "
                    "a rollback cannot restore a mixture. Recreate them on one image first."
                ),
            )
        topologies: set[Literal["running", "stopped"]] = {
            "running" if container.state.lower() == "running" else "stopped"
            for container in containers
        }
        if len(topologies) > 1:
            raise StackGatewayError(
                action="rollback_capture",
                detail=(
                    f"The {len(containers)} {service} containers are not all in one "
                    "topology (some running, some stopped); a rollback cannot restore "
                    "that. Start or stop them together first."
                ),
            )
        services.append(
            StackContainerRollbackState(
                service=service,
                topology=topologies.pop(),
                container_id=containers[0].container_id,
                image_id=service_images.pop(),
                replicas=len(containers),
            )
        )
    return StackRollbackSnapshot(
        operation_id=operation_id,
        project_name=target.project_name,
        selected_services=selected_services,
        services=tuple(services),
    )


def runtime_image_drift(
    target: ComposeTarget,
    snapshot: StackRollbackSnapshot,
    *,
    run: DockerCommandRunner,
    capture_rollback: DockerRollbackCapture,
) -> tuple[StackImageTransition, ...]:
    """Name every retained container that left its recorded image identity.

    A profile switch intentionally removes source-only runtime services from
    the target Compose model.  Their immutable image ids remain in the
    rollback snapshot, but they cannot be observed through the target model
    and must not make a valid topology transition fail preservation.
    """
    if snapshot.project_name != target.project_name:
        raise StackGatewayError(
            action="image_drift",
            detail="The saved rollback evidence belongs to a different Compose project.",
        )
    managed_services = tuple(item.service for item in snapshot.services)
    observed = capture_rollback(
        target,
        operation_id=snapshot.operation_id,
        selected_services=snapshot.selected_services,
        managed_services=managed_services,
    )
    after_by_service = {item.service: item for item in observed.services}
    target_services = set(target.required_services)
    drifted: list[StackImageTransition] = []
    for before in snapshot.services:
        if before.service not in target_services or before.topology == "absent":
            continue
        after = after_by_service.get(before.service)
        observed_image = "" if after is None or after.topology == "absent" else after.image_id
        if observed_image == before.image_id:
            continue
        drifted.append(
            StackImageTransition(
                service=before.service,
                expected_image_id=before.image_id,
                observed_image_id=observed_image,
                expected_image_available=_image_present(before.image_id, run=run),
            )
        )
    return tuple(drifted)


def configured_image_identities(
    target: ComposeTarget, *, run: DockerCommandRunner, run_compose: ComposeCommandRunner
) -> Mapping[str, str]:
    """Resolve the exact image id the selected Compose model declares per service."""
    outcome = run_compose(
        target,
        ("config", "--format", "json"),
        action="configured_images",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
    )
    try:
        model = json.loads(outcome.stdout or "{}")
    except ValueError as exc:
        raise StackGatewayError(
            action="configured_images",
            detail="The selected Compose model could not be read.",
        ) from exc
    services = model.get("services") if isinstance(model, dict) else None
    if not isinstance(services, dict):
        raise StackGatewayError(
            action="configured_images",
            detail="The selected Compose model declares no services.",
        )
    references = {
        str(name): reference
        for name, spec in services.items()
        if isinstance(spec, dict) and (reference := str(spec.get("image") or "").strip())
    }
    if not references:
        return {}
    unique = tuple(dict.fromkeys(references.values()))
    inspected = run(
        ("docker", "image", "inspect", "--format", _FORMAT_IMAGE_ID, *unique),
        action="configured_image_identity",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
    )
    identities = tuple(
        line.strip().lower() for line in inspected.stdout.splitlines() if line.strip()
    )
    if len(identities) != len(unique) or any(
        not _IMAGE_ID.fullmatch(identity) for identity in identities
    ):
        raise StackGatewayError(
            action="configured_image_identity",
            detail="Docker did not resolve every configured image to an exact identity.",
        )
    by_reference = dict(zip(unique, identities, strict=True))
    return {service: by_reference[reference] for service, reference in references.items()}


def _image_present(image_id: str, *, run: DockerCommandRunner) -> bool:
    if not _IMAGE_ID.fullmatch(image_id):
        return False
    try:
        outcome = run(
            ("docker", "image", "inspect", "--format", _FORMAT_IMAGE_ID, image_id),
            action="image_presence",
            timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
        )
    except StackGatewayError:
        return False
    return any(line.strip().lower() == image_id for line in outcome.stdout.splitlines())


def restore(
    target: ComposeTarget,
    *,
    action: str,
    snapshot: StackRollbackSnapshot,
    timeout_seconds: int,
    output_sink: Callable[[str], None],
    heartbeat: Callable[[str], None],
    environment: Mapping[str, str] | None = None,
    dependencies: DockerRollbackDependencies,
) -> bool:
    project_root = dependencies.project_root
    run = dependencies.run
    run_compose = dependencies.run_compose
    stop_project_services = dependencies.stop_project_services
    remove_project_services = dependencies.remove_project_services
    capture_rollback = dependencies.capture_rollback
    runtime_image_drift = dependencies.runtime_image_drift
    stop = dependencies.stop

    if snapshot.project_name != target.project_name:
        output_sink(
            "restore mismatch: the saved rollback evidence names Compose project "
            f"{snapshot.project_name}, the target is {target.project_name}"
        )
        return False
    by_service = {item.service: item for item in snapshot.services}
    managed_services = tuple(by_service)
    selected = tuple(snapshot.selected_services)
    docker_runtime_support.validate_services((*managed_services, *selected))
    previously_runnable, should_stop, should_remove = docker_runtime_support.rollback_topologies(
        by_service
    )
    rollback_tags: list[str] = []
    restored_stopped: set[str] = set()
    try:
        # AC17-R175: the target's own services go first -- behind a host proxy both
        # profiles publish one port, so the previous ones could not start.
        if should_remove:
            remove_project_services(
                target.project_name,
                should_remove,
                timeout_seconds=timeout_seconds,
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
        needs_start = previously_runnable
        if action in {"rebuild", "start"}:
            exact_images = {
                service: by_service[service].image_id
                for service in selected
                if service in by_service and by_service[service].topology != "absent"
            }
            if action == "start":
                # A start never rebuilds an image, so only a service whose running
                # image actually left the recorded identity needs exact recreation.
                drifted = {item.service for item in runtime_image_drift(target, snapshot)}
                exact_images = {
                    service: image_id
                    for service, image_id in exact_images.items()
                    if service in drifted
                }
            unproven = sorted(
                service
                for service, image_id in exact_images.items()
                if not _IMAGE_ID.fullmatch(image_id)
            )
            if unproven:
                output_sink(
                    "restore mismatch: no exact prior image identity was recorded for "
                    + ", ".join(unproven)
                )
                return False
            if exact_images:
                pinned_images = {
                    service: _rollback_image_ref(snapshot, service, image_id)
                    for service, image_id in exact_images.items()
                }
                for service, image_id in exact_images.items():
                    run(
                        ("docker", "image", "tag", image_id, pinned_images[service]),
                        action="restore_pin_image",
                        timeout_seconds=timeout_seconds,
                    )
                    rollback_tags.append(pinned_images[service])
                override = docker_runtime_support.write_rollback_override(
                    project_root,
                    operation_id=snapshot.operation_id,
                    images=pinned_images,
                )
                exact_running = tuple(
                    service for service in exact_images if by_service[service].topology == "running"
                )
                exact_stopped = tuple(
                    service for service in exact_images if by_service[service].topology == "stopped"
                )
                if exact_running:
                    run_compose(
                        target,
                        (
                            "up",
                            "--wait",
                            "--wait-timeout",
                            str(timeout_seconds),
                            "--force-recreate",
                            "--no-build",
                            "--no-deps",
                            *restore_scale_args(by_service, exact_running),
                            *exact_running,
                        ),
                        action="restore_exact_running_images",
                        timeout_seconds=timeout_seconds,
                        output_sink=output_sink,
                        heartbeat=heartbeat,
                        additional_compose_files=(override,),
                        environment=environment,
                    )
                if exact_stopped:
                    run_compose(
                        target,
                        (
                            "up",
                            "--no-start",
                            "--force-recreate",
                            "--no-build",
                            "--no-deps",
                            *restore_scale_args(by_service, exact_stopped),
                            *exact_stopped,
                        ),
                        action="restore_exact_stopped_images",
                        timeout_seconds=timeout_seconds,
                        output_sink=output_sink,
                        heartbeat=heartbeat,
                        additional_compose_files=(override,),
                        environment=environment,
                    )
                    restored_stopped.update(exact_stopped)
            needs_start = tuple(
                service for service in previously_runnable if service not in exact_images
            )
        if needs_start:
            run_compose(
                target,
                (
                    "up",
                    "--wait",
                    "--wait-timeout",
                    str(timeout_seconds),
                    "--no-recreate",
                    "--no-deps",
                    *restore_scale_args(by_service, needs_start),
                    *needs_start,
                ),
                action="restore_topology",
                timeout_seconds=timeout_seconds,
                output_sink=output_sink,
                heartbeat=heartbeat,
                environment=environment,
            )
        configured_services = set(target.required_services)
        compose_stop = tuple(
            service
            for service in should_stop
            if service in configured_services and service not in restored_stopped
        )
        direct_stop = tuple(
            service
            for service in should_stop
            if service not in configured_services and service not in restored_stopped
        )
        if compose_stop:
            stop(
                target,
                compose_stop,
                timeout_seconds=timeout_seconds,
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
        if direct_stop:
            stop_project_services(
                target.project_name,
                direct_stop,
                timeout_seconds=timeout_seconds,
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
        observed = capture_rollback(
            target,
            operation_id=snapshot.operation_id,
            selected_services=snapshot.selected_services,
            managed_services=managed_services,
        )
    except StackGatewayError as exc:
        output_sink(f"restore failed: {exc.detail}")
        return False
    # The temporary tags and the rollback override stay in place until the caller
    # has finished every post-restore check. Releasing them here would leave an
    # override on disk naming tags that no longer resolve, and the next Compose
    # invocation that reads it would fail with a pull error instead of a real cause.
    mismatches = _restore_mismatches(snapshot, observed)
    for mismatch in mismatches:
        output_sink(f"restore mismatch: {mismatch}")
    return not mismatches


def restore_mismatches(
    target: ComposeTarget,
    snapshot: StackRollbackSnapshot,
    *,
    capture_rollback: DockerRollbackCapture,
) -> tuple[str, ...]:
    """Name every service that does not match the recorded runtime, for the operator."""
    if snapshot.project_name != target.project_name:
        return (
            "the saved rollback evidence names Compose project "
            f"{snapshot.project_name}, the target is {target.project_name}",
        )
    observed = capture_rollback(
        target,
        operation_id=snapshot.operation_id,
        selected_services=snapshot.selected_services,
        managed_services=tuple(item.service for item in snapshot.services),
    )
    return _restore_mismatches(snapshot, observed)


def release_rollback_pins(
    snapshot: StackRollbackSnapshot, *, project_root: Path, run: DockerCommandRunner
) -> tuple[str, ...]:
    """Drop the temporary rollback tags and override after verification completes.

    Idempotent: a pin that was never created, or was already released, is skipped.
    Returns the references that could not be released, for the operation log.
    """
    retained: list[str] = []
    for item in snapshot.services:
        if item.topology == "absent" or not _IMAGE_ID.fullmatch(item.image_id):
            continue
        reference = _rollback_image_ref(snapshot, item.service, item.image_id)
        try:
            run(
                ("docker", "image", "inspect", "--format", _FORMAT_IMAGE_ID, reference),
                action="rollback_pin_probe",
                timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
            )
        except StackGatewayError:
            continue
        try:
            run(
                ("docker", "image", "rm", reference),
                action="restore_unpin_image",
                timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
            )
        except StackGatewayError:
            retained.append(reference)
    override = (
        project_root
        / _PATH_FLOW_STEWARD
        / "operations"
        / f"{snapshot.operation_id}.rollback.compose.json"
    )
    if not retained:
        try:
            override.unlink(missing_ok=True)
        except OSError:
            retained.append(str(override))
    elif override.is_file():
        # A retained tag still resolves, so the override it names stays valid.
        retained.append(str(override))
    return tuple(retained)


def _rollback_image_ref(snapshot: StackRollbackSnapshot, service: str, image_id: str) -> str:
    identity = hashlib.sha256(
        f"{snapshot.project_name}\0{snapshot.operation_id}\0{service}\0{image_id}".encode()
    ).hexdigest()
    return f"flow-steward-rollback:{identity}"
