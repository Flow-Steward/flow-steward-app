"""Selected installer/configuration adapters for first-administrator recovery."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from core.application.installer.contracts import InstallConfig
from core.application.installer.errors import InstallerFailure
from core.application.lifecycle.contracts import DeploymentConfig
from core.infrastructure.installer.bootstrap_snapshot import BootstrapSnapshot, require_safe_path
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

_STATUS_FIELDS = {
    "schema_version",
    "local_auth_enabled",
    "bootstrap_enabled",
    "localhost_only",
    "allow_trusted_proxy",
    "token_sha256",
    "expires_at",
    "state_path",
    "state_sha256",
    "state_expires_at",
}
_STATUS_FLAGS = {"local_auth_enabled", "bootstrap_enabled", "localhost_only", "allow_trusted_proxy"}
_QUERY_MODULE = "core.entrypoints.cli.bootstrap_query"


def _compose_plugin_version_supported(output: str) -> bool:
    # Accept one normal CLI line terminator, never multiple lines or loose suffixes.
    if len(output) > 128:
        return False
    version = output.removesuffix("\n").removesuffix("\r")
    match = re.fullmatch(
        r"v?(?P<major>0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
        r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
        version,
    )
    if match is None or int(match.group("major")) < 2:
        return False
    return not any(
        item.isdigit() and len(item) > 1 and item.startswith("0")
        for item in (match.group("prerelease") or "").split(".")
    )


class _ReadinessLog:
    path: Path = Path("/dev/null")

    def close(self) -> None:
        pass

    def write(self, message: str) -> None:
        # Recovery exposes only its bounded safe errors, never probe diagnostics.
        pass


class BootstrapRecoveryGateway:
    def __init__(self, *, installer: DockerInstallerGateway, selection: Mapping[str, str]) -> None:
        self.installer = installer
        self.selection = dict(selection)
        self.store = cast(LifecycleConfigurationStore, installer._configuration.store)
        self.state = installer.state_store
        self.snapshot_path = self.state.root / "operations/bootstrap-recovery"
        self.config: DeploymentConfig
        self.prefix: list[str] = []
        self.image = ""
        self.previous_status: dict[str, object] = {}
        self.expected_status: dict[str, object] = {}

    def operation_lock(self) -> AbstractContextManager[object]:
        return self.state.operation_lock()

    def _failure(self, message: str, *, recovery: bool = False) -> InstallerFailure:
        return InstallerFailure(
            code="bootstrap_recovery_needed" if recovery else "bootstrap_recovery_precondition",
            cause=message,
            remediation="Reconcile the selected installation and its retained management runtime before retrying.",
            exit_code=8 if recovery else 4,
            retryable=not recovery,
            step="bootstrap",
        )

    def prepare(self) -> None:
        try:
            self._prepare()
        except (OSError, ValueError, KeyError, TypeError):
            raise self._failure(
                "The selected bootstrap recovery target is unavailable or inconsistent."
            ) from None

    def _prepare(self) -> None:
        if self.snapshot_path.exists() or self.snapshot_path.is_symlink():
            raise self._failure(
                "Unfinished bootstrap recovery material requires reconciliation.", recovery=True
            )
        if any(
            path.exists()
            for path in (
                self.store.active_publication_pending_path,
                self.state.pending_release_path,
                self.state.release_handoff_path,
                self.state.release_transition_binding_path,
            )
        ):
            raise self._failure("An unresolved configuration or release publication is present.")
        if LifecycleStateStore(self.state.root).unfinished_journals():
            raise self._failure("An unfinished lifecycle operation requires reconciliation.")
        self.pending_identity = (
            self.store.pending_path.read_bytes() if self.store.pending_path.exists() else None
        )
        config = self.store.read_active()
        if config is None or config.deployment_profile != "scalable":
            raise self._failure(
                "Host bootstrap recovery requires an installed Scalable configuration."
            )
        self.config = config
        self.install_config = InstallConfig.model_validate(config.model_dump(mode="json"))
        target = self.installer._configuration.derive_target(config)
        root = self.installer.repo_root
        self.selected_env = Path(self.selection.get("FS_ENV_FILE", ""))
        self.selected_env = (
            self.selected_env if self.selected_env.is_absolute() else root / self.selected_env
        )
        control = self.store.read_env_values(self.store.compose_env_path)
        selected_owner = Path(control.get("FS_ENV_FILE") or target.env_file)
        selected_owner = selected_owner if selected_owner.is_absolute() else root / selected_owner
        selected_files = tuple(
            str((root / path).resolve())
            for path in self.selection.get("FS_COMPOSE_FILES", "").split()
        )
        expected_files = tuple(str((root / path).resolve()) for path in target.compose_files)
        if (
            self.selected_env.resolve() != selected_owner.resolve()
            or selected_files != expected_files
            or tuple(self.selection.get("FS_COMPOSE_PROFILES", "").split())
            != target.compose_profiles
            or self.selection.get("FS_COMPOSE_PROJECT_NAME") != target.project_name
            or self.selection.get("FS_STACK_ENVIRONMENT")
            not in {
                config.environment,
                "local_development" if config.environment == "development" else "production",
            }
            or Path(self.selection.get("FS_INSTALLER_STATE_DIR", "")).resolve()
            != self.state.root.resolve()
        ):
            raise self._failure(
                "Selected launcher descriptor disagrees with the canonical installation."
            )
        self.installer._active_project_identity = target.project_name
        self.service = self.installer._application_service(config)
        self.prefix = self.installer._compose_prefix(config, env_path=self.selected_env)
        require_safe_path(self.selected_env, allow_leaf_symlink=True)
        require_safe_path(self.store.env_path(config), allow_leaf_symlink=True)
        self.env_paths = tuple(
            dict.fromkeys((self.selected_env.resolve(), self.store.env_path(config).resolve()))
        )
        for path in (*self.env_paths, *(Path(path) for path in expected_files)):
            require_safe_path(path)
            if not path.is_file():
                raise ValueError("Required installation file is absent")
        for path in self.env_paths:
            auth = self.store.read_env_values(path).get("FS_LOCAL_AUTH_ENABLED", "1").lower()
            if auth not in {"1", "true", "on", "yes"}:
                raise self._failure("Local authentication must already be enabled.")
        self.paths = (
            *self.env_paths,
            self.state.root / "runtime/bootstrap-token-state.json",
            self.state.root / "secret-output/bootstrap-token",
            self.store.active_revision_path,
        )
        for path in self.paths:
            require_safe_path(path, allow_leaf_symlink=True)
        require_safe_path(self.snapshot_path / "manifest.json")
        self.snapshot = BootstrapSnapshot(self.snapshot_path, self.paths)
        if not self.installer._docker_cli_available():
            raise self._failure("Docker is unavailable in the host management runtime.")
        host = str(self.installer._environment.get("DOCKER_HOST", "")).lower()
        context = str(self.installer._environment.get("DOCKER_CONTEXT", "")).lower()
        if (host and not host.startswith("unix://")) or context not in {
            "",
            "default",
            "desktop-linux",
        }:
            raise self._failure("Bootstrap recovery requires the installed local Docker context.")
        endpoint = self._run(
            ("docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
        )
        if not endpoint.strip().startswith("unix://"):
            raise self._failure("Bootstrap recovery requires a local Docker endpoint.")
        version = self._run(("docker", "compose", "version", "--short"))
        if not _compose_plugin_version_supported(version):
            raise self._failure(
                "Bootstrap recovery requires the Docker Compose plugin, v2 or later."
            )
        self._run(("docker", "info", "--format", "{{.ID}}"))
        record = self._inspect()
        self.image = str(record["Image"])
        selected_image = self._run((*self.prefix, "config", "--images", self.service)).strip()
        if not selected_image or len(selected_image.split()) != 1:
            raise self._failure("The selected application image is unresolved.")
        if (
            self._run(("docker", "image", "inspect", "--format", "{{.Id}}", selected_image)).strip()
            != self.image
        ):
            raise self._failure(
                "The configured application image differs from the running container."
            )
        self._verify_public_health()
        self.previous_status = self._status()
        if self.previous_status["local_auth_enabled"] is not True:
            raise self._failure("The running application must already use local authentication.")

    def _run(self, command: Sequence[str], *, timeout: float = 30) -> str:
        try:
            outcome = self.installer.process.run(
                command,
                cwd=self.installer.repo_root,
                timeout_seconds=timeout,
                separate_stderr=True,
                environment={
                    "FS_INSTALLER_STATE_DIR": str(self.state.root),
                    "FS_DOCKER_HOST_PROJECT_ROOT": str(self.installer.repo_root),
                },
            )
        except (OSError, TimeoutError):
            raise self._failure(
                "A required Docker operation was unavailable or timed out."
            ) from None
        if outcome.returncode != 0 or len(outcome.stdout.encode()) > 16384:
            raise self._failure(
                "A required Docker operation failed or returned an invalid response."
            )
        # Process output is deliberately never projected into diagnostics or CLI logs.
        return outcome.stdout

    def _inspect(self) -> dict[str, Any]:
        identifier = self._run((*self.prefix, "ps", "-q", self.service)).strip()
        if not identifier or len(identifier.split()) != 1:
            raise self._failure("Exactly one selected application container must be running.")
        template = '{"Id":{{json .Id}},"Image":{{json .Image}},"State":{"Running":{{json .State.Running}}{{if .State.Health}},"Health":{"Status":{{json .State.Health.Status}}}{{end}}},"Config":{"Labels":{{json .Config.Labels}}},"Mounts":{{json .Mounts}}}'
        record = json.loads(
            self._run(("docker", "container", "inspect", "--format", template, identifier))
        )
        labels = record["Config"]["Labels"]
        state = record["State"]
        health = state.get("Health")
        if (
            state.get("Running") is not True
            or (
                "Health" in state
                and (not isinstance(health, dict) or health.get("Status") != "healthy")
            )
            or labels.get("com.docker.compose.project") != self.selection["FS_COMPOSE_PROJECT_NAME"]
            or labels.get("com.docker.compose.service") != self.service
            or not record.get("Image")
            or (self.image and record["Image"] != self.image)
        ):
            raise self._failure("The selected application identity or health is inconsistent.")
        if not any(
            mount.get("Destination") == "/run/flow-steward-runtime"
            and Path(mount.get("Source", "")).resolve() == (self.state.root / "runtime").resolve()
            for mount in record["Mounts"]
        ):
            raise self._failure(
                "The selected application does not mount this installation's runtime state."
            )
        return record

    def _verify_public_health(self) -> None:
        try:
            self.installer.verify_public_route(self.install_config, log=_ReadinessLog())
        except (InstallerFailure, OSError, ValueError):
            raise self._failure(
                "The selected application's public health route is unavailable."
            ) from None

    def _query(self, operation: str) -> dict[str, object]:
        try:
            payload = json.loads(
                self._run(
                    (
                        *self.prefix,
                        "exec",
                        "-T",
                        self.service,
                        "python",
                        "-m",
                        _QUERY_MODULE,
                        operation,
                    )
                )
            )
            if (
                not isinstance(payload, dict)
                or type(payload.get("schema_version")) is not int
                or payload["schema_version"] != 1
            ):
                raise ValueError("Invalid private probe schema")
            return payload
        except (TypeError, ValueError):
            raise self._failure(
                "The application returned an invalid private bootstrap probe."
            ) from None

    def user_count(self) -> int:
        payload = self._query("count")
        count = payload.get("count")
        if set(payload) != {"schema_version", "count"} or type(count) is not int or count < 0:
            raise self._failure("The application could not prove its all-row user count.")
        return count

    def _status(self) -> dict[str, object]:
        status = self._query("status")
        if (
            set(status) != _STATUS_FIELDS
            or any(type(status[name]) is not bool for name in _STATUS_FLAGS)
            or any(
                not isinstance(status[name], str)
                for name in _STATUS_FIELDS - _STATUS_FLAGS - {"schema_version"}
            )
        ):
            raise self._failure("The application returned invalid bootstrap metadata.")
        for name in ("token_sha256", "state_sha256"):
            if status[name] and not re.fullmatch(r"[a-f0-9]{64}", str(status[name])):
                raise self._failure("The application returned invalid bootstrap digests.")
        if status["state_path"] not in {"", "/run/flow-steward-runtime/bootstrap-token-state.json"}:
            raise self._failure("The application returned a foreign bootstrap state path.")
        for field in ("expires_at", "state_expires_at"):
            value = str(status[field])
            try:
                if len(value) > 64 or (
                    value and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None
                ):
                    raise ValueError("Invalid expiry")
            except ValueError:
                raise self._failure(
                    "The application returned an invalid bootstrap expiry."
                ) from None
        return status

    def capture(self) -> None:
        self.snapshot.capture(
            {
                "status": self.previous_status,
                "project": self.selection["FS_COMPOSE_PROJECT_NAME"],
                "compose_prefix": self.prefix,
                "service": self.service,
                "image": self.image,
            }
        )

    def publish(self, *, token: str, expires_at: datetime) -> str:
        output = self.installer.publish_bootstrap_token(
            self.install_config, token=token, expires_at=expires_at, env_paths=self.env_paths
        )
        digest = hashlib.sha256(token.encode()).hexdigest()
        from core.infrastructure.installer.gateway_support import _is_loopback

        self.expected_status = {
            "schema_version": 1,
            "local_auth_enabled": True,
            "bootstrap_enabled": True,
            "localhost_only": _is_loopback(self.config.public_host),
            "allow_trusted_proxy": False,
            "token_sha256": digest,
            "expires_at": expires_at.isoformat(),
            "state_path": "/run/flow-steward-runtime/bootstrap-token-state.json",
            "state_sha256": digest,
            "state_expires_at": expires_at.isoformat(),
        }
        return str(
            output.relative_to(self.installer.repo_root)
            if output.is_relative_to(self.installer.repo_root)
            else output
        )

    def activate(self) -> None:
        self.installer.restart_bootstrap_application(
            self.install_config, env_path=self.selected_env
        )

    def verify(self, *, restored: bool = False) -> None:
        expected = self.previous_status if restored else self.expected_status
        self._verify_public_health()
        for attempt in range(20):
            try:
                self._inspect()
                if self._status() != expected:
                    raise self._failure(
                        "Running bootstrap metadata does not match the intended publication."
                    )
                required = self.installer._probe_bootstrap_required(
                    self.install_config,
                    self.installer.product_url(self.install_config) + "/api/app/bootstrap",
                    require_first_admin=not restored,
                )
                if not restored and required is not True:
                    raise self._failure(
                        "The application did not require first-administrator bootstrap."
                    )
                pending = (
                    self.store.pending_path.read_bytes()
                    if self.store.pending_path.exists()
                    else None
                )
                if pending != self.pending_identity:
                    raise self._failure("Pending configuration changed during bootstrap recovery.")
                if self.store.read_active() != self.config:
                    raise self._failure(
                        "Canonical configuration changed during bootstrap recovery."
                    )
                return
            except (InstallerFailure, OSError, ValueError, KeyError, TypeError):
                if attempt == 19:
                    raise self._failure(
                        "Bootstrap activation verification did not converge."
                    ) from None
                self.installer._sleep(0.5)

    def restore(self) -> None:
        self.previous_status = self.snapshot.restore()["status"]

    def finish(self) -> None:
        self.snapshot.finish()
