"""Configuration technical operations for the installer adapter."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from core.application.installer.contracts import (
    InstallConfig,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerLog
from core.application.lifecycle.contracts import (
    profile_owned_environment,
)
from core.application.release.catalog import DEFAULT_RELEASE_REPOSITORY
from core.application.release.contracts import ReleaseIdentity
from core.infrastructure.config.env_registry import (
    build_installer_env,
)
from core.infrastructure.installer.gateway_support import (
    _FULL_GIT_REVISION,
    _INGRESS_OWNED_TLS_MODES,
    _INSTALLER_SECRET_NAMES,
    _RUNTIME_CERTIFICATE_PATH,
    _RUNTIME_PRIVATE_KEY_PATH,
    _TOKEN_COMPOSE_ENV,
    _TOKEN_HEAD_COMMIT,
    _env_selection,
    _future_timestamp,
    _is_ip,
    _is_loopback,
    _minted_bootstrap_token,
    _public_bind_host,
    _read_env,
)
from core.infrastructure.installer.registry_image_resolver import (
    DEFAULT_OFFICIAL_REGISTRIES,
)
from core.infrastructure.installer.secret_values import is_missing_or_placeholder_secret
from core.infrastructure.installer.storage_identity import BundledStorageIdentityService

if TYPE_CHECKING:
    from core.infrastructure.installer.gateway import DockerInstallerGateway


class ConfigurationOperations:
    def set_operator_settings(
        self: DockerInstallerGateway,
        config: InstallConfig,
        *,
        environment: Mapping[str, str],
        log: InstallerLog,
    ) -> InstallConfig:
        """Write host settings from ./install.sh --setting and restart the runtime with them.

        *config* already carries the merged settings and *environment* the keys to write
        (a reset one with its default); the installer service decides both. A run on an
        unchanged revision renders no environment, so the keys are written here, one by
        one: nothing else in the file is rewritten. stack.toml -- the applied
        configuration -- is written last, so a run killed before it applies them again.
        """
        self.state_store.update_env_values(self._env_path(config), environment)
        self._configuration.store.write_active(config)
        log.write(
            "Recorded host settings: "
            + ", ".join(f"{name}={value}" for name, value in sorted(environment.items()))
            + "."
        )
        self.start_runtime(config, log=log)
        return config

    def ensure_configuration(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        self._bootstrap_isolation_overrides(step="configuration")
        target = self._canonical_target(config)
        store = self._configuration.store
        store.write_active(config)
        compose_env_path = self.state_store.root / _TOKEN_COMPOSE_ENV
        if not compose_env_path.exists():
            self.state_store.write_owner_only_text(
                compose_env_path,
                f"FS_COMPOSE_PROJECT_NAME={self._project_identity()}\n",
            )
        compose_values = self._configuration.compose_environment(config, target)
        compose_values.update({"FS_COMPOSE_PROJECT_NAME": self._project_identity()})
        store.write_compose_environment(compose_values)
        if config.tls_mode == "reverse_proxy" and config.proxy_placement == "host":
            override = self._reverse_proxy_override_path()
            self.state_store.write_owner_only_text(
                override,
                "services:\n"
                "  traefik:\n"
                '    profiles: ["installer-disabled-traefik"]\n'
                "  fs-app:\n"
                "    ports:\n"
                f'      - "{config.reverse_proxy_backend_host}:'
                f'{config.reverse_proxy_backend_port}:8099"\n'
                "    depends_on:\n"
                "      traefik:\n"
                "        condition: service_started\n"
                "        required: false\n",
            )
        log.write("Saved non-secret installer configuration.")

    def ensure_secrets(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        base_url = self.product_url(config)
        isolation = self._bootstrap_isolation_overrides(step="secrets")
        env_path = self._env_path(config)
        self.state_store.ensure_runtime_directory()
        self.state_store.ensure_env_secrets(
            env_path,
            _INSTALLER_SECRET_NAMES,
            secret_factory=self._secret_factory,
        )
        existing = _read_env(env_path)
        selection = _env_selection(config)
        managed = build_installer_env(
            selection=selection,
            existing=existing,
            secret_factory=self._secret_factory,
            deployment_profile=config.deployment_profile,
        )
        managed.update(profile_owned_environment(config.deployment_profile))
        managed.update(
            {
                "FS_RELEASE_REPOSITORY": str(
                    self._environment.get("FS_RELEASE_REPOSITORY")
                    or existing.get("FS_RELEASE_REPOSITORY")
                    or DEFAULT_RELEASE_REPOSITORY
                ).strip(),
                "FS_RELEASE_REGISTRIES": str(
                    self._environment.get("FS_RELEASE_REGISTRIES")
                    or existing.get("FS_RELEASE_REGISTRIES")
                    or ",".join(item.namespace for item in DEFAULT_OFFICIAL_REGISTRIES)
                ).strip(),
            }
        )
        managed.update(
            {
                "FS_COMPOSE_PROJECT_NAME": self._project_identity(),
                "FS_INSTALL_MODE": self._install_mode(config),
                "FS_PUBLIC_BASE_URL": base_url,
                "FS_PUBLIC_BIND_HOST": _public_bind_host(config.public_host),
                # AC17-R116: docker-compose.prod.yaml runs five services as
                # ${FS_HOST_UID:-1000}:${FS_HOST_GID:-1000} so they can write the
                # bind-mounted extensions directory, and the installer never recorded
                # them -- so a Scalable installation worked only for an operator whose
                # uid happened to be 1000, and otherwise died with a PermissionError on
                # a directory it had just created. The launcher already does this for
                # its own runs (`FS_HOST_UID=$(id -u)`); the installer did not.
                **dict(
                    zip(
                        ("FS_HOST_UID", "FS_HOST_GID"), map(str, self._host_identity()), strict=True
                    )
                ),
                "APP_PORT": str(config.public_port),
                # AC17-R82: bundled Traefik binds this second host port for its
                # HTTP->HTTPS redirect. Recording it makes the value the installation
                # actually uses visible in the env file and to `config validate`, instead
                # of an unwritten Compose default that only surfaces as a bind failure.
                # An operator override arrives via `isolation` below and still wins.
                **(
                    {"HTTP_PORT": str(self._resolved_redirect_http_port(config))}
                    if config.ingress_mode == "bundled_traefik"
                    else {}
                ),
                "FS_AUTH_TRUSTED_ORIGINS": base_url,
                "FS_AUTH_BOOTSTRAP_ENABLED": "1",
                "FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY": "1"
                if _is_loopback(config.public_host)
                else "0",
                "FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY": "0",
            }
        )
        if self._install_mode(config) == "source":
            revision = (
                self._checked(
                    ("git", "-C", str(self.repo_root), "rev-parse", _TOKEN_HEAD_COMMIT),
                    log=log,
                    separate_stderr=True,
                    code="source_revision_unavailable",
                    remediation="Restore the Git checkout and resume ./install.sh.",
                    exit_code=7,
                    step="secrets",
                )
                .stdout.strip()
                .lower()
            )
            if _FULL_GIT_REVISION.fullmatch(revision) is None:
                raise InstallerFailure(
                    code="source_revision_unavailable",
                    cause="The installer could not resolve the exact source Git revision.",
                    remediation="Restore the Git checkout and resume ./install.sh.",
                    exit_code=7,
                    retryable=True,
                    step="secrets",
                )
            managed["FS_BUILD_SHA"] = revision
        managed.update(isolation)
        if config.environment == "production":
            managed.update(
                {
                    "FS_CANDIDATE_STATE_DIR": str(self.state_store.root.resolve()),
                    "FS_CANDIDATE_PROD_ENV": str(env_path.resolve()),
                }
            )
        bundled_credentials = self._ensure_bundled_storage_credentials(config, existing)
        if config.file_storage == "external_s3":
            managed.update(self._external_s3_values(config))
        elif config.file_storage == "bundled":
            assert bundled_credentials is not None
            access_key, secret_key = bundled_credentials
            managed["ARTIFACT_S3_ACCESS_KEY"] = access_key
            managed["ARTIFACT_S3_SECRET_KEY"] = secret_key
        token = str(existing.get("FS_AUTH_BOOTSTRAP_TOKEN") or "").strip()
        expiry = str(existing.get("FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT") or "").strip()
        # AC17-R49/R50: reuse is here so that resuming an interrupted install keeps a link the
        # operator may already be holding. It must not keep a token whose hour is over: the env
        # file lives at the repo root, outside every volume, so an expired value otherwise
        # outlives `down -v` and the whole state directory, and the printed "within 60 minutes"
        # becomes a lie on the next run.
        if (
            is_missing_or_placeholder_secret(token)
            or not _future_timestamp(expiry)
            # A token this installation did not mint is an inheritance from a destroyed one,
            # not a link an operator is holding. Reuse exists for resumes, so it ends here.
            or not self.state_store.bootstrap_token_matches_state(token)
        ):
            token, expiry = _minted_bootstrap_token()
        managed["FS_AUTH_BOOTSTRAP_TOKEN"] = token
        managed["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"] = expiry
        managed["FS_AUTH_BOOTSTRAP_STATE_FILE"] = (
            "/run/flow-steward-runtime/bootstrap-token-state.json"
        )
        self.state_store.write_bootstrap_token_state(
            token=token, expires_at=datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        )
        self.state_store.write_secret_output("bootstrap-token", token)
        self.state_store.update_env_values(env_path, managed)
        _target, canonical_path, canonical_environment = self._configuration.prepare_environment(
            config
        )
        canonical_environment.update(managed)
        self._configuration.store.write_runtime_environment(
            canonical_path,
            canonical_environment,
        )
        log.write("Durable installer secrets are present; values were not logged.")

    def publish_release_environment(
        self: DockerInstallerGateway, config: InstallConfig, release: ReleaseIdentity
    ) -> None:
        """Expose release markers only after InstallerService completed verification."""
        values = {
            "FS_INSTALL_MODE": "official",
            "FS_RELEASE_VERSION": release.version,
            "FS_BUILD_SHA": release.commit_sha,
        }
        self.state_store.update_env_values(self._env_path(config), values)
        self._environment.update(values)

    def publish_bootstrap_token(
        self: DockerInstallerGateway,
        config: InstallConfig,
        *,
        token: str,
        expires_at: datetime,
        env_paths: tuple[Path, ...],
    ) -> Path:
        """Publish the installer's bootstrap policy using its existing secure writers."""
        values = {
            "FS_AUTH_BOOTSTRAP_ENABLED": "1",
            "FS_AUTH_BOOTSTRAP_TOKEN": token,
            "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT": expires_at.isoformat(),
            "FS_AUTH_BOOTSTRAP_STATE_FILE": "/run/flow-steward-runtime/bootstrap-token-state.json",
            "FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY": "1" if _is_loopback(config.public_host) else "0",
            "FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY": "0",
        }
        self.state_store._ensure_directory(self.state_store.root / "secret-output")
        self.state_store.write_bootstrap_token_state(token=token, expires_at=expires_at)
        output = self.state_store.write_secret_output("bootstrap-token", token)
        for path in dict.fromkeys(path.resolve() for path in env_paths):
            self.state_store.update_env_values(path, values)
        from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

        store = cast(LifecycleConfigurationStore, self._configuration.store)
        store.refresh_active_environment(config, env_path=store.env_path(config))
        return output

    def restart_bootstrap_application(
        self: DockerInstallerGateway,
        config: InstallConfig,
        *,
        env_path: Path | None = None,
    ) -> None:
        """Activate only the selected profile application without exposing process output."""
        command = [
            *self._compose_prefix(config, env_path=env_path),
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            self._application_service(config),
        ]
        try:
            outcome = self.process.run(
                command,
                cwd=self.repo_root,
                timeout_seconds=120,
                environment={
                    "FS_INSTALLER_STATE_DIR": str(self.state_store.root),
                    "FS_DOCKER_HOST_PROJECT_ROOT": str(self.repo_root),
                },
            )
            if outcome.returncode != 0:
                raise OSError("Application activation failed")
        except (OSError, TimeoutError):
            raise InstallerFailure(
                code="bootstrap_token_activation_failed",
                cause="The selected application could not activate the bootstrap configuration.",
                remediation="Inspect application health and retry the installed host recovery command.",
                exit_code=8,
                step="bootstrap",
            ) from None

    def reissue_bootstrap_token(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        """Installer resume shares publication and complete artifact compensation."""
        from core.infrastructure.installer.bootstrap_snapshot import BootstrapSnapshot
        from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

        store = cast(LifecycleConfigurationStore, self._configuration.store)

        env_path = self._env_path(config)
        _target, canonical_path, canonical_environment = self._configuration.prepare_environment(
            config
        )
        env_paths = tuple(dict.fromkeys((env_path.resolve(), canonical_path.resolve())))
        snapshot = BootstrapSnapshot(
            self.state_store.root / "operations/bootstrap-token-reissue",
            (
                *env_paths,
                self.state_store.root / "runtime/bootstrap-token-state.json",
                self.state_store.root / "secret-output/bootstrap-token",
                store.active_revision_path,
            ),
        )
        token, expiry = _minted_bootstrap_token()
        snapshot.capture(
            {"project": self._project_identity(), "service": self._application_service(config)}
        )
        attempted = False
        try:
            if not canonical_path.exists():
                self._configuration.store.write_runtime_environment(
                    canonical_path, canonical_environment
                )
            self.publish_bootstrap_token(
                config, token=token, expires_at=datetime.fromisoformat(expiry), env_paths=env_paths
            )
            attempted = True
            self.restart_bootstrap_application(config)
        except BaseException as original:
            try:
                snapshot.restore()
                if attempted:
                    self.restart_bootstrap_application(config)
                    # Installer resume has no running-environment verifier for Compact.
                    # A successful Compose exit alone cannot certify compensation.
                    raise ValueError("Runtime compensation requires reconciliation")
                snapshot.finish()
            except BaseException:
                raise InstallerFailure(
                    code="bootstrap_recovery_needed",
                    cause="Token activation failed and runtime compensation could not be verified; preserve the owner-only recovery material.",
                    remediation="Reconcile the installation using operations/bootstrap-token-reissue before retrying.",
                    exit_code=8,
                    retryable=False,
                    step="bootstrap",
                ) from None
            if isinstance(original, (KeyboardInterrupt, SystemExit)):
                raise
            raise InstallerFailure(
                code="bootstrap_token_activation_failed",
                cause="Token activation failed; previous bootstrap artifacts were restored.",
                remediation="Inspect application health and rerun ./install.sh.",
                exit_code=8,
                step="bootstrap",
            ) from None
        snapshot.finish()
        log.write("Activated a replacement first-admin setup token; the value was not logged.")

    def _ensure_bundled_storage_credentials(
        self: DockerInstallerGateway, config: InstallConfig, existing: Mapping[str, str]
    ) -> tuple[str, str] | None:
        bundled_credentials: tuple[str, str] | None = None
        if config.file_storage == "bundled" or config.environment == "production":
            identity_path = self.state_store.root / "runtime/seaweedfs-s3.json"
            identity_service = BundledStorageIdentityService(
                identity_path,
                secret_factory=self._secret_factory,
            )
            identity_service.ensure(
                existing_access_key=(
                    str(existing.get("ARTIFACT_S3_ACCESS_KEY") or "").strip()
                    if config.file_storage == "bundled"
                    else ""
                ),
                existing_secret_key=(
                    str(existing.get("ARTIFACT_S3_SECRET_KEY") or "").strip()
                    if config.file_storage == "bundled"
                    else ""
                ),
            )
            identity_credentials = identity_service.read()
            access_key = identity_credentials["aws_access_key_id"]
            secret_key = identity_credentials["aws_secret_access_key"]
            bundled_credentials = (access_key, secret_key)
        return bundled_credentials

    def ensure_tls(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        if config.tls_mode in _INGRESS_OWNED_TLS_MODES:
            log.write("TLS is owned by the selected ingress mode.")
            return
        key_path = self.repo_root / config.private_key_path
        cert_path = self.repo_root / config.certificate_path
        if config.tls_mode == "provided":
            if not (key_path.is_file() and cert_path.is_file()):
                raise InstallerFailure(
                    code="tls_files_unavailable",
                    cause="The configured TLS certificate or private key is unavailable.",
                    remediation="Provide readable certificate and key files and rerun ./install.sh.",
                    exit_code=5,
                    step="tls",
                )
            if not self._tls_material_valid(config, cert_path=cert_path, key_path=key_path):
                raise InstallerFailure(
                    code="tls_material_invalid",
                    cause="The provided certificate is expired, has the wrong SAN, or does not match its private key.",
                    remediation="Provide a valid matching certificate/key pair for the selected public host and rerun ./install.sh.",
                    exit_code=5,
                    step="tls",
                )
            self._sync_runtime_tls(cert_path=cert_path, key_path=key_path)
            return
        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_path.parent.chmod(0o700)
        if (
            key_path.exists()
            and cert_path.exists()
            and self._tls_material_valid(config, cert_path=cert_path, key_path=key_path)
        ):
            key_path.chmod(0o600)
            cert_path.chmod(0o644)
            self._sync_runtime_tls(cert_path=cert_path, key_path=key_path)
            log.write(f"Preserved existing valid TLS certificate at {cert_path}.")
            return
        host = config.public_host
        san = (
            f"IP:{host},DNS:localhost" if _is_ip(host) else f"DNS:{host},DNS:localhost,IP:127.0.0.1"
        )
        self._checked(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:3072",
                "-sha256",
                "-days",
                "365",
                "-nodes",
                "-subj",
                f"/CN={host}",
                "-addext",
                f"subjectAltName={san}",
                "-keyout",
                str(key_path),
                "-out",
                str(cert_path),
            ],
            log=log,
            code="tls_generation_failed",
            remediation="Install OpenSSL and verify write access to .ssl, then resume ./install.sh.",
            exit_code=7,
        )
        if not (key_path.exists() and cert_path.exists()):
            raise InstallerFailure(
                code="tls_generation_failed",
                cause="OpenSSL returned success without creating the certificate files.",
                remediation="Inspect the installer log, correct filesystem permissions, and resume.",
                exit_code=8,
                step="tls",
            )
        key_path.chmod(0o600)
        cert_path.chmod(0o644)
        self._sync_runtime_tls(cert_path=cert_path, key_path=key_path)
        log.write(f"Generated local certificate with SANs at {cert_path}.")

    def _sync_runtime_tls(self: DockerInstallerGateway, *, cert_path: Path, key_path: Path) -> None:
        runtime_cert = self.repo_root / _RUNTIME_CERTIFICATE_PATH
        runtime_key = self.repo_root / _RUNTIME_PRIVATE_KEY_PATH
        if cert_path.resolve() != runtime_cert.resolve():
            self.state_store.write_text(
                runtime_cert, cert_path.read_text(encoding="utf-8"), mode=0o644
            )
        if key_path.resolve() != runtime_key.resolve():
            self.state_store.write_text(
                runtime_key, key_path.read_text(encoding="utf-8"), mode=0o600
            )
        runtime_cert.chmod(0o644)
        runtime_key.chmod(0o600)

    def _tls_material_valid(
        self: DockerInstallerGateway, config: InstallConfig, *, cert_path: Path, key_path: Path
    ) -> bool:
        host_check = "-checkip" if _is_ip(config.public_host) else "-checkhost"
        checks = (
            ["openssl", "x509", "-checkend", "86400", "-noout", "-in", str(cert_path)],
            [
                "openssl",
                "x509",
                "-noout",
                host_check,
                config.public_host,
                "-in",
                str(cert_path),
            ],
        )
        if any(self.process.run(command, cwd=self.repo_root).returncode != 0 for command in checks):
            return False
        certificate_key = self.process.run(
            ["openssl", "x509", "-pubkey", "-noout", "-in", str(cert_path)],
            cwd=self.repo_root,
        )
        private_key = self.process.run(
            ["openssl", "pkey", "-pubout", "-in", str(key_path)],
            cwd=self.repo_root,
        )
        return bool(
            certificate_key.returncode == 0
            and private_key.returncode == 0
            and certificate_key.stdout.strip() == private_key.stdout.strip()
        )
