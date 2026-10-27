"""CLI composition for the installed Clone service."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from core.application.clone.contracts import CloneDataClass
from core.application.clone.planning import PreparedCloneRestore
from core.application.clone.service import CloneRestoreArchivePort
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.lifecycle.errors import LifecycleError
from core.infrastructure.clone.private_namespace import initialize_clone_namespace
from core.infrastructure.config.installer_config import installer_state_root
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.installer.storage_identity import BundledStorageIdentityService

_INSTALLED_CONFIGURATION_UNAVAILABLE = "Installed configuration is unavailable."


def _bounded_host_id(raw: object, fallback: int) -> int:
    value = str(raw or "").strip()
    if not value:
        return fallback
    if not value.isascii() or not value.isdecimal():
        raise RuntimeError("Installed host identity is invalid for Clone.")
    parsed = int(value)
    if parsed > 2**31 - 1:
        raise RuntimeError("Installed host identity is invalid for Clone.")
    return parsed


def _capture_clone_database_parity(*, dsn: str, output: Path) -> None:
    from core.application.tooling.db.restore_parity_check import capture_restore_parity_metadata

    capture_restore_parity_metadata(dsn=dsn, label="clone-source", output=output)


def _verify_clone_database_parity(*, dsn: str, baseline: Path) -> bool:
    from core.application.tooling.db.restore_parity_check import verify_restore_parity_metadata

    result = verify_restore_parity_metadata(
        dsn=dsn,
        label="clone-restored",
        baseline=baseline,
    )
    return bool(result.get("passed"))


def build_installed_clone_service(
    runtime: Any,
    *,
    repo_root: Path,
    environment_loader: Callable[[], dict[str, str]],
    platform_machine: Callable[[], str],
) -> Any:
    """Compose Clone from retained installed-stack owners."""
    installed = runtime.release_service.require_official_installation()
    from core.application.clone.planning import CloneTargetShell
    from core.application.clone.runtime_policy import (
        build_clone_target_shell,
        restored_clone_configuration,
    )
    from core.application.clone.secret_policy import select_clone_source_configuration
    from core.application.clone.service import CloneService
    from core.application.clone.verification import require_clone_readiness
    from core.application.readiness.service import CoreReadinessService
    from core.entrypoints.cli.lifecycle_runtime import _app_runtime_readiness_payload
    from core.infrastructure.clone.archive_gateway import (
        CloneArchiveGateway,
        CloneRestoreArchiveGateway,
    )
    from core.infrastructure.clone.installation_gateway import (
        CloneInstallationStateGuard,
        ComposeCloneServiceControl,
        DockerCloneVolumeTransfer,
    )
    from core.infrastructure.clone.installation_restore_effects import InstalledCloneRestoreEffects
    from core.infrastructure.clone.restore_gateway import CloneRestoreGateway
    from core.infrastructure.clone.runtime_gateway import CloneRuntimeGateway

    config = runtime.configuration_service.read_active()
    if config is None:  # pragma: no cover - required installed-runtime invariant
        raise RuntimeError(_INSTALLED_CONFIGURATION_UNAVAILABLE)
    target = runtime.configuration_service.derive_target(config)
    environment = environment_loader()
    roots = runtime._clone_data_roots(config, environment)
    capture_roots: dict[CloneDataClass | str, Path] = {
        cast(CloneDataClass | str, key): value for key, value in roots.items()
    }
    output_root = runtime._clone_output_root(config, environment)
    input_root = runtime._clone_input_root(config, environment)
    clone_state_root = runtime._clone_workspace_root()
    release_state = runtime.release_service.state
    tool_image = next(
        (image.registry_ref for image in installed.images if image.role == "fs-cli"),
        "",
    )
    if not tool_image:
        raise RuntimeError("Installed fs-cli image identity is unavailable for Clone.")
    initialize_clone_namespace(clone_state_root)
    services = ComposeCloneServiceControl(
        docker=runtime.docker_lifecycle_gateway,
        target=target,
    )
    volumes = DockerCloneVolumeTransfer(
        project_name=target.project_name,
        tool_image=tool_image,
    )
    archive = CloneArchiveGateway()
    from core.infrastructure.config.env_registry import env_specs

    source_secrets = select_clone_source_configuration(
        environment,
        specs=env_specs(),
        environment=config.environment,
    )

    def capture_source_preservation(plan: Any, output: Path) -> None:
        manifest = runtime.preservation_service.capture(
            operation_id=plan.operation_id,
            stage="before",
            compose_project=target.project_name,
            release=plan.release.version,
            environment=plan.config.environment,
            storage_backend=plan.config.file_storage,
            storage_connectivity=runtime.docker_lifecycle_gateway.storage_connectivity(target),
        )
        output.write_text(
            json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    create_runtime = CloneRuntimeGateway(
        staging_root=clone_state_root / "create",
        output_root=output_root,
        backup=runtime.backup_service,
        services=services,
        volumes=volumes,
        source_roots=capture_roots,
        archive=archive,
        source_secrets=source_secrets,
        capture_database_parity=lambda output: _capture_clone_database_parity(
            dsn=runtime._configured_platform_dsn(),
            output=output,
        ),
        capture_preservation=capture_source_preservation,
        external_effect_categories=(
            "mail",
            "oauth_callbacks",
            "polling",
            "schedules",
            "shared_storage",
            "webhooks",
        ),
        compact_data_volume=config.data_placement == "compact_volume",
    )
    restore_archive = CloneRestoreArchiveGateway(
        staging_parent=clone_state_root / "restore-staging",
        archive=archive,
    )

    def target_shell(active: DeploymentConfig) -> CloneTargetShell:
        current_target = runtime.configuration_service.derive_target(active)
        current_environment = environment_loader()
        return build_clone_target_shell(
            installation_id=current_target.project_name,
            compose_project=current_target.project_name,
            config=active,
            environment=current_environment,
            host_uid=_bounded_host_id(current_environment.get("FS_HOST_UID"), os.getuid()),
            host_gid=_bounded_host_id(current_environment.get("FS_HOST_GID"), os.getgid()),
            filesystem_roots=runtime._clone_shell_filesystem_roots(
                active,
                current_environment,
            ),
        )

    installer_state = InstallerStateStore(
        installer_state_root(repo_root),
        lifecycle_state_root=runtime._clone_state_root(),
    )
    lifecycle_state = runtime._lifecycle_state()
    installation_guard = CloneInstallationStateGuard(
        installer_state,
        lifecycle_state=lifecycle_state,
    )

    def import_secrets(values: Mapping[str, str]) -> None:
        active = runtime.configuration_service.read_active()
        if active is None:
            raise RuntimeError(_INSTALLED_CONFIGURATION_UNAVAILABLE)
        installer_state.update_env_values(
            runtime.configuration_service.store.env_path(active),
            values,
        )

    def restored_configuration(prepared: PreparedCloneRestore) -> DeploymentConfig:
        return restored_clone_configuration(prepared)

    def regenerate_configuration(prepared: PreparedCloneRestore) -> None:
        restored = restored_configuration(prepared)
        if restored.file_storage == "bundled":
            source_secrets_path = Path(prepared.staged_root) / "secrets/source.json"
            source_secrets = json.loads(source_secrets_path.read_text(encoding="utf-8"))
            if not isinstance(source_secrets, dict):
                raise ValueError("Clone source secrets payload must be an object")
            BundledStorageIdentityService(
                installer_state.root / "runtime/seaweedfs-s3.json"
            ).replace(
                access_key=str(source_secrets.get("ARTIFACT_S3_ACCESS_KEY") or ""),
                secret_key=str(source_secrets.get("ARTIFACT_S3_SECRET_KEY") or ""),
            )
        runtime.configuration_service.write_active_configuration(restored)

    def activate_release(prepared: PreparedCloneRestore) -> None:
        runtime.release_service.activator.activate(
            prepared.exact_release,
            timeout_seconds=120,
            held_locks=lifecycle_state.held_lock_descriptors(),
        )
        if release_state.load_installed_release() != prepared.exact_release:
            raise LifecycleError.recovery_required(
                code="clone_restore_release_activation_incomplete",
                cause="The archived exact release was not activated.",
                remediation=(
                    "Resolve any required database migration without running it from Clone, "
                    "then resume the same restore operation."
                ),
                step="activate_release",
                recovery_command="flow-steward clone restore",
            )

    def verify_database(prepared: PreparedCloneRestore) -> bool:
        return _verify_clone_database_parity(
            dsn=runtime._configured_platform_dsn(),
            baseline=Path(prepared.staged_root) / "evidence/database-parity.json",
        )

    def secrets_match(prepared: PreparedCloneRestore) -> bool:
        current = environment_loader()
        return all(
            hashlib.sha256(str(current.get(name) or "").encode()).hexdigest() == fingerprint
            for name, fingerprint in prepared.manifest.source_secret_fingerprints.items()
        )

    def configuration_matches(prepared: PreparedCloneRestore) -> bool:
        return runtime.configuration_service.read_active() == restored_configuration(prepared)

    def release_activated(prepared: PreparedCloneRestore) -> bool:
        return release_state.load_installed_release() == prepared.exact_release

    def verify_target(prepared: PreparedCloneRestore) -> None:
        maintenance = runtime.maintenance_service.status()
        if maintenance.status != "enabled" or maintenance.owner_kind != "manual":
            raise RuntimeError("Clone restore did not retain manual maintenance.")
        require_clone_readiness(runtime.lifecycle_mutation_readiness_service.check())
        if not verify_database(prepared):
            raise RuntimeError("Clone restore database parity verification failed.")
        if release_state.load_installed_release() != prepared.exact_release:
            raise RuntimeError("Clone restore did not activate the archived exact release.")

    def probe_target_readiness(_prepared: PreparedCloneRestore, remaining: float) -> None:
        active = runtime.configuration_service.read_active()
        if active is None:
            raise RuntimeError(_INSTALLED_CONFIGURATION_UNAVAILABLE)
        # Only the canonical HTTP transport is retryable. In particular, Compact's
        # additional Docker app probe and lifecycle reconciliation stay in the
        # one-shot full readiness check above, outside the listener startup budget.
        readiness = CoreReadinessService(
            probe=lambda: _app_runtime_readiness_payload(
                allow_lifecycle_mutation=True,
                config=active,
                timeout_seconds=remaining,
            )
        )
        require_clone_readiness(readiness.check())

    def capture_target_preservation(
        prepared: PreparedCloneRestore,
        stage: str,
    ) -> Any:
        active = runtime.configuration_service.read_active()
        installed_release = release_state.load_installed_release()
        if active is None or installed_release is None:
            raise RuntimeError("Clone preservation target identity is unavailable")
        current_target = runtime.configuration_service.derive_target(active)
        return runtime.preservation_service.capture(
            operation_id=prepared.operation_id,
            stage=cast(Any, stage),
            compose_project=current_target.project_name,
            release=installed_release.version,
            environment=active.environment,
            storage_backend=active.file_storage,
            storage_connectivity=runtime.docker_lifecycle_gateway.storage_connectivity(
                current_target
            ),
        )

    effects = InstalledCloneRestoreEffects(
        operation_root=clone_state_root / "prepared",
        services=services,
        service_names=tuple(
            name for name in target.runtime_services if name not in {"fs-cli", "pg-fs", "traefik"}
        ),
        volumes=volumes,
        target_roots=roots,
        backup=runtime.backup_service,
        import_secrets=import_secrets,
        regenerate_configuration=regenerate_configuration,
        activate_release=activate_release,
        verify_database=verify_database,
        secrets_match=secrets_match,
        configuration_matches=configuration_matches,
        release_activated=release_activated,
        verify_target=verify_target,
        readiness_probe=probe_target_readiness,
        compact_data_volume=config.data_placement == "compact_volume",
        preservation=runtime.preservation_service,
        capture_preservation=capture_target_preservation,
    )

    def current_target_shell() -> CloneTargetShell:
        active = runtime.configuration_service.read_active()
        if active is None:
            raise RuntimeError(_INSTALLED_CONFIGURATION_UNAVAILABLE)
        return target_shell(active)

    restore_runtime = CloneRestoreGateway(
        operation_root=clone_state_root / "restore-evidence",
        target_shell=current_target_shell,
        phase_handlers=effects.handlers(),
        phase_postconditions=effects.postconditions(),
    )

    def resolve_output_directory(raw: str) -> Path:
        selected = Path(raw)
        if selected.is_absolute():
            return selected
        if selected == Path("backups"):
            return output_root
        return selected

    def resolve_archive_path(raw: str) -> Path:
        selected = Path(raw)
        if selected.is_absolute():
            return selected
        parts = selected.parts
        if parts and parts[0] == "backups":
            candidate = input_root.joinpath(*parts[1:])
            if candidate.resolve(strict=False).is_relative_to(input_root.resolve(strict=False)):
                return candidate
            raise ValueError("Clone archive path escaped the configured backup root")
        return selected

    return CloneService(
        maintenance=runtime.maintenance_service,
        lifecycle_state=lifecycle_state,
        configuration=runtime.configuration_service,
        release_state=release_state,
        release_verifier=runtime.release_service.selector,
        runtime=create_runtime,
        source_installation_id=lambda: target.project_name,
        restore_archive=cast(CloneRestoreArchivePort, restore_archive),
        exact_release=runtime.release_service.selector,
        target_machine=platform_machine,
        target_shell=target_shell,
        restore_runtime=restore_runtime,
        resolve_output_directory=resolve_output_directory,
        resolve_archive_path=resolve_archive_path,
        installation_ready=installation_guard.require_ready,
    )
