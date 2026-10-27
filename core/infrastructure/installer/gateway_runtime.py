"""Runtime technical operations for the installer adapter."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from core.application.installer.contracts import (
    InstallConfig,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerLog
from core.application.lifecycle.contracts import (
    DeploymentConfig,
)
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from core.infrastructure.installer.core_dependency_images import (
    core_service_names,
)
from core.infrastructure.installer.gateway_support import (
    _WORKER_TOKEN_ENV,
    _compose_ps_rows,
    _last_output_line,
    _read_env,
)

if TYPE_CHECKING:
    from core.infrastructure.installer.gateway import DockerInstallerGateway


class RuntimeOperations:
    def _prepare_default_host_artifact_root(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        if (
            config.deployment_profile != "scalable"
            or config.data_placement != "host_directories"
            or config.file_storage != "local_fs"
        ):
            return
        environment = _read_env(self._env_path(config))
        default_logs = self.repo_root / "logs"
        selected = Path(str(environment.get("FS_LOGS_DIR") or default_logs))
        if not selected.is_absolute():
            selected = self.repo_root / "infra/docker" / selected
        try:
            if selected.is_symlink():
                raise ValueError("FS_LOGS_DIR must not be a symlink")
            if selected.resolve() != default_logs:
                return
            # Create this installation's defaults as the persisted host user
            # before any Compose invocation can create root-owned bind sources.
            # External roots and existing permissions remain operator-owned.
            for directory in (default_logs, default_logs / "artifacts"):
                if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                    raise ValueError("The default artifact root must be a real directory")
                directory.mkdir(mode=0o700, exist_ok=True)
        except (OSError, ValueError) as exc:
            log.write(f"Default artifact root preparation failed: {exc}")
            raise InstallerFailure(
                code="host_artifact_root_unavailable",
                cause=f"The installation-owned artifact root could not be prepared: {exc}",
                remediation="Repair the default logs directory ownership or select an existing FS_LOGS_DIR, then resume ./install.sh.",
                exit_code=8,
                retryable=True,
                step="core_start",
            ) from exc

    def start_core(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        self._prepare_default_host_artifact_root(config, log=log)
        self.restore_container_names(config)
        services = core_service_names(config)
        safety_args = self._exact_prebuilt_compose_safety_args(config)
        self._checked(
            [*self._compose_prefix(config), "up", "-d", *safety_args, *services],
            log=log,
            code="core_start_failed",
            remediation="Inspect the installer log and container health, then resume ./install.sh.",
            exit_code=8,
        )

    def start_runtime(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        self._prepare_default_host_artifact_root(config, log=log)
        self.restore_container_names(config)
        services = (
            ["compact-runtime"]
            if config.deployment_profile == "compact"
            else ["fs-mcp", "fs-app", "fs-extension-installer", "fs-worker", "control-plane"]
        )
        if config.environment == "development":
            services.extend(("fs-frontend", "fs-test-runner", "fs-audit"))
        safety_args = self._exact_prebuilt_compose_safety_args(config)
        self._checked(
            [*self._compose_prefix(config), "up", "-d", *safety_args, *services],
            log=log,
            code="core_start_failed",
            remediation="Inspect the installer log and container health, then resume ./install.sh.",
            exit_code=8,
        )

    def _exact_prebuilt_compose_safety_args(
        self: DockerInstallerGateway,
        config: DeploymentConfig,
    ) -> tuple[str, ...]:
        environment = _read_env(self._env_path(config))
        safety_args: list[str] = []
        if self._complete_exact_prebuilt_image_refs(config, environment) is not None:
            safety_args.extend(("--no-build", "--pull", "never"))
        return tuple(safety_args)

    def _exact_prebuilt_compose_run_safety_args(
        self: DockerInstallerGateway,
        config: DeploymentConfig,
    ) -> tuple[str, ...]:
        environment = _read_env(self._env_path(config))
        safety_args: list[str] = []
        if self._complete_exact_prebuilt_image_refs(config, environment) is not None:
            safety_args.extend(("--pull", "never"))
        return tuple(safety_args)

    def ensure_worker_identity(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        if config.deployment_profile == "compact":
            # AC17-R142: the Compact runtime owns this identity. It issues the credential
            # at bootstrap into its data volume and hands it to its worker roles, so a
            # copy issued here was never used -- but issuing it revoked the one the
            # running runtime held, and writing it to .env.prod changed the runtime's
            # configuration, so every rerun of ./install.sh recreated the whole runtime
            # and left a revoked token in the env file.
            log.write("Compact: the runtime owns its worker machine identity; none issued.")
            return
        host_env_path = self._env_path(config).resolve()
        env_parent = "/run/flow-steward-installer"
        env_path = f"{env_parent}/{host_env_path.name}"
        env_mount = f"{host_env_path.parent}:{env_parent}:rw"
        runtime_user = f"{os.getuid()}:{os.getgid()}"
        account_id = str(
            self._environment.get("FS_DEFAULT_ACCOUNT_ID") or DEFAULT_ACCOUNT_ID
        ).strip()
        safety_args = self._exact_prebuilt_compose_run_safety_args(config)
        self._checked(
            [
                *self._compose_prefix(config),
                "run",
                "--rm",
                "--no-deps",
                *safety_args,
                "--user",
                runtime_user,
                "--volume",
                env_mount,
                "--entrypoint",
                "python",
                self._application_service(config),
                "-m",
                "core.entrypoints.cli.ops.worker_identity_bootstrap",
                "--env-file",
                env_path,
                "--account-id",
                account_id,
            ],
            log=log,
            code="worker_identity_bootstrap_failed",
            remediation=(
                "Verify the account and database schema, then rerun ./install.sh. "
                "No queued jobs were deleted."
            ),
            exit_code=8,
        )
        log.write("Worker machine identity is ready; its credential was not logged.")

    def drop_superseded_worker_credential(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        """Remove the installer's old copy of the Compact worker credential from .env.prod.

        AC17-R142: before the runtime owned its identity, the installer issued a token
        into .env.prod on every run, and the runtime revoked it minutes later. Installs
        from before that change keep the revoked value -- unused, but a trap for anyone
        who takes the worker token from the env file. Run only during an update, where
        the runtime is recreated anyway.
        """
        if config.deployment_profile != "compact":
            return
        env_path = self._env_path(config)
        if not str(_read_env(env_path).get(_WORKER_TOKEN_ENV) or "").strip():
            return
        self.state_store.update_env_values(env_path, {_WORKER_TOKEN_ENV: ""})
        log.write(
            "Removed the installer's superseded copy of the worker credential from "
            f"{env_path.name}; the Compact runtime owns that identity."
        )

    def activate_worker_identity(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        safety_args = self._exact_prebuilt_compose_safety_args(config)
        self._checked(
            [
                *self._compose_prefix(config),
                "up",
                "-d",
                "--no-deps",
                *safety_args,
                ("compact-runtime" if config.deployment_profile == "compact" else "fs-worker"),
            ],
            log=log,
            code="worker_identity_activation_failed",
            remediation=(
                # AC17-R106: the command five lines up already chose the right service
                # for the profile; the sentence telling the operator what to look at
                # did not, and named a container Compact does not run.
                f"Inspect {self._operator_container('fs-worker', config)} startup, "
                "correct the worker identity readiness error, and rerun ./install.sh. "
                "Queued jobs remain durable."
            ),
            exit_code=8,
        )

    def verify_schema(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        safety_args = self._exact_prebuilt_compose_run_safety_args(config)
        self._checked(
            [
                *self._compose_prefix(config),
                "run",
                "--rm",
                "--no-deps",
                *safety_args,
                "memory-postgres-extensions",
            ],
            log=log,
            code="schema_readiness_failed",
            remediation="Inspect PostgreSQL health and schema output, then resume ./install.sh.",
            exit_code=8,
        )
        self._verify_migration_history(config, safety_args, log=log)
        self._checked(
            [
                *self._compose_prefix(config),
                "run",
                "--rm",
                "--no-deps",
                *safety_args,
                "--entrypoint",
                "python",
                self._application_service(config),
                "-m",
                "core.application.tooling.db.bootstrap_seed",
                "--author=installer",
            ],
            log=log,
            code="schema_seed_failed",
            remediation=(
                "Inspect the canonical seed error, correct the database state, and resume "
                "./install.sh. The canonical seed command is idempotent."
            ),
            exit_code=8,
            step="schema",
        )
        self._checked(
            [
                *self._compose_prefix(config),
                "run",
                "--rm",
                "--no-deps",
                *safety_args,
                "--entrypoint",
                "python",
                self._application_service(config),
                "-c",
                (
                    "from core.application.tooling.db.init_db import "
                    "init_workflow_knowledge_graph; init_workflow_knowledge_graph()"
                ),
            ],
            log=log,
            code="workflow_knowledge_graph_bootstrap_failed",
            remediation=(
                "Inspect the Apache AGE extension and workflow knowledge graph error, then "
                "resume ./install.sh. Graph initialization is idempotent."
            ),
            exit_code=8,
            step="schema",
        )

    def precheck_migrations(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        """AC17-R160: the release's migration check, before any core service restarts."""
        self._start_stopped_database(config, log=log)
        self._verify_migration_history(
            config, self._exact_prebuilt_compose_run_safety_args(config), log=log
        )

    def _start_stopped_database(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        """AC17-R162: an update of a stopped stack found no database to check against.

        `--no-recreate`: the existing container on the image it already has. Moving
        Postgres to the release's image is the `start` step's, after this check.
        (`compose start --wait` is not in the Compose the installer runs.)
        """
        outcome = self.process.run(
            [*self._compose_prefix(config), "ps", "--all", "--format", "json", "pg-fs"],
            separate_stderr=True,
        )
        if outcome.stderr:
            log.write(outcome.stderr)
        rows = _compose_ps_rows(outcome.stdout) if outcome.returncode == 0 else []
        states = {str(row.get("State") or row.get("state") or "").lower() for row in rows}
        if not rows or states == {"running"}:
            return  # running, or no container yet: the `start` step creates it
        log.write("Starting the stopped database alone for the migration check.")
        self._checked(
            [
                *self._compose_prefix(config),
                *("up", "-d", "--no-deps", "--no-recreate", "--wait", "pg-fs"),
            ],
            log=log,
            code="core_start_failed",
            remediation="Inspect the installer log and the pg-fs container, then rerun ./install.sh.",
            exit_code=8,
        )

    def _verify_migration_history(
        self: DockerInstallerGateway,
        config: InstallConfig,
        safety_args: tuple[str, ...],
        *,
        log: InstallerLog,
    ) -> None:
        try:
            self._check_migration_baseline(config, safety_args, log=log)
        except InstallerFailure as failure:
            # AC17-R159: a rollback across a migration. The remediation below is for the
            # other direction -- apply the new migration -- and would send the operator
            # to `database upgrade`, which cannot take a schema back.
            reason = _last_output_line(failure.cause)
            if "this release does not know" in failure.cause:
                raise InstallerFailure(
                    code="database_newer_than_release",
                    cause=reason or failure.cause,
                    remediation=(
                        "This is a rollback across a database migration. The application that "
                        "is running was not restarted and stays on its version. Check out the "
                        "newer release again and rerun ./install.sh, or restore the backup "
                        "taken before its migration as docs/operations/compact-scalable-"
                        "deployment.md describes under 'Going back across a database "
                        "migration', then rerun ./install.sh."
                    ),
                    exit_code=8,
                    retryable=False,
                    step="schema",
                    log_path=failure.log_path,
                ) from failure
            if "Migration history already exists but does not match this release" in failure.cause:
                raise InstallerFailure(
                    code="release_migration_required",
                    cause=reason or failure.cause,
                    remediation=failure.remediation,
                    exit_code=8,
                    retryable=False,
                    step="schema",
                    log_path=failure.log_path,
                ) from failure
            raise

    def _check_migration_baseline(
        self: DockerInstallerGateway,
        config: InstallConfig,
        safety_args: tuple[str, ...],
        *,
        log: InstallerLog,
    ) -> None:
        self._checked(
            [
                *self._compose_prefix(config),
                "run",
                "--rm",
                "--no-deps",
                *safety_args,
                "--entrypoint",
                "python",
                self._application_service(config),
                "-m",
                "core.application.tooling.db.upgrade_db",
                "--baseline-migration-history",
                "--author=installer",
            ],
            log=log,
            code="schema_baseline_failed",
            # AC17-R141: this is where an update whose release carries a database
            # migration stops, with the running application still on its old image
            # and its old schema. The old text sent the operator back to ./install.sh,
            # which stopped here again; it never named the command that resolves it.
            remediation=(
                "When the output says migration history does not match this release, "
                "this update carries a database migration the installer does not "
                "apply. An application already running was not restarted and stays on "
                "its previous version. Enable maintenance with ./flow-steward maintenance "
                "enable --confirm, take a backup with ./flow-steward backup create, "
                "apply the migration with ./flow-steward database upgrade "
                "--maintenance-confirmed --confirm, then rerun ./install.sh and disable "
                "maintenance. Otherwise run ./flow-steward database status and inspect "
                "the output above."
            ),
            exit_code=8,
        )

    def verify_storage(
        self: DockerInstallerGateway,
        config: InstallConfig,
        *,
        log: InstallerLog,
        read_only: bool = False,
    ) -> None:
        if config.file_storage == "local_fs":
            read_only_value = "True" if read_only else "False"
            script = (
                "import os,uuid; from pathlib import Path; "
                "root=Path(os.environ.get('ARTIFACT_LOCAL_ROOT_DIR') or '/data/artifacts'); "
                f"read_only={read_only_value}; "
                "\nif read_only:\n"
                "  if not root.is_dir(): raise FileNotFoundError(root)\n"
                "  with os.scandir(root) as entries: next(entries, None)\n"
                "else:\n"
                "  root.mkdir(parents=True, exist_ok=True)\n"
                "  probe=root / ('.flow-steward-installer-probe-' + uuid.uuid4().hex)\n"
                "  try:\n"
                "    with probe.open('xb') as stream: stream.write(b'')\n"
                "  finally:\n"
                "    probe.unlink(missing_ok=True)"
            )
            self._checked(
                [
                    *self._compose_prefix(config),
                    "exec",
                    "-T",
                    self._application_service(config),
                    "python",
                    "-c",
                    script,
                ],
                log=log,
                code="storage_verification_failed",
                remediation=(
                    "Verify the persistent local artifact directory and write permission, "
                    "then resume ./install.sh."
                ),
                exit_code=8,
            )
            return
        create_bucket = "True" if config.file_storage == "bundled" and not read_only else "False"
        read_only_value = "True" if read_only else "False"
        client_options = (
            "aws_access_key_id=os.environ['ARTIFACT_S3_ACCESS_KEY'], "
            "aws_secret_access_key=os.environ['ARTIFACT_S3_SECRET_KEY']"
        )
        script = (
            "import os,uuid,boto3; from botocore.exceptions import ClientError; "
            "bucket=os.environ['ARTIFACT_S3_BUCKET']; "
            "client=boto3.client('s3', endpoint_url=os.environ['ARTIFACT_S3_ENDPOINT_URL'], "
            f"{client_options}, "
            "region_name=os.environ.get('ARTIFACT_S3_REGION') or 'us-east-1', "
            "use_ssl=os.environ.get('ARTIFACT_S3_USE_SSL','true').lower()=='true'); "
            f"create_bucket={create_bucket}; "
            f"read_only={read_only_value}; "
            "\ntry: client.head_bucket(Bucket=bucket)\n"
            "except ClientError as exc:\n"
            "  code=str(exc.response.get('Error',{}).get('Code',''))\n"
            "  if create_bucket and code in {'404','NoSuchBucket','NotFound'}: "
            "client.create_bucket(Bucket=bucket)\n"
            "  else: raise\n"
            "if not read_only:\n"
            "  key='.flow-steward-installer-probe-'+uuid.uuid4().hex\n"
            "  client.put_object(Bucket=bucket,Key=key,Body=b'')\n"
            "  client.delete_object(Bucket=bucket,Key=key)"
        )
        command = [
            *self._compose_prefix(config),
            "exec",
            "-T",
            self._application_service(config),
            "python",
            "-c",
            script,
        ]
        self._checked(
            command,
            log=log,
            code="storage_verification_failed",
            remediation="Verify the selected storage endpoint, bucket, credentials, and write permission, then resume ./install.sh.",
            exit_code=8,
        )

    def _health_expected_services(self: DockerInstallerGateway, config: InstallConfig) -> set[str]:
        expected = set(self._canonical_target(config).runtime_services)
        expected.difference_update(
            {
                "fs-cli",
                "fs-frontend",
                "fs-test-runner",
                "fs-audit",
                "pg-audit",
                "rabbitmq-audit",
            }
        )
        return expected

    # Item 5, step 4 -- the Docker side of ./install.sh --deployment-profile. The order
    # of the switch is core/application/installer/profile_switch.py.
