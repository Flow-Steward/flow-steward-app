"""ImageBuild technical operations for the installer adapter."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from core.application.installer.contracts import (
    InstallConfig,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerLog
from core.infrastructure.installer import release_retention
from core.infrastructure.installer.gateway_support import (
    _COMPACT_BUILD_TARGET,
    _COMPACT_BUILD_TIMEOUT_SECONDS,
    _COMPACT_DOCKERFILE,
    _COMPACT_IMAGE_PIN_NAMES,
    _EXACT_IMAGE_DIGEST_KEYS,
    _EXACT_IMAGE_REVISION_KEYS,
    _FORMAT_IMAGE_ID,
    _FULL_GIT_REVISION,
    _IMAGE_ID,
    _INGRESS_OWNED_TLS_MODES,
    _PATH_CANDIDATE_ENV,
    _PATH_RELEASE_RECORDS,
    _REMEDIATION_INFRASTRUCTURE_IMAGE,
    _RUNTIME_IMAGES,
    _SOURCE_REVISION_FAILURE_CODE,
    _SOURCE_REVISION_REMEDIATION,
    _TOKEN_HEAD_COMMIT,
    _last_output_line,
    _normalized_docker_architecture,
    _read_env,
)

if TYPE_CHECKING:
    from core.infrastructure.installer.gateway import DockerInstallerGateway


class ImageBuildOperations:
    _COMPACT_CLI_IMAGE_RECORD = "compact-cli-image.json"

    def _compact_cli_owned_identity(
        self: DockerInstallerGateway, values: Mapping[str, str]
    ) -> dict[str, str] | None:
        """Match the selected image against schema 1's bounded ownership tuples.

        Unlike an image label, this owner-only installer record survives the image
        being removed from Docker. A foreign or malformed record grants no right
        to replace a selected image.
        """
        try:
            record = json.loads(
                (self.state_store.root / self._COMPACT_CLI_IMAGE_RECORD).read_text("utf-8")
            )
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        if (
            record.get("schema_version") != 1
            or record.get("project_identity") != self._project_identity()
        ):
            return None
        for candidate in (record, record.get("previous")):
            if not isinstance(candidate, dict):
                continue
            revision = str(candidate.get("revision") or "")
            image_id = str(candidate.get("image_id") or "")
            if (
                candidate.get("project_identity") == self._project_identity()
                and image_id == values.get("FS_CLI_IMAGE")
                and _IMAGE_ID.fullmatch(image_id) is not None
                and _FULL_GIT_REVISION.fullmatch(revision) is not None
                and revision == values.get("FS_CLI_IMAGE_REVISION")
            ):
                return {
                    "image_id": image_id,
                    "revision": revision,
                    "project_identity": self._project_identity(),
                }
        return None

    def _compact_cli_owned_revision(self: DockerInstallerGateway, values: Mapping[str, str]) -> str:
        identity = self._compact_cli_owned_identity(values)
        return identity["revision"] if identity is not None else ""

    def _build_compact_management_image(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        """Keep host management tools in the separate CLI image, outside Compact."""
        if config.deployment_profile != "compact" or config.environment != "production":
            return
        env_path = self._env_path(config)
        values = _read_env(env_path)
        if str(self._environment.get("FS_CLI_IMAGE") or "").strip():
            return
        selected = str(values.get("FS_CLI_IMAGE") or "").strip()
        owned_revision = self._compact_cli_owned_revision(values)
        if selected and not owned_revision:
            return
        revision = self._source_revision(log, step="prepare")
        if _FULL_GIT_REVISION.fullmatch(revision) is None:
            raise InstallerFailure(
                code=_SOURCE_REVISION_FAILURE_CODE,
                cause=f"The source revision for the Compact management image is invalid: {revision!r}.",
                remediation=_SOURCE_REVISION_REMEDIATION,
                exit_code=7,
                retryable=False,
                step="prepare",
            )
        if owned_revision == revision and self._production_image_matches(selected, revision):
            log.write(f"Reusing the Compact management image built at {revision[:12]}.")
            return
        tag = f"flow-steward-cli:compact-{self._project_identity()}-{revision[:12]}"
        log.write(
            f"Building separate Compact management image {tag}; Docker and Git "
            "belong to this one-shot image rather than the Compact runtime."
        )
        self._checked(
            [
                "docker",
                "buildx",
                "build",
                "--target",
                "cli",
                "--build-arg",
                f"FS_BUILD_REVISION={revision}",
                "--label",
                f"{release_retention.INSTALLATION_LABEL}={self._project_identity()}",
                "--tag",
                tag,
                "--file",
                str(self.repo_root / _COMPACT_DOCKERFILE),
                "--load",
                str(self.repo_root),
            ],
            log=log,
            code="compact_management_image_build_failed",
            remediation="Resolve the reported CLI image build failure and resume ./install.sh.",
            exit_code=7,
            step="prepare",
            timeout_seconds=_COMPACT_BUILD_TIMEOUT_SECONDS,
        )
        image_id = self._inspect_production_image(
            tag,
            revision,
            lambda command: self._checked(
                command,
                log=log,
                code="compact_management_image_build_failed",
                remediation="Inspect the built CLI image and resume ./install.sh.",
                exit_code=7,
                step="prepare",
            ),
        )
        # Publish ownership before selection. Keep exactly the previous selected
        # owned tuple so a crash before the env write can still recover that image;
        # never retain foreign selections or an unbounded ownership history.
        previous = self._compact_cli_owned_identity(values)
        record = {
            "schema_version": 1,
            "image_id": image_id,
            "revision": revision,
            "project_identity": self._project_identity(),
            **({"previous": previous} if previous is not None else {}),
        }
        self.state_store.write_owner_only_text(
            self.state_store.root / self._COMPACT_CLI_IMAGE_RECORD,
            json.dumps(record) + "\n",
        )
        self.state_store.update_env_values(
            env_path,
            {
                "FS_CLI_IMAGE": image_id,
                "FS_CLI_IMAGE_DIGEST": image_id,
                "FS_CLI_IMAGE_REVISION": revision,
            },
        )
        log.write(f"Recorded Compact management image {image_id} at revision {revision}.")

    def _build_compact_runtime_image(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        """Build the Compact runtime image when the operator supplied none.

        AC17-R92. The build argument handling is the whole point of doing this here
        rather than by adding a `build:` stanza to the Compose file: the
        compact-runtime stage bakes FS_EMBEDDED_IMAGE_REVISION and
        FS_EMBEDDED_IMAGE_PLATFORM from BUILD_SHA and FS_IMAGE_PLATFORM, both of
        which default to "unknown", and FS_IMAGE_PLATFORM is not derived from
        BuildKit's TARGETPLATFORM. An image built without them is accepted by the
        installer and then rejected by the runtime with EX_CONFIG (AC17-R91). A
        Compose `build:` would reintroduce exactly that.

        The result is fed back as FS_COMPACT_IMAGE, so the existing exact-prebuilt
        provenance path -- including the embedded identity and label checks -- runs
        over this image unchanged.
        """
        if config.deployment_profile != "compact":
            return
        supplied = self._compact_image_input(config)
        recorded = self._recorded_compact_image_revision(config)
        if supplied and not recorded:
            # Nothing to compare against; see _stale_persisted_compact_image for why
            # that is left alone. Resolving the source revision here would also make
            # every resume run git for an answer it is not going to use.
            return
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
            raise InstallerFailure(
                code=_SOURCE_REVISION_FAILURE_CODE,
                cause=(
                    "The installer could not resolve a full source revision to stamp into "
                    f"the Compact image; git reported {revision!r}."
                ),
                remediation=_SOURCE_REVISION_REMEDIATION,
                exit_code=7,
                retryable=False,
                step="prepare",
            )
        stale_revision = self._stale_persisted_compact_image(config, revision)
        if supplied and not stale_revision:
            persisted = _read_env(self._env_path(config))
            lost = (
                []
                if str(persisted.get("FS_COMPACT_IMAGE_REGISTRY_REF") or "").strip()
                else self._absent_image_ids(persisted, ("FS_COMPACT_IMAGE",))
            )
            if not lost:
                return
            log.write(
                f"The Compact image this installer built at {revision[:12]} is no longer in "
                f"Docker ({lost[0]}); an image ID cannot be pulled back, so it is built again."
            )
            stale_revision = revision
        platform_name = f"linux/{_normalized_docker_architecture(self._architecture())}"
        tag = f"flow-steward-compact:{self._project_identity()}-{revision[:12]}"
        if stale_revision and stale_revision != revision:
            log.write(
                "The Compact image recorded by the previous install was built from "
                f"{stale_revision}, and these sources are at {revision}. Rebuilding: "
                "reusing it would report a successful update while running the old code."
            )
        # Say what is about to happen before it happens: this is minutes of work that
        # the operator did not ask for by name, and silence here reads as a hang.
        log.write(
            f"No Compact runtime image was supplied; building {tag} from "
            f"{_COMPACT_DOCKERFILE} for {platform_name}. "
            "The first build compiles dependencies and can take 10-20 minutes; "
            "later installs reuse Docker's layer cache."
        )
        self._checked(
            [
                "docker",
                "buildx",
                "build",
                "--target",
                _COMPACT_BUILD_TARGET,
                "--build-arg",
                f"BUILD_SHA={revision}",
                "--build-arg",
                f"FS_IMAGE_PLATFORM={platform_name}",
                "--label",
                f"{release_retention.INSTALLATION_LABEL}={self._project_identity()}",
                "--tag",
                tag,
                "--file",
                str(self.repo_root / _COMPACT_DOCKERFILE),
                "--load",
                str(self.repo_root),
            ],
            log=log,
            code="compact_image_build_failed",
            remediation=(
                "Resolve the reported Docker build failure and resume ./install.sh. The "
                "build needs a working Docker buildx builder and disk space for the image."
            ),
            exit_code=7,
            step="prepare",
            timeout_seconds=_COMPACT_BUILD_TIMEOUT_SECONDS,
        )
        image_id = self._checked(
            ["docker", "image", "inspect", "--format", _FORMAT_IMAGE_ID, tag],
            log=log,
            code="compact_image_build_failed",
            remediation=(
                "The Compact image was built but could not be read back from Docker. "
                "Inspect the Docker daemon and resume ./install.sh."
            ),
            exit_code=7,
            step="prepare",
        ).stdout.strip()
        if _IMAGE_ID.fullmatch(image_id) is None:
            raise InstallerFailure(
                code="compact_image_build_failed",
                cause=f"Docker reported an unusable image ID for the built image: {image_id!r}.",
                remediation="Inspect the Docker daemon and resume ./install.sh.",
                exit_code=7,
                retryable=True,
                step="prepare",
            )
        env_path = self._env_path(config)
        # A failed first build may have saved configuration at another checkout.
        # Record the revision of this successful build even without an older image.
        # The following provenance step still validates its embedded identity.
        replaced: dict[str, str] = {}
        if stale_revision:
            # Clear the previous install's digest pin in the same write that records the
            # new image. Left in place it would win in _resolve_compact_registry_image
            # and pull the superseded image straight back. The revision is rewritten
            # rather than cleared: an image left undated here would be rebuilt on every
            # subsequent run, and would also be an image nothing can date later.
            replaced.update(dict.fromkeys(_COMPACT_IMAGE_PIN_NAMES, ""))
        replaced.update(
            {
                "FS_COMPACT_IMAGE": image_id,
                "FS_COMPACT_IMAGE_REVISION": revision,
                "FS_BUILD_SHA": revision,
            }
        )
        self.state_store.update_env_values(env_path, replaced)
        self._environment = {
            **self._environment,
            **dict.fromkeys(_COMPACT_IMAGE_PIN_NAMES if stale_revision else (), ""),
            "FS_COMPACT_IMAGE": image_id,
            "FS_COMPACT_IMAGE_REVISION": revision,
            "FS_BUILD_SHA": revision,
        }
        log.write(f"Built Compact runtime image {tag} ({image_id}) at revision {revision}.")

    _SCALABLE_CANDIDATE_SCRIPT = Path("scripts/release/build_candidate.sh")
    # The image references an operator can supply. Deliberately not the digests: those
    # are produced by the provenance path inside prepare_core itself, so requiring them
    # here would rebuild over an installation that is about to derive them.
    _SCALABLE_IMAGE_NAMES = (
        "FS_APP_IMAGE",
        "FS_WORKER_IMAGE",
        "FS_CONTROL_IMAGE",
        "FS_CLI_IMAGE",
        "MCP_IMAGE",
        "POSTGRES_IMAGE",
    )

    @staticmethod
    def _is_chosen(name: str, known: Mapping[str, str]) -> bool:
        """Whether this image reference was chosen rather than defaulted into place.

        AC17-R116. The installer writes the environment registry's defaults during
        "Save installation configuration", which runs before any of this, so presence
        proves nothing. A value that still equals that default was chosen by nobody.
        """
        from core.infrastructure.config.env_registry import registry_default_value

        value = str(known.get(name) or "").strip()
        if not value:
            return False
        return value != str(registry_default_value(name) or "").strip()

    def _build_scalable_candidate_images(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> bool:
        """Build the Scalable images with the release path the project already has.

        AC17-R104: an interactive Scalable install died at "Prepare core images" with
        `required variable FS_CLI_IMAGE is missing a value`, under a remediation telling
        the operator to resolve a Docker build failure that had not happened -- the
        installer never reached a build. It cannot: docker-compose.prod.yaml declares
        five images and no build stanzas, and the installer knew how to build exactly
        one of them, the Compact runtime. Meanwhile scripts/release/build_candidate.sh
        has built all of them for about a year. Nothing was missing but the wiring.

        Returns False when the operator already supplied image references, so an
        installation pinned to published images is never rebuilt underneath them.
        """
        if config.deployment_profile == "compact" or config.environment != "production":
            return False
        env_path = self._env_path(config)
        known = {**_read_env(env_path), **self._environment}
        stale_revision = ""
        lost_revision = ""
        if any(self._is_chosen(name, known) for name in self._SCALABLE_IMAGE_NAMES):
            # Someone is managing these images -- an operator pinning published ones, a
            # previous run, a release harness -- and building would overwrite their
            # choice. Requiring *all* of them instead would rebuild over a provenance
            # flow that names four and derives the rest, which a pre-existing test
            # caught while this was being written.
            #
            # AC17-R141: except when "someone" is this installer and the sources have
            # moved past what it built. That is bookkeeping, not a choice, and keeping
            # it reported an update that was never applied.
            # Settled without git first, so an installation on supplied images -- the
            # common case here -- runs exactly the commands it ran before.
            built_here = self._scalable_images_built_here(config)
            if not built_here:
                return False
            if built_here == self._source_revision(log, step="prepare"):
                lost = self._absent_image_ids(known, self._SCALABLE_IMAGE_NAMES)
                if not lost:
                    return False
                log.write(
                    f"The images this installer built at {built_here[:12]} are no longer in "
                    f"Docker ({'; '.join(lost)}); an image ID cannot be pulled back, so they "
                    "are built again."
                )
                lost_revision = built_here
            stale_revision = built_here
        # AC17-R116: the first version asked whether *any* name was set, which was never
        # false. "Save installation configuration" runs before this and writes the
        # environment registry's defaults into .env.prod, so FS_APP_IMAGE arrives as
        # flow-steward-app:0.2.0 before the question is even put -- the installer's own
        # bookkeeping read back as the operator's choice, which is the same mistake
        # AC17-R102 fixed in the sibling method on the same day. So the question is not
        # "is anything written" but "did anyone choose this", and the registry's own
        # default is the definition of nobody having chosen.
        script = self.repo_root / self._SCALABLE_CANDIDATE_SCRIPT
        if stale_revision and not lost_revision:
            log.write(
                "The Scalable images recorded by the previous install were built from "
                f"{stale_revision}, and these sources have moved on; they are replaced "
                "with this release's: keeping them would report a successful update "
                "while running the old code."
            )
        revision = self._checked(
            ["git", "-C", str(self.repo_root), "rev-parse", _TOKEN_HEAD_COMMIT],
            log=log,
            separate_stderr=True,
            code=_SOURCE_REVISION_FAILURE_CODE,
            remediation=_SOURCE_REVISION_REMEDIATION,
            exit_code=7,
            step="prepare",
        ).stdout.strip()
        if self._verified_release_images(revision, log=log):
            log.write(
                f"Reusing the images built for {revision[:12]}: each is in Docker and matches "
                "its provenance, so nothing is rebuilt."
            )
        else:
            # R159 acceptance: said only when a build really runs -- it used to precede
            # a reuse that rebuilt nothing.
            log.write(
                f"Building the Scalable images with {self._SCALABLE_CANDIDATE_SCRIPT}. This "
                "builds five images and can take considerably longer than a Compact build."
            )
            try:
                self._run_candidate_build(config, script, log=log)
            except InstallerFailure as failure:
                if not self._clear_unusable_release(
                    revision,
                    recorded_current="" if lost_revision else stale_revision,
                    failure=failure,
                    log=log,
                ):
                    raise
                self._run_candidate_build(config, script, log=log)
        candidate_env = self.repo_root / _PATH_RELEASE_RECORDS / revision / _PATH_CANDIDATE_ENV
        produced = _read_env(candidate_env)
        recorded = {
            name: value
            for name in self._SCALABLE_IMAGE_NAMES
            if (value := str(produced.get(name) or "").strip())
        }
        missing = sorted(set(self._SCALABLE_IMAGE_NAMES) - set(recorded))
        if missing:
            raise InstallerFailure(
                code="scalable_image_build_failed",
                cause=(
                    f"{self._SCALABLE_CANDIDATE_SCRIPT} finished but {candidate_env} does not "
                    f"name: {', '.join(missing)}."
                ),
                remediation=(
                    "Inspect the candidate build output and resume ./install.sh; the "
                    "installer will not start a stack whose images it cannot name."
                ),
                exit_code=7,
                retryable=False,
                step="prepare",
            )
        # docker-compose.prod.yaml requires MCP_IMAGE_REVISION and candidate.env does not
        # write it, so wiring the build alone would move the failure rather than remove
        # it. The MCP image is built from this commit, which is what that variable means.
        recorded["MCP_IMAGE_REVISION"] = revision
        if stale_revision:
            # AC17-R141 acceptance: an image's identity is a tuple -- reference, digest,
            # revision -- and the rebuild rewrote only the first. The new image ID then
            # sat next to the previous build's digest and revision, and the exact-image
            # selection refused the contradiction. Every derived member of the replaced
            # images' record is superseded in this same write; the provenance step
            # derives them again from the new images, and verifies those images as it
            # always has. Nothing an operator chose reaches this branch.
            for role in ("app", "worker", "control", "mcp", "cli"):
                digest_key = _EXACT_IMAGE_DIGEST_KEYS.get(role)
                if digest_key is not None:
                    recorded[digest_key] = ""
                revision_key = _EXACT_IMAGE_REVISION_KEYS[role]
                if revision_key != "MCP_IMAGE_REVISION":
                    recorded[revision_key] = ""
            recorded["FS_BUILD_SHA"] = revision
        self.state_store.update_env_values(env_path, recorded)
        self._environment = {**self._environment, **recorded}
        log.write(f"Recorded {len(recorded)} Scalable image references at revision {revision}.")
        return True

    def _absent_image_ids(
        self: DockerInstallerGateway, values: Mapping[str, str], names: Iterable[str]
    ) -> list[str]:
        """Return each named image ID that Docker no longer has."""
        absent = []
        for name in names:
            value = str(values.get(name) or "").strip()
            if _IMAGE_ID.fullmatch(value) is None:
                continue
            inspected = self.process.run(
                ["docker", "image", "inspect", "--format", _FORMAT_IMAGE_ID, value]
            )
            if inspected.returncode != 0:
                absent.append(f"{name}={value}")
        return absent

    def _refuse_lost_image_ids(
        self: DockerInstallerGateway,
        config: InstallConfig,
        *,
        names: Iterable[str] | None = None,
    ) -> None:
        """Refuse a missing externally selected image ID before starting anything."""
        selected_names = (
            list(names)
            if names is not None
            else [name for _service, name in _RUNTIME_IMAGES[config.deployment_profile]]
        )
        if names is None:
            selected_names.append("POSTGRES_IMAGE")
            selected_names.append("FS_CLI_IMAGE")
        values = {**_read_env(self._env_path(config)), **self._environment}
        lost = self._absent_image_ids(values, selected_names)
        if lost:
            raise InstallerFailure(
                code="recorded_image_missing",
                cause=(
                    f"{'; '.join(lost)} is not present locally, and an image ID cannot be pulled."
                ),
                remediation=(
                    "Nothing was started. Either restore it (docker load from a saved copy of "
                    "that image) or clear the value in .env.prod so the installer uses its own, "
                    "then rerun ./install.sh."
                ),
                exit_code=7,
                retryable=True,
                step="prepare",
            )

    def _run_candidate_build(
        self: DockerInstallerGateway, config: InstallConfig, script: Path, *, log: InstallerLog
    ) -> None:
        # AC17-R143: tell the build whether this deployment holds its certificate as
        # files. Passed on the command line rather than through `environment=`, which
        # would replace the inherited environment with an allowlist and drop every
        # other variable the script reads.
        tls_inputs = "ingress" if config.tls_mode in _INGRESS_OWNED_TLS_MODES else "local"
        try:
            self._checked(
                ["env", f"FS_RELEASE_TLS_INPUTS={tls_inputs}", "bash", str(script)],
                log=log,
                code="scalable_image_build_failed",
                remediation=(
                    "Resolve the reported cause and resume ./install.sh; the complete build "
                    "output is in the log. The build refuses to run with uncommitted "
                    "tracked changes, so commit or stash them first; it can also be run by "
                    "hand as scripts/release/build_candidate.sh."
                ),
                exit_code=7,
                step="prepare",
                timeout_seconds=_COMPACT_BUILD_TIMEOUT_SECONDS,
            )
        except InstallerFailure as failure:
            # AC17-R143: the cause was the whole merged output, and what reached the
            # screen was its first line -- "#0 building with "default" instance using
            # docker driver" -- while the reason was the script's own last line. A
            # script that fails says why at the end; the log keeps everything.
            reason = _last_output_line(failure.cause)
            if not reason or reason == failure.cause:
                raise
            # AC17-R159: the commit-or-stash hint was printed whatever the cause -- under
            # a stale candidate snapshot, which it has nothing to do with.
            remediation = (
                failure.remediation
                if "must be committed" in reason
                else "Resolve the reported cause and resume ./install.sh; the complete build "
                "output is in the log."
            )
            raise InstallerFailure(
                code=failure.code,
                cause=reason,
                remediation=remediation,
                exit_code=failure.exit_code,
                retryable=failure.retryable,
                step=failure.step,
                log_path=failure.log_path,
            ) from failure

    # AC17-R159: the two refusals that mean "this revision's release record cannot be
    # reused", as build_candidate.sh words them.
    _UNUSABLE_RELEASE_REASONS = (
        "candidate snapshot is stale",
        "candidate identity is already recorded",
    )

    def _clear_unusable_release(
        self: DockerInstallerGateway,
        revision: str,
        *,
        recorded_current: str,
        failure: InstallerFailure,
        log: InstallerLog,
    ) -> bool:
        """Remove a release record the build refused to reuse, once, and say so.

        AC17-R159: rolling back to the kept previous revision failed -- its candidate
        snapshot was bound to the env file of its own day, which every later update
        rewrites -- and a record whose archive was pruned was refused as "already
        recorded". Only those two refusals, never the running installation's own
        record, and only once: anything else is reported as it is.
        """
        reason = failure.cause
        if not any(marker in reason for marker in self._UNUSABLE_RELEASE_REASONS):
            return False
        if not _FULL_GIT_REVISION.fullmatch(revision) or revision == recorded_current:
            return False
        release = self.repo_root / _PATH_RELEASE_RECORDS / revision
        if not release.is_dir():
            return False
        shutil.rmtree(release)
        notice = (
            f"The release recorded for {revision[:12]} could not be reused ({reason}); "
            f"removed {release} and built it again."
        )
        log.write(notice)
        self.notices.append(notice)
        return True

    def _verified_release_images(
        self: DockerInstallerGateway, revision: str, *, log: InstallerLog
    ) -> bool:
        """Whether this revision's recorded images are all in Docker and are its own.

        AC17-R159 acceptance: a rollback to the previous revision must not rebuild while
        its images are there -- nobody can rely on the build cache, which `docker
        builder prune` empties. Its record names each image; each must exist, carry the
        revision label the build stamped, and appear in the record's provenance.
        """
        release = self.repo_root / _PATH_RELEASE_RECORDS / revision
        recorded = _read_env(release / _PATH_CANDIDATE_ENV)
        images = [str(recorded.get(name) or "").strip() for name in self._SCALABLE_IMAGE_NAMES]
        if not all(_IMAGE_ID.fullmatch(image) for image in images):
            return False
        try:
            provenance = json.loads((release / "candidate-provenance.json").read_text("utf-8"))
        except (OSError, ValueError):
            return False
        digests = provenance.get("image_digests") if isinstance(provenance, dict) else None
        if not isinstance(digests, dict) or not digests:
            return False
        if not set(digests.values()) <= set(images):
            return False
        for image in images:
            inspected = self.process.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
                    image,
                ]
            )
            if inspected.returncode != 0 or inspected.stdout.strip() != revision:
                log.write(
                    f"The images built for {revision[:12]} are no longer all in Docker; "
                    "building them again."
                )
                return False
        return True

    @staticmethod
    def _core_dependency_failure(cause: str) -> InstallerFailure:
        return InstallerFailure(
            code="core_image_preparation_failed",
            cause=cause,
            remediation=_REMEDIATION_INFRASTRUCTURE_IMAGE,
            exit_code=7,
            step="prepare",
        )
