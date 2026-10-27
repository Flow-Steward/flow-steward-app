"""ImagePreparation technical operations for the installer adapter."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.application.installer.contracts import (
    InstallConfig,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerLog
from core.infrastructure.installer import registry_image_resolver
from core.infrastructure.installer.core_dependency_images import (
    prepare_core_dependency_images,
)
from core.infrastructure.installer.gateway_support import (
    _EXACT_IMAGE_DIGEST_KEYS,
    _EXACT_IMAGE_KEYS,
    _EXACT_IMAGE_REVISION_KEYS,
    _FULL_GIT_REVISION,
    _IMAGE_ID,
    _REMEDIATION_INFRASTRUCTURE_IMAGE,
    _SOURCE_REVISION_FAILURE_CODE,
    _SOURCE_REVISION_REMEDIATION,
    _TOKEN_HEAD_COMMIT,
    _read_env,
)
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.registry_image_resolver import (
    RegistryImageIdentity,
)

if TYPE_CHECKING:
    from core.infrastructure.installer.gateway import DockerInstallerGateway


class ImagePreparationOperations:
    def prepare_core(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        if self._install_mode(config) == "official":
            self._refuse_lost_image_ids(config, names=("POSTGRES_IMAGE",))
            registry_identity = self._materialize_official_release(config, log=log)
            environment = _read_env(self._env_path(config))
            if self._exact_prebuilt_image_refs(config, environment) is None:
                raise self._production_provenance_failure(
                    "The official release did not produce a complete immutable local image set."
                )
            self._prepare_exact_prebuilt_provenance(
                config, log=log, registry_identity=registry_identity
            )
            prepare_core_dependency_images(
                failure=self._core_dependency_failure,
                config=config,
                compose_prefix=self._compose_prefix(config),
                repo_root=self.repo_root,
                environment={**environment, **self._environment},
                run=self.process.run,
                checked=lambda command: self._checked(
                    command,
                    log=log,
                    code="core_image_preparation_failed",
                    remediation=_REMEDIATION_INFRASTRUCTURE_IMAGE,
                    exit_code=7,
                    step="prepare",
                ),
            )
            return
        self._build_compact_runtime_image(config, log=log)
        self._build_compact_management_image(config, log=log)
        self._build_scalable_candidate_images(config, log=log)
        self._refuse_lost_image_ids(config)
        registry_identity = self._resolve_compact_registry_image(config, log=log)
        environment = _read_env(self._env_path(config))
        if self._exact_prebuilt_image_refs(config, environment) is not None:
            self._prepare_exact_prebuilt_provenance(
                config, log=log, registry_identity=registry_identity
            )
            prepare_core_dependency_images(
                failure=self._core_dependency_failure,
                config=config,
                compose_prefix=self._compose_prefix(config),
                repo_root=self.repo_root,
                environment={**environment, **self._environment},
                run=self.process.run,
                checked=lambda command: self._checked(
                    command,
                    log=log,
                    code="core_image_preparation_failed",
                    remediation=_REMEDIATION_INFRASTRUCTURE_IMAGE,
                    exit_code=7,
                    step="prepare",
                ),
            )
            return
        compose_prefix = self._compose_prefix(config)
        if config.environment == "development":
            self._checked(
                [
                    *compose_prefix,
                    "pull",
                    "--ignore-buildable",
                    "--policy",
                    "missing",
                    "sonar-scanner",
                ],
                log=log,
                code="core_image_preparation_failed",
                remediation=(
                    "Resolve the reported Docker build/pull failure and resume ./install.sh."
                ),
                exit_code=7,
                step="prepare",
            )
        args = [*compose_prefix, "build"]
        self._checked(
            args,
            log=log,
            code="core_image_preparation_failed",
            remediation="Resolve the reported Docker build/pull failure and resume ./install.sh.",
            exit_code=7,
            step="prepare",
        )
        self._prepare_production_image_provenance(config, log=log)

    def _materialize_official_release(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> RegistryImageIdentity | None:
        env_path = self._env_path(config)
        known = {**_read_env(env_path), **self._environment}
        existing_refs = self._complete_exact_prebuilt_image_refs(config, known)
        if (
            self.state_store.load_pending_release() is None
            and existing_refs is not None
            and all(_IMAGE_ID.fullmatch(reference) for reference in existing_refs.values())
        ):
            return self._resolve_compact_registry_image(config, log=log)
        release = self._official_pending_release(config)
        assert release is not None
        role_names = {
            "compact-runtime": "compact",
            "fs-app": "app",
            "fs-worker": "worker",
            "control-plane": "control",
            "fs-cli": "cli",
            "fs-mcp": "mcp",
        }
        updates: dict[str, str] = {"FS_BUILD_SHA": release.commit_sha}
        compact_identity: RegistryImageIdentity | None = None

        def run(command: Sequence[str]) -> ProcessOutcome:
            return self._checked(
                command,
                log=log,
                code="official_release_image_unavailable",
                remediation="Restore registry access and resume the same official release.",
                exit_code=7,
                step="prepare",
            )

        for image in release.images:
            repository = image.registry_ref.partition("@")[0]
            identity = registry_image_resolver.resolve_digest_pinned_image(
                registry_ref=f"{repository}@{image.index_digest}",
                platform=image.platform,
                expected_revision=image.revision,
                expected_role=image.role,
                run=run,
            )
            if (
                identity.registry_ref != image.registry_ref
                or identity.index_digest != image.index_digest
                or identity.manifest_digest != image.manifest_digest
                or identity.config_digest != image.config_digest
                or identity.revision != image.revision
                or identity.role != image.role
                or identity.platform != image.platform
            ):
                raise self._production_provenance_failure(
                    f"Materialized official image {image.role} does not match pending identity."
                )
            legacy_role = role_names[image.role]
            updates[_EXACT_IMAGE_KEYS[legacy_role]] = identity.registry_ref
            updates[_EXACT_IMAGE_REVISION_KEYS[legacy_role]] = identity.revision
            digest_key = _EXACT_IMAGE_DIGEST_KEYS.get(legacy_role)
            if digest_key is not None:
                updates[digest_key] = identity.config_digest
            if legacy_role == "compact":
                compact_identity = identity
                updates.update(
                    {
                        "FS_COMPACT_IMAGE_REGISTRY_REF": identity.registry_ref,
                        "FS_COMPACT_IMAGE_INDEX_DIGEST": identity.index_digest,
                        "FS_COMPACT_IMAGE_MANIFEST_DIGEST": identity.manifest_digest,
                        "FS_COMPACT_IMAGE_ROLE": identity.role,
                        "FS_COMPACT_IMAGE_PLATFORM": identity.platform,
                    }
                )
        self.state_store.update_env_values(env_path, updates)
        self._environment = {**self._environment, **updates}
        log.write("Resolved the complete official release image set without local builds.")
        return compact_identity

    def _require_resolvable_source_revision(
        self: DockerInstallerGateway, *, log: InstallerLog
    ) -> str:
        """Fail before the first write when the checkout cannot yield a revision.

        R90: this used to surface during prepare_core as
        `core_image_preparation_failed` with an image-inventory remediation, which
        named the wrong subsystem, and only after the installer had already written
        configuration, secrets and a TLS certificate to disk.
        """
        outcome = self._checked(
            ["git", "-C", str(self.repo_root), "rev-parse", _TOKEN_HEAD_COMMIT],
            log=log,
            separate_stderr=True,
            code=_SOURCE_REVISION_FAILURE_CODE,
            remediation=_SOURCE_REVISION_REMEDIATION,
            exit_code=7,
            step="preflight",
        )
        revision = outcome.stdout.strip()
        if _FULL_GIT_REVISION.fullmatch(revision) is None:
            raise InstallerFailure(
                code=_SOURCE_REVISION_FAILURE_CODE,
                cause=(
                    "The installer could not resolve a full 40-character source "
                    f"revision; git reported {revision!r}."
                ),
                remediation=_SOURCE_REVISION_REMEDIATION,
                exit_code=7,
                retryable=False,
                step="preflight",
            )
        return revision

    def _prepare_exact_prebuilt_provenance(
        self: DockerInstallerGateway,
        config: InstallConfig,
        *,
        log: InstallerLog,
        registry_identity: RegistryImageIdentity | None = None,
    ) -> None:
        def checked(command: Sequence[str]) -> ProcessOutcome:
            return self._checked(
                command,
                log=log,
                code="core_image_preparation_failed",
                remediation="Restore the exact local prebuilt image inventory and resume ./install.sh.",
                exit_code=7,
                step="prepare",
            )

        def checked_source_revision(command: Sequence[str]) -> ProcessOutcome:
            # The image-inventory remediation above is wrong for a git failure: it
            # names a different subsystem than the one that failed. Preflight now
            # refuses this case before anything is written, so reaching here means
            # the checkout stopped being readable mid-install.
            return self._checked(
                command,
                log=log,
                code=_SOURCE_REVISION_FAILURE_CODE,
                remediation=_SOURCE_REVISION_REMEDIATION,
                exit_code=7,
                step="prepare",
                separate_stderr=True,
            )

        env_path = self._env_path(config)
        environment = _read_env(env_path)
        refs = self._exact_prebuilt_image_refs(config, environment)
        if refs is None:
            raise self._production_provenance_failure(
                "The exact-prebuilt image selection is unavailable."
            )
        source_revision = checked_source_revision(
            ["git", "-C", str(self.repo_root), "rev-parse", _TOKEN_HEAD_COMMIT]
        ).stdout.strip()
        if _FULL_GIT_REVISION.fullmatch(source_revision) is None:
            raise self._production_provenance_failure(
                "The installer could not resolve the full source revision."
            )
        persisted_source_revision = str(environment.get("FS_BUILD_SHA") or "").strip()
        if persisted_source_revision and persisted_source_revision != source_revision:
            raise self._production_provenance_failure(
                "Persisted source provenance does not match the installer checkout."
            )

        inspection_refs = dict(refs)
        if registry_identity is not None:
            inspection_refs["compact"] = registry_identity.local_image_id
        identities = {
            role: self._inspect_exact_prebuilt_image(role, image, checked)
            for role, image in inspection_refs.items()
        }
        for role, identity in identities.items():
            revision_key = _EXACT_IMAGE_REVISION_KEYS[role]
            persisted_revision = str(environment.get(revision_key) or "").strip()
            if persisted_revision and persisted_revision != identity.revision:
                raise self._production_provenance_failure(
                    f"Persisted {role} image revision does not match Docker inventory."
                )
            if role == "compact":
                if identity.content is None:
                    raise self._production_provenance_failure(
                        "The exact compact image has no verified OCI content identity."
                    )
                if registry_identity is not None:
                    local_registry_values = {
                        "image ID": (identity.image_id, registry_identity.local_image_id),
                        # Docker save can regenerate the manifest serialization.
                        # The resolver verifies the published manifest -> config
                        # chain; bind that config to the inspected local archive.
                        "config digest": (
                            identity.content.config_digest,
                            registry_identity.config_digest,
                        ),
                        "revision": (identity.revision, registry_identity.revision),
                        "role": (identity.role, registry_identity.role),
                        "platform": (identity.platform, registry_identity.platform),
                    }
                    for description, (actual, expected) in local_registry_values.items():
                        if actual != expected:
                            raise self._production_provenance_failure(
                                "The local Compact image "
                                f"{description} does not match the validated registry identity."
                            )
                expected_values = {
                    "FS_COMPACT_IMAGE_INDEX_DIGEST": (
                        registry_identity.index_digest
                        if registry_identity is not None
                        else identity.content.index_digest
                    ),
                    "FS_COMPACT_IMAGE_MANIFEST_DIGEST": (
                        registry_identity.manifest_digest
                        if registry_identity is not None
                        else identity.content.manifest_digest
                    ),
                    "FS_COMPACT_IMAGE_CONFIG_DIGEST": (
                        registry_identity.config_digest
                        if registry_identity is not None
                        else identity.content.config_digest
                    ),
                    "FS_COMPACT_IMAGE_ROLE": identity.role,
                    "FS_COMPACT_IMAGE_PLATFORM": identity.platform,
                }
                for key, expected in expected_values.items():
                    persisted = str(environment.get(key) or "").strip()
                    if persisted and persisted != expected:
                        raise self._production_provenance_failure(
                            f"Persisted {key} does not match Docker inventory."
                        )

        updates = {"FS_BUILD_SHA": source_revision}
        for role, identity in identities.items():
            updates[_EXACT_IMAGE_KEYS[role]] = (
                registry_identity.registry_ref
                if role == "compact" and registry_identity is not None
                else refs[role]
            )
            updates[_EXACT_IMAGE_REVISION_KEYS[role]] = identity.revision
            digest_key = _EXACT_IMAGE_DIGEST_KEYS.get(role)
            if digest_key is not None:
                updates[digest_key] = (
                    identity.content.config_digest
                    if role == "compact" and identity.content is not None
                    else identity.image_id
                )
            if role == "compact" and identity.content is not None:
                updates.update(
                    {
                        "FS_COMPACT_IMAGE_REGISTRY_REF": (
                            registry_identity.registry_ref
                            if registry_identity is not None
                            else refs[role]
                        ),
                        "FS_COMPACT_IMAGE_INDEX_DIGEST": (
                            registry_identity.index_digest
                            if registry_identity is not None
                            else identity.content.index_digest
                        ),
                        "FS_COMPACT_IMAGE_MANIFEST_DIGEST": (
                            registry_identity.manifest_digest
                            if registry_identity is not None
                            else identity.content.manifest_digest
                        ),
                        "FS_COMPACT_IMAGE_ROLE": identity.role,
                        "FS_COMPACT_IMAGE_PLATFORM": identity.platform,
                    }
                )
        self.state_store.update_env_values(env_path, updates)
        for role, identity in identities.items():
            log.write(
                f"Validated exact prebuilt image role={role} "
                f"id={identity.image_id} revision={identity.revision}."
            )
        log.write("Durable exact-prebuilt image provenance is ready without build or pull.")
