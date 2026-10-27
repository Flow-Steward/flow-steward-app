"""Docker installer adapter composed from focused technical operation modules."""

from __future__ import annotations

import json
import os as os
import platform as platform
import shutil
import socket as socket
import time
from collections.abc import Callable, Mapping, Sequence
from urllib import request as urlrequest

__all__ = ["DockerInstallerGateway", "urlrequest"]
from pathlib import Path

from core.application.installer.contracts import (
    InstallationEvidence,
    InstallConfig,
    InstallJournal,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerLog
from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import (
    DeploymentConfig,
    public_base_url,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.installer.core_dependency_images import (
    core_dependency_images_present,
)
from core.infrastructure.installer.gateway_cli_runtime import CliRuntimeOperations
from core.infrastructure.installer.gateway_configuration import ConfigurationOperations
from core.infrastructure.installer.gateway_doctor import DoctorOperations
from core.infrastructure.installer.gateway_health import HealthOperations
from core.infrastructure.installer.gateway_host_preflight import HostPreflightOperations
from core.infrastructure.installer.gateway_image_build import ImageBuildOperations
from core.infrastructure.installer.gateway_image_preparation import ImagePreparationOperations
from core.infrastructure.installer.gateway_image_provenance import ImageProvenanceOperations
from core.infrastructure.installer.gateway_image_selection import ImageSelectionOperations
from core.infrastructure.installer.gateway_profile_switch import ProfileSwitchOperations
from core.infrastructure.installer.gateway_runtime import RuntimeOperations
from core.infrastructure.installer.gateway_support import (
    _BOOTSTRAP_ISOLATION_ENV_NAMES as _BOOTSTRAP_ISOLATION_ENV_NAMES,
)
from core.infrastructure.installer.gateway_support import (
    _CLI_READINESS_SCRIPT as _CLI_READINESS_SCRIPT,
)
from core.infrastructure.installer.gateway_support import (
    _COMPACT_BUILD_TARGET as _COMPACT_BUILD_TARGET,
)
from core.infrastructure.installer.gateway_support import (
    _COMPACT_CLI_READINESS_SCRIPT as _COMPACT_CLI_READINESS_SCRIPT,
)
from core.infrastructure.installer.gateway_support import (
    _COMPOSE_PROJECT_NAME,
    _DOCKER_INVENTORY_TIMEOUT_SECONDS,
    _DURABLE_SECRET_NAMES,
    _FULL_GIT_REVISION,
    _MESSAGE_RUNNING,
    _PATH_ENV_PROD,
    _ROLLBACK_TIMEOUT_SECONDS,
    _RUNTIME_CERTIFICATE_PATH,
    _RUNTIME_PRIVATE_KEY_PATH,
    _TOKEN_COMPOSE_ENV,
    _TOKEN_HEAD_COMMIT,
    _TOKEN_STACK_TOML,
    _inventory_names,
    _is_loopback,
    _required_generated_secret_names,
    _secret_value,
)
from core.infrastructure.installer.gateway_support import (
    _MAX_CLI_READINESS_FIELD_LENGTH as _MAX_CLI_READINESS_FIELD_LENGTH,
)
from core.infrastructure.installer.gateway_support import (
    _READINESS_ATTEMPTS as _READINESS_ATTEMPTS,
)
from core.infrastructure.installer.gateway_support import (
    _READINESS_DELAY_SECONDS as _READINESS_DELAY_SECONDS,
)
from core.infrastructure.installer.gateway_support import (
    ProcessExecutor as ProcessExecutor,
)
from core.infrastructure.installer.gateway_support import (
    _env_selection as _env_selection,
)
from core.infrastructure.installer.gateway_support import (
    _future_timestamp as _future_timestamp,
)
from core.infrastructure.installer.gateway_support import (
    _published_ports as _published_ports,
)
from core.infrastructure.installer.gateway_support import (
    _read_env as _read_env,
)
from core.infrastructure.installer.gateway_support import (
    _routed_urlopen as _routed_urlopen,
)
from core.infrastructure.installer.gateway_support import (
    _RoutedHTTPSConnection as _RoutedHTTPSConnection,
)
from core.infrastructure.installer.gateway_worker_scale import WorkerScaleOperations
from core.infrastructure.installer.process import ProcessOutcome, SubprocessExecutor
from core.infrastructure.installer.registry_image_resolver import (
    LocalImageContentIdentity as LocalImageContentIdentity,
)
from core.infrastructure.installer.secret_values import is_missing_or_placeholder_secret
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore


class DockerInstallerGateway(
    HostPreflightOperations,
    ConfigurationOperations,
    ImageSelectionOperations,
    WorkerScaleOperations,
    ImageBuildOperations,
    ImagePreparationOperations,
    ImageProvenanceOperations,
    RuntimeOperations,
    ProfileSwitchOperations,
    DoctorOperations,
    HealthOperations,
    CliRuntimeOperations,
):
    """Keep technical side effects outside the installer application service."""

    repo_root: Path
    state_store: InstallerStateStore
    process: ProcessExecutor
    _disk_free_bytes: Callable[[Path], int]
    _port_available: Callable[[str, int], bool] | None
    _architecture: Callable[[], str]
    _docker_cli_available: Callable[[], bool]
    _host_identity: Callable[[], tuple[int, int]]
    _environment: dict[str, str]
    _active_project_identity: str
    _secret_factory: Callable[[], str]
    _technical_output_sink: Callable[[str], None] | None
    _sleep: Callable[[float], None]
    notices: list[str]
    _configuration: ConfigurationService

    def __init__(
        self,
        *,
        repo_root: Path,
        state_store: InstallerStateStore,
        process: ProcessExecutor | None = None,
        disk_free_bytes: Callable[[Path], int] | None = None,
        port_available: Callable[[str, int], bool] | None = None,
        architecture: Callable[[], str] | None = None,
        docker_cli_available: Callable[[], bool] | None = None,
        host_identity: Callable[[], tuple[int, int]] | None = None,
        environment: Mapping[str, str] | None = None,
        secret_factory: Callable[[], str] | None = None,
        technical_output_sink: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        configuration_service: ConfigurationService | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.state_store = state_store
        self.process = process or SubprocessExecutor()
        self._disk_free_bytes = disk_free_bytes or (lambda path: shutil.disk_usage(path).free)
        self._port_available = port_available
        self._architecture = architecture or platform.machine
        self._docker_cli_available = docker_cli_available or (
            lambda: shutil.which("docker") is not None
        )
        self._host_identity = host_identity or (lambda: (os.getuid(), os.getgid()))
        self._environment = dict(_runtime_env if environment is None else environment)
        self._active_project_identity = str(
            self._environment.get("FS_COMPOSE_PROJECT_NAME") or "flow-steward"
        )
        self._secret_factory = secret_factory or _secret_value
        self._technical_output_sink = technical_output_sink
        self._sleep = sleep or time.sleep
        # What this run did on the operator's behalf that belongs in the result, not only
        # in the log (AC17-R159: a removed release record).
        self.notices: list[str] = []
        self._configuration = configuration_service or ConfigurationService(
            store=LifecycleConfigurationStore(self.repo_root, state_root=self.state_store.root)
        )

    def inspect(self) -> InstallationEvidence:
        stack_path = self.state_store.root / _TOKEN_STACK_TOML
        journal_exists = self.state_store.journal_path.exists()
        journal_valid = True
        try:
            journal = self.state_store.load_journal()
        except (OSError, ValueError):
            journal = None
            journal_valid = False
        environment = "local_development"
        if stack_path.exists() and 'environment = "production"' in stack_path.read_text(
            encoding="utf-8"
        ):
            environment = "production"
        env_path = self.repo_root / (_PATH_ENV_PROD if environment == "production" else ".env")
        env = _read_env(env_path)
        env_project_identity = str(
            env.get("FS_COMPOSE_PROJECT_NAME")
            or self._environment.get("FS_COMPOSE_PROJECT_NAME")
            or "flow-steward"
        )
        project_identity = journal.project_identity if journal else env_project_identity
        self._active_project_identity = project_identity
        try:
            volume_result = self.process.run(
                [
                    "docker",
                    "volume",
                    "ls",
                    "--filter",
                    f"label=com.docker.compose.project={project_identity}",
                    "--format",
                    "{{.Name}}",
                ],
                cwd=self.repo_root,
                timeout_seconds=_DOCKER_INVENTORY_TIMEOUT_SECONDS,
            )
            container_result = self.process.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"label=com.docker.compose.project={project_identity}",
                    "--format",
                    "{{.Names}}",
                ],
                cwd=self.repo_root,
                timeout_seconds=_DOCKER_INVENTORY_TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, PermissionError, TimeoutError):
            volume_result = ProcessOutcome(command=("docker",), returncode=127)
            container_result = ProcessOutcome(command=("docker",), returncode=127)
        inventory_valid = volume_result.returncode == 0 and container_result.returncode == 0
        volumes = _inventory_names(volume_result)
        containers = _inventory_names(container_result)
        if not inventory_valid:
            volumes = ()
            containers = ()
        backups_root = self.repo_root / "backups"
        protected_backups = bool(backups_root.exists() and next(backups_root.rglob("*.enc"), None))
        return InstallationEvidence(
            journal_exists=journal_exists,
            journal_valid=journal_valid,
            journal_completed=bool(journal and journal.status == "completed"),
            journal_completed_steps=journal.completed_steps if journal else (),
            config_exists=stack_path.exists(),
            env_exists=env_path.exists(),
            project_identity=project_identity,
            observed_project_identity=(
                env_project_identity if (containers or volumes or env_path.exists()) else ""
            ),
            resource_inventory_valid=inventory_valid,
            durable_secrets_present=all(
                not is_missing_or_placeholder_secret(env.get(name))
                for name in _DURABLE_SECRET_NAMES
            ),
            backup_encryption_key_present=not is_missing_or_placeholder_secret(
                env.get("FS_BACKUP_ENCRYPTION_KEY")
            ),
            protected_backups_exist=protected_backups,
            owned_containers=containers,
            owned_volumes=volumes,
        )

    def rollback_initial_install(self, journal: InstallJournal, *, log: InstallerLog) -> None:
        if journal.project_identity != self._project_identity():
            raise InstallerFailure(
                code="installation_rollback_failed",
                cause="The interrupted journal does not match the active Compose project.",
                remediation=(
                    "Inspect the installer journal and Compose project identity before "
                    "running ./install.sh again."
                ),
                exit_code=10,
                retryable=True,
                step="rollback",
            )
        evidence = self.inspect()
        if not evidence.resource_inventory_valid:
            raise InstallerFailure(
                code="installation_rollback_failed",
                cause="Could not verify the interrupted install's exact Docker inventory.",
                remediation=(
                    "Inspect the exact Compose project inventory before running ./install.sh again."
                ),
                exit_code=10,
                retryable=True,
                step="rollback",
            )
        compose_materialized = self._env_path(journal.config).is_file() and all(
            path.is_file() for path in self._compose_files(journal.config)
        )
        if evidence.owned_containers or evidence.owned_volumes or compose_materialized:
            self._checked(
                [
                    *self._compose_prefix(journal.config),
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ],
                log=log,
                code="installation_rollback_failed",
                remediation=(
                    "Remove only the interrupted install's Compose resources, then run "
                    "./install.sh again."
                ),
                exit_code=10,
                step="rollback",
                timeout_seconds=_ROLLBACK_TIMEOUT_SECONDS,
            )
        state_root = Path(os.path.abspath(self.state_store.root))
        if state_root.is_symlink():
            raise InstallerFailure(
                code="installation_rollback_failed",
                cause="Installer state root is a symbolic link; rollback refused.",
                remediation=(
                    "Restore .flow-steward as an ordinary directory before running ./install.sh again."
                ),
                exit_code=10,
                retryable=True,
                step="rollback",
            )
        logs_root = Path(os.path.abspath(self.state_store.logs_dir))
        protected = {
            Path(os.path.abspath(self.state_store.journal_path)),
            Path(os.path.abspath(self.state_store.lock_path)),
            logs_root,
        }
        try:
            for relative_path in journal.created_paths:
                candidate = Path(os.path.abspath(self.repo_root / relative_path))
                if candidate == state_root or state_root not in candidate.parents:
                    continue
                if candidate in protected or logs_root in candidate.parents:
                    continue
                self._remove_owned_state_path(candidate, state_root)
        except OSError as exc:
            raise InstallerFailure(
                code="installation_rollback_failed",
                cause=f"Could not remove installer-owned state: {exc}",
                remediation=(
                    "Inspect owner permissions under .flow-steward, then remove only the "
                    "interrupted install's managed state before running ./install.sh again."
                ),
                exit_code=10,
                retryable=True,
                step="rollback",
            ) from exc

    @staticmethod
    def _remove_owned_state_path(candidate: Path, state_root: Path) -> None:
        ancestor = candidate.parent
        while ancestor != state_root:
            if ancestor.is_symlink():
                raise OSError(f"installer-owned path ancestor is a symbolic link: {ancestor}")
            ancestor = ancestor.parent
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink(missing_ok=True)
        elif candidate.is_dir():
            shutil.rmtree(candidate)

    def postcondition_holds(self, step_id: str, config: InstallConfig) -> bool:
        if step_id == "prepare":
            return self._prepare_postcondition_holds(config)
        if step_id == "worker_identity":
            return False
        if step_id == "cli_runtime":
            return self._cli_runtime_postcondition_holds(config)
        if step_id == "configuration":
            return self._configuration_postcondition_holds(config)
        if step_id == "secrets":
            return self._secrets_postcondition_holds(config)
        if step_id == "tls":
            return self._tls_postcondition_holds(config)
        if step_id == "manifest":
            return (self.state_store.root / "initial-preservation-manifest.json").is_file()
        return False

    def _prepare_postcondition_holds(self, config: InstallConfig) -> bool:
        try:
            environment = _read_env(self._env_path(config))
            if (
                config.deployment_profile == "compact"
                and self._install_mode(config) == "source"
                and (
                    not str(environment.get("FS_CLI_IMAGE") or "").strip()
                    or environment.get("FS_CLI_IMAGE_REVISION") != environment.get("FS_BUILD_SHA")
                )
            ):
                return False
            if self._complete_exact_prebuilt_image_refs(config, environment) is not None:
                return self._production_provenance_postcondition_holds(
                    config
                ) and core_dependency_images_present(
                    failure=self._core_dependency_failure,
                    config=config,
                    compose_prefix=self._compose_prefix(config),
                    repo_root=self.repo_root,
                    run=self.process.run,
                )
            outcome = self.process.run(
                [*self._compose_prefix(config), "config", "--format", "json"],
                cwd=self.repo_root,
            )
            if outcome.returncode != 0:
                return False
            payload = json.loads(outcome.stdout)
            services = payload.get("services") if isinstance(payload, dict) else None
            if not isinstance(services, dict):
                return False
            built_images = self._prepared_compose_images(services, config)
            if not built_images:
                return False
            images_present = all(
                self.process.run(
                    ["docker", "image", "inspect", image],
                    cwd=self.repo_root,
                ).returncode
                == 0
                for image in dict.fromkeys(built_images)
            )
            return images_present and (
                config.environment != "development"
                or self._production_provenance_postcondition_holds(config)
            )
        except (InstallerFailure, OSError, TypeError, json.JSONDecodeError):
            return False

    def _prepared_compose_images(
        self, services: dict[str, object], config: InstallConfig
    ) -> list[str] | None:
        built_images: list[str] = []
        if config.environment == "development":
            scanner_image = self._prepared_scanner_image(services)
            if scanner_image is None:
                return None
            built_images.append(scanner_image)
        for service_name, service in services.items():
            if not isinstance(service_name, str) or not isinstance(service, dict):
                return None
            if service.get("build") is None:
                continue
            image = service.get("image")
            if image is None:
                image = f"{self._project_identity()}-{service_name}"
            if not isinstance(image, str) or not image.strip():
                return None
            built_images.append(image.strip())
        return built_images

    @staticmethod
    def _prepared_scanner_image(services: dict[str, object]) -> str | None:
        scanner = services.get("sonar-scanner")
        if not isinstance(scanner, dict):
            return None
        scanner_image = scanner.get("image")
        if not isinstance(scanner_image, str) or not scanner_image.strip():
            return None
        return scanner_image.strip()

    def _cli_runtime_postcondition_holds(self, config: InstallConfig) -> bool:
        if config.deployment_profile == "compact":
            return False
        try:
            context = self._cli_runtime_context(config, log=None)
            if self._cli_image_revision(context, log=None) != context.build_revision:
                return False
            image_id = self._cli_image_id(context, log=None)
            matches, _mismatch = self._cli_runtime_matches(
                context,
                expected_image_id=image_id,
                log=None,
            )
            return matches
        except (OSError, ValueError, TypeError):
            return False

    def _configuration_postcondition_holds(self, config: InstallConfig) -> bool:
        compose_env_path = self.state_store.root / _TOKEN_COMPOSE_ENV
        try:
            active = self._configuration.store.read_active()
            compose_values = _read_env(compose_env_path)
            target = self._canonical_target(config)
        except (OSError, ValueError):
            return False
        if active is None or active.model_dump(mode="json") != config.model_dump(mode="json"):
            return False
        expected_compose = self._configuration.compose_environment(config, target)
        if any(compose_values.get(key) != value for key, value in expected_compose.items()):
            return False
        if compose_values.get("FS_COMPOSE_PROJECT_NAME") != self._project_identity():
            return False
        if config.tls_mode == "reverse_proxy" and config.proxy_placement == "host":
            override = self._reverse_proxy_override_path()
            try:
                override_text = override.read_text(encoding="utf-8")
            except OSError:
                return False
            return all(
                marker in override_text
                for marker in (
                    f"{config.reverse_proxy_backend_host}:{config.reverse_proxy_backend_port}:8099",
                    "installer-disabled-traefik",
                    "required: false",
                )
            )
        return True

    def _secrets_postcondition_holds(self, config: InstallConfig) -> bool:
        values = _read_env(self._env_path(config))
        core_ready = all(
            not is_missing_or_placeholder_secret(values.get(name))
            for name in _required_generated_secret_names(config)
        )
        expected_url = self.product_url(config)
        selected_ready = bool(
            values.get("FS_COMPOSE_PROJECT_NAME") == self._project_identity()
            and values.get("FS_PUBLIC_BASE_URL") == expected_url
            and values.get("FS_AUTH_TRUSTED_ORIGINS") == expected_url
        )
        if config.environment == "development":
            revision_outcome = self.process.run(
                ["git", "-C", str(self.repo_root), "rev-parse", _TOKEN_HEAD_COMMIT],
                cwd=self.repo_root,
                separate_stderr=True,
            )
            revision = revision_outcome.stdout.strip().lower()
            selected_ready = bool(
                selected_ready
                and revision_outcome.returncode == 0
                and _FULL_GIT_REVISION.fullmatch(revision)
                and values.get("FS_BUILD_SHA") == revision
            )
        if config.file_storage == "external_s3":
            selected_ready = bool(
                selected_ready
                and values.get("ARTIFACT_S3_ENDPOINT_URL") == config.s3_endpoint
                and values.get("ARTIFACT_S3_BUCKET") == config.s3_bucket
                and values.get("ARTIFACT_S3_REGION") == config.s3_region
                and not is_missing_or_placeholder_secret(values.get("ARTIFACT_S3_ACCESS_KEY"))
                and not is_missing_or_placeholder_secret(values.get("ARTIFACT_S3_SECRET_KEY"))
            )
        if not _is_loopback(config.public_host):
            return bool(
                core_ready
                and selected_ready
                and not is_missing_or_placeholder_secret(values.get("FS_AUTH_BOOTSTRAP_TOKEN"))
                # An expiry that merely exists used to satisfy this, so a resumed install
                # skipped the secrets step outright and the expired token survived. This has to
                # agree with the reuse condition in ensure_secrets on every clause, or the step
                # is skipped exactly when it most needs to run.
                and _future_timestamp(str(values.get("FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT") or ""))
                and self.state_store.bootstrap_token_matches_state(
                    str(values.get("FS_AUTH_BOOTSTRAP_TOKEN") or "")
                )
            )
        return bool(core_ready and selected_ready)

    def _tls_postcondition_holds(self, config: InstallConfig) -> bool:
        if config.tls_mode in {
            "reverse_proxy",
            "external_resolver",
            "external_certificate",
            "platform_managed",
            "acme",
        }:
            return True
        cert_path = self.repo_root / config.certificate_path
        key_path = self.repo_root / config.private_key_path
        runtime_cert = self.repo_root / _RUNTIME_CERTIFICATE_PATH
        runtime_key = self.repo_root / _RUNTIME_PRIVATE_KEY_PATH
        return bool(
            cert_path.is_file()
            and key_path.is_file()
            and self._tls_material_valid(config, cert_path=cert_path, key_path=key_path)
            and runtime_cert.is_file()
            and runtime_key.is_file()
            and self._tls_material_valid(config, cert_path=runtime_cert, key_path=runtime_key)
        )

    def product_url(self, config: InstallConfig) -> str:
        return public_base_url(config)

    def _checked(
        self,
        command: Sequence[str],
        *,
        log: InstallerLog,
        code: str,
        remediation: str,
        exit_code: int,
        step: str = "preflight",
        timeout_seconds: float | None = None,
        separate_stderr: bool = False,
    ) -> ProcessOutcome:
        """Run a command, failing the step with a named code when it does not succeed.

        AC17-R138: streams are merged by default, which is what the operation log
        wants -- one transcript in the order it happened. A caller that reads the
        output as a *value* rather than as a transcript must ask for them apart,
        because a successful command is still allowed to write to stderr, and the
        merged text is then not the answer it printed.
        """
        log.write(_MESSAGE_RUNNING + " ".join(str(part) for part in command))

        def capture(line: str) -> None:
            log.write(line)
            if self._technical_output_sink is not None:
                self._technical_output_sink(line)

        def heartbeat(elapsed: float) -> None:
            log.write(f"Heartbeat: still running ({elapsed:.1f}s)")

        try:
            outcome = self.process.run(
                command,
                cwd=self.repo_root,
                timeout_seconds=timeout_seconds,
                output_sink=capture,
                heartbeat_sink=heartbeat,
                separate_stderr=separate_stderr,
            )
        except (FileNotFoundError, PermissionError, TimeoutError) as exc:
            raise InstallerFailure(
                code=code,
                cause=str(exc),
                remediation=remediation,
                exit_code=5 if isinstance(exc, PermissionError) else exit_code,
                step=step,
            ) from exc
        if outcome.stdout:
            log.write(outcome.stdout)
        if outcome.stderr:
            log.write(outcome.stderr)
        if outcome.returncode != 0:
            cause = (
                outcome.stderr.strip()
                or outcome.stdout.strip()
                or f"exit code {outcome.returncode}"
            )
            raise InstallerFailure(
                code=code,
                cause=cause,
                remediation=remediation,
                exit_code=exit_code,
                step=step,
            )
        return outcome

    def _compose_prefix(
        self, config: DeploymentConfig, *, env_path: Path | None = None
    ) -> list[str]:
        target = self._canonical_target(config)
        compose_files = tuple((self.repo_root / path).resolve() for path in target.compose_files)
        prefix = [
            "docker",
            "compose",
            "-p",
            self._project_identity(),
        ]
        for compose_file in compose_files:
            prefix.extend(["-f", str(compose_file)])
        prefix.extend(["--env-file", str(env_path or self._env_path(config))])
        for profile in target.compose_profiles:
            prefix.extend(["--profile", profile])
        return prefix

    def _env_path(self, config: DeploymentConfig) -> Path:
        return self.repo_root / (".env" if config.environment == "development" else _PATH_ENV_PROD)

    def _bootstrap_isolation_overrides(self, *, step: str) -> dict[str, str]:
        overrides: dict[str, str] = {}
        for name in _BOOTSTRAP_ISOLATION_ENV_NAMES:
            value = str(self._environment.get(name) or "")
            if any(not character.isprintable() for character in value):
                raise InstallerFailure(
                    code="invalid_bootstrap_override",
                    cause=f"The bootstrap isolation override {name} contains non-printable characters.",
                    remediation="Remove control characters from the non-secret installer override and rerun ./install.sh.",
                    exit_code=3,
                    retryable=False,
                    step=step,
                )
            if (
                name == "FS_COMPOSE_PROJECT_NAME"
                and value
                and not _COMPOSE_PROJECT_NAME.fullmatch(value)
            ):
                raise InstallerFailure(
                    code="invalid_bootstrap_override",
                    cause="The bootstrap Compose project name is invalid.",
                    remediation=(
                        "Use a lowercase project name that starts with a letter or digit and "
                        "contains only letters, digits, hyphens, or underscores."
                    ),
                    exit_code=3,
                    retryable=False,
                    step=step,
                )
            if value:
                overrides[name] = value
        return overrides

    def _project_identity(self) -> str:
        return self._active_project_identity

    def project_identity(self) -> str:
        return self._project_identity()

    def _external_s3_values(self, config: InstallConfig) -> dict[str, str]:
        if not config.s3_endpoint.strip():
            raise InstallerFailure(
                code="s3_configuration_missing",
                cause="External S3 storage requires an endpoint.",
                remediation="Provide --s3-endpoint and an owner-only credentials JSON file.",
                exit_code=3,
                retryable=True,
                step="secrets",
            )
        credentials_path = Path(config.s3_credentials_path).expanduser()
        if not credentials_path.is_absolute():
            credentials_path = self.repo_root / credentials_path
        try:
            stat_result = credentials_path.lstat()
            if credentials_path.is_symlink() or not credentials_path.is_file():
                raise OSError("credentials path is not a regular file")
            if stat_result.st_mode & 0o077:
                raise PermissionError("credentials file must use mode 0600 or stricter")
            payload = json.loads(credentials_path.read_text(encoding="utf-8"))
            access_key = str(payload.get("access_key") or "").strip()
            secret_key = str(payload.get("secret_key") or "").strip()
            if not access_key or not secret_key:
                raise ValueError("access_key and secret_key are required")
        except (OSError, ValueError) as exc:
            raise InstallerFailure(
                code="s3_credentials_unavailable",
                cause=f"The external S3 credentials file is invalid: {exc}",
                remediation="Create an owner-only JSON file with access_key and secret_key, then resume.",
                exit_code=5,
                retryable=True,
                step="secrets",
            ) from exc
        return {
            "ARTIFACT_S3_ENDPOINT_URL": config.s3_endpoint.strip(),
            "ARTIFACT_S3_BUCKET": config.s3_bucket.strip(),
            "ARTIFACT_S3_REGION": config.s3_region.strip(),
            "ARTIFACT_S3_ACCESS_KEY": access_key,
            "ARTIFACT_S3_SECRET_KEY": secret_key,
            "ARTIFACT_S3_USE_SSL": "true"
            if config.s3_endpoint.lower().startswith("https://")
            else "false",
        }

    def _port_owned_by_project(self, port: int) -> bool:
        outcome = self.process.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={self._project_identity()}",
                "--format",
                "{{.Ports}}",
            ],
            cwd=self.repo_root,
        )
        if outcome.returncode != 0:
            return False
        return port in _published_ports(outcome.stdout)
