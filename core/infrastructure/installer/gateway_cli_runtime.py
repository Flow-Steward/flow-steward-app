"""CliRuntime technical operations for the installer adapter."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from core.application.installer.contracts import (
    InstallConfig,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerLog
from core.application.lifecycle.contracts import (
    ComposeTarget,
    DeploymentConfig,
)
from core.infrastructure.installer.gateway_support import (
    _CLI_RUNTIME_HELPER,
    _CLI_RUNTIME_LABELS,
    _DOCKER_SOCKET_PATH,
    _FORMAT_IMAGE_ID,
    _FULL_GIT_REVISION,
    _IMAGE_ID,
    _MESSAGE_INVALID_PRODUCTION_CLI_CANDIDATE_MOUNT_PATHS,
    _MESSAGE_RUNNING,
    _TOKEN_COMPOSE_ENV,
    _CliRuntimeContext,
    _docker_socket_group_id,
    _read_env,
    _repository_release_version,
)
from core.infrastructure.installer.process import ProcessOutcome

if TYPE_CHECKING:
    from core.infrastructure.installer.gateway import DockerInstallerGateway


class CliRuntimeOperations:
    def ensure_cli_runtime(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        """Build, start, and exactly verify the private reusable CLI service."""
        if config.deployment_profile == "compact":
            if config.environment == "production" and self._install_mode(config) == "source":
                # Installed-system reruns skip prepare_core. Repair only our one-shot
                # management image here; never rebuild or restart the product runtime.
                self._build_compact_management_image(config, log=log)
                self._refuse_lost_image_ids(config, names=("FS_CLI_IMAGE",))
                values = _read_env(self._env_path(config))
                selected = str(
                    self._environment.get("FS_CLI_IMAGE") or values.get("FS_CLI_IMAGE") or ""
                ).strip()
                if not selected:
                    raise self._source_provenance_failure(
                        "The source image provenance has no Compact management image."
                    )
                identity = self._inspect_exact_prebuilt_image(
                    "cli",
                    selected,
                    lambda command: self._checked(
                        command,
                        log=log,
                        code="cli_runtime_unavailable",
                        remediation="Restore the exact Compact management image and rerun ./install.sh.",
                        exit_code=8,
                        step="cli_runtime",
                    ),
                )
                if identity.revision != values.get("FS_BUILD_SHA"):
                    raise self._source_provenance_failure(
                        "The source image provenance for the selected Compact management "
                        "image does not match the installed build revision."
                    )
                # Retain any registry-pinned runtime's original OCI index identity:
                # repairing a CLI must not re-materialize or re-stamp runtime images.
                self.validated_source_revision(config)
            # The runtime owns the worker credential in its private data volume.
            # A one-shot CLI cannot read that root-owned secret as the host user;
            # mounting its host runtime directory also follows the image's symlink
            # into /data and creates unsafe root-owned parents during bootstrap.
            # Inspect the actual runtime's identity without introducing any mounts.
            self._checked(
                [
                    *self._compose_prefix(config),
                    "exec",
                    "-T",
                    "--user",
                    "0:0",
                    "compact-runtime",
                    "python",
                    "-m",
                    "core.entrypoints.cli.ops.candidate_runtime_preflight",
                ],
                log=log,
                code="cli_runtime_unavailable",
                remediation="Verify the Compact image and rerun ./install.sh.",
                exit_code=8,
                step="cli_runtime",
            )
            log.write("Verified the Compact runtime database and worker identity.")
            return
        try:
            context = self._cli_runtime_context(config, log=log)
            image_revision = self._cli_image_revision(context, log=log)
            if image_revision != context.build_revision:
                if context.exact_image:
                    raise self._cli_runtime_failure(
                        log,
                        "The exact CLI image revision does not match installer provenance.",
                    )
                self._cli_runtime_checked(
                    [*context.compose_prefix, "build", "fs-cli"],
                    context=context,
                    log=log,
                    cause="The reusable CLI image could not be prepared.",
                )
                image_revision = self._cli_image_revision(context, log=log)
                if image_revision != context.build_revision:
                    raise self._cli_runtime_failure(
                        log,
                        "The rebuilt CLI image does not carry the expected source revision.",
                    )

            expected_image_id = self._cli_image_id(context, log=log)
            if context.exact_image and expected_image_id != context.image:
                raise self._cli_runtime_failure(
                    log,
                    "Docker resolved a different image ID for the exact CLI selection.",
                )
            matches, mismatch = self._cli_runtime_matches(
                context,
                expected_image_id=expected_image_id,
                log=log,
            )
            if matches:
                log.write("Verified the existing reusable CLI runtime.")
                return

            log.write(f"Repairing reusable CLI runtime: {mismatch}")
            self._prepare_default_host_artifact_root(config, log=log)
            # The new transfer bind must not let Docker create a root-owned
            # directory. Only this installation's default root is ours to create;
            # explicitly configured external roots must already exist.
            for source, destination in context.host_directory_mounts:
                if destination == "/app/backups" and source == self.repo_root / "backups":
                    if source.is_symlink() or (source.exists() and not source.is_dir()):
                        raise self._cli_runtime_failure(
                            log, "The default Clone transfer root must be a real directory."
                        )
                    source.mkdir(mode=0o700, exist_ok=True)
            start_command = [
                *context.compose_prefix,
                "up",
                "-d",
                "--no-deps",
                "--no-build",
            ]
            if context.exact_image:
                start_command.extend(("--pull", "never"))
            start_command.extend(("--force-recreate", "fs-cli"))
            self._cli_runtime_checked(
                start_command,
                context=context,
                log=log,
                cause="The reusable CLI container could not be started.",
            )
            matches, mismatch = self._cli_runtime_matches(
                context,
                expected_image_id=expected_image_id,
                log=log,
            )
            if not matches:
                raise self._cli_runtime_failure(
                    log,
                    f"The reusable CLI container failed exact verification: {mismatch}",
                )
            log.write("Started and exactly verified the reusable CLI runtime.")
        except InstallerFailure:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise self._cli_runtime_failure(
                log,
                f"The reusable CLI runtime could not be prepared: {type(exc).__name__}: {exc}",
            ) from exc

    def _cli_runtime_context(
        self: DockerInstallerGateway,
        config: DeploymentConfig,
        *,
        log: InstallerLog | None,
        remaining_seconds: Callable[[], float] | None = None,
    ) -> _CliRuntimeContext:
        try:
            version = _repository_release_version(self.repo_root)
        except (OSError, ValueError) as exc:
            if log is None:
                raise
            raise self._cli_runtime_failure(
                log, "The VERSION file is unavailable or invalid."
            ) from exc
        env_path = self._env_path(config).resolve()
        env_values = _read_env(env_path)
        image = env_values.get("FS_CLI_IMAGE", "").strip()
        if not image:
            image = f"flow-steward-cli:{version}"
        exact_image = _IMAGE_ID.fullmatch(image) is not None

        candidate_state_dir, candidate_prod_env = self._cli_candidate_paths(
            config, env_values, log=log
        )

        build_revision = self._cli_build_revision(
            exact_image, env_values, log=log, remaining_seconds=remaining_seconds
        )

        compose_values = _read_env(self.state_store.root / _TOKEN_COMPOSE_ENV)
        stack_environment = str(
            compose_values.get("FS_STACK_ENVIRONMENT")
            or ("local_development" if config.environment == "development" else "production")
        )
        env_file_identity = str(env_path) if env_path.is_file() else "absent"
        env_file_digest = (
            hashlib.sha256(env_path.read_bytes()).hexdigest()
            if env_file_identity != "absent"
            else ""
        )
        compose_files = self._compose_files(config)
        host_directory_mounts: tuple[tuple[Path, str], ...] = ()
        host_directory_values: dict[str, str] = {}
        if config.deployment_profile == "scalable" and config.data_placement == "host_directories":
            compose_root = self.repo_root / "infra/docker"

            def selected_path(name: str, default: str) -> Path:
                raw = Path(str(env_values.get(name) or self.repo_root / default))
                selected = raw if raw.is_absolute() else compose_root / raw
                if name in {"FS_BACKUPS_DIR", "FS_LOGS_DIR"} and selected.is_symlink():
                    raise ValueError(f"{name} must not be a symlink")
                return selected.resolve()

            extensions = selected_path("FS_EXTENSIONS_DIR", "extensions")
            transfers = selected_path("FS_BACKUPS_DIR", "backups")
            host_directory_mounts = (
                (extensions, "/app/extensions"),
                (transfers, "/app/backups"),
            )
            host_directory_values.update(
                FS_EXTENSIONS_DIR=str(extensions), FS_BACKUPS_DIR=str(transfers)
            )
            if config.file_storage == "local_fs":
                logs = selected_path("FS_LOGS_DIR", "logs")
                host_directory_mounts += ((logs / "artifacts", "/app/logs/artifacts"),)
                host_directory_values["FS_LOGS_DIR"] = str(logs)
        host_uid = str(os.getuid())
        host_gid = str(os.getgid())
        docker_gid = self._cli_docker_group_id()
        active_revision = self._active_runtime_revision()
        helper = (self.repo_root / _CLI_RUNTIME_HELPER).resolve()
        helper_command = [
            str(helper),
            "--compose-project",
            self._project_identity(),
            "--stack-environment",
            stack_environment,
            "--active-revision",
            active_revision,
            "--compose-files",
            ":".join(str(path) for path in compose_files),
            "--env-file",
            env_file_identity,
            "--host-project-root",
            str(self.repo_root),
            "--host-uid",
            host_uid,
            "--host-gid",
            host_gid,
            "--docker-gid",
            docker_gid,
        ]
        if env_file_digest:
            helper_command.append("--env-file-digest-from-environment")
        environment = {
            "FS_BUILD_REVISION": build_revision,
            "FS_CLI_IMAGE": image,
            "FS_COMPOSE_FILES": " ".join(str(path) for path in compose_files),
            "FS_COMPOSE_PROJECT_NAME": self._project_identity(),
            "FS_DOCKER_GID": docker_gid,
            "FS_DOCKER_HOST_PROJECT_ROOT": str(self.repo_root),
            "FS_ENV_FILE": str(env_path),
            "FS_HOST_GID": host_gid,
            "FS_HOST_UID": host_uid,
            "FS_INSTALLER_STATE_DIR": str(self.state_store.root.resolve()),
            "FS_STACK_ENVIRONMENT": stack_environment,
            **host_directory_values,
        }
        if env_file_digest:
            environment["FS_CLI_ENV_FILE_DIGEST"] = env_file_digest
        buildx_config = str(self._environment.get("BUILDX_CONFIG") or "")
        if buildx_config.strip():
            environment["BUILDX_CONFIG"] = buildx_config
        helper_outcome = self._cli_runtime_process(
            helper_command,
            environment=environment,
            log=log,
            timeout_seconds=remaining_seconds() if remaining_seconds else None,
        )
        runtime_revision = self._validated_runtime_revision(helper_outcome, log=log)
        environment["FS_CLI_RUNTIME_REVISION"] = runtime_revision
        return _CliRuntimeContext(
            image=image,
            exact_image=exact_image,
            build_revision=build_revision,
            runtime_revision=runtime_revision,
            environment=environment,
            compose_prefix=(*self._compose_prefix(config), "--profile", "cli"),
            host_uid=host_uid,
            host_gid=host_gid,
            docker_gid=docker_gid,
            state_runtime_root=(self.state_store.root / "runtime").resolve(),
            candidate_state_dir=candidate_state_dir,
            candidate_prod_env=candidate_prod_env,
            host_directory_mounts=host_directory_mounts,
        )

    def _cli_candidate_paths(
        self: DockerInstallerGateway,
        config: DeploymentConfig,
        env_values: Mapping[str, str],
        *,
        log: InstallerLog | None,
    ) -> tuple[Path | None, Path | None]:
        candidate_state_dir: Path | None = None
        candidate_prod_env: Path | None = None
        if config.environment == "production":
            try:
                state_value = str(env_values.get("FS_CANDIDATE_STATE_DIR") or "").strip()
                env_value = str(env_values.get("FS_CANDIDATE_PROD_ENV") or "").strip()
                state_path = Path(state_value)
                candidate_env_path = Path(env_value)
                if (
                    not state_value
                    or not env_value
                    or not state_path.is_absolute()
                    or not candidate_env_path.is_absolute()
                ):
                    raise ValueError(_MESSAGE_INVALID_PRODUCTION_CLI_CANDIDATE_MOUNT_PATHS)
                candidate_state_dir = state_path.resolve(strict=True)
                candidate_prod_env = candidate_env_path.resolve(strict=True)
                if not candidate_state_dir.is_dir() or not candidate_prod_env.is_file():
                    raise ValueError(_MESSAGE_INVALID_PRODUCTION_CLI_CANDIDATE_MOUNT_PATHS)
            except (OSError, RuntimeError, ValueError) as exc:
                if log is None:
                    raise ValueError(_MESSAGE_INVALID_PRODUCTION_CLI_CANDIDATE_MOUNT_PATHS) from exc
                raise self._cli_runtime_failure(
                    log,
                    "The Production CLI candidate mount paths are invalid.",
                ) from exc

        return candidate_state_dir, candidate_prod_env

    def _cli_build_revision(
        self: DockerInstallerGateway,
        exact_image: bool,
        env_values: Mapping[str, str],
        *,
        log: InstallerLog | None,
        remaining_seconds: Callable[[], float] | None = None,
    ) -> str:
        if exact_image:
            build_revision = str(env_values.get("FS_CLI_IMAGE_REVISION") or "").strip()
            if _FULL_GIT_REVISION.fullmatch(build_revision) is None:
                if log is None:
                    raise ValueError("exact CLI image revision is unavailable")
                raise self._cli_runtime_failure(
                    log,
                    "The exact CLI image revision is unavailable from installer provenance.",
                )
        else:
            git_command = ["git", "-C", str(self.repo_root), "rev-parse", "HEAD"]
            git_outcome = self._cli_runtime_process(
                git_command,
                environment={},
                log=log,
                separate_stderr=True,
                timeout_seconds=remaining_seconds() if remaining_seconds else None,
            )
            build_revision = (
                git_outcome.stdout.strip()
                if git_outcome.returncode == 0 and git_outcome.stdout.strip()
                else "unknown"
            )

        return build_revision

    def _cli_docker_group_id(self: DockerInstallerGateway) -> str:
        bootstrap_docker_gid = self._environment.get("FS_DOCKER_GID")
        if bootstrap_docker_gid is None:
            docker_gid = str(_docker_socket_group_id())
        else:
            docker_gid = str(bootstrap_docker_gid).strip()
            if not docker_gid.isdecimal() or int(docker_gid) > 2**32 - 1:
                raise ValueError("FS_DOCKER_GID must be a valid numeric group ID")
            docker_gid = str(int(docker_gid))
        return docker_gid

    def _validated_runtime_revision(
        self: DockerInstallerGateway, helper_outcome: ProcessOutcome, *, log: InstallerLog | None
    ) -> str:
        runtime_revision = helper_outcome.stdout.strip()
        if (
            helper_outcome.returncode != 0
            or len(runtime_revision) != 64
            or any(character not in "0123456789abcdef" for character in runtime_revision)
        ):
            if log is None:
                raise ValueError("runtime identity helper failed")
            raise self._cli_runtime_failure(
                log,
                "The reusable CLI runtime identity could not be calculated.",
            )
        return runtime_revision

    def _active_runtime_revision(self: DockerInstallerGateway) -> str:
        path = self.state_store.root / "active-revision.json"
        if not path.exists():
            return "initial"
        if path.is_symlink() or not path.is_file():
            raise ValueError("active runtime revision is not a regular file")
        payload = json.loads(path.read_text(encoding="utf-8"))
        revision = payload.get("revision") if isinstance(payload, dict) else None
        if (
            not isinstance(revision, str)
            or len(revision) != 64
            or any(character not in "0123456789abcdef" for character in revision)
        ):
            raise ValueError("active runtime revision is invalid")
        return revision

    def _compose_files(self: DockerInstallerGateway, config: DeploymentConfig) -> tuple[Path, ...]:
        return tuple(
            (self.repo_root / path).resolve()
            for path in self._canonical_target(config).compose_files
        )

    def _canonical_target(self: DockerInstallerGateway, config: DeploymentConfig) -> ComposeTarget:
        return self._configuration.derive_target(
            config,
            project_name_override=self._project_identity(),
        )

    @staticmethod
    def _application_service(config: DeploymentConfig) -> str:
        return "compact-runtime" if config.deployment_profile == "compact" else "fs-app"

    def _reverse_proxy_override_path(self: DockerInstallerGateway) -> Path:
        return self.repo_root / ".flow-steward/installer.compose.override.yaml"

    def _cli_image_revision(
        self: DockerInstallerGateway,
        context: _CliRuntimeContext,
        *,
        log: InstallerLog | None,
    ) -> str:
        outcome = self._cli_runtime_process(
            [
                "docker",
                "image",
                "inspect",
                context.image,
                "--format",
                '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
            ],
            environment=context.environment,
            log=log,
        )
        return outcome.stdout.strip() if outcome.returncode == 0 else ""

    def _cli_image_id(
        self: DockerInstallerGateway,
        context: _CliRuntimeContext,
        *,
        log: InstallerLog | None,
    ) -> str:
        outcome = self._cli_runtime_process(
            ["docker", "image", "inspect", context.image, "--format", _FORMAT_IMAGE_ID],
            environment=context.environment,
            log=log,
        )
        image_id = outcome.stdout.strip()
        if outcome.returncode != 0 or not image_id:
            if log is None:
                raise ValueError("CLI image ID is unavailable")
            raise self._cli_runtime_failure(
                log,
                "Docker could not resolve the exact reusable CLI image ID.",
            )
        return image_id

    def _cli_runtime_matches(
        self: DockerInstallerGateway,
        context: _CliRuntimeContext,
        *,
        expected_image_id: str,
        log: InstallerLog | None,
    ) -> tuple[bool, str]:
        ps_outcome = self._cli_runtime_process(
            [*context.compose_prefix, "ps", "-q", "--all", "fs-cli"],
            environment=context.environment,
            log=log,
        )
        container_ids = tuple(
            line.strip() for line in ps_outcome.stdout.splitlines() if line.strip()
        )
        if ps_outcome.returncode != 0 or len(container_ids) != 1:
            return False, "Docker Compose did not resolve exactly one fs-cli container."
        inspect_outcome = self._cli_runtime_process(
            ["docker", "container", "inspect", container_ids[0]],
            environment=context.environment,
            log=log,
        )
        if inspect_outcome.returncode != 0:
            return False, "Docker could not inspect the fs-cli container."
        try:
            records = json.loads(inspect_outcome.stdout)
        except json.JSONDecodeError:
            return False, "Docker returned malformed fs-cli inspection data."
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            return False, "Docker returned an invalid fs-cli inspection record."
        return self._cli_record_matches(records[0], context, expected_image_id)

    def _cli_record_matches(
        self: DockerInstallerGateway,
        record: dict[str, object],
        context: _CliRuntimeContext,
        expected_image_id: str,
    ) -> tuple[bool, str]:
        state = record.get("State")
        config = record.get("Config")
        host_config = record.get("HostConfig")
        if not isinstance(state, dict) or state.get("Running") is not True:
            return False, "The fs-cli container is not running."
        if record.get("Image") != expected_image_id:
            return False, "The fs-cli container uses the wrong exact image ID."
        if not isinstance(config, dict):
            return False, "The fs-cli container configuration is unavailable."
        labels = config.get("Labels")
        expected_labels = {
            "com.docker.compose.project": self._project_identity(),
            "com.docker.compose.service": "fs-cli",
            **_CLI_RUNTIME_LABELS,
            "com.flow-steward.cli-runtime-revision": context.runtime_revision,
        }
        if not isinstance(labels, dict) or any(
            labels.get(name) != value for name, value in expected_labels.items()
        ):
            return False, "The fs-cli container labels do not match the runtime contract."
        if config.get("User") != f"{context.host_uid}:{context.host_gid}":
            return False, "The fs-cli container user does not match the host identity."
        if not isinstance(host_config, dict) or host_config.get("Privileged") is not False:
            return False, "The fs-cli container privilege contract is invalid."
        group_add = host_config.get("GroupAdd")
        if not isinstance(group_add, list) or tuple(str(value) for value in group_add) != (
            context.docker_gid,
        ):
            return False, "The fs-cli container Docker group contract does not match."
        return self._cli_mounts_match(record.get("Mounts"), context)

    def _cli_mounts_match(
        self: DockerInstallerGateway, mounts: object, context: _CliRuntimeContext
    ) -> tuple[bool, str]:
        if not isinstance(mounts, list):
            return False, "The fs-cli container mount contract is unavailable."
        canonical_host_sources = {
            str(self.repo_root),
            str(context.state_runtime_root),
        }
        required_mounts = {
            (
                "bind",
                str(self.repo_root),
                str(self.repo_root),
                True,
            ),
            (
                "bind",
                str(context.state_runtime_root),
                "/run/flow-steward-runtime",
                True,
            ),
            (
                "bind",
                str(_DOCKER_SOCKET_PATH),
                str(_DOCKER_SOCKET_PATH),
                True,
            ),
        }
        for host_directory, destination in context.host_directory_mounts:
            canonical_host_sources.add(str(host_directory))
            required_mounts.add(("bind", str(host_directory), destination, True))
        if context.environment["FS_STACK_ENVIRONMENT"] != "production":
            installer_state_root = context.environment["FS_INSTALLER_STATE_DIR"]
            canonical_host_sources.add(installer_state_root)
            required_mounts.update(
                {
                    (
                        "bind",
                        installer_state_root,
                        "/run/flow-steward-install-state",
                        True,
                    ),
                    (
                        "bind",
                        installer_state_root,
                        installer_state_root,
                        True,
                    ),
                }
            )
        if context.candidate_state_dir is not None and context.candidate_prod_env is not None:
            canonical_host_sources.update(
                (str(context.candidate_state_dir), str(context.candidate_prod_env))
            )
            required_mounts.update(
                {
                    (
                        "bind",
                        str(context.candidate_state_dir),
                        "/run/flow-steward-candidate-state",
                        True,
                    ),
                    (
                        "bind",
                        str(context.candidate_prod_env),
                        "/app/.env.prod",
                        True,
                    ),
                }
            )
        observed_mounts: list[tuple[str, str, str, object]] = []
        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            source = str(mount.get("Source"))
            if source.startswith("/host_mnt/"):
                host_source = source.removeprefix("/host_mnt")
                if host_source in canonical_host_sources:
                    source = host_source
            observed_mounts.append(
                (
                    str(mount.get("Type")),
                    source,
                    str(mount.get("Destination")),
                    mount.get("RW"),
                )
            )
        if len(observed_mounts) != len(required_mounts) or set(observed_mounts) != required_mounts:
            return False, "The fs-cli container mount contract does not match."
        return True, ""

    def _cli_runtime_process(
        self: DockerInstallerGateway,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        log: InstallerLog | None,
        separate_stderr: bool = False,
        timeout_seconds: float | None = None,
    ) -> ProcessOutcome:
        if log is not None:
            log.write(_MESSAGE_RUNNING + " ".join(str(part) for part in command))
        try:
            outcome = self.process.run(
                command,
                cwd=self.repo_root,
                environment=environment,
                output_sink=(log.write if log is not None else None),
                heartbeat_sink=(
                    (lambda elapsed: log.write(f"Heartbeat: still running ({elapsed:.1f}s)"))
                    if log is not None
                    else None
                ),
                separate_stderr=separate_stderr,
                **({"timeout_seconds": timeout_seconds} if timeout_seconds is not None else {}),
            )
        except (FileNotFoundError, PermissionError) as exc:
            if log is None:
                raise
            raise self._cli_runtime_failure(log, str(exc)) from exc
        if log is not None:
            if outcome.stdout:
                log.write(outcome.stdout)
            if outcome.stderr:
                log.write(outcome.stderr)
        return outcome

    def _cli_runtime_checked(
        self: DockerInstallerGateway,
        command: Sequence[str],
        *,
        context: _CliRuntimeContext,
        log: InstallerLog,
        cause: str,
    ) -> ProcessOutcome:
        outcome = self._cli_runtime_process(
            command,
            environment=context.environment,
            log=log,
        )
        if outcome.returncode != 0:
            detail = outcome.stderr.strip() or outcome.stdout.strip()
            raise self._cli_runtime_failure(
                log,
                f"{cause} {detail or f'exit code {outcome.returncode}'}",
            )
        return outcome

    @staticmethod
    def _cli_runtime_failure(log: InstallerLog, cause: str) -> InstallerFailure:
        return InstallerFailure(
            code="cli_runtime_unavailable",
            cause=cause,
            remediation=(
                "Inspect the installer log, restore Docker access or the CLI runtime "
                "contract, and rerun ./install.sh. Existing data was preserved."
            ),
            exit_code=8,
            retryable=True,
            step="cli_runtime",
            log_path=str(log.path),
        )
