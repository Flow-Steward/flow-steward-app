"""WorkerScale technical operations for the installer adapter."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from core.application.installer.contracts import (
    InstallConfig,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerLog
from core.infrastructure.installer import release_retention
from core.infrastructure.installer.gateway_support import (
    _COMPACT_HEALTH_DETAILS,
    _COMPACT_WORKER_POLL_SECONDS,
    _COMPACT_WORKER_WAIT_SECONDS,
    _FORMAT_IMAGE_ID,
    _FULL_GIT_REVISION,
    _IMAGE_ID,
    _MINIMUM_DISK_BYTES,
    _PATH_CANDIDATE_ENV,
    _PATH_RELEASE_RECORDS,
    _RUNTIME_IMAGES,
    _TOKEN_COMPOSE_ENV,
    _TOKEN_ORG_OPENCONTAINERS_IMAGE_REVISION,
    _read_env,
)

if TYPE_CHECKING:
    from core.infrastructure.installer.gateway import DockerInstallerGateway


_COMPACT_IMAGE_FACTS = (
    '{{.Id}}\t{{index .Config.Labels "org.opencontainers.image.revision"}}\t{{.Created}}'
    "\t{{.Size}}\t{{json .RepoTags}}"
)
_GONE_IMAGE = ("No such image", "No such object")


class WorkerScaleOperations:
    def set_compact_worker_processes(
        self: DockerInstallerGateway, config: InstallConfig, *, processes: int, log: InstallerLog
    ) -> InstallConfig:
        """AC17-R157: record a new Compact worker count; the runtime follows on converge.

        One key in each file, so nothing else the installation holds is rewritten, and
        stack.toml -- the applied configuration -- last: killed before it, the next run
        still sees the old count and applies again; killed after, it converges.
        """
        candidate = InstallConfig.model_validate(
            {**config.model_dump(mode="json"), "worker_processes": processes}
        )
        value = str(candidate.worker_processes)
        self.state_store.update_env_values(
            self._env_path(candidate), {"FS_WORKER_PROCESSES": value}
        )
        store = self._configuration.store
        compose_values = _read_env(self.state_store.root / _TOKEN_COMPOSE_ENV)
        compose_values["FS_WORKER_PROCESSES"] = value
        store.write_compose_environment(compose_values)
        store.write_active(candidate)
        log.write(f"Recorded Compact worker processes: {config.worker_processes} -> {processes}.")
        return candidate

    def converge_compact_worker_processes(
        self: DockerInstallerGateway,
        config: InstallConfig,
        *,
        log: InstallerLog,
        changed: bool = False,
    ) -> str:
        """Recreate the Compact runtime when it runs another worker count than applied.

        No count change, no recreate. After a recreate -- or when this run `changed` the
        count, whichever step recreated the runtime with it -- the run waits until exactly
        the applied number of workers has registered with the runtime's own readiness.
        """
        applied = str(config.worker_processes)
        running = self.process.run(
            [
                *self._compose_prefix(config),
                "exec",
                "-T",
                "compact-runtime",
                "printenv",
                "FS_WORKER_PROCESSES",
            ],
            separate_stderr=True,
        )
        if running.stderr:
            log.write(running.stderr)
        current = running.stdout.strip() if running.returncode == 0 else ""
        if current == applied and not changed:
            return ""
        if current != applied:
            log.write(
                f"The Compact runtime runs {current or 'no'} worker processes; applying {applied}."
            )
            self.start_runtime(config, log=log)
        registered = self._await_compact_workers(config, log=log)
        if registered != config.worker_processes:
            raise InstallerFailure(
                code="compact_workers_unverified",
                cause=(
                    f"The runtime was recreated with {applied} worker processes, and "
                    f"{registered} registered within {int(_COMPACT_WORKER_WAIT_SECONDS)} s."
                ),
                remediation=(
                    "Inspect ./flow-steward status and the runtime log, then rerun "
                    "./install.sh: the count is recorded and the next run converges to it."
                ),
                exit_code=8,
                retryable=True,
                step="health",
            )
        if current == applied:
            return ""  # the caller that changed the count reports it
        return f"Compact worker processes: {current or 'unknown'} -> {applied}."

    def _await_compact_workers(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> int:
        """How many workers the runtime reports ready, waiting for the applied count."""
        registered = 0
        for _attempt in range(int(_COMPACT_WORKER_WAIT_SECONDS / _COMPACT_WORKER_POLL_SECONDS)):
            answer = self.process.run(
                [
                    *self._compose_prefix(config),
                    "exec",
                    "-T",
                    "compact-runtime",
                    "python",
                    "-c",
                    _COMPACT_HEALTH_DETAILS,
                ],
                separate_stderr=True,
            )
            if answer.stderr:
                log.write(answer.stderr)
            try:
                details = json.loads(answer.stdout) if answer.returncode == 0 else {}
            except ValueError:
                details = {}
            states = details.get("worker_states") if isinstance(details, dict) else None
            ready = [
                state
                for state in (states or {}).values()
                if state in {"registered", "active", "maintenance_paused"}
            ]
            registered = len(ready)
            if registered == config.worker_processes and details.get("workers_complete", True):
                return registered
            self._sleep(_COMPACT_WORKER_POLL_SECONDS)
        log.write(f"Compact runtime reported {registered} ready worker processes.")
        return registered

    def prune_superseded_releases(
        self: DockerInstallerGateway,
        config: InstallConfig,
        *,
        current: str,
        previous: str,
        log: InstallerLog,
    ) -> str:
        """AC17-R158: keep the running release and its rollback; remove the rest.

        Runs after a verified update, and on the "already installed" path, which finishes
        a cleanup an interrupted run left. Housekeeping never fails an installation: a
        failure is logged and named in the result.
        """
        if config.deployment_profile == "compact":
            return self._prune_compact_images(config, current=current, previous=previous, log=log)
        release_root = self.repo_root / _PATH_RELEASE_RECORDS
        if not release_root.is_dir():
            return ""
        try:
            protected, tags, retention = self._retention_inputs(config, current, previous)
            report = release_retention.prune(
                release_root,
                current=current,
                protected=protected,
                retention=retention,
                candidate_tags=tags,
            )
        except OSError as exc:
            log.write(f"Release cleanup stopped: {type(exc).__name__}: {exc}")
            return f"Old releases were not all removed ({type(exc).__name__}); see the log."
        summary = report.summary()
        if summary:
            log.write(summary)
        return summary

    def _previous_revision(self: DockerInstallerGateway, current: str, previous: str) -> str:
        if previous:
            return previous
        # The "already installed" path: the installation's own record of what its last
        # update replaced -- or, before there was one, its logs; never mtime.
        record = release_retention.read_record(self.state_store.root)
        return (
            record.get("previous", "")
            if record.get("current") == current
            else release_retention.previous_from_logs(self.state_store.logs_dir, current)
        )

    def _retention_count(self: DockerInstallerGateway) -> int:
        return release_retention.retention_count(
            self._environment.get("FS_INSTALLER_RELEASE_RETENTION")
        )

    def _retention_inputs(
        self: DockerInstallerGateway, config: InstallConfig, current: str, previous: str
    ) -> tuple[set[str], list[str], int]:
        """What retention keeps and what it may remove: protected revisions, this host's
        candidate tags, and the count -- one reading for prune and for the doctor."""
        previous = self._previous_revision(current, previous)
        protected = {revision for revision in (current, previous) if revision}
        protected |= release_retention.referenced_revisions(_read_env(self._env_path(config)))
        listed = self.process.run(list(release_retention.CANDIDATE_IMAGE_LISTING))
        if listed.returncode != 0:
            raise OSError("Docker could not list candidate images; release records retained")
        tags = listed.stdout.splitlines()
        return protected, tags, self._retention_count()

    def _compact_images(self: DockerInstallerGateway) -> list[release_retention.CompactImage]:
        """This installation's own Compact images: labelled with its project, or recorded
        by exact ID in its install logs (the ones built before the label)."""
        listed = self.process.run(
                ["docker", "image", "ls", "--no-trunc", "--quiet", "--filter",
                 f"label={release_retention.INSTALLATION_LABEL}={self._project_identity()}"],
                separate_stderr=True,
            )  # fmt: skip
        ids = set(listed.stdout.split()) if listed.returncode == 0 else set()
        ids |= release_retention.compact_images_built_in(self.state_store.logs_dir)
        if not ids:
            return []
        # A logged image that is gone since is left out of the answer, not an error.
        inspected = self.process.run(
            ["docker", "image", "inspect", "--format", _COMPACT_IMAGE_FACTS, *sorted(ids)],
            separate_stderr=True,
        )
        # Anything else Docker says is a failure to read them, and retention must say so
        # rather than find nothing to do (R178 live: the template error was silent).
        problems = [
            line.strip()
            for line in str(inspected.stderr or "").splitlines()
            if line.strip() and not any(gone in line for gone in _GONE_IMAGE)
        ]
        if problems:
            raise OSError(f"Docker could not describe this installation's images: {problems[0]}")
        images = []
        for line in inspected.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 5 or _IMAGE_ID.fullmatch(fields[0]) is None:
                continue
            image_id, revision, created, size, tags = fields
            try:
                tag_list = json.loads(tags) or []
            except ValueError:
                raise OSError(f"Docker described image {image_id[7:19]} unreadably") from None
            images.append(
                release_retention.CompactImage(
                    image_id=image_id,
                    revision=revision if _FULL_GIT_REVISION.fullmatch(revision) else "",
                    created=created,
                    size=int(size) if size.isdigit() else 0,
                    tags=tuple(str(tag) for tag in tag_list if tag),
                )
            )
        return images

    def _compact_plan(
        self: DockerInstallerGateway, config: InstallConfig, current: str, previous: str
    ) -> release_retention.CompactPlan:
        return release_retention.compact_superseded(
            self._compact_images(),
            protected=(current, self._previous_revision(current, previous)),
            current_image=str(_read_env(self._env_path(config)).get("FS_COMPACT_IMAGE") or ""),
            retention=self._retention_count(),
        )

    def _prune_compact_images(
        self: DockerInstallerGateway,
        config: InstallConfig,
        *,
        current: str,
        previous: str,
        log: InstallerLog,
    ) -> str:
        """AC17-R178: the Compact update's retention. Never an image a container of any
        project uses -- Docker is asked, and refuses one besides; never forced."""
        try:
            plan = self._compact_plan(config, current, previous)
        except OSError as exc:
            # Housekeeping never fails an installation, and never pretends to have run.
            said = f"Superseded Compact images were not removed: {exc}"
            log.write(said)
            return said
        report = release_retention.CompactPruneReport(
            kept=plan.kept, retained=plan.ambiguous_images
        )
        summary = report.summary()
        if summary:
            log.write(summary)
        return summary

    def superseded_releases(
        self: DockerInstallerGateway, config: InstallConfig, *, current: str
    ) -> release_retention.Superseded:
        """What prune_superseded_releases would remove now (doctor's release_retention)."""
        if config.deployment_profile == "compact":
            plan = self._compact_plan(config, current, "")
            return release_retention.Superseded(
                kept=plan.kept,
                ambiguous_images=tuple(
                    image.tags[0] if image.tags else image.image_id
                    for image in plan.ambiguous_images
                ),
            )
        protected, tags, retention = self._retention_inputs(config, current, "")
        return release_retention.superseded(
            self.repo_root / _PATH_RELEASE_RECORDS,
            current=current,
            protected=protected,
            retention=retention,
            candidate_tags=tags,
        )

    def free_disk_bytes(self: DockerInstallerGateway) -> int:
        return int(self._disk_free_bytes(self.repo_root))

    @staticmethod
    def minimum_free_disk_bytes() -> int:
        """What the preflight of an install or update demands."""
        return _MINIMUM_DISK_BYTES

    def record_installed_releases(
        self: DockerInstallerGateway, *, current: str, previous: str
    ) -> None:
        """AC17-R159: what the verified update runs, and the release it replaced."""
        if self.state_store.root.is_dir():
            self.state_store.write_owner_only_text(
                self.state_store.root / release_retention.RECORD_NAME,
                json.dumps({"current": current, "previous": previous}, sort_keys=True) + "\n",
            )

    def take_notices(self: DockerInstallerGateway) -> tuple[str, ...]:
        notices, self.notices = tuple(self.notices), []
        return notices

    def installed_config(self: DockerInstallerGateway, recorded: InstallConfig) -> InstallConfig:
        """The applied configuration of a completed installation, or what the journal recorded.

        The installer journal keeps the configuration of the install itself; every later
        `config apply` changes stack.toml only (measured on a live Scalable: stack.toml,
        .env.prod and fs-app held three trusted proxies, the journal still two).
        """
        active = self._configuration.store.read_active()
        if active is None:
            return recorded
        return InstallConfig.model_validate(active.model_dump(mode="json"))

    def refuse_unrecorded_worker_scale(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        """Any run that brings fs-worker up does it at its recorded count; name another one.

        Review P2: a bare `--scale fs-worker=N` was undone by the update's `compose up`
        without a word. The count is configuration now (FS_WORKER_REPLICAS, set with
        `config set --worker-replicas`), so a different running count is refused here,
        before anything is rebuilt.
        """
        if config.deployment_profile != "scalable":
            return
        recorded_text = str(_read_env(self._env_path(config)).get("FS_WORKER_REPLICAS") or "1")
        recorded = int(recorded_text) if recorded_text.isdigit() else 1
        running = len(self._running_container_ids("fs-worker"))
        if running in {0, recorded}:
            return
        # C3 (External review): a worker of the recorded set that is only stopped -- a
        # crash, a host restart -- is not an unrecorded --scale, which adds or removes
        # containers; the run starts it again (start_stopped_services).
        if len(self._running_container_ids("fs-worker", stopped_too=True)) == recorded:
            return
        log.write(f"fs-worker runs {running} containers; the installation records {recorded}.")
        raise InstallerFailure(
            code="worker_replicas_unrecorded",
            cause=(
                f"fs-worker runs {running} containers, but this installation records "
                f"{recorded}. The update would recreate it at {recorded}."
            ),
            remediation=(
                f"Record the running count with ./flow-steward config set --worker-replicas "
                f"{running} and ./flow-steward config apply --confirm, or return to "
                f"{recorded}, then rerun ./install.sh."
            ),
            exit_code=7,
            retryable=True,
            step="preflight",
        )

    def _image_id(self: DockerInstallerGateway, reference: str) -> str:
        """Resolve a recorded image reference, tag or ID, to the exact local image ID."""
        if _IMAGE_ID.fullmatch(reference):
            return reference
        inspected = self.process.run(
            ["docker", "image", "inspect", "--format", _FORMAT_IMAGE_ID, reference]
        )
        image = inspected.stdout.strip()
        return image if inspected.returncode == 0 and _IMAGE_ID.fullmatch(image) else ""

    def _running_runtime_revision(self: DockerInstallerGateway, config: InstallConfig) -> str:
        service, _name = _RUNTIME_IMAGES[config.deployment_profile][0]
        image = self._running_image_id(config, service)
        return self._image_revision_label(image) if image else ""

    def _recorded_images_not_running(
        self: DockerInstallerGateway, config: InstallConfig
    ) -> list[str]:
        """Services whose running container is not the image the installer recorded.

        A service with no running container is left out: there is nothing to compare,
        and starting it is the runtime step's job, not a reason to call this an update.
        """
        persisted = _read_env(self._env_path(config))
        mismatched = []
        for service, name in _RUNTIME_IMAGES[config.deployment_profile]:
            recorded = str(persisted.get(name) or "").strip()
            if not recorded:
                continue
            running = self._running_image_id(config, service)
            if not running:
                continue
            if self._image_id(recorded) != running:
                mismatched.append(service)
        return mismatched

    def _stale_scalable_images(
        self: DockerInstallerGateway, config: InstallConfig, source_revision: str
    ) -> str:
        """The revision of Scalable images this installer built, when the sources moved on."""
        built_here = self._scalable_images_built_here(config)
        return built_here if built_here and built_here != source_revision else ""

    def _scalable_images_built_here(self: DockerInstallerGateway, config: InstallConfig) -> str:
        """The revision the recorded Scalable images were built at, if this installer built them.

        AC17-R141, and the Scalable form of AC17-R102's question. The rule R102 uses --
        a name in the process environment is the operator's, a name only the env file
        knows is bookkeeping -- does not carry over, because this path promises that an
        installation pinned to published images is never rebuilt underneath them, and
        an operator may well have written those pins into .env.prod by hand.

        So "this installer built these" is checked rather than inferred: every build
        records its output in .flow-steward/release/<revision>/candidate.env, and the
        recorded names are copied from it. Only when every recorded name equals what
        that file says are the images the installer's own. The directory is found from
        the recorded revision, never by scanning release/, which also holds state that
        is not a build.
        """
        if config.deployment_profile == "compact" or config.environment != "production":
            return ""
        if any(self._is_chosen(name, self._environment) for name in self._SCALABLE_IMAGE_NAMES):
            return ""
        persisted = _read_env(self._env_path(config))
        recorded_revision = str(persisted.get("MCP_IMAGE_REVISION") or "").strip()
        if _FULL_GIT_REVISION.fullmatch(recorded_revision) is None:
            return ""
        candidate_env = (
            self.repo_root / _PATH_RELEASE_RECORDS / recorded_revision / _PATH_CANDIDATE_ENV
        )
        if not candidate_env.is_file():
            return ""
        produced = _read_env(candidate_env)
        for name in self._SCALABLE_IMAGE_NAMES:
            built = str(produced.get(name) or "").strip()
            if not built or str(persisted.get(name) or "").strip() != built:
                return ""
        return recorded_revision

    def _compact_image_label_revision(self: DockerInstallerGateway, config: InstallConfig) -> str:
        """Ask the recorded image what it was built from, when the env file does not say."""
        persisted = _read_env(self._env_path(config))
        image = str(persisted.get("FS_COMPACT_IMAGE") or "").strip()
        if not image:
            return ""
        return self._image_revision_label(image)

    def _image_revision_label(self: DockerInstallerGateway, image: str) -> str:
        """The source revision an image says it was built from, or "" if it does not."""
        outcome = self.process.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{index .Config.Labels " + f'"{_TOKEN_ORG_OPENCONTAINERS_IMAGE_REVISION}"' + "}}",
                image,
            ]
        )
        if outcome.returncode != 0:
            # A missing or unreadable image is not this method's problem to report:
            # the provenance path downstream says so with its own error.
            return ""
        revision = outcome.stdout.strip()
        return revision if _FULL_GIT_REVISION.fullmatch(revision) else ""
