"""Docker resource inventory gateway for lifecycle preservation."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.application.installer.redaction import redact_installer_text
from core.application.lifecycle.contracts import (
    ComposeTarget,
)
from core.application.lifecycle.stack_service import (
    StackGatewayError,
    StackImageTransition,
    StackRollbackSnapshot,
    StackRuntimeState,
    StackVolumeState,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.installer.process import (
    DockerHostStateExecutor,
    SubprocessExecutor,
    docker_host_state_dir,
)
from core.infrastructure.lifecycle import docker_runtime_support
from core.infrastructure.lifecycle.docker_runtime_support import (
    _IMAGE_PREPARATION_TIMEOUT_SECONDS,
    _RETAINED_MANAGEMENT_SERVICES,
    _TOKEN_INSTALLER_COMPOSE_OVERRIDE_YAML,
)

from .docker_gateway_contracts import (
    _CONTAINER_ID,
    _DOCKER_TIMEOUT_SECONDS,
    _FORMAT_IMAGE_ID,
    _IMAGE_ID,
    _MESSAGE_JSON,
    DockerRollbackDependencies,
)
from .docker_preservation_gateway import DockerPreservationGateway as DockerPreservationGateway
from .docker_preservation_gateway import DockerPreservationSnapshot as DockerPreservationSnapshot
from .docker_preservation_gateway import ProcessRunner as ProcessRunner
from .docker_profile_runtime_state import stage_profile_runtime_state
from .docker_rollback_operations import (
    capture_rollback,
    configured_image_identities,
    release_rollback_pins,
    restore,
    restore_mismatches,
    runtime_image_drift,
)
from .docker_runtime_probes import (
    bundled_storage_inventory,
    compact_app_readiness,
    compact_candidate_preflight,
    compact_maintenance_operation,
    management_storage_connectivity,
    prepare_bundled_storage_probe,
    probe_bundled_storage,
    reconcile_security_posture,
    reconcile_workflow_knowledge,
    require_storage_connectivity,
    service_release,
)


class DockerLifecycleGateway:
    """Construct and execute selected installed Compose lifecycle operations."""

    def __init__(
        self,
        *,
        project_root: Path,
        docker_host_project_root: Path | None = None,
        process: Any | None = None,
        port_available: Callable[[str, int, str], bool] | None = None,
    ) -> None:
        self._project_root = Path(project_root).resolve()
        self._docker_host_project_root = Path(docker_host_project_root or project_root).resolve()
        self._process = DockerHostStateExecutor(
            process or SubprocessExecutor(), self._docker_host_state_dir
        )
        self._port_available = port_available or docker_runtime_support.port_is_available

    def _docker_host_state_dir(self) -> str | None:
        state_root = str(_runtime_env.get("FS_INSTALLER_STATE_DIR") or "").strip()
        if not state_root:
            return None
        return docker_host_state_dir(Path(state_root), self._docker_host_project_root / ".env.prod")

    def services(self, target: ComposeTarget) -> tuple[str, ...]:
        outcome = self._run_compose(
            target,
            ("config", "--services"),
            action="config",
            timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
        )
        return tuple(
            dict.fromkeys(line.strip() for line in outcome.stdout.splitlines() if line.strip())
        )

    def validate_target(
        self,
        target: ComposeTarget,
        *,
        environment: Mapping[str, str],
        override_content: str | None = None,
    ) -> None:
        """Render the exact selected Compose target without mutation."""
        self._run(
            self._candidate_compose_command(
                target,
                ("config", "--quiet"),
                override_content=override_content,
            ),
            action="configuration",
            timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
            environment=environment,
            input_text=override_content,
        )

    def prepare_target_images(
        self,
        target: ComposeTarget,
        *,
        environment: Mapping[str, str],
        override_content: str | None = None,
        timeout_seconds: int = _IMAGE_PREPARATION_TIMEOUT_SECONDS,
    ) -> None:
        """Pull and build the exact target while leaving the running stack untouched."""
        services = tuple(
            service
            for service in target.required_services
            if service not in _RETAINED_MANAGEMENT_SERVICES
        )
        docker_runtime_support.validate_services(services)
        with docker_runtime_support.temporary_candidate_environment(
            self._project_root, env_file=target.env_file, environment=environment
        ):
            inventory = self._run(
                self._candidate_compose_command(
                    target,
                    ("config", "--images"),
                    override_content=override_content,
                ),
                action="target_image_inventory",
                timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
                environment=environment,
                input_text=override_content,
            )
            images = tuple(
                dict.fromkeys(
                    line.strip() for line in inventory.stdout.splitlines() if line.strip()
                )
            )
            if images:
                try:
                    inspected = self._run(
                        (
                            "docker",
                            "image",
                            "inspect",
                            "--format",
                            _FORMAT_IMAGE_ID,
                            *images,
                        ),
                        action="target_image_reuse",
                        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
                    )
                except StackGatewayError:
                    pass
                else:
                    image_ids = tuple(
                        line.strip() for line in inspected.stdout.splitlines() if line.strip()
                    )
                    if len(image_ids) == len(images) and all(
                        _IMAGE_ID.fullmatch(image_id) for image_id in image_ids
                    ):
                        return
            for action, arguments in (
                (
                    "target_image_pull",
                    ("pull", "--ignore-buildable", "--policy", "missing", *services),
                ),
                ("target_image_build", ("build", *services)),
            ):
                self._run(
                    self._candidate_compose_command(
                        target,
                        arguments,
                        override_content=override_content,
                    ),
                    action=action,
                    timeout_seconds=timeout_seconds,
                    environment=environment,
                    input_text=override_content,
                )

    def apply_configuration(
        self,
        target: ComposeTarget,
        *,
        managed_services: tuple[str, ...] = (),
        stop_before_reconcile: tuple[str, ...] = (),
        reconcile_services: tuple[str, ...] | None = None,
        no_dependencies: bool = False,
        remove_orphans: bool = True,
        timeout_seconds: int,
    ) -> None:
        """Reconcile only the selected installed services without volume operations."""
        configured = self.services(target)
        desired_runtime = tuple(
            service
            for service in target.runtime_services
            if service not in _RETAINED_MANAGEMENT_SERVICES
        )
        runtime = desired_runtime if reconcile_services is None else reconcile_services
        configured_set = set(configured)
        if not set(runtime).issubset(set(desired_runtime)):
            raise ValueError("reconcile services must be selected target runtime services")
        managed = tuple(dict.fromkeys((*configured, *managed_services)))
        docker_runtime_support.validate_services((*managed, *stop_before_reconcile))
        if stop_before_reconcile:
            self._run_compose(
                target,
                ("stop", *stop_before_reconcile),
                action="configuration_stop_port_holders",
                timeout_seconds=timeout_seconds,
            )
        excluded = tuple(
            service
            for service in managed
            if service in configured_set
            if service not in set(desired_runtime)
            and service not in set(stop_before_reconcile)
            and service not in _RETAINED_MANAGEMENT_SERVICES
        )
        if excluded:
            self._run_compose(
                target,
                ("stop", *excluded),
                action="configuration_stop_excluded",
                timeout_seconds=timeout_seconds,
            )
        arguments = [
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            str(timeout_seconds),
        ]
        if remove_orphans:
            arguments.append("--remove-orphans")
        if no_dependencies:
            arguments.append("--no-deps")
        arguments.extend(runtime)
        self._run_compose(
            target,
            tuple(arguments),
            action="configuration_apply",
            timeout_seconds=timeout_seconds,
        )
        self._restore_canonical_container_names(target, timeout_seconds=timeout_seconds)

    def stage_profile_runtime_state(
        self,
        previous_target: ComposeTarget,
        target: ComposeTarget,
        *,
        source_environment: Mapping[str, str] | None = None,
    ) -> None:
        """Copy host lifecycle state into Compact's sole persistent volume."""
        return stage_profile_runtime_state(
            previous_target,
            target,
            source_environment=source_environment,
            project_root=self._project_root,
            run_compose=self._run_compose,
        )

    def status(self, target: ComposeTarget) -> StackRuntimeState:
        outcome = self._run_compose(
            target,
            ("ps", "--all", "--format", "json"),
            action="status",
            timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
        )
        services = tuple(
            sorted(
                (
                    docker_runtime_support.container_state(item)
                    for item in docker_runtime_support.json_objects(outcome.stdout)
                ),
                key=lambda item: (item.service, item.container_name),
            )
        )
        return StackRuntimeState(
            project_name=target.project_name,
            services=services,
            volumes=self._volumes(target.project_name),
        )

    def capture_rollback(
        self,
        target: ComposeTarget,
        *,
        operation_id: str,
        selected_services: tuple[str, ...],
        managed_services: tuple[str, ...],
    ) -> StackRollbackSnapshot:
        return capture_rollback(
            target,
            operation_id=operation_id,
            selected_services=selected_services,
            managed_services=managed_services,
            run=self._run,
            status=self.status,
        )

    def runtime_images_match(
        self,
        target: ComposeTarget,
        snapshot: StackRollbackSnapshot,
    ) -> bool:
        """Prove that retained pre-existing containers kept their exact images."""
        if snapshot.project_name != target.project_name:
            return False
        return not self.runtime_image_drift(target, snapshot)

    def runtime_image_drift(
        self, target: ComposeTarget, snapshot: StackRollbackSnapshot
    ) -> tuple[StackImageTransition, ...]:
        """Name every retained container that left its recorded image identity.

        A profile switch intentionally removes source-only runtime services from
        the target Compose model.  Their immutable image ids remain in the
        rollback snapshot, but they cannot be observed through the target model
        and must not make a valid topology transition fail preservation.
        """
        return runtime_image_drift(
            target, snapshot, run=self._run, capture_rollback=self.capture_rollback
        )

    def configured_image_identities(self, target: ComposeTarget) -> Mapping[str, str]:
        """Resolve the exact image id the selected Compose model declares per service."""
        return configured_image_identities(target, run=self._run, run_compose=self._run_compose)

    def start(
        self,
        target: ComposeTarget,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: Callable[[str], None],
        heartbeat: Callable[[str], None],
    ) -> None:
        self._run_compose(
            target,
            (
                "up",
                "--wait",
                "--wait-timeout",
                str(timeout_seconds),
                "--no-build",
                "--pull",
                "never",
                *services,
            ),
            action="start",
            timeout_seconds=timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
        )
        self._restore_canonical_container_names(target, timeout_seconds=timeout_seconds)

    def _restore_canonical_container_names(
        self, target: ComposeTarget, *, timeout_seconds: float | None
    ) -> None:
        docker_runtime_support.restore_temporary_names(
            lambda command: self._run(
                command, action="container_names", timeout_seconds=timeout_seconds
            ),
            project_name=target.project_name,
        )

    def stop(
        self,
        target: ComposeTarget,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: Callable[[str], None],
        heartbeat: Callable[[str], None],
    ) -> None:
        self._run_compose(
            target,
            ("stop", *services),
            action="stop",
            timeout_seconds=timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
        )

    def stop_project_services(
        self,
        project_name: str,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: Callable[[str], None],
        heartbeat: Callable[[str], None],
    ) -> None:
        """Stop project-owned services without rendering a superseded Compose profile."""

        self._stop_project_services(
            project_name,
            services,
            timeout_seconds=timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
        )

    def restart(
        self,
        target: ComposeTarget,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: Callable[[str], None],
        heartbeat: Callable[[str], None],
    ) -> None:
        self._run_compose(
            target,
            ("restart", *services),
            action="restart",
            timeout_seconds=timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
        )
        self._run_compose(
            target,
            (
                "up",
                "--wait",
                "--wait-timeout",
                str(timeout_seconds),
                "--no-recreate",
                *services,
            ),
            action="restart_health",
            timeout_seconds=timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
        )
        self._restore_canonical_container_names(target, timeout_seconds=timeout_seconds)

    def rebuild(
        self,
        target: ComposeTarget,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: Callable[[str], None],
        heartbeat: Callable[[str], None],
    ) -> None:
        self._run_compose(
            target,
            ("build", *services),
            action="rebuild_build",
            timeout_seconds=timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
        )
        self._run_compose(
            target,
            (
                "up",
                "--wait",
                "--wait-timeout",
                str(timeout_seconds),
                "--force-recreate",
                "--no-deps",
                *services,
            ),
            action="rebuild_recreate",
            timeout_seconds=timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
        )

    def restore(
        self,
        target: ComposeTarget,
        *,
        action: str,
        snapshot: StackRollbackSnapshot,
        timeout_seconds: int,
        output_sink: Callable[[str], None],
        heartbeat: Callable[[str], None],
        environment: Mapping[str, str] | None = None,
    ) -> bool:
        return restore(
            target,
            action=action,
            snapshot=snapshot,
            timeout_seconds=timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
            environment=environment,
            dependencies=DockerRollbackDependencies(
                project_root=self._project_root,
                run=self._run,
                run_compose=self._run_compose,
                stop_project_services=self._stop_project_services,
                remove_project_services=self._remove_project_services,
                capture_rollback=self.capture_rollback,
                runtime_image_drift=self.runtime_image_drift,
                stop=self.stop,
            ),
        )

    def restore_mismatches(
        self, target: ComposeTarget, snapshot: StackRollbackSnapshot
    ) -> tuple[str, ...]:
        """Name every service that does not match the recorded runtime, for the operator."""
        return restore_mismatches(target, snapshot, capture_rollback=self.capture_rollback)

    def release_rollback_pins(self, snapshot: StackRollbackSnapshot) -> tuple[str, ...]:
        """Drop the temporary rollback tags and override after verification completes.

        Idempotent: a pin that was never created, or was already released, is skipped.
        Returns the references that could not be released, for the operation log.
        """
        return release_rollback_pins(snapshot, project_root=self._project_root, run=self._run)

    def _remove_project_services(
        self,
        project_name: str,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: Callable[[str], None],
        heartbeat: Callable[[str], None],
    ) -> None:
        """Remove rollback-absent services even when the source Compose omits them."""
        docker_runtime_support.validate_project_name(project_name)
        docker_runtime_support.validate_services(services)
        container_ids = self._project_service_container_ids(
            project_name,
            services,
            timeout_seconds=timeout_seconds,
            action="restore_absent_inventory",
        )
        if container_ids:
            self._run(
                ("docker", "rm", "--force", *container_ids),
                action="restore_absent",
                timeout_seconds=timeout_seconds,
                output_sink=output_sink,
                heartbeat=heartbeat,
            )

    def _stop_project_services(
        self,
        project_name: str,
        services: tuple[str, ...],
        *,
        timeout_seconds: int,
        output_sink: Callable[[str], None],
        heartbeat: Callable[[str], None],
    ) -> None:
        """Stop retained cross-profile containers absent from the source Compose."""
        container_ids = self._project_service_container_ids(
            project_name,
            services,
            timeout_seconds=timeout_seconds,
            action="restore_stopped_inventory",
        )
        if container_ids:
            self._run(
                ("docker", "stop", *container_ids),
                action="restore_stopped",
                timeout_seconds=timeout_seconds,
                output_sink=output_sink,
                heartbeat=heartbeat,
            )

    def _project_service_container_ids(
        self,
        project_name: str,
        services: tuple[str, ...],
        *,
        timeout_seconds: float,
        action: str,
    ) -> list[str]:
        docker_runtime_support.validate_project_name(project_name)
        docker_runtime_support.validate_services(services)
        container_ids: list[str] = []
        for service in services:
            outcome = self._run(
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

    def logs(
        self,
        target: ComposeTarget,
        *,
        service: str | None,
        tail: int,
        follow: bool,
        output_sink: Callable[[str], None],
    ) -> int:
        command = ["logs", "--no-color", "--tail", str(tail)]
        if follow:
            command.append("--follow")
        if service:
            command.append(service)
        lines = [0]

        def count_output(message: str) -> None:
            lines[0] += 1
            output_sink(message)

        self._run_compose(
            target,
            tuple(command),
            action="logs",
            timeout_seconds=None,
            output_sink=count_output,
        )
        return lines[0]

    def manual_recovery_command(
        self,
        action: str,
    ) -> str:
        if action not in {"start", "stop", "restart", "rebuild"}:
            raise StackGatewayError(action="recovery", detail="Unknown stack recovery action.")
        return shlex.join(
            (
                "./flow-steward",
                "stack",
                action,
                "--confirm",
            )
        )

    def storage_connectivity(self, target: ComposeTarget) -> bool:
        try:
            self.require_storage_connectivity(target)
        except StackGatewayError:
            return False
        return True

    def require_storage_connectivity(self, target: ComposeTarget) -> None:
        """Run the disposable storage probe; its failure carries the probe's own output.

        AC17-R148: `config apply` answered only "the target configuration failed" when this
        probe failed, because the boolean form above drops the reason.
        """
        return require_storage_connectivity(target, run_compose=self._run_compose)

    def compact_candidate_preflight(self, target: ComposeTarget) -> str:
        """Run product-owned database/identity checks in Compact, not management."""
        return compact_candidate_preflight(target, run_compose=self._run_compose)

    def compact_app_readiness(self, target: ComposeTarget) -> dict[str, Any]:
        """Read the candidate app readiness from inside the Compact container."""
        return compact_app_readiness(target, run_compose=self._run_compose)

    def compact_maintenance_operation(
        self, target: ComposeTarget, operation: str
    ) -> dict[str, object]:
        """Delegate fixed maintenance operations to the existing runtime marker owner."""
        return compact_maintenance_operation(target, operation, run_compose=self._run_compose)

    def management_storage_connectivity(self, target: ComposeTarget) -> bool:
        """Probe storage from the retained CLI while release services are changing."""
        return management_storage_connectivity(target, run_compose=self._run_compose)

    def prepare_bundled_storage_probe(self, target: ComposeTarget) -> None:
        """Start only candidate SeaweedFS; rollback inventory owns reconciliation."""
        return prepare_bundled_storage_probe(target, run_compose=self._run_compose)

    def probe_bundled_storage(self, target: ComposeTarget) -> None:
        """Run a disposable write/read/delete probe on the target storage network."""
        return probe_bundled_storage(target, run_compose=self._run_compose)

    def bundled_storage_inventory(self, target: ComposeTarget) -> dict[str, object]:
        """Inventory bundled objects from a disposable target-network service."""
        return bundled_storage_inventory(target, run_compose=self._run_compose)

    def service_release(self, target: ComposeTarget) -> str:
        return service_release(target, run_compose=self._run_compose)

    def reconcile_workflow_knowledge(self, target: ComposeTarget) -> None:
        """Refresh derived knowledge using the runtime that is actually installed."""
        return reconcile_workflow_knowledge(target, run_compose=self._run_compose)

    def reconcile_security_posture(self, target: ComposeTarget) -> None:
        """Refresh durable security-posture evidence before candidate readiness."""
        return reconcile_security_posture(target, run_compose=self._run_compose)

    def port_available_for_transition(
        self,
        managed_target: ComposeTarget,
        host: str,
        port: int,
        *,
        protocol: str = "tcp",
    ) -> bool:
        normalized_protocol = str(protocol).strip().lower()
        if normalized_protocol not in {"tcp", "udp"}:
            return False
        if self._port_available(host, port, normalized_protocol):
            return True
        try:
            state = self.status(managed_target)
        except StackGatewayError:
            return False
        return any(
            container.state.strip().lower() == "running"
            and (binding := docker_runtime_support.published_binding(mapping)) is not None
            and binding.port == port
            and binding.protocol == normalized_protocol
            and docker_runtime_support.bind_addresses_conflict(host, binding.host)
            for container in state.services
            for mapping in container.published_ports
        )

    def _volumes(self, project_name: str) -> tuple[StackVolumeState, ...]:
        docker_runtime_support.validate_project_name(project_name)
        listing = self._run(
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
        names = tuple(
            sorted(line.strip() for line in str(listing.stdout).splitlines() if line.strip())
        )
        if not names:
            return ()
        inspected = self._run(
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

    def _run_compose(
        self,
        target: ComposeTarget,
        arguments: Sequence[str],
        *,
        action: str,
        timeout_seconds: float | None,
        output_sink: Callable[[str], None] | None = None,
        heartbeat: Callable[[str], None] | None = None,
        additional_compose_files: tuple[Path, ...] = (),
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        separate_stderr: bool = False,
    ) -> Any:
        return self._run(
            tuple(
                self._compose_command(
                    target,
                    arguments,
                    additional_compose_files=additional_compose_files,
                )
            ),
            action=action,
            timeout_seconds=timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
            environment=environment,
            input_text=input_text,
            separate_stderr=separate_stderr,
        )

    def _run(
        self,
        command: Sequence[str],
        *,
        action: str,
        timeout_seconds: float | None,
        output_sink: Callable[[str], None] | None = None,
        heartbeat: Callable[[str], None] | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        separate_stderr: bool = False,
    ) -> Any:
        heartbeat_sink = (
            (lambda elapsed: heartbeat(f"{action} running for {elapsed:.0f}s"))
            if heartbeat is not None
            else None
        )
        try:
            outcome = self._process.run(
                list(command),
                cwd=self._project_root,
                timeout_seconds=timeout_seconds,
                output_sink=output_sink,
                heartbeat_sink=heartbeat_sink,
                environment=environment,
                input_text=input_text,
                separate_stderr=separate_stderr,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise StackGatewayError(
                action=action,
                detail=f"Docker {action} failed: {type(exc).__name__}.",
            ) from exc
        if int(outcome.returncode) != 0:
            raw_detail = str(getattr(outcome, "stderr", "") or outcome.stdout or "").strip()
            detail = redact_installer_text(raw_detail)
            if len(detail) > 600:
                detail = f"...{detail[-597:]}"
            raise StackGatewayError(
                action=action,
                detail=detail or f"Docker {action} exited with code {outcome.returncode}.",
            )
        return outcome

    def _candidate_compose_command(
        self,
        target: ComposeTarget,
        arguments: Sequence[str],
        *,
        override_content: str | None,
    ) -> tuple[str, ...]:
        docker_runtime_support.validate_project_name(target.project_name)
        command = ["docker", "compose", "--env-file", "/dev/null", "-p", target.project_name]
        override_used = False
        for compose_file in target.compose_files:
            if override_content is not None and str(compose_file).endswith(
                _TOKEN_INSTALLER_COMPOSE_OVERRIDE_YAML
            ):
                command.extend(("-f", "-"))
                override_used = True
            else:
                command.extend(("-f", str(self._compose_path(compose_file))))
        if override_content is not None and not override_used:
            raise StackGatewayError(
                action="configuration",
                detail="Candidate reverse-proxy override has no selected Compose target.",
            )
        for profile in target.compose_profiles:
            command.extend(("--profile", profile))
        command.extend(str(argument) for argument in arguments)
        return tuple(command)

    def _compose_command(
        self,
        target: ComposeTarget,
        arguments: Sequence[str],
        *,
        additional_compose_files: tuple[Path, ...] = (),
    ) -> list[str]:
        docker_runtime_support.validate_project_name(target.project_name)
        command = [
            "docker",
            "compose",
            "--env-file",
            str(self._compose_path(target.env_file)),
            "-p",
            target.project_name,
        ]
        for compose_file in target.compose_files:
            command.extend(("-f", str(self._compose_path(compose_file))))
        for compose_file in additional_compose_files:
            command.extend(("-f", str(self._compose_path(str(compose_file)))))
        for profile in target.compose_profiles:
            command.extend(("--profile", profile))
        command.extend(str(argument) for argument in arguments)
        return command

    def _compose_path(self, raw_path: str) -> Path:
        local = docker_runtime_support.project_path(self._project_root, raw_path)
        try:
            relative = local.relative_to(self._project_root)
        except ValueError:
            host = docker_runtime_support.project_path(self._docker_host_project_root, raw_path)
            try:
                host.relative_to(self._docker_host_project_root)
            except ValueError:
                return local
            return host
        return (self._docker_host_project_root / relative).resolve()
