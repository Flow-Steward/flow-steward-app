"""ImageSelection technical operations for the installer adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from core.application.installer.contracts import (
    ImageCurrency,
    InstallConfig,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerLog
from core.application.lifecycle.contracts import (
    DeploymentConfig,
)
from core.application.release.contracts import ReleaseIdentity
from core.application.release.images import required_product_roles
from core.infrastructure.installer import registry_image_resolver
from core.infrastructure.installer.gateway_support import (
    _COMPACT_DOCKERFILE,
    _COMPACT_IMAGE_INPUT_NAMES,
    _EXACT_IMAGE_DIGEST_KEYS,
    _EXACT_IMAGE_KEYS,
    _IMAGE_ID,
    _RUNTIME_IMAGES,
    _SOURCE_REVISION_FAILURE_CODE,
    _SOURCE_REVISION_REMEDIATION,
    _TOKEN_HEAD_COMMIT,
    _immutable_image_ref,
    _read_env,
)
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.registry_image_resolver import (
    RegistryImageIdentity,
    is_digest_pinned_reference,
)

if TYPE_CHECKING:
    from core.infrastructure.installer.gateway import DockerInstallerGateway


#: The digests a persisted Compact image ID may legitimately equal. The classic
#: Docker image store reports the config digest as the image ID; the containerd
#: image store (Docker Desktop's default) reports the digest of the descriptor it
#: stored, which is the OCI index or the manifest. The installer itself persists
#: all three beside FS_COMPACT_IMAGE, so the selection accepts the ID when it is
#: any one of them and refuses it only when it matches none.
_COMPACT_IMAGE_IDENTITY_DIGEST_KEYS = (
    "FS_COMPACT_IMAGE_CONFIG_DIGEST",
    "FS_COMPACT_IMAGE_MANIFEST_DIGEST",
    "FS_COMPACT_IMAGE_INDEX_DIGEST",
)


def _compact_image_id_matches_digests(environment: Mapping[str, str], image_id: str) -> bool:
    """Whether a local Compact image ID agrees with the digests recorded beside it."""
    recorded = {
        str(environment.get(key) or "").strip() for key in _COMPACT_IMAGE_IDENTITY_DIGEST_KEYS
    } - {""}
    return not recorded or image_id in recorded


class ImageSelectionOperations:
    def _exact_prebuilt_image_refs(
        self: DockerInstallerGateway,
        config: DeploymentConfig,
        environment: Mapping[str, str],
    ) -> dict[str, str] | None:
        refs = self._complete_exact_prebuilt_image_refs(config, environment)
        if refs is not None:
            return refs
        required_roles = self._required_exact_roles(config)
        selected = {
            role: str(environment.get(key) or "").strip() for role, key in _EXACT_IMAGE_KEYS.items()
        }
        exact_roles = {role for role, image in selected.items() if _immutable_image_ref(image)}
        if not exact_roles:
            return None
        if any(role not in exact_roles for role in required_roles):
            raise InstallerFailure(
                code="invalid_exact_image_selection",
                cause=(
                    "Exact-prebuilt installation requires a complete immutable image set; "
                    "mixed, partial, or tagged selections are not allowed."
                ),
                remediation=(
                    "Provide digest-pinned registry references or local sha256 IDs for app, "
                    "worker, control, MCP, CLI, and the Development frontend when applicable."
                ),
                exit_code=7,
                retryable=False,
                step="preflight",
            )
        if (
            config.deployment_profile == "scalable"
            and selected["frontend"]
            and "frontend" not in exact_roles
        ):
            raise InstallerFailure(
                code="invalid_exact_image_selection",
                cause="The optional frontend image must also be immutable.",
                remediation="Pin FS_FRONTEND_IMAGE by registry digest or local sha256 ID.",
                exit_code=7,
                retryable=False,
                step="preflight",
            )
        for role, digest_key in _EXACT_IMAGE_DIGEST_KEYS.items():
            if role not in required_roles:
                continue
            if role == "compact":
                if _IMAGE_ID.fullmatch(selected[role]) and not _compact_image_id_matches_digests(
                    environment, selected[role]
                ):
                    raise InstallerFailure(
                        code="invalid_exact_image_selection",
                        cause=(
                            "FS_COMPACT_IMAGE matches none of the config, manifest or index "
                            "digests recorded for it."
                        ),
                        remediation="Restore one exact image ID for each image and digest pair.",
                        exit_code=7,
                        retryable=False,
                        step="preflight",
                    )
                continue
            digest = str(environment.get(digest_key) or "").strip()
            if digest and _IMAGE_ID.fullmatch(selected[role]) and digest != selected[role]:
                raise InstallerFailure(
                    code="invalid_exact_image_selection",
                    cause=f"{digest_key} does not match the selected immutable image ID.",
                    remediation="Restore one exact image ID for each image and digest pair.",
                    exit_code=7,
                    retryable=False,
                    step="preflight",
                )
        raise InstallerFailure(
            code="invalid_exact_image_selection",
            cause="The exact-prebuilt image selection is invalid.",
            remediation="Restore a complete immutable image selection.",
            exit_code=7,
            retryable=False,
            step="preflight",
        )

    def _complete_exact_prebuilt_image_refs(
        self: DockerInstallerGateway,
        config: DeploymentConfig,
        environment: Mapping[str, str],
    ) -> dict[str, str] | None:
        required_roles = self._required_exact_roles(config)
        selected = {
            role: str(environment.get(key) or "").strip() for role, key in _EXACT_IMAGE_KEYS.items()
        }
        if any(not _immutable_image_ref(selected[role]) for role in required_roles):
            return None
        if (
            config.deployment_profile == "scalable"
            and selected["frontend"]
            and not _immutable_image_ref(selected["frontend"])
        ):
            return None
        refs = {role: selected[role] for role in required_roles}
        if config.deployment_profile == "compact":
            for role in ("cli", "mcp"):
                if selected[role]:
                    if not _immutable_image_ref(selected[role]):
                        # The Compact compose file runs FS_CLI_IMAGE but never
                        # MCP_IMAGE (the MCP server runs inside the Compact image).
                        # A tag left in .env.prod by an earlier Scalable install is
                        # therefore inert here and must not void the selection.
                        if role == "mcp":
                            continue
                        return None
                    refs[role] = selected[role]
        if (
            config.deployment_profile == "scalable"
            and selected["frontend"]
            and "frontend" not in refs
        ):
            refs["frontend"] = selected["frontend"]
        for role, digest_key in _EXACT_IMAGE_DIGEST_KEYS.items():
            if role not in refs:
                continue
            if role == "compact":
                if _IMAGE_ID.fullmatch(refs[role]) and not _compact_image_id_matches_digests(
                    environment, refs[role]
                ):
                    return None
                continue
            digest = str(environment.get(digest_key) or "").strip()
            if digest and _IMAGE_ID.fullmatch(refs[role]) and digest != refs[role]:
                return None
        return refs

    def _compact_image_input(self: DockerInstallerGateway, config: DeploymentConfig) -> str:
        """Return the operator-supplied Compact image reference, from either source.

        The process environment is what ./install.sh forwards on a first run; the env file
        is what a resume reads back after the first run persisted it. A clean tree has
        neither until the operator supplies one.
        """
        persisted = _read_env(self._env_path(config))
        for name in _COMPACT_IMAGE_INPUT_NAMES:
            value = str(self._environment.get(name) or persisted.get(name) or "").strip()
            if value:
                return value
        return ""

    def _require_compact_image_input(self: DockerInstallerGateway, config: InstallConfig) -> None:
        """Prove a Compact install will have an image, before anything is written.

        AC17-R81 made this refuse when no image reference was supplied, because
        `compact-runtime` in docker-compose.compact.yaml declares `image:` and no
        `build:`: the profile consumed an immutable prebuilt image and nothing said
        so before the run started, so the installer wrote .env.prod, the durable
        secrets and a certificate and only then failed inside Compose with a raw
        interpolation error.

        AC17-R92 removes the premise, by the owner's decision: most operators clone
        the repository and will never hold a prebuilt image, so an install that
        cannot proceed without one fails at the first step. The installer now builds
        the runtime image itself when none is supplied. Do not restore the old
        "prebuilt only" intent here -- it was deliberately withdrawn, not overlooked.

        What still has to hold before the first write is that a build is possible at
        all. An unbuildable tree must fail here, not after a certificate is issued.
        """
        if config.deployment_profile != "compact":
            return
        supplied = self._compact_image_input(config)
        if supplied:
            self._require_pinned_compact_image(supplied)
            return
        dockerfile = self.repo_root / _COMPACT_DOCKERFILE
        if not dockerfile.is_file():
            raise InstallerFailure(
                code="compact_image_unbuildable",
                cause=(
                    "No Compact runtime image was supplied and this tree cannot build one: "
                    f"{_COMPACT_DOCKERFILE} is missing."
                ),
                remediation=(
                    "Install from a complete checkout of the repository, or supply a built "
                    "image with FS_COMPACT_IMAGE=<local image ID>, then rerun ./install.sh."
                ),
                exit_code=3,
                retryable=True,
                step="preflight",
            )

    @staticmethod
    def _require_pinned_compact_image(supplied: str) -> None:
        """Refuse a supplied Compact image that nothing downstream can accept.

        AC17-R139. Two shapes are supported, both documented in
        compact-scalable-deployment.md: a local image ID, which the exact-prebuilt path
        verifies, and a registry reference pinned by digest, which
        _resolve_compact_registry_image resolves. A tag is neither. It used to pass
        here silently, skip the resolution that writes FS_COMPACT_IMAGE_REVISION and
        the rest of the image identity, and surface minutes later -- after
        configuration, secrets and a certificate were written -- as a raw Compose
        interpolation error about a missing variable. That sent the operator to add a
        variable by hand, which would have produced an install of unverified origin.

        The installer's own records are always one of the two shapes -- a self-build
        records its image ID, a registry resolution records both -- so a resume is
        never refused by this.
        """
        if _IMAGE_ID.fullmatch(supplied) or is_digest_pinned_reference(supplied):
            return
        raise InstallerFailure(
            code="compact_image_not_pinned",
            cause=(
                f"The supplied Compact image {supplied!r} is not pinned. A tag can be "
                "moved after it is checked, so the installer cannot establish which "
                "image it would be installing."
            ),
            remediation=(
                "Supply the image by its local ID (FS_COMPACT_IMAGE=sha256:<id>, from "
                "`docker image inspect --format '{{.Id}}' <image>`) or as a registry "
                "reference pinned by digest (<repository>@sha256:<digest>), or supply "
                "none and the installer builds the image itself. Then rerun ./install.sh."
            ),
            exit_code=3,
            retryable=True,
            step="preflight",
        )

    @staticmethod
    def _required_exact_roles(config: DeploymentConfig) -> list[str]:
        if config.deployment_profile == "compact":
            return ["compact"]
        roles = ["app", "worker", "control", "mcp", "cli"]
        if config.environment == "development":
            roles.append("frontend")
        return roles

    def _install_mode(self: DockerInstallerGateway, config: DeploymentConfig) -> str:
        if config.environment != "production":
            return "source"
        # An official activation journal/identity must never fall back to a build.
        if (
            self.state_store.load_installed_release() is not None
            or self.state_store.load_pending_release() is not None
            or self.state_store.load_release_handoff() is not None
        ):
            return "official"
        environment = {**_read_env(self._env_path(config)), **self._environment}
        return "source" if environment.get("FS_INSTALL_MODE") == "source" else "official"

    def _official_pending_release(
        self: DockerInstallerGateway, config: DeploymentConfig
    ) -> ReleaseIdentity | None:
        if self._install_mode(config) == "source":
            return None
        release = self.state_store.load_pending_release()
        if release is None:
            raise InstallerFailure(
                code="official_release_missing",
                cause="Production installation requires a resolved official release identity.",
                remediation="Restore GitHub and registry access, then rerun ./install.sh.",
                exit_code=7,
                retryable=True,
                step="preflight",
            )
        expected_roles = {role.role for role in required_product_roles(config.deployment_profile)}
        actual_roles = {image.role for image in release.images}
        if actual_roles != expected_roles:
            raise InstallerFailure(
                code="official_release_incomplete",
                cause="The pending official release does not contain the exact product role set.",
                remediation="Resolve the complete release from one official registry and retry.",
                exit_code=7,
                retryable=False,
                step="preflight",
            )
        architecture = self._architecture().lower()
        platform_name = {
            "amd64": "linux/amd64",
            "x86_64": "linux/amd64",
            "arm64": "linux/arm64",
            "aarch64": "linux/arm64",
        }.get(architecture)
        if platform_name != release.platform:
            raise InstallerFailure(
                code="official_release_platform_mismatch",
                cause="The pending official release does not match the installer host platform.",
                remediation="Resolve the release again on the target host.",
                exit_code=7,
                retryable=False,
                step="preflight",
            )
        return release

    def _resolve_compact_registry_image(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> RegistryImageIdentity | None:
        if config.deployment_profile != "compact":
            return None
        env_path = self._env_path(config)
        environment = _read_env(env_path)
        registry_ref = str(
            environment.get("FS_COMPACT_IMAGE_REGISTRY_REF")
            or environment.get("FS_COMPACT_IMAGE")
            or ""
        ).strip()
        if "@sha256:" not in registry_ref:
            return None
        revision = str(environment.get("FS_COMPACT_IMAGE_REVISION") or "").strip()
        platform_name = str(environment.get("FS_COMPACT_IMAGE_PLATFORM") or "").strip()
        persisted_parent = str(environment.get("FS_COMPACT_IMAGE_INDEX_DIGEST") or "").strip()
        requested_ref = registry_ref
        if persisted_parent:
            if _IMAGE_ID.fullmatch(persisted_parent) is None:
                raise self._production_provenance_failure(
                    "Persisted Compact registry parent index digest is invalid."
                )
            # Resolution persists a platform reference, not the original parent.
            # Resume must reverify the parent bytes rather than demote provenance
            # to that platform manifest or blindly copy the recorded parent.
            requested_ref = f"{registry_ref.rpartition('@')[0]}@{persisted_parent}"

        def run(command: Sequence[str]) -> ProcessOutcome:
            return self._checked(
                command,
                log=log,
                code="core_image_preparation_failed",
                remediation="Restore the digest-pinned Compact image and resume ./install.sh.",
                exit_code=7,
                step="prepare",
            )

        try:
            identity = registry_image_resolver.resolve_digest_pinned_image(
                registry_ref=requested_ref,
                platform=platform_name,
                expected_revision=revision,
                expected_role="compact-runtime",
                run=run,
            )
        except ValueError as exc:
            raise self._production_provenance_failure(str(exc)) from exc
        if persisted_parent:
            expected_values = {
                "FS_COMPACT_IMAGE_INDEX_DIGEST": identity.index_digest,
                "FS_COMPACT_IMAGE_MANIFEST_DIGEST": identity.manifest_digest,
                "FS_COMPACT_IMAGE_CONFIG_DIGEST": identity.config_digest,
                "FS_COMPACT_IMAGE_REVISION": identity.revision,
                "FS_COMPACT_IMAGE_ROLE": identity.role,
                "FS_COMPACT_IMAGE_PLATFORM": identity.platform,
            }
            for key, expected in expected_values.items():
                persisted = str(environment.get(key) or "").strip()
                if persisted and persisted != expected:
                    raise self._production_provenance_failure(
                        f"Persisted {key} does not match the validated Compact registry identity."
                    )
            if registry_ref not in {requested_ref, identity.registry_ref}:
                raise self._production_provenance_failure(
                    "Persisted Compact registry reference does not match its validated parent."
                )
        self.state_store.update_env_values(
            env_path,
            {
                "FS_COMPACT_IMAGE": identity.local_image_id,
                "FS_COMPACT_IMAGE_REGISTRY_REF": identity.registry_ref,
                "FS_COMPACT_IMAGE_INDEX_DIGEST": identity.index_digest,
                "FS_COMPACT_IMAGE_MANIFEST_DIGEST": identity.manifest_digest,
                "FS_COMPACT_IMAGE_CONFIG_DIGEST": identity.config_digest,
                "FS_COMPACT_IMAGE_REVISION": identity.revision,
                "FS_COMPACT_IMAGE_ROLE": identity.role,
                "FS_COMPACT_IMAGE_PLATFORM": identity.platform,
            },
        )
        log.write(
            "Resolved digest-pinned Compact image "
            f"index={identity.index_digest} manifest={identity.manifest_digest} "
            f"config={identity.config_digest}."
        )
        return identity

    def _recorded_compact_image_revision(
        self: DockerInstallerGateway, config: InstallConfig
    ) -> str:
        """The source revision a previously recorded Compact image was built from."""
        if config.deployment_profile != "compact":
            return ""
        if any(
            str(self._environment.get(name) or "").strip() for name in _COMPACT_IMAGE_INPUT_NAMES
        ):
            return ""
        persisted = _read_env(self._env_path(config))
        if not any(str(persisted.get(name) or "").strip() for name in _COMPACT_IMAGE_INPUT_NAMES):
            return ""
        recorded = str(persisted.get("FS_COMPACT_IMAGE_REVISION") or "").strip()
        # The env file is not the only place that knows: the image carries
        # org.opencontainers.image.revision from build time. See
        # _stale_persisted_compact_image for why that matters.
        return recorded or self._compact_image_label_revision(config)

    def _stale_persisted_compact_image(
        self: DockerInstallerGateway, config: InstallConfig, source_revision: str
    ) -> str:
        """The revision of a recorded Compact image that no longer matches these sources.

        AC17-R102. `./install.sh` on an existing installation reported
        `"status": "installed"` and kept running the image from the previous install,
        with no build step in the journal. The cause is that a reference written by the
        last run is read back the same way as one the operator supplied, so the branch
        meaning "the operator brought their own image" was taken by the installer's own
        bookkeeping. An update that closes a dependency vulnerability would be reported
        as applied and would not be, which is why this is not merely a stale-cache
        annoyance.

        An image named in the process environment is still the operator's choice and is
        left alone. A reference only the env file knows is compared against the source
        revision it was recorded with.

        An image the env file records without a revision is not undatable: the image
        itself carries org.opencontainers.image.revision, baked in at build time, and
        that is asked for instead. AC17-R109 is the same realisation from the other
        side -- we were asking the environment about something the artifact already
        knows. The locally pinned exact-prebuilt path records digests and no revision,
        so it relies on this rather than being rebuilt over.

        An image with neither a recorded revision nor a revision label is left alone.
        A recorded source installation can rebuild its missing local image even at
        the same revision; registry bindings and explicit process pins remain protected.
        """
        recorded = self._recorded_compact_image_revision(config)
        if not recorded:
            return ""
        if recorded == source_revision:
            persisted = _read_env(self._env_path(config))
            if (
                persisted.get("FS_INSTALL_MODE") == "source"
                and not str(persisted.get("FS_COMPACT_IMAGE_REGISTRY_REF") or "").strip()
                and self._absent_image_ids(persisted, ("FS_COMPACT_IMAGE",))
            ):
                return recorded
            return ""
        return recorded

    def image_currency(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> ImageCurrency:
        """Compare the sources, what was recorded, and what is actually running.

        AC17-R141. Three different questions, and the journal answers none of them:
        a completed journal is exactly what a stale installation has too.

        - Did this installer build the recorded images, from older sources? Then an
          update is due, and the image build decides what to rebuild.
        - Are the recorded images the ones running? An earlier run can have recorded
          new images and stopped before starting them -- a schema refusal leaves it
          exactly so. Then finishing the update is due, and until it is finished the
          "already installed" path must not run: it recreates the runtime from the
          recorded image, which would put new code on an unmigrated database.
        - What is running, for the operator's screen.

        Images an operator chose are not this installer's to replace, so neither
        question can ask for their update; the running revision still reaches the
        screen so a stale pin is named rather than reported as current.
        """
        if config.environment != "production" or config.deployment_profile not in _RUNTIME_IMAGES:
            return ImageCurrency(source_revision="")
        source = self._source_revision(log, step="verification")
        running = self._running_runtime_revision(config)
        if config.deployment_profile == "compact":
            behind = self._stale_persisted_compact_image(config, source)
        else:
            behind = self._stale_scalable_images(config, source)
        if behind:
            return ImageCurrency(
                source_revision=source,
                # What is running, and only that. Falling back to the recorded revision
                # reported a revision that had never run, after an interrupted update.
                running_revision=running,
                update_required=True,
                reason=(
                    "The recorded source Compact image is no longer available locally. "
                    "Preparing its replacement rather than recreating from a missing image."
                    if behind == source
                    else f"The images this installer built were built from {behind}, and these "
                    f"sources are at {source}. Updating: reporting this installation as "
                    "current would leave the old code running."
                ),
            )
        unapplied = self._recorded_images_not_running(config)
        if unapplied:
            return ImageCurrency(
                source_revision=source,
                running_revision=running,
                update_required=True,
                resumes_prepared_update=True,
                reason=(
                    "An earlier run recorded images that are not running yet "
                    f"({', '.join(unapplied)}). Finishing that update rather than "
                    "restarting the runtime around it."
                ),
            )
        return ImageCurrency(
            source_revision=source,
            running_revision=running,
            shell_pins=self._shell_image_pins(config),
        )

    def _shell_image_pins(self: DockerInstallerGateway, config: InstallConfig) -> tuple[str, ...]:
        """The image variables the process environment -- the operator's shell -- sets."""
        if config.deployment_profile == "compact":
            return tuple(
                name
                for name in _COMPACT_IMAGE_INPUT_NAMES
                if str(self._environment.get(name) or "").strip()
            )
        return tuple(
            name for name in self._SCALABLE_IMAGE_NAMES if self._is_chosen(name, self._environment)
        )

    def _source_revision(self: DockerInstallerGateway, log: InstallerLog, *, step: str) -> str:
        return self._checked(
            ["git", "-C", str(self.repo_root), "rev-parse", _TOKEN_HEAD_COMMIT],
            log=log,
            separate_stderr=True,
            code=_SOURCE_REVISION_FAILURE_CODE,
            remediation=_SOURCE_REVISION_REMEDIATION,
            exit_code=7,
            step=step,
        ).stdout.strip()

    def _running_image_id(self: DockerInstallerGateway, config: InstallConfig, service: str) -> str:
        """The exact image ID the one running container of a service was created from.

        Asked of Docker by the labels Compose puts on its containers, not through
        `docker compose ps`. AC17-R141 acceptance: after an interrupted update the env
        file can lack a value the Compact compose file declares with `:?`, and Compose
        then refuses to parse the file at all -- so the question "what is running"
        went unanswered in exactly the state where the answer matters most, and the
        result reported the recorded revision as the one updated from. One-off
        `compose run` containers carry the same service label and are excluded.
        """
        del config
        container_ids = self._running_container_ids(service)
        if not container_ids:
            return ""
        # Review P2: Scalable workers may run as several replicas. They answer with one
        # image when they share it -- which is the only state an update can reason about.
        inspected = self.process.run(
            ["docker", "container", "inspect", "--format", "{{.Image}}", *container_ids]
        )
        images = {line.strip() for line in inspected.stdout.splitlines() if line.strip()}
        if inspected.returncode != 0 or len(images) != 1:
            return ""
        image = images.pop()
        return image if _IMAGE_ID.fullmatch(image) else ""

    def _running_container_ids(
        self: DockerInstallerGateway, service: str, *, stopped_too: bool = False
    ) -> list[str]:
        """The running (or all) non-one-off containers of a service, asked by label."""
        listed = self.process.run(
            [
                "docker",
                "ps",
                *(("-a",) if stopped_too else ()),
                "-q",
                "--filter",
                f"label=com.docker.compose.project={self._project_identity()}",
                "--filter",
                f"label=com.docker.compose.service={service}",
                "--filter",
                "label=com.docker.compose.oneoff=False",
            ]
        )
        if listed.returncode != 0:
            return []
        return [line.strip() for line in listed.stdout.splitlines() if line.strip()]
