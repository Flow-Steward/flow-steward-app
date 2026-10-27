"""ImageProvenance technical operations for the installer adapter."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from core.application.installer.contracts import (
    InstallConfig,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerLog
from core.application.release.images import required_product_roles
from core.infrastructure.installer.gateway_support import (
    _COMPOSE_PROJECT_NAME,
    _EXACT_IMAGE_COMMAND_MARKERS,
    _EXACT_IMAGE_DIGEST_KEYS,
    _EXACT_IMAGE_KEYS,
    _EXACT_IMAGE_REVISION_KEYS,
    _EXACT_IMAGE_SERVICE_LABELS,
    _FULL_GIT_REVISION,
    _IMAGE_ID,
    _RUNTIME_IMAGES,
    _SOURCE_REVISION_FAILURE_CODE,
    _SOURCE_REVISION_REMEDIATION,
    _TOKEN_HEAD_COMMIT,
    _TOKEN_ORG_OPENCONTAINERS_IMAGE_REVISION,
    _TOKEN_SHA256,
    _ExactImageIdentity,
    _image_environment,
    _normalized_docker_architecture,
    _read_env,
)
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.registry_image_resolver import (
    LocalImageContentIdentity,
    resolve_local_image_archive,
)

if TYPE_CHECKING:
    from core.infrastructure.installer.gateway import DockerInstallerGateway


class ImageProvenanceOperations:
    _SOURCE_REVISION_KEY_BY_ROLE: ClassVar[dict[str, str]] = {
        "compact-runtime": "FS_COMPACT_IMAGE_REVISION",
        "fs-app": "FS_APP_IMAGE_REVISION",
        "fs-worker": "FS_WORKER_IMAGE_REVISION",
        "control-plane": "FS_CONTROL_IMAGE_REVISION",
        "fs-cli": "FS_CLI_IMAGE_REVISION",
        "fs-mcp": "MCP_IMAGE_REVISION",
    }

    def validated_source_revision(
        self: DockerInstallerGateway,
        config: InstallConfig,
    ) -> str:
        """Return source revision only for a complete locally built image set."""
        environment = _read_env(self._env_path(config))
        revision = str(environment.get("FS_BUILD_SHA") or "").strip()
        if (
            config.environment != "production"
            or str(environment.get("FS_INSTALL_MODE") or "").strip() != "source"
            or _FULL_GIT_REVISION.fullmatch(revision) is None
        ):
            raise self._source_provenance_failure(
                "The source image provenance mode or build revision is invalid."
            )
        for role in required_product_roles(config.deployment_profile):
            image = str(environment.get(role.environment_variable) or "").strip()
            # A source Compact runtime embeds CLI and MCP; separate images are
            # optional here, but any explicitly selected image must still prove
            # the same revision. Official release validation requires all roles.
            if (
                config.deployment_profile == "compact"
                and role.role != "compact-runtime"
                and not image
            ):
                continue
            revision_key = self._SOURCE_REVISION_KEY_BY_ROLE[role.role]
            if (
                _IMAGE_ID.fullmatch(image) is None
                or str(environment.get(revision_key) or "").strip() != revision
                or not self._production_image_matches(image, revision)
            ):
                raise self._source_provenance_failure(
                    f"The source image provenance is incomplete or inconsistent for {role.role}."
                )
        for service, image_key in _RUNTIME_IMAGES[config.deployment_profile]:
            recorded_image = str(environment.get(image_key) or "").strip()
            running_image = self._running_image_id(config, service)
            if not running_image or running_image != recorded_image:
                raise self._source_provenance_failure(
                    f"The running source image provenance is missing or stale for {service}."
                )
        return revision

    def _inspect_exact_prebuilt_image(
        self: DockerInstallerGateway,
        role: str,
        image: str,
        checked: Callable[[Sequence[str]], ProcessOutcome],
    ) -> _ExactImageIdentity:
        outcome = checked(["docker", "image", "inspect", image])
        try:
            details = json.loads(outcome.stdout)[0]
            image_id = str(details["Id"])
            architecture = str(details["Architecture"])
            operating_system = str(details.get("Os") or "linux")
            descriptor = details.get("Descriptor")
            repo_digests = tuple(str(item) for item in (details.get("RepoDigests") or ()))
            config = details["Config"]
            labels = config["Labels"]
            revision = str(labels[_TOKEN_ORG_OPENCONTAINERS_IMAGE_REVISION])
            service_label = str(labels.get("com.docker.compose.service") or "")
            embedded_environment = _image_environment(config.get("Env"))
            command = tuple(str(part) for part in (config.get("Entrypoint") or ())) + tuple(
                str(part) for part in (config.get("Cmd") or ())
            )
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise self._production_provenance_failure(
                f"Docker returned incomplete exact-prebuilt metadata for {role}."
            ) from exc
        if _IMAGE_ID.fullmatch(image_id) is None:
            raise self._production_provenance_failure(
                f"Docker inventory does not expose an immutable {role} image ID."
            )
        if _IMAGE_ID.fullmatch(image):
            if image_id != image:
                raise self._production_provenance_failure(
                    f"Docker inventory does not match the exact {role} image ID."
                )
        else:
            expected_manifest = image.rpartition("@")[2]
            descriptor_digest = (
                str(descriptor.get("digest") or "") if isinstance(descriptor, dict) else ""
            )
            repository_digests = {
                reference.rpartition("@")[2] for reference in repo_digests if "@" in reference
            }
            if expected_manifest not in {descriptor_digest, *repository_digests}:
                raise self._production_provenance_failure(
                    f"Docker inventory is not bound to the exact {role} registry manifest."
                )
        if _FULL_GIT_REVISION.fullmatch(revision) is None:
            raise self._production_provenance_failure(
                f"The exact {role} image has no honest immutable revision label."
            )
        # AC17-R91: the label and the baked-in revision can disagree silently. A
        # command-line `--label` replaces the Dockerfile's label while
        # FS_EMBEDDED_IMAGE_REVISION keeps its build-argument default, so an image
        # built without `--build-arg BUILD_SHA` passes this inspection and is then
        # rejected by the runtime, which exits EX_CONFIG and restarts. Compare them
        # here, where the installer can still name both values and stop.
        embedded_revision = embedded_environment.get("FS_EMBEDDED_IMAGE_REVISION", "")
        if embedded_revision and embedded_revision != revision:
            raise self._production_provenance_failure(
                f"The exact {role} image revision label and its embedded revision "
                f"disagree: label {revision!r}, embedded {embedded_revision!r}. "
                "Rebuild with --build-arg BUILD_SHA=<revision> instead of overriding "
                "the label."
            )
        expected_architecture = _normalized_docker_architecture(self._architecture())
        if (
            operating_system != "linux"
            or _normalized_docker_architecture(architecture) != expected_architecture
        ):
            raise self._production_provenance_failure(
                f"The exact {role} image platform does not match this Docker host."
            )
        expected_service = _EXACT_IMAGE_SERVICE_LABELS.get(role)
        if service_label and expected_service is not None and service_label != expected_service:
            raise self._production_provenance_failure(
                f"The exact {role} image carries the wrong existing service label."
            )
        marker = _EXACT_IMAGE_COMMAND_MARKERS[role]
        if marker not in " ".join(command):
            raise self._production_provenance_failure(
                f"The exact {role} image does not expose the expected role entrypoint."
            )
        normalized_architecture = _normalized_docker_architecture(architecture)
        image_platform = f"{operating_system}/{normalized_architecture}"
        content: LocalImageContentIdentity | None = None
        if role == "compact":
            # The local OCI descriptor exists only on the containerd image store. A
            # packaged Docker Engine on overlay2 reports it as null for every image, so
            # requiring it made Compact uninstallable on an ordinary server. The image
            # archive carries the same digests on both stores; bind the inspected image
            # to it instead of depending on how the daemon happens to store images.
            descriptor_digest = (
                str(descriptor.get("digest") or "") if isinstance(descriptor, dict) else ""
            )
            content = self._inspect_compact_image_content(
                image_id=image_id,
                descriptor_digest=descriptor_digest or image_id,
                platform_name=image_platform,
                checked=checked,
            )
            self._verify_compact_image_identity(
                image_id=image_id,
                descriptor_digest=descriptor_digest,
                content=content,
            )
        return _ExactImageIdentity(
            image_id=image_id,
            revision=revision,
            role=expected_service or role,
            platform=image_platform,
            content=content,
        )

    def _verify_compact_image_identity(
        self: DockerInstallerGateway,
        *,
        image_id: str,
        descriptor_digest: str,
        content: LocalImageContentIdentity,
    ) -> None:
        """Bind the inspected image to its archive without an OCI descriptor.

        containerd reports the index digest as the image ID, overlay2 reports the config
        digest. Either binds the inspected image to the archive we just read.
        RepoDigests may name a published parent index absent from a regenerated
        Docker-save archive; registry provenance is verified separately through
        the published platform manifest and its config digest.
        """
        archive_digests = {content.index_digest, content.manifest_digest, content.config_digest}
        if descriptor_digest and descriptor_digest not in archive_digests:
            raise self._production_provenance_failure(
                "The exact compact image descriptor does not match its own image content."
            )
        if image_id in archive_digests:
            return
        raise self._production_provenance_failure(
            "The exact compact image could not be bound to its own content: Docker reports "
            f"image ID {image_id}, while the image archive contains index "
            f"{content.index_digest}, manifest {content.manifest_digest} and config "
            f"{content.config_digest}. Reinstall the exact published image, or report this "
            "Docker image store as unsupported."
        )

    def _inspect_compact_image_content(
        self: DockerInstallerGateway,
        *,
        image_id: str,
        descriptor_digest: str,
        platform_name: str,
        checked: Callable[[Sequence[str]], ProcessOutcome],
    ) -> LocalImageContentIdentity:
        self.state_store.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".compact-image-", suffix=".tar", dir=self.state_store.root
        )
        os.close(descriptor)
        archive_path = Path(temporary_name)
        archive_path.unlink()
        try:
            checked(["docker", "image", "save", "--output", str(archive_path), image_id])
            return resolve_local_image_archive(
                archive_path=archive_path,
                descriptor_digest=descriptor_digest,
                platform=platform_name,
            )
        except ValueError as exc:
            raise self._production_provenance_failure(str(exc)) from exc
        finally:
            archive_path.unlink(missing_ok=True)

    def _compose_default_role_images(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> dict[str, str]:
        """Read back the image tags Compose assigns to each production role.

        Explicit tags come from the rendered service's `image:` field. Development
        build-only services use Compose's project-service tag, derived from the
        rendered project name. Production still requires an explicit image; every
        resolved tag must pass the existing revision and immutable-ID checks.
        """
        outcome = self._checked(
            [*self._compose_prefix(config), "config", "--format", "json"],
            log=log,
            code="core_image_preparation_failed",
            remediation="Resolve the reported Docker Compose failure and resume ./install.sh.",
            exit_code=7,
            step="prepare",
        )
        try:
            document = json.loads(outcome.stdout)
            services = document.get("services") or {}
            project_name = str(document.get("name") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            return {}
        resolved: dict[str, str] = {}
        for role, service_name in _EXACT_IMAGE_SERVICE_LABELS.items():
            service = services.get(service_name)
            if not isinstance(service, dict):
                continue
            image = str(service.get("image") or "").strip()
            if (
                not image
                and config.environment == "development"
                and isinstance(service.get("build"), dict)
                and _COMPOSE_PROJECT_NAME.fullmatch(project_name)
            ):
                image = f"{project_name}-{service_name}"
            if image:
                resolved[role] = image
        return resolved

    def _prepare_production_image_provenance(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        def checked(command: Sequence[str]) -> ProcessOutcome:
            return self._checked(
                command,
                log=log,
                code="core_image_preparation_failed",
                remediation="Resolve the Production image failure and resume ./install.sh.",
                exit_code=7,
                step="prepare",
            )

        env_path = self._env_path(config)
        environment = _read_env(env_path)
        role_images = {
            "app": str(environment.get("FS_APP_IMAGE") or "").strip(),
            "worker": str(environment.get("FS_WORKER_IMAGE") or "").strip(),
            "control": str(environment.get("FS_CONTROL_IMAGE") or "").strip(),
        }
        mcp_image = str(environment.get("MCP_IMAGE") or "").strip()
        # Same R90 shape as the exact-prebuilt path: a git failure reported as a
        # Production image failure sends the operator to the wrong subsystem.
        revision = self._checked(
            ["git", "-C", str(self.repo_root), "rev-parse", _TOKEN_HEAD_COMMIT],
            log=log,
            separate_stderr=True,
            code=_SOURCE_REVISION_FAILURE_CODE,
            remediation=_SOURCE_REVISION_REMEDIATION,
            exit_code=7,
            step="prepare",
        ).stdout.strip()
        if _FULL_GIT_REVISION.fullmatch(revision) is None:
            raise self._production_provenance_failure(
                "The installer could not resolve the full Git revision for Production images."
            )

        persisted_signal = self._has_persisted_production_provenance(
            config, environment, revision, role_images, mcp_image
        )
        if persisted_signal:
            self._verify_persisted_production_images(
                revision,
                environment,
                role_images,
                mcp_image,
                checked,
            )
            log.write("Reusing durable Production image provenance.")
            return

        if not all(role_images.values()) or not mcp_image:
            # AC17-R95: this used to be the end of the road. The installer had a branch
            # for "reuse what a previous install recorded" and a branch for "refuse",
            # and none for "this machine has never built them" -- so a clean checkout
            # reached here and stopped, even though `docker compose build` had just run
            # immediately above and produced exactly these images under the tags the
            # Compose file declares. Ask Compose what it tagged rather than guessing.
            resolved = self._compose_default_role_images(config, log=log)
            role_images = {
                role: role_images[role] or resolved.get(role, "") for role in role_images
            }
            mcp_image = mcp_image or resolved.get("mcp", "")
        if not all(role_images.values()) or not mcp_image:
            missing = sorted(
                _EXACT_IMAGE_KEYS[role]
                for role, image in {**role_images, "mcp": mcp_image}.items()
                if not image
            )
            raise self._production_provenance_failure(
                "Production image tags are missing from the installer environment and "
                f"could not be resolved from the Compose definition: {', '.join(missing)}."
            )

        role_image_ids = {
            role: self._inspect_production_image(image, revision, checked)
            for role, image in role_images.items()
        }
        mcp_image_id = self._inspect_production_image(mcp_image, revision, checked)

        self.state_store.update_env_values(
            env_path,
            {
                "FS_BUILD_SHA": revision,
                "FS_APP_IMAGE": role_image_ids["app"],
                "FS_APP_IMAGE_DIGEST": role_image_ids["app"],
                "FS_APP_IMAGE_REVISION": revision,
                "FS_WORKER_IMAGE": role_image_ids["worker"],
                "FS_WORKER_IMAGE_DIGEST": role_image_ids["worker"],
                "FS_WORKER_IMAGE_REVISION": revision,
                "FS_CONTROL_IMAGE": role_image_ids["control"],
                "FS_CONTROL_IMAGE_DIGEST": role_image_ids["control"],
                "FS_CONTROL_IMAGE_REVISION": revision,
                "MCP_IMAGE": mcp_image_id,
                "MCP_IMAGE_DIGEST": mcp_image_id,
                "MCP_IMAGE_REVISION": revision,
            },
        )
        log.write("Durable Production image provenance is ready.")

    @staticmethod
    def _has_persisted_production_provenance(
        config: InstallConfig,
        environment: Mapping[str, str],
        revision: str,
        role_images: Mapping[str, str],
        mcp_image: str,
    ) -> bool:
        persisted_revision = str(environment.get("FS_BUILD_SHA") or "").strip()
        role_digests = {
            "app": str(environment.get("FS_APP_IMAGE_DIGEST") or "").strip(),
            "worker": str(environment.get("FS_WORKER_IMAGE_DIGEST") or "").strip(),
            "control": str(environment.get("FS_CONTROL_IMAGE_DIGEST") or "").strip(),
        }
        mcp_digest = str(environment.get("MCP_IMAGE_DIGEST") or "").strip()
        development_revision_seed = bool(
            config.environment == "development"
            and persisted_revision == revision
            and not any(role_digests.values())
            and not mcp_digest
            and not any(image.startswith(_TOKEN_SHA256) for image in role_images.values())
            and not mcp_image.startswith(_TOKEN_SHA256)
        )
        return not development_revision_seed and bool(
            persisted_revision
            or any(role_digests.values())
            or mcp_digest
            or any(image.startswith(_TOKEN_SHA256) for image in role_images.values())
            or mcp_image.startswith(_TOKEN_SHA256)
        )

    def _verify_persisted_production_images(
        self: DockerInstallerGateway,
        revision: str,
        environment: Mapping[str, str],
        role_images: Mapping[str, str],
        mcp_image: str,
        checked: Callable[[Sequence[str]], ProcessOutcome],
    ) -> None:
        persisted_revision = str(environment.get("FS_BUILD_SHA") or "").strip()
        role_digests = {
            role: str(environment.get(_EXACT_IMAGE_DIGEST_KEYS[role]) or "").strip()
            for role in role_images
        }
        role_revisions = {
            role: str(environment.get(_EXACT_IMAGE_REVISION_KEYS[role]) or "").strip()
            for role in role_images
        }
        mcp_digest = str(environment.get("MCP_IMAGE_DIGEST") or "").strip()
        mcp_revision = str(environment.get("MCP_IMAGE_REVISION") or "").strip()
        complete = (
            persisted_revision == revision
            and all(role_images[role] == role_digests[role] for role in role_images)
            and mcp_image == mcp_digest
            and all(role_revisions[role] == revision for role in role_images)
            and mcp_revision == revision
            and all(_IMAGE_ID.fullmatch(image) is not None for image in role_images.values())
            and _IMAGE_ID.fullmatch(mcp_image) is not None
        )
        if not complete:
            raise self._production_provenance_failure(
                "Persisted Production image provenance is incomplete or stale."
            )
        inspected_role_ids = {
            role: self._inspect_production_image(image, revision, checked)
            for role, image in role_images.items()
        }
        inspected_mcp_id = self._inspect_production_image(mcp_image, revision, checked)
        if inspected_role_ids != role_images or inspected_mcp_id != mcp_image:
            raise self._production_provenance_failure(
                "Persisted Production image IDs do not match Docker inventory."
            )

    def _inspect_production_image(
        self: DockerInstallerGateway,
        image: str,
        revision: str,
        checked: Callable[[Sequence[str]], ProcessOutcome],
    ) -> str:
        outcome = checked(["docker", "image", "inspect", image])
        try:
            payload = json.loads(outcome.stdout)
            details = payload[0]
            image_id = details["Id"]
            labels = details["Config"]["Labels"]
            image_revision = labels[_TOKEN_ORG_OPENCONTAINERS_IMAGE_REVISION]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise self._production_provenance_failure(
                "Docker returned incomplete Production image identity metadata."
            ) from exc
        if _IMAGE_ID.fullmatch(str(image_id)) is None:
            raise self._production_provenance_failure(
                "Docker returned an invalid Production image ID."
            )
        if image_revision != revision:
            raise self._production_provenance_failure(
                "The Production image revision label does not match the current Git revision."
            )
        return str(image_id)

    def _production_provenance_postcondition_holds(
        self: DockerInstallerGateway, config: InstallConfig
    ) -> bool:
        environment = _read_env(self._env_path(config))
        revision = str(environment.get("FS_BUILD_SHA") or "").strip()
        try:
            exact_refs = self._complete_exact_prebuilt_image_refs(config, environment)
        except InstallerFailure:
            return False
        if exact_refs is not None:
            return self._exact_provenance_postcondition_holds(exact_refs, environment, revision)

        role_images = {
            "app": str(environment.get("FS_APP_IMAGE") or "").strip(),
            "worker": str(environment.get("FS_WORKER_IMAGE") or "").strip(),
            "control": str(environment.get("FS_CONTROL_IMAGE") or "").strip(),
        }
        role_digests = {
            "app": str(environment.get("FS_APP_IMAGE_DIGEST") or "").strip(),
            "worker": str(environment.get("FS_WORKER_IMAGE_DIGEST") or "").strip(),
            "control": str(environment.get("FS_CONTROL_IMAGE_DIGEST") or "").strip(),
        }
        mcp_image = str(environment.get("MCP_IMAGE") or "").strip()
        mcp_digest = str(environment.get("MCP_IMAGE_DIGEST") or "").strip()
        if not (
            _FULL_GIT_REVISION.fullmatch(revision)
            and all(role_images[role] == role_digests[role] for role in role_images)
            and all(
                environment.get(_EXACT_IMAGE_REVISION_KEYS[role]) == revision
                for role in role_images
            )
            and mcp_image == mcp_digest
            and environment.get("MCP_IMAGE_REVISION") == revision
            and all(_IMAGE_ID.fullmatch(image) for image in role_images.values())
            and _IMAGE_ID.fullmatch(mcp_image)
        ):
            return False
        revision_outcome = self.process.run(
            ["git", "-C", str(self.repo_root), "rev-parse", _TOKEN_HEAD_COMMIT],
            cwd=self.repo_root,
            separate_stderr=True,
        )
        if revision_outcome.returncode != 0 or revision_outcome.stdout.strip() != revision:
            return False
        return all(
            self._production_image_matches(image, revision)
            for image in (*role_images.values(), mcp_image)
        )

    def _exact_provenance_postcondition_holds(
        self: DockerInstallerGateway,
        exact_refs: Mapping[str, str],
        environment: Mapping[str, str],
        revision: str,
    ) -> bool:
        revision_outcome = self.process.run(
            ["git", "-C", str(self.repo_root), "rev-parse", _TOKEN_HEAD_COMMIT],
            cwd=self.repo_root,
            separate_stderr=True,
        )
        if revision_outcome.returncode != 0 or revision_outcome.stdout.strip() != revision:
            return False

        def checked(command: Sequence[str]) -> ProcessOutcome:
            outcome = self.process.run(command, cwd=self.repo_root)
            if outcome.returncode != 0:
                raise self._production_provenance_failure(
                    "An exact-prebuilt image is unavailable from Docker inventory."
                )
            return outcome

        try:
            for role, image in exact_refs.items():
                digest_key = _EXACT_IMAGE_DIGEST_KEYS.get(role)
                if (
                    digest_key is not None
                    and _IMAGE_ID.fullmatch(image)
                    and environment.get(digest_key) != image
                ):
                    return False
                identity = self._inspect_exact_prebuilt_image(role, image, checked)
                if environment.get(_EXACT_IMAGE_REVISION_KEYS[role]) != identity.revision:
                    return False
        except InstallerFailure:
            return False
        return True

    def _production_image_matches(self: DockerInstallerGateway, image: str, revision: str) -> bool:
        outcome = self.process.run(
            ["docker", "image", "inspect", image],
            cwd=self.repo_root,
        )
        if outcome.returncode != 0:
            return False
        try:
            details = json.loads(outcome.stdout)[0]
            if details["Id"] != image:
                return False
            if details["Config"]["Labels"][_TOKEN_ORG_OPENCONTAINERS_IMAGE_REVISION] != revision:
                return False
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return False
        return True

    @staticmethod
    def _production_provenance_failure(cause: str) -> InstallerFailure:
        return InstallerFailure(
            code="core_image_preparation_failed",
            cause=cause,
            remediation="Restore exact Production image provenance and resume ./install.sh.",
            exit_code=7,
            retryable=True,
            step="prepare",
        )

    @staticmethod
    def _source_provenance_failure(cause: str) -> InstallerFailure:
        return InstallerFailure(
            code="source_installation_provenance_invalid",
            cause=cause,
            remediation="Repair the completed source installation images and rerun the command.",
            exit_code=8,
            retryable=False,
            step="release_state",
        )
