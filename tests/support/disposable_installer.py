"""Real-distribution installer acceptance support with exact owned cleanup."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pty
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import termios
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import boto3
import yaml
from botocore.config import Config
from botocore.exceptions import ClientError

from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

_MINIO_IMAGE = (
    "minio/minio:RELEASE.2025-07-23T15-54-02Z"
    "@sha256:d249d1fb6966de4d8ad26c04754b545205ff15a62e4fd19ebd0f26fa5baacbc0"
)
# A first install deliberately builds several Linux images and may exceed 30 minutes on
# Docker Desktop when the registry link is slow. Signal bounds are tested separately.
_INSTALL_TIMEOUT_SECONDS = 5_400
_READY_TIMEOUT_SECONDS = 90
_IMAGE_INSPECT_ATTEMPTS = 3
_IMAGE_INSPECT_RETRY_SECONDS = 0.2
_LOW_CAPACITY_FILESYSTEM_BYTES = 2 * 1024**3
_ACCEPTANCE_PORT_FIRST = 20_000
_ACCEPTANCE_PORT_STOP = 30_000
_ACCEPTANCE_PORT_ATTEMPTS = 1_000
_BUILDKIT_CACHE_ID = re.compile(r"^[a-z0-9]{25}$")
_DOCKER_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROOF = b"installer-proof"
_PTY_WINDOW = struct.pack("HHHH", 30, 100, 0, 0)
_PLAIN_FALLBACK_BANNER = b"Terminal is smaller than 80x24; using the plain interactive wizard."
_ALTERNATE_SCREEN_MARKERS = (b"\x1b[?1049h", b"\x1b[?1047h", b"\x1b[?47h")
_TEXTUAL_FORM_MARKER = b"Install or safely resume Flow Steward."
_TEXTUAL_REVIEW_MARKER = b"Review: Install or safely resume Flow Steward."
_PLAIN_OPTIONAL_PROMPT_MARKERS = (
    b"Development tools: ",
    b"Allow insecure S3 HTTP [y/N]: ",
)
_GLOBAL_CLI_FLAGS = frozenset({"--json", "--plain", "--no-color", "--verbose", "--non-interactive"})


def _installed_command_id(arguments: tuple[str, ...]) -> str:
    """Resolve the exact public registry identity used by evidence manifests."""
    command_arguments = tuple(
        argument for argument in arguments if argument not in _GLOBAL_CLI_FLAGS
    )
    matches = [
        spec.command_id
        for spec in PUBLIC_COMMAND_SPECS
        if command_arguments[: len(spec.path)] == spec.path
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"installed CLI arguments have no unique registry binding: {arguments}"
        )
    return matches[0]


_BUNDLED_S3_ROUND_TRIP_SCRIPT = (
    "import json,sys;"
    "import boto3;"
    "from botocore.config import Config;"
    "payload=json.loads(sys.stdin.read());"
    "client=boto3.client('s3',endpoint_url='http://seaweedfs:8333',"
    "aws_access_key_id=payload['access_key'],aws_secret_access_key=payload['secret_key'],"
    "region_name='us-east-1',config=Config(s3={'addressing_style':'path'}));"
    "proof=bytes.fromhex(payload['proof_hex']);"
    "client.put_object(Bucket=payload['bucket'],Key=payload['key'],Body=proof);"
    "value=client.get_object(Bucket=payload['bucket'],Key=payload['key'])['Body'].read();"
    "client.delete_object(Bucket=payload['bucket'],Key=payload['key']);"
    "print(value.hex())"
)
_BUNDLED_S3_SENTINEL_SCRIPT = (
    "import json,sys;"
    "import boto3;"
    "from botocore.config import Config;"
    "payload=json.loads(sys.stdin.read());"
    "client=boto3.client('s3',endpoint_url='http://seaweedfs:8333',"
    "aws_access_key_id=payload['access_key'],aws_secret_access_key=payload['secret_key'],"
    "region_name='us-east-1',config=Config(s3={'addressing_style':'path'}));"
    "action=payload['action'];"
    "client.put_object(Bucket=payload['bucket'],Key=payload['key'],"
    "Body=bytes.fromhex(payload['value_hex'])) if action=='put' else None;"
    "value=(client.get_object(Bucket=payload['bucket'],Key=payload['key'])['Body'].read()"
    " if action=='get' else bytes.fromhex(payload['value_hex']));"
    "print(value.hex())"
)
_LOW_CAPACITY_DOCKER_WRAPPER = """#!/usr/bin/env python3
import os
import sys

real_docker = os.environ["ACCEPTANCE_REAL_DOCKER"]
source = os.environ["ACCEPTANCE_DISTRIBUTION"]
image = os.environ["FS_INSTALLER_IMAGE"]
container_name = os.environ["ACCEPTANCE_BOOTSTRAP_CONTAINER"]
owner = os.environ["ACCEPTANCE_OWNER"]
project = os.environ["FS_COMPOSE_PROJECT_NAME"]
capacity = os.environ["ACCEPTANCE_TMPFS_BYTES"]
args = sys.argv[1:]
command_index = 2 if args[:1] == ["--host"] else 0
if len(args) <= command_index or args[command_index] != "run":
    os.execv(real_docker, [real_docker, *args])

run_args = args[command_index + 1 :]
image_index = run_args.index(image)
options = run_args[:image_index]
application_args = run_args[image_index + 1 :]
rewritten = []
index = 0
while index < len(options):
    option = options[index]
    if option == "-v" and index + 1 < len(options) and options[index + 1] == f"{source}:{source}":
        rewritten.extend(("-v", f"{source}:/source:ro"))
        index += 2
        continue
    if option == "-w" and index + 1 < len(options) and options[index + 1] == source:
        rewritten.extend(("-w", "/low-capacity"))
        index += 2
        continue
    if (
        option == "-e"
        and index + 1 < len(options)
        and options[index + 1] == f"FS_INSTALLER_STATE_DIR={source}/.flow-steward"
    ):
        rewritten.extend(
            ("-e", "FS_INSTALLER_STATE_DIR=/low-capacity/distribution/.flow-steward")
        )
        index += 2
        continue
    rewritten.append(option)
    index += 1
rewritten.extend(
    (
        "--name",
        container_name,
        "--label",
        f"com.flow-steward.disposable-owner={owner}",
        "--label",
        f"com.flow-steward.disposable-project={project}",
        "--mount",
        f"type=tmpfs,destination=/low-capacity,tmpfs-size={capacity},tmpfs-mode=1777",
        "--entrypoint",
        "/bin/sh",
    )
)
bootstrap = (
    'mkdir -p /low-capacity/distribution '
    '&& cp -R /source/. /low-capacity/distribution '
    '&& cd /low-capacity/distribution '
    '&& exec python3 "$@"'
)
rewritten_args = [
    *args[: command_index + 1],
    *rewritten,
    image,
    "-c",
    bootstrap,
    "sh",
    *application_args,
]
os.execv(real_docker, [real_docker, *rewritten_args])
"""
_TEST_RUNNER_COMMAND_KEYS = {
    ("extensions", "test"),
    *{
        ("testing", command)
        for command in (
            "smoke-test",
            "nightfire-regression",
            "fuzz-api",
            "fuzz-mcp",
            "fuzz-ui",
            "cutover-preflight",
            "cutover-smoke",
        )
    },
}
_SERVICE_NAMES = (
    "traefik",
    "rabbitmq-fs",
    "mailpit",
    "pg-fs",
    "pg-audit",
    "rabbitmq-audit",
    "memory-postgres-extensions",
    "fs-mcp",
    "fs-app",
    "fs-cli",
    "fs-worker",
    "control-plane",
    "fs-frontend",
    "fs-test-runner",
    "fs-audit",
    "sonarqube",
    "sonar-scanner",
    "seaweedfs",
)
_VOLUME_NAMES = (
    "fs-frontend-node-modules",
    "fs-postgres",
    "fs-rabbitmq",
    "sonarqube-data",
    "sonarqube-extensions",
    "sonarqube-logs",
    "seaweedfs-data",
)
_PORT_KEYS = (
    "public",
    "http",
    "postgres",
    "rabbitmq_amqp",
    "rabbitmq_management",
    "mailpit_smtp",
    "mailpit_web",
    "frontend",
    "mcp",
    "sonarqube",
    "seaweedfs_s3",
    "seaweedfs_master",
    "external_s3",
    "reverse_proxy_backend",
)


@dataclass(frozen=True)
class InstallerProcessResult:
    """Captured result from the installed distribution launcher."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    compose_service: str = ""

    @property
    def combined_output(self) -> str:
        return self.stdout + self.stderr


@dataclass(frozen=True)
class PublicHTTPResult:
    """One public-route observation captured by disposable acceptance."""

    status: int
    payload: dict[str, Any]
    headers: dict[str, str]


class DisposableScenarioUnavailable(RuntimeError):
    """Raised when a scenario cannot be produced without faking the product boundary."""


class DisposableInstallerProject:
    """One cryptographically isolated installer distribution and Docker inventory."""

    def __init__(
        self,
        *,
        manifest_path: Path,
        source_root: Path,
        external_s3_secret: str = "",
    ) -> None:
        self.manifest_path = manifest_path
        self.source_root = source_root.resolve()
        self.cleaned = False
        self._last_result: InstallerProcessResult | None = None
        self._last_install: dict[str, str] = {}
        self._external_s3_secret = external_s3_secret
        self._interruption_evidence: dict[str, Any] = {}
        self._repeat_evidence: dict[str, Any] = {}

    @classmethod
    def create(
        cls,
        *,
        work_root: Path,
        source_root: Path,
        external_storage: bool = False,
        external_tls: bool = False,
        external_reachable: bool = True,
        image_version: str = "acceptance",
    ) -> DisposableInstallerProject:
        """Write the complete planned identity manifest before creating any resource."""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", image_version) is None:
            raise ValueError("disposable image version is invalid")
        work_root.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(16)
        project_name = f"fs-inst-{token}"
        distribution_dir = work_root / "distribution"
        state_dir = distribution_dir / ".flow-steward"
        credentials_path = work_root / "external-s3-credentials.json"
        certificate_dir = work_root / "external-s3-certs"
        installer_image = f"fs-installer-{token}:acceptance"
        external_s3_image = f"fs-external-s3-{token}:acceptance"
        app_image = f"fs-app-{token}:{image_version}"
        mcp_image = f"fs-mcp-{token}:{image_version}"
        cli_image = f"fs-cli-{token}:{image_version}"
        postgres_image = f"fs-postgres-{token}:{image_version}"
        containers = [f"{project_name}-{service}-1" for service in _SERVICE_NAMES]
        containers.append(f"fs-external-s3-{token}")
        volumes = [f"{project_name}_{name}" for name in _VOLUME_NAMES]
        volumes.append(f"fs-external-s3-data-{token}")
        networks = [f"{project_name}_default", f"{project_name}_artifact-storage"]
        images = [
            installer_image,
            external_s3_image,
            app_image,
            mcp_image,
            cli_image,
            postgres_image,
            *(f"{project_name}-{service}:latest" for service in _SERVICE_NAMES),
        ]
        ports = _reserve_unique_ports(_PORT_KEYS)
        external_secret = secrets.token_urlsafe(32) if external_storage else ""
        external_identity = (
            {
                "service": "minio",
                "container": f"fs-external-s3-{token}",
                "volume": f"fs-external-s3-data-{token}",
                "image": external_s3_image,
                "access_key_id": f"fstest{token[:20]}",
                "secret_sha256": hashlib.sha256(external_secret.encode()).hexdigest(),
                "bucket": f"fs-installer-{token}",
                "credentials_path": str(credentials_path),
                "certificate_directory": str(certificate_dir) if external_tls else "",
                "reachable": external_reachable,
            }
            if external_storage
            else None
        )
        manifest = {
            "schema_version": 1,
            "owner_token": token,
            "project_name": project_name,
            "ports": ports,
            "state_directory": str(state_dir),
            "distribution_directory": str(distribution_dir),
            "catalog_identity": f"installer-acceptance-catalog-{token}",
            "source_revision": _git_revision(source_root),
            "production_image_tags": {
                "app": app_image,
                "mcp": mcp_image,
                "cli": cli_image,
                "postgres": postgres_image,
            },
            "observed_image_ids": {},
            "observed_dependency_image_ids": {},
            "external_s3": external_identity,
            "dependency_images": [
                _MINIO_IMAGE,
            ],
            "planned_resources": {
                "containers": containers,
                "networks": networks,
                "volumes": volumes,
                "images": images,
                "paths": [
                    str(distribution_dir),
                    str(state_dir),
                    str(credentials_path),
                    str(certificate_dir),
                ],
            },
            "active_resources": {
                "containers": list(containers),
                "networks": list(networks),
                "volumes": list(volumes),
                "images": list(images),
                "paths": [
                    str(distribution_dir),
                    str(state_dir),
                    str(credentials_path),
                    str(certificate_dir),
                ],
            },
            "baseline_inventory": _docker_inventory(),
            "baseline_build_cache": sorted(_docker_build_cache_records()),
            "baseline_image_tags": _docker_image_tag_mappings(),
        }
        manifest_path = work_root / "disposable-installer-resources.json"
        _write_new_owner_only_json(manifest_path, manifest)
        return cls(
            manifest_path=manifest_path,
            source_root=source_root,
            external_s3_secret=external_secret,
        )

    @property
    def project_name(self) -> str:
        return str(self._manifest()["project_name"])

    @property
    def ports(self) -> dict[str, int]:
        return {key: int(value) for key, value in self._manifest()["ports"].items()}

    @property
    def state_directory(self) -> Path:
        return Path(str(self._manifest()["state_directory"]))

    @property
    def distribution_directory(self) -> Path:
        return Path(str(self._manifest()["distribution_directory"]))

    @property
    def external_s3_identity(self) -> dict[str, str] | None:
        value = self._manifest()["external_s3"]
        return dict(value) if isinstance(value, dict) else None

    @property
    def external_s3_secret(self) -> str:
        return self._external_s3_secret

    def install(
        self,
        *,
        environment: str,
        renderer: str,
        storage: str,
        tls_mode: str = "local",
    ) -> InstallerProcessResult:
        """Run the public launcher from a new source-free distribution checkout."""
        self._materialize_distribution()
        if storage == "external_s3":
            endpoint, credentials_path = self._start_external_s3(
                use_tls=environment == "production"
            )
        else:
            endpoint, credentials_path = "", None
        if tls_mode == "provided":
            self._create_provided_tls_material()
        args = self._installer_args(
            environment=environment,
            renderer=renderer,
            storage=storage,
            tls_mode=tls_mode,
            endpoint=endpoint,
            credentials_path=credentials_path,
        )
        result = self._run_launcher(args, renderer=renderer)
        self._last_result = result
        self._last_install = {
            "environment": environment,
            "renderer": renderer,
            "storage": storage,
            "tls_mode": tls_mode,
        }
        self._refresh_exact_docker_identities()
        return result

    def cli(self, *args: str, input_text: str | None = None) -> InstallerProcessResult:
        """Invoke one command through the installed public launcher."""
        if not args:
            raise ValueError("installed CLI invocation requires command arguments")
        command = [str(self.distribution_directory / "flow-steward"), *args]
        command_id = _installed_command_id(tuple(args))
        command_spec = next(spec for spec in PUBLIC_COMMAND_SPECS if spec.command_id == command_id)
        command_key = tuple(command_spec.path[:2])
        compose_service = (
            "fs-test-runner"
            if command_key in _TEST_RUNNER_COMMAND_KEYS
            else "fs-audit"
            if command_key in {("quality", "lint"), ("quality", "release-gate")}
            else "fs-cli"
        )
        process = subprocess.Popen(
            command,
            cwd=self.distribution_directory,
            env=self._installed_launcher_environment(),
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = _communicate_with_process_group_timeout(
            process,
            input_text=input_text,
            timeout_seconds=_INSTALL_TIMEOUT_SECONDS,
        )
        result = InstallerProcessResult(
            args=tuple(command),
            returncode=int(process.returncode),
            stdout=stdout,
            stderr=stderr,
            compose_service=compose_service,
        )
        if result.returncode != 0:
            operation_logs = sorted((self.state_directory / "operations/logs").glob("*.log"))
            operation_log = (
                operation_logs[-1].read_text(encoding="utf-8") if operation_logs else "unavailable"
            )
            diagnostic = operation_log[-8_192:]
            self._record_installed_cli_failure_evidence(
                command_id=command_id,
                returncode=result.returncode,
                diagnostic=diagnostic,
            )
            result = InstallerProcessResult(
                args=result.args,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr + "Lifecycle operation evidence:\n" + diagnostic + "\n",
                compose_service=result.compose_service,
            )
        manifest = self._manifest()
        evidence = manifest.setdefault("installed_cli_evidence", [])
        if not isinstance(evidence, list):
            raise AssertionError("installed CLI evidence manifest shape is invalid")
        evidence.append(
            {
                "command_id": command_id,
                "compose_service": compose_service,
                "exit_code": result.returncode,
            }
        )
        _replace_owner_only_json(self.manifest_path, manifest)
        self._refresh_exact_docker_identities()
        return result

    def repeat_install(self) -> InstallerProcessResult:
        if not self._last_install:
            raise RuntimeError("repeat_install requires one completed install")
        before = self._owned_resource_snapshot()
        external = self._last_install["storage"] == "external_s3"
        args = self._installer_args(
            environment=self._last_install["environment"],
            renderer="non_interactive",
            storage=self._last_install["storage"],
            tls_mode=self._last_install["tls_mode"],
            endpoint=self._external_endpoint() if external else "",
            credentials_path=self._external_credentials_path() if external else None,
        )
        result = self._run_launcher(args, renderer="non_interactive")
        after = self._owned_resource_snapshot()
        self._repeat_evidence = {"before": before, "after": after}
        return result

    def install_with_unreachable_s3(self) -> InstallerProcessResult:
        self._materialize_distribution()
        credentials_path = self._record_external_s3_identity(reachable=False, use_tls=True)
        endpoint = f"https://127.0.0.1:{self.ports['external_s3']}"
        args = self._installer_args(
            environment="production",
            renderer="non_interactive",
            storage="external_s3",
            tls_mode="local",
            endpoint=endpoint,
            credentials_path=credentials_path,
        )
        result = self._run_launcher(args, renderer="non_interactive")
        self._last_result = result
        self._refresh_exact_docker_identities()
        return result

    def install_with_occupied_public_port(self) -> InstallerProcessResult:
        self._materialize_distribution()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.ports["public"]))
        listener.listen(1)
        try:
            args = self._installer_args(
                environment="development",
                renderer="non_interactive",
                storage="bundled",
                tls_mode="local",
                endpoint="",
                credentials_path=None,
            )
            result = self._run_launcher(args, renderer="non_interactive")
        finally:
            listener.close()
        self._last_result = result
        self._refresh_exact_docker_identities()
        return result

    def install_with_ambiguous_volume(self) -> InstallerProcessResult:
        self._materialize_distribution()
        volume = f"{self.project_name}_fs-postgres"
        _run(
            [
                "docker",
                "volume",
                "create",
                "--label",
                f"com.docker.compose.project={self.project_name}",
                "--label",
                "com.docker.compose.volume=fs-postgres",
                volume,
            ]
        )
        args = self._installer_args(
            environment="development",
            renderer="non_interactive",
            storage="bundled",
            tls_mode="local",
            endpoint="",
            credentials_path=None,
        )
        result = self._run_launcher(args, renderer="non_interactive")
        self._last_result = result
        self._refresh_exact_docker_identities()
        return result

    def install_on_low_capacity_filesystem(self) -> InstallerProcessResult:
        wrapper, bootstrap_container, real_docker = self._prepare_low_capacity_projection()
        self._materialize_distribution()
        args = self._installer_args(
            environment="development",
            renderer="non_interactive",
            storage="bundled",
            tls_mode="local",
            endpoint="",
            credentials_path=None,
        )
        environment = {
            **self._launcher_environment(),
            "PATH": f"{wrapper.parent}{os.pathsep}{os.environ.get('PATH', '')}",
            "ACCEPTANCE_REAL_DOCKER": real_docker,
            "ACCEPTANCE_DISTRIBUTION": str(self.distribution_directory),
            "ACCEPTANCE_BOOTSTRAP_CONTAINER": bootstrap_container,
            "ACCEPTANCE_OWNER": str(self._manifest()["owner_token"]),
            "ACCEPTANCE_TMPFS_BYTES": str(_LOW_CAPACITY_FILESYSTEM_BYTES),
        }
        result = self._run_launcher(
            args,
            renderer="non_interactive",
            environment=environment,
        )
        self._last_result = result
        self._refresh_exact_docker_identities()
        return result

    def interrupt_and_restart(
        self,
    ) -> tuple[InstallerProcessResult, InstallerProcessResult]:
        self._materialize_distribution()
        args = self._installer_args(
            environment="development",
            renderer="non_interactive",
            storage="bundled",
            tls_mode="local",
            endpoint="",
            credentials_path=None,
        )
        command = [str(self.distribution_directory / "install.sh"), *args]
        stdout_path = self.distribution_directory / ".acceptance-interruption.stdout.log"
        stderr_path = self.distribution_directory / ".acceptance-interruption.stderr.log"
        signal_sent_at = 0.0
        signal_started = 0.0
        bootstrap_container_id = ""
        with (
            stdout_path.open("w", encoding="utf-8") as stdout_log,
            stderr_path.open("w", encoding="utf-8") as stderr_log,
        ):
            process = subprocess.Popen(
                command,
                cwd=self.distribution_directory,
                env=self._launcher_environment(),
                stdout=stdout_log,
                stderr=stderr_log,
                text=True,
                start_new_session=True,
            )
            deadline = time.monotonic() + _INSTALL_TIMEOUT_SECONDS
            interruption_journal: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                journal = self.state_directory / "install-state.json"
                if journal.is_file():
                    payload = json.loads(journal.read_text(encoding="utf-8"))
                    build_process_tree = self._bootstrap_build_process_tree()
                    if build_process_tree:
                        interruption_journal = payload
                        bootstrap_container_id = self._bootstrap_container_id()
                        signal_sent_at = time.time()
                        signal_started = time.monotonic()
                        os.killpg(process.pid, signal.SIGINT)
                        break
                if process.poll() is not None:
                    break
                time.sleep(0.25)
            try:
                process.wait(timeout=120)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise AssertionError(
                    "interrupted installer did not terminate its process group"
                ) from exc
        stdout = stdout_path.read_text(encoding="utf-8")
        stderr = stderr_path.read_text(encoding="utf-8")
        interrupted = InstallerProcessResult(
            args=tuple(command),
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if interruption_journal is None:
            raise AssertionError(
                "installer exited before a durable Docker build could be interrupted"
            )
        journal_path = self.state_directory / "install-state.json"
        if journal_path.exists():
            retained_journal = json.loads(journal_path.read_text(encoding="utf-8"))
            operation_logs = sorted(
                (self.state_directory / "logs").glob(
                    f"install-*-{retained_journal['operation_id']}.log"
                )
            )
            operation_log = operation_logs[-1] if operation_logs else None
            log_tail = (
                operation_log.read_text(encoding="utf-8", errors="replace")[-4000:]
                if operation_log is not None and operation_log.is_file()
                else "operation log unavailable"
            )
            raise AssertionError(
                "interrupted install did not clear its rolled-back journal: "
                f"journal={json.dumps(retained_journal, sort_keys=True)} log_tail={log_tail}"
            )
        bootstrap_present_after_exit = _inspect("container", bootstrap_container_id) is not None
        if bootstrap_present_after_exit:
            raise AssertionError("interrupted installer left its bootstrap container running")
        rollback_resources = self._owned_resource_snapshot()
        if any(rollback_resources[kind] for kind in ("containers", "networks", "volumes")):
            raise AssertionError("interrupted install left task-owned runtime resources")
        manifest = self._manifest()
        manifest["interruption_evidence"] = {
            "command": command,
            "installer_pid": process.pid,
            "operation_id": interruption_journal["operation_id"],
            "signal": "SIGINT",
            "signal_sent_at_unix": signal_sent_at,
            "bounded_exit_seconds": time.monotonic() - signal_started,
            "returncode": process.returncode,
            "journal_at_signal": interruption_journal,
            "journal_after_signal": None,
            "rollback_completed": True,
            "bootstrap_container_id": bootstrap_container_id,
            "bootstrap_container_present_after_exit": bootstrap_present_after_exit,
            "resources_after_rollback": rollback_resources,
            "container_process_tree_at_signal": build_process_tree,
            "stdout_tail": stdout[-4_000:],
            "stderr_tail": stderr[-4_000:].replace(self._external_s3_secret, "[REDACTED]")
            if self._external_s3_secret
            else stderr[-4_000:],
        }
        _replace_owner_only_json(self.manifest_path, manifest)
        restarted = self._run_launcher(args, renderer="non_interactive")
        self._last_result = restarted
        self._last_install = {
            "environment": "development",
            "renderer": "non_interactive",
            "storage": "bundled",
            "tls_mode": "local",
        }
        self._refresh_exact_docker_identities()
        restarted_journal = json.loads(
            (self.state_directory / "install-state.json").read_text(encoding="utf-8")
        )
        self._interruption_evidence = {
            "operation_before": interruption_journal["operation_id"],
            "operation_after": restarted_journal["operation_id"],
            "resources_after_rollback": rollback_resources,
            "resources_after_restart": self._owned_resource_snapshot(),
        }
        manifest = self._manifest()
        manifest["interruption_evidence"].update(
            {
                "operation_after_restart": restarted_journal["operation_id"],
                "journal_after_restart": restarted_journal,
                "restart_returncode": restarted.returncode,
                "resources_after_restart": self._interruption_evidence["resources_after_restart"],
            }
        )
        _replace_owner_only_json(self.manifest_path, manifest)
        return interrupted, restarted

    def _bootstrap_build_process_tree(self) -> str:
        """Return the bootstrap tree only while its real Docker build is active."""
        container_id = self._bootstrap_container_id()
        if not container_id:
            return ""
        completed = subprocess.run(
            ["docker", "top", container_id, "-eo", "pid,ppid,etime,state,args"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode != 0:
            return ""
        process_tree = completed.stdout
        build_markers = (" docker build ", "docker-buildx buildx build ")
        return process_tree if any(marker in process_tree for marker in build_markers) else ""

    def _bootstrap_container_id(self) -> str:
        cid_path = self.state_directory / "installer-runtime.cid"
        return cid_path.read_text(encoding="utf-8").strip() if cid_path.is_file() else ""

    def interruption_restarted_cleanly(self) -> bool:
        evidence = self._interruption_evidence
        if not evidence or evidence["operation_before"] == evidence["operation_after"]:
            return False
        rollback = evidence["resources_after_rollback"]
        restarted = evidence["resources_after_restart"]
        return all(not rollback[kind] for kind in ("containers", "networks", "volumes")) and any(
            restarted[kind] for kind in ("containers", "networks", "volumes")
        )

    def repeat_reused_resources(self) -> bool:
        evidence = self._repeat_evidence
        return (
            bool(evidence)
            and any(evidence["before"].values())
            and evidence["before"] == evidence["after"]
        )

    def required_services_healthy(self) -> bool:
        rows = _project_container_rows(self.project_name)
        stack = {} if self._last_install else self._active_stack()
        deployment_profile = str(
            self._last_install.get("deployment_profile")
            or stack.get("deployment_profile")
            or "scalable"
        )
        if deployment_profile == "compact":
            required = {"rabbitmq-fs", "pg-fs", "compact-runtime", "fs-cli"}
        else:
            required = {
                "rabbitmq-fs",
                "pg-fs",
                "fs-mcp",
                "fs-app",
                "fs-cli",
                "fs-worker",
                "control-plane",
            }
        tls_mode = str(self._last_install.get("tls_mode") or stack.get("tls_mode") or "local")
        if tls_mode != "reverse_proxy":
            required.add("traefik")
        storage = str(
            self._last_install.get("storage")
            or {"bundled_seaweedfs": "bundled"}.get(
                str(stack.get("file_storage") or ""), stack.get("file_storage")
            )
            or ""
        )
        if storage == "bundled":
            required.add("seaweedfs")
        environment = str(self._last_install.get("environment") or stack.get("environment") or "")
        if environment in {"development", "local_development"}:
            required.update(
                ("fs-frontend", "fs-test-runner", "fs-audit", "pg-audit", "rabbitmq-audit")
            )
        by_service = {str(row["service"]): row for row in rows}
        return (
            required == set(by_service)
            and len(rows) == len(required)
            and all(
                str(by_service[name]["state"]) == "running"
                and str(by_service[name]["health"] or "healthy") == "healthy"
                for name in required
            )
        )

    def switch_environment(
        self,
        target: str,
        *,
        storage: str = "keep",
    ) -> InstallerProcessResult:
        """Run the public retained-CLI transition command for the installed project."""
        if target not in {"development", "production"}:
            raise ValueError(f"unsupported environment target: {target}")
        if storage not in {"keep", "bundled", "external_s3"}:
            raise ValueError(f"unsupported storage target: {storage}")
        command = [
            str(self.distribution_directory / "flow-steward"),
            "--verbose",
            "environment",
            "switch",
            target,
            "--confirm",
        ]
        if storage == "bundled":
            command.extend(
                (
                    "--file-storage",
                    "bundled",
                    "--artifact-decision",
                    "accept_unavailable",
                )
            )
        elif storage == "external_s3":
            endpoint, source_credentials = self._start_external_s3(use_tls=target == "production")
            if endpoint.startswith("https://"):
                self._persist_external_ca_bundle_for_lifecycle()
            host_credentials = self._write_lifecycle_s3_credentials(source_credentials)
            command.extend(
                (
                    "--file-storage",
                    "external_s3",
                    "--s3-endpoint",
                    endpoint,
                    "--s3-bucket",
                    self._bucket_name(),
                    "--s3-region",
                    "us-east-1",
                    "--s3-credentials-file",
                    f"/app/{host_credentials.name}",
                    "--artifact-decision",
                    "accept_unavailable",
                )
            )
        process = subprocess.Popen(
            command,
            cwd=self.distribution_directory,
            env=self._installed_launcher_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        runtime_samples: list[str] = []
        runtime_failures: list[str] = []
        monitor_stop = threading.Event()

        def capture_runtime_samples() -> None:
            while not monitor_stop.wait(2):
                with suppress(Exception):
                    sample = self._transition_runtime_diagnostic()
                    if not runtime_samples or runtime_samples[-1] != sample:
                        runtime_samples.append(sample)
                        del runtime_samples[:-8]
                    if '"container_logs"' in sample and (
                        not runtime_failures or runtime_failures[-1] != sample
                    ):
                        runtime_failures.append(sample)
                        del runtime_failures[:-4]

        monitor = threading.Thread(target=capture_runtime_samples, daemon=True)
        monitor.start()
        try:
            stdout, stderr = _communicate_with_process_group_timeout(
                process,
                timeout_seconds=_INSTALL_TIMEOUT_SECONDS,
            )
        finally:
            monitor_stop.set()
            monitor.join(timeout=5)
        result = InstallerProcessResult(
            args=tuple(command),
            returncode=int(process.returncode),
            stdout=stdout,
            stderr=stderr,
        )
        if result.returncode != 0:
            runtime_logs = sorted((self.state_directory / "logs").glob("cli-runtime-*.log"))
            technical_log = (
                runtime_logs[-1].read_text(encoding="utf-8") if runtime_logs else "unavailable"
            )
            operation_logs = sorted((self.state_directory / "operations/logs").glob("*.log"))
            operation_log = (
                operation_logs[-1].read_text(encoding="utf-8") if operation_logs else "unavailable"
            )
            validation_diagnostic = self._transition_validation_diagnostic(target)
            result = InstallerProcessResult(
                args=result.args,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=(
                    result.stderr
                    + "CLI runtime mount evidence: "
                    + json.dumps(self._cli_runtime_mounts(), sort_keys=True)
                    + "\nCLI runtime technical evidence:\n"
                    + technical_log
                    + "\nLifecycle operation evidence:\n"
                    + operation_log
                    + "\nRuntime transition evidence:\n"
                    + self._transition_runtime_diagnostic()
                    + "\nRuntime transition samples:\n"
                    + "\n".join(runtime_samples)
                    + "\nTarget Compose validation evidence:\n"
                    + validation_diagnostic
                ),
            )
            self._record_transition_failure_evidence(
                target=target,
                storage=storage,
                returncode=result.returncode,
                diagnostic=(
                    "Launcher stdout tail:\n"
                    + stdout[-1_800:]
                    + "\nLauncher stderr tail:\n"
                    + stderr[-800:]
                    + "\nLifecycle operation tail:\n"
                    + operation_log[-4_096:]
                    + "\nTarget Compose validation:\n"
                    + validation_diagnostic[-1_024:]
                    + "\nFailed container runtime sample:\n"
                    + (runtime_failures[-1][-2_048:] if runtime_failures else "unavailable")
                ),
            )
        self._last_result = result
        if result.returncode == 0:
            self._last_install["environment"] = target
            if storage != "keep":
                self._last_install["storage"] = storage
        self._refresh_exact_docker_identities()
        return result

    def _write_lifecycle_s3_credentials(self, source: Path) -> Path:
        """Adapt installer credentials to the public lifecycle CLI input contract."""
        credentials = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(credentials, dict):
            raise AssertionError("installer S3 credentials evidence is invalid")
        access_key = str(credentials.get("access_key") or "")
        secret_key = str(credentials.get("secret_key") or "")
        if not access_key or not secret_key:
            raise AssertionError("installer S3 credentials evidence is incomplete")
        target = self.distribution_directory / ".acceptance-external-s3-credentials.json"
        expected = {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
        }
        if target.exists():
            if json.loads(target.read_text(encoding="utf-8")) != expected:
                raise AssertionError("lifecycle S3 credentials evidence changed unexpectedly")
            return target
        _write_new_owner_only_json(target, expected)
        return target

    def _persist_external_ca_bundle_for_lifecycle(self) -> None:
        """Make the disposable custom CA durable before the retained CLI transition."""
        if not self._external_host_ca_path().is_file():
            raise AssertionError("external S3 CA bundle is unavailable")
        active = self._active_configuration()
        env_name = ".env.prod" if active["environment"] == "production" else ".env"
        path = self.distribution_directory / env_name
        lines = path.read_text(encoding="utf-8").splitlines()
        retained = [line for line in lines if not line.strip().startswith("AWS_CA_BUNDLE=")]
        retained.append(f"AWS_CA_BUNDLE={self._external_container_ca_path()}")
        _replace_owner_only_text(path, "\n".join(retained) + "\n")

    def _record_transition_failure_evidence(
        self,
        *,
        target: str,
        storage: str,
        returncode: int,
        diagnostic: str,
    ) -> None:
        """Persist the bounded, secret-free tail needed to diagnose a torn-down pilot."""
        self._persist_failure_evidence(
            {
                "operation": "environment switch",
                "target": target,
                "storage": storage,
                "returncode": returncode,
                "preservation_comparison": self._preservation_comparison_evidence(),
            },
            diagnostic=diagnostic,
        )

    def _preservation_comparison_evidence(self) -> dict[str, object] | None:
        """Capture a bounded, secret-free diff for the latest failed transition."""
        operations = self.state_directory / "operations"
        before_paths = sorted(operations.glob("*.before-preservation.json"))
        for before_path in reversed(before_paths):
            operation_id = before_path.name.removesuffix(".before-preservation.json")
            after_path = operations / f"{operation_id}.after-preservation.json"
            if not after_path.is_file():
                continue
            try:
                before = json.loads(before_path.read_text(encoding="utf-8"))
                after = json.loads(after_path.read_text(encoding="utf-8"))
                before_fields = before["fields"]
                after_fields = after["fields"]
                if not isinstance(before_fields, dict) or not isinstance(after_fields, dict):
                    continue
                changed_fields = [
                    {
                        "field": field,
                        "before": _bounded_preservation_value(before_fields.get(field)),
                        "after": _bounded_preservation_value(after_fields.get(field)),
                    }
                    for field in sorted(set(before_fields).union(after_fields))
                    if before_fields.get(field) != after_fields.get(field)
                ]
                return {
                    "operation_id": operation_id,
                    "database_reachable": {
                        "before": before.get("database_reachable"),
                        "after": after.get("database_reachable"),
                    },
                    "unknown_fields": {
                        "before": sorted(
                            field for field, value in before_fields.items() if value == "unknown"
                        ),
                        "after": sorted(
                            field for field, value in after_fields.items() if value == "unknown"
                        ),
                    },
                    "changed_fields": changed_fields,
                }
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return None

    def _record_install_failure_evidence(self, *, returncode: int, diagnostic: str) -> None:
        self._persist_failure_evidence(
            {"operation": "installation", "returncode": returncode},
            diagnostic=diagnostic,
        )

    def _record_installed_cli_failure_evidence(
        self,
        *,
        command_id: str,
        returncode: int,
        diagnostic: str,
    ) -> None:
        """Persist lifecycle comparison evidence before disposable cleanup."""
        self._persist_failure_evidence(
            {
                "operation": "installed_cli",
                "command_id": command_id,
                "returncode": returncode,
                "preservation_comparison": self._preservation_comparison_evidence(),
            },
            diagnostic=diagnostic,
        )

    def _persist_failure_evidence(
        self,
        evidence: dict[str, object],
        *,
        diagnostic: str,
    ) -> None:
        manifest = self._manifest()
        external = manifest.get("external_s3")
        access_key = str(external.get("access_key_id") or "") if isinstance(external, dict) else ""
        detail = diagnostic
        for secret in (self._external_s3_secret, access_key):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        failure_evidence = {**evidence, "diagnostic": detail[-8_192:]}
        encoded = json.dumps(failure_evidence, sort_keys=True)
        for secret in (self._external_s3_secret, access_key):
            if secret:
                encoded = encoded.replace(secret, "[REDACTED]")
        manifest["failure_evidence"] = json.loads(encoded)
        _replace_owner_only_json(self.manifest_path, manifest)

    def _transition_runtime_diagnostic(self) -> str:
        evidence: list[dict[str, object]] = []
        for name in _container_names_for_project(self.project_name):
            record = _inspect("container", name) or {}
            labels = record.get("Config", {}).get("Labels") or {}
            state = record.get("State") or {}
            health = state.get("Health") or {}
            item: dict[str, object] = {
                "service": str(labels.get("com.docker.compose.service") or ""),
                "state": str(state.get("Status") or ""),
                "exit_code": int(state.get("ExitCode") or 0),
                "error": str(state.get("Error") or ""),
                "health": str(health.get("Status") or ""),
                "health_log": [
                    {
                        "exit_code": int(entry.get("ExitCode") or 0),
                        "output": str(entry.get("Output") or "")[-500:],
                    }
                    for entry in (health.get("Log") or ())[-3:]
                    if isinstance(entry, dict)
                ],
            }
            if (
                item["exit_code"] != 0
                or item["health"] == "unhealthy"
                or item["service"] in {"fs-app", "fs-worker"}
            ):
                logs = _run(
                    ["docker", "logs", "--tail", "80", name],
                    check=False,
                )
                item["container_logs"] = (logs.stdout + logs.stderr)[-2_000:]
            evidence.append(item)
        detail = json.dumps(evidence, sort_keys=True)
        for secret in (
            self._external_s3_secret,
            str((self.external_s3_identity or {}).get("access_key_id") or ""),
        ):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        return detail

    def _transition_validation_diagnostic(self, target: str) -> str:
        environment_path = self.distribution_directory / (
            ".env" if target == "development" else ".env.prod"
        )
        if not environment_path.is_file():
            return "target environment file unavailable"
        command = [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-p",
            self.project_name,
            "-f",
            str(
                self.distribution_directory
                / "infra/docker"
                / ("docker-compose.yaml" if target == "development" else "docker-compose.prod.yaml")
            ),
        ]
        profiles = ["cli"]
        if self._last_install.get("storage") == "bundled":
            profiles.insert(0, "storage-seaweedfs")
        if target == "development":
            profiles.extend(("development", "test", "audit", "quality"))
        for profile in profiles:
            command.extend(("--profile", profile))
        command.extend(("config", "--quiet"))
        environment = {**self._launcher_environment(), **_read_env(environment_path)}
        completed = _run(
            command,
            cwd=self.distribution_directory,
            check=False,
            env=environment,
        )
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        for secret in (
            self._external_s3_secret,
            str((self.external_s3_identity or {}).get("access_key_id") or ""),
        ):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        return f"exit={completed.returncode}: {detail}"

    def capture_deployment_profile(self, *, stage: str) -> dict[str, object]:
        """Persist a secret-free, literal snapshot of the active deployment profile."""
        if not stage or any(
            character not in "-_abcdefghijklmnopqrstuvwxyz0123456789" for character in stage
        ):
            raise ValueError("deployment profile evidence stage is invalid")
        selected = self._active_configuration()
        compose_config = self._compose_json_config()
        services = compose_config.get("services")
        if not isinstance(services, dict):
            raise AssertionError("active Docker Compose config has no service inventory")
        image_evidence: dict[str, dict[str, object]] = {}
        image_references: dict[str, str] = {}
        missing_image_references: dict[str, str] = {}
        materialized: list[str] = []
        for service, raw_config in sorted(services.items()):
            config = raw_config if isinstance(raw_config, dict) else {}
            image = str(config.get("image") or f"{self.project_name}-{service}")
            image_references[str(service)] = image
            inspected = _inspect_image_with_retry(image)
            if inspected is None:
                missing_image_references[str(service)] = image
                continue
            materialized.append(str(service))
            image_evidence[str(service)] = {
                "reference": image,
                "id": str(inspected.get("Id") or ""),
                "repo_digests": sorted(str(value) for value in inspected.get("RepoDigests") or ()),
            }
        result = self._run_active_compose("ps", "--all", "--format", "json")
        ps_records = _json_records(result.stdout)
        cli_records = [
            record for record in ps_records if str(record.get("Service") or "") == "fs-cli"
        ]
        if len(cli_records) != 1 or not str(cli_records[0].get("ID") or ""):
            raise AssertionError("active deployment does not expose exactly one fs-cli identity")
        cli_inspection = _inspect("container", f"{self.project_name}-fs-cli-1") or {}
        cli_identity = str(cli_inspection.get("Id") or "")
        if not cli_identity:
            raise AssertionError("active deployment has no exact fs-cli container identity")
        container_ids = {
            row["service"]: row["container_id"]
            for row in _project_container_rows(self.project_name)
            if row["service"] and row["container_id"]
        }
        inventory: dict[str, object] = {
            "selected_environment": selected["environment"],
            "selected_storage": selected["file_storage"],
            "running_services": sorted(self.running_services()),
            "materialized_services": materialized,
            "named_volumes": sorted(self.named_volumes()),
            "compose_ps": ps_records,
            "images": image_evidence,
            "image_references": image_references,
            "missing_image_references": missing_image_references,
            "cli_runtime_mounts": self._cli_runtime_mounts(),
            "cli_runtime_container_id": cli_identity,
            "container_ids": container_ids,
            "retained_cli_runtime": "fs-cli" in self.running_services(),
            "long_running_services": sorted(self.running_services()),
            "on_demand_services": sorted(
                {"mailpit", "sonarqube", "sonar-scanner"}.intersection(materialized)
            ),
        }
        manifest = self._manifest()
        evidence = manifest.setdefault("deployment_profile_evidence", {})
        if not isinstance(evidence, dict):
            raise AssertionError("deployment profile evidence manifest shape is invalid")
        if stage in evidence:
            raise AssertionError(f"deployment profile evidence stage already exists: {stage}")
        dependency_images = manifest.setdefault("dependency_images", [])
        observed_dependency_ids = manifest.setdefault("observed_dependency_image_ids", {})
        if not isinstance(dependency_images, list) or not isinstance(observed_dependency_ids, dict):
            raise AssertionError("dependency image evidence manifest shape is invalid")
        dependency_images[:] = list(dict.fromkeys((*dependency_images, *image_references.values())))
        for service, evidence_entry in image_evidence.items():
            observed_dependency_ids[image_references[service]] = str(evidence_entry["id"])
        evidence[stage] = inventory
        _replace_owner_only_json(self.manifest_path, manifest)
        return inventory

    def reconcile_and_prove_cli_runtime_reuse(self) -> str:
        """Repair a changed profile mount contract once, then prove warm reuse."""
        identities: list[str] = []
        command = [
            str(self.distribution_directory / "flow-steward"),
            "environment",
            "show",
            "--json",
        ]
        for _attempt in range(2):
            result = _run(
                command,
                cwd=self.distribution_directory,
                check=False,
                env=self._installed_launcher_environment(),
            )
            if result.returncode != 0:
                raise AssertionError(
                    "retained CLI runtime did not complete environment show: "
                    + result.stderr.strip()
                )
            inspected = _inspect("container", f"{self.project_name}-fs-cli-1") or {}
            identity = str(inspected.get("Id") or "")
            if not identity:
                raise AssertionError("retained CLI runtime has no container identity")
            identities.append(identity)
        if identities[0] != identities[1]:
            raise AssertionError("same-profile CLI command recreated the warm runtime")
        manifest = self._manifest()
        evidence = manifest.setdefault("cli_runtime_reuse_evidence", [])
        if not isinstance(evidence, list):
            raise AssertionError("CLI runtime reuse evidence manifest shape is invalid")
        evidence.append(
            {
                "selected": self._active_configuration(),
                "first_container_id": identities[0],
                "second_container_id": identities[1],
            }
        )
        _replace_owner_only_json(self.manifest_path, manifest)
        return identities[1]

    def _cli_runtime_mounts(self) -> list[dict[str, object]]:
        inspected = _inspect("container", f"{self.project_name}-fs-cli-1")
        mounts = (inspected or {}).get("Mounts") or ()
        return sorted(
            (
                {
                    "type": str(mount.get("Type") or ""),
                    "source": str(mount.get("Source") or ""),
                    "destination": str(mount.get("Destination") or ""),
                    "rw": bool(mount.get("RW")),
                }
                for mount in mounts
                if isinstance(mount, dict)
            ),
            key=lambda mount: str(mount["destination"]),
        )

    def running_services(self) -> set[str]:
        rows = _project_container_rows(self.project_name)
        services = [str(row["service"]) for row in rows if str(row["state"]) == "running"]
        if len(services) != len(set(services)):
            raise AssertionError(f"duplicate running Compose services: {services}")
        return set(services)

    def named_volumes(self) -> set[str]:
        manifest = self._manifest()
        logical_names: set[str] = set()
        for name in _names_with_owned_labels(
            "volume",
            project_name=self.project_name,
            owner_token=str(manifest["owner_token"]),
        ):
            inspected = _inspect("volume", name)
            labels = (inspected or {}).get("Labels") or {}
            logical_name = str(labels.get("com.docker.compose.volume") or "")
            if logical_name:
                logical_names.add(logical_name)
        return logical_names

    def seed_transition_sentinels(self) -> dict[str, object]:
        token = secrets.token_hex(16)
        database = {"user": f"user-{token}", "data": f"data-{token}"}
        sql = (
            "CREATE TABLE IF NOT EXISTS public.fs_acceptance_transition_sentinel ("
            "kind text PRIMARY KEY, value text NOT NULL);"
            f"INSERT INTO public.fs_acceptance_transition_sentinel(kind,value) VALUES "
            f"('user','{database['user']}'),('data','{database['data']}') "
            "ON CONFLICT (kind) DO UPDATE SET value=EXCLUDED.value;"
        )
        self._postgres_sql(sql)
        storage_value = f"storage-{token}".encode()
        storage_key = f"acceptance/profile-transition/{token}"
        self._storage_sentinel(action="put", key=storage_key, value=storage_value)
        return {
            "database": database,
            "storage": {
                "key": storage_key,
                "sha256": hashlib.sha256(storage_value).hexdigest(),
            },
        }

    def assert_transition_sentinels(self, expected: dict[str, object]) -> None:
        database = expected.get("database")
        storage = expected.get("storage")
        if not isinstance(database, dict) or not isinstance(storage, dict):
            raise AssertionError("transition sentinel evidence has an invalid shape")
        self.assert_database_transition_sentinels(database)
        key = str(storage.get("key") or "")
        value = self._storage_sentinel(action="get", key=key, value=b"")
        actual_digest = hashlib.sha256(value).hexdigest()
        if actual_digest != storage.get("sha256"):
            raise AssertionError(
                "storage transition sentinel changed: "
                f"expected={storage.get('sha256')}, actual={actual_digest}"
            )

    def assert_storage_transition_sentinel_unavailable(
        self,
        expected: dict[str, object],
    ) -> None:
        """Prove storage selection changed without copying the source object."""
        storage = expected.get("storage")
        if not isinstance(storage, dict) or not str(storage.get("key") or ""):
            raise AssertionError("transition storage sentinel evidence has an invalid shape")
        try:
            self._storage_sentinel(
                action="get",
                key=str(storage["key"]),
                value=b"",
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            if code in {"NoSuchKey", "NoSuchObject", "404"}:
                return
            raise
        raise AssertionError("source storage sentinel is unexpectedly available in target storage")

    def assert_database_transition_sentinels(self, expected: dict[str, object]) -> None:
        rows = self._postgres_sql(
            "SELECT kind || '=' || value FROM public.fs_acceptance_transition_sentinel "
            "WHERE kind IN ('user','data') ORDER BY kind;"
        ).splitlines()
        actual_database = dict(row.split("=", 1) for row in rows if "=" in row)
        if actual_database != expected:
            raise AssertionError(
                f"database transition sentinels changed: expected={expected}, "
                f"actual={actual_database}"
            )

    def _active_stack(self) -> dict[str, Any]:
        path = self.state_directory / "stack.toml"
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise AssertionError("active deployment configuration is invalid")
        return raw

    def _active_configuration(self) -> dict[str, str]:
        raw = self._active_stack()
        environment = {
            "local_development": "development",
            "development": "development",
            "production": "production",
        }.get(str(raw.get("environment") or ""))
        file_storage = {
            "bundled_seaweedfs": "bundled",
            "bundled": "bundled",
            "external_s3": "external_s3",
        }.get(str(raw.get("file_storage") or ""))
        if environment is None or file_storage is None:
            raise AssertionError("active deployment configuration is invalid")
        return {"environment": environment, "file_storage": file_storage}

    def _active_compose_prefix(self) -> list[str]:
        values = _read_env(self.state_directory / "compose.env")
        if values.get("FS_COMPOSE_PROJECT_NAME") != self.project_name:
            raise AssertionError("active Compose project identity differs from the manifest")
        command = ["docker", "compose", "-p", self.project_name]
        for raw_path in shlex.split(values.get("FS_COMPOSE_FILES") or ""):
            compose_path = Path(raw_path)
            if not compose_path.is_absolute():
                compose_path = self.distribution_directory / compose_path
            command.extend(("-f", str(compose_path)))
        env_path = Path(values.get("FS_ENV_FILE") or "")
        if not env_path.is_absolute():
            env_path = self.distribution_directory / env_path
        command.extend(("--env-file", str(env_path)))
        for profile in shlex.split(values.get("FS_COMPOSE_PROFILES") or ""):
            command.extend(("--profile", profile))
        return command

    def _run_active_compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _run(
            [*self._active_compose_prefix(), *args],
            cwd=self.distribution_directory,
            env=self._installed_launcher_environment(),
        )

    def _compose_json_config(self) -> dict[str, Any]:
        result = self._run_active_compose("config", "--format", "json")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise AssertionError("active Docker Compose config is not a JSON object")
        return payload

    def _postgres_sql(self, sql: str) -> str:
        container = f"{self.project_name}-pg-fs-1"
        result = _run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-ceu",
                'psql -X -qAt -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" '
                '--dbname "$POSTGRES_DB" -c "$1"',
                "sh",
                sql,
            ]
        )
        return result.stdout.strip()

    def _storage_sentinel(self, *, action: str, key: str, value: bytes) -> bytes:
        active = self._active_configuration()
        credentials = (
            self._read_external_credentials()
            if active["file_storage"] == "external_s3"
            else self._bundled_credentials()
        )
        if active == {"environment": "production", "file_storage": "bundled"}:
            payload = json.dumps(
                {
                    "action": action,
                    "access_key": credentials["access_key"],
                    "secret_key": credentials["secret_key"],
                    "bucket": self._bucket_name(),
                    "key": key,
                    "value_hex": value.hex(),
                }
            )
            completed = subprocess.run(
                [
                    *self._active_compose_prefix(),
                    "exec",
                    "-T",
                    "fs-cli",
                    "python",
                    "-c",
                    _BUNDLED_S3_SENTINEL_SCRIPT,
                ],
                cwd=self.distribution_directory,
                env=self._installed_launcher_environment(),
                input=payload,
                check=False,
                capture_output=True,
                text=True,
                timeout=_READY_TIMEOUT_SECONDS,
            )
            if completed.returncode != 0 or completed.stderr:
                raise AssertionError("Production bundled storage sentinel operation failed")
            return bytes.fromhex(completed.stdout.strip())
        if active["file_storage"] == "external_s3":
            endpoint = self._external_endpoint().replace("host.docker.internal", "127.0.0.1")
            verify: bool | str = (
                str(self._external_ca_path()) if endpoint.startswith("https://") else True
            )
        else:
            endpoint = f"http://127.0.0.1:{self.ports['seaweedfs_s3']}"
            verify = True
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=credentials["access_key"],
            aws_secret_access_key=credentials["secret_key"],
            region_name="us-east-1",
            verify=verify,
            config=Config(s3={"addressing_style": "path"}),
        )
        if action == "put":
            client.put_object(Bucket=self._bucket_name(), Key=key, Body=value)
            return value
        if action == "get":
            return client.get_object(Bucket=self._bucket_name(), Key=key)["Body"].read()
        raise ValueError(f"unsupported storage sentinel action: {action}")

    def storage_round_trip(self) -> bytes:
        active = self._active_configuration()
        storage = active["file_storage"]
        if storage == "external_s3":
            endpoint = self._external_endpoint().replace("host.docker.internal", "127.0.0.1")
            credentials = self._read_external_credentials()
            verify: bool | str = (
                str(self._external_ca_path()) if endpoint.startswith("https") else True
            )
        elif storage == "bundled":
            credentials = self._bundled_credentials()
            if active["environment"] == "production":
                return self._production_bundled_storage_round_trip(credentials)
            endpoint = f"http://127.0.0.1:{self.ports['seaweedfs_s3']}"
            verify = True
        else:
            raise RuntimeError("storage_round_trip requires a successful install")
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=credentials["access_key"],
            aws_secret_access_key=credentials["secret_key"],
            region_name="us-east-1",
            verify=verify,
            config=Config(s3={"addressing_style": "path"}),
        )
        bucket = self._bucket_name()
        key = f"acceptance/{secrets.token_hex(12)}"
        client.put_object(Bucket=bucket, Key=key, Body=_PROOF)
        value = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        client.delete_object(Bucket=bucket, Key=key)
        return value

    def _production_bundled_storage_round_trip(self, credentials: dict[str, str]) -> bytes:
        key = f"acceptance/{secrets.token_hex(12)}"
        payload = json.dumps(
            {
                "access_key": credentials["access_key"],
                "secret_key": credentials["secret_key"],
                "bucket": self._bucket_name(),
                "key": key,
                "proof_hex": _PROOF.hex(),
            }
        )
        command = [
            "docker",
            "compose",
            "-p",
            self.project_name,
            "-f",
            str(self.distribution_directory / "infra/docker/docker-compose.prod.yaml"),
            "--env-file",
            str(self.distribution_directory / ".env.prod"),
            "--profile",
            "cli",
            "--profile",
            "storage-seaweedfs",
            "exec",
            "-T",
            "fs-cli",
            "python",
            "-c",
            _BUNDLED_S3_ROUND_TRIP_SCRIPT,
        ]
        completed = subprocess.run(
            command,
            cwd=self.distribution_directory,
            env=self._installed_launcher_environment(),
            input=payload,
            check=False,
            capture_output=True,
            text=True,
            timeout=_READY_TIMEOUT_SECONDS,
        )
        lines = completed.stdout.splitlines()
        if completed.returncode != 0 or completed.stderr or len(lines) != 1:
            raise AssertionError("Production bundled storage round-trip failed inside fs-cli")
        try:
            value = bytes.fromhex(lines[0])
        except ValueError as exc:
            raise AssertionError(
                "Production bundled storage round-trip returned invalid proof"
            ) from exc
        if value != _PROOF:
            raise AssertionError("Production bundled storage round-trip returned the wrong proof")
        return value

    def journal_status(self) -> str:
        payload = json.loads((self.state_directory / "install-state.json").read_text())
        return str(payload["status"])

    def public_route_healthy(self, *, tls_mode: str) -> bool:
        port = (
            self.ports["reverse_proxy_backend"]
            if tls_mode == "reverse_proxy"
            else self.ports["public"]
        )
        scheme = "http" if tls_mode == "reverse_proxy" else "https"
        try:
            context = None
            if scheme == "https":
                context = ssl.create_default_context(
                    cafile=str(self.distribution_directory / ".ssl/cert.pem")
                )
                if tls_mode == "provided":
                    with (
                        socket.create_connection(("127.0.0.1", port), timeout=10) as raw,
                        context.wrap_socket(raw, server_hostname="127.0.0.1") as connection,
                    ):
                        served_certificate = connection.getpeercert(binary_form=True)
                    provided_pem = (
                        self.distribution_directory / ".acceptance-tls/cert.pem"
                    ).read_text(encoding="utf-8")
                    if served_certificate != ssl.PEM_cert_to_DER_cert(provided_pem):
                        return False
            with urllib.request.urlopen(
                f"{scheme}://127.0.0.1:{port}/health", timeout=10, context=context
            ) as response:
                return response.status == 200
        except (OSError, ValueError):
            return False

    def public_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> PublicHTTPResult:
        """Call the installed public route with its real TLS trust contract."""
        if not path.startswith("/"):
            raise ValueError("public acceptance path must be absolute")
        tls_mode = str(
            self._last_install.get("tls_mode") or self._active_stack().get("tls_mode") or "local"
        )
        port = (
            self.ports["reverse_proxy_backend"]
            if tls_mode == "reverse_proxy"
            else self.ports["public"]
        )
        scheme = "http" if tls_mode == "reverse_proxy" else "https"
        context = (
            None
            if scheme == "http"
            else ssl.create_default_context(
                cafile=str(self.distribution_directory / ".ssl/cert.pem")
            )
        )
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{scheme}://127.0.0.1:{port}{path}",
            data=body,
            headers={
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
                **(headers or {}),
            },
            method=method.upper(),
        )
        try:
            response = urllib.request.urlopen(request, timeout=10, context=context)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read().decode("utf-8")
            decoded = json.loads(raw) if raw else {}
            if not isinstance(decoded, dict):
                raise AssertionError("public acceptance response is not a JSON object")
            result = PublicHTTPResult(
                status=int(response.status),
                payload=decoded,
                headers=dict(response.headers.items()),
            )
        manifest = self._manifest()
        evidence = manifest.setdefault("maintenance_http_evidence", [])
        if not isinstance(evidence, list):
            raise AssertionError("maintenance HTTP evidence manifest shape is invalid")
        error_payload = result.payload.get("error")
        evidence.append(
            {
                "method": method.upper(),
                "path": path,
                "status": result.status,
                "code": (
                    str(error_payload.get("code") or "") if isinstance(error_payload, dict) else ""
                ),
            }
        )
        _replace_owner_only_json(self.manifest_path, manifest)
        return result

    def runtime_maintenance_observations(self) -> dict[str, bool]:
        """Read the mounted marker from the real worker and control runtimes."""
        script = (
            "import json;"
            "from core.infrastructure.runtime.maintenance_http import "
            "runtime_maintenance_active;"
            "print(json.dumps({'active':runtime_maintenance_active()}))"
        )
        observations: dict[str, bool] = {}
        for service in ("fs-worker", "control-plane"):
            result = self._run_active_compose("exec", "-T", service, "python", "-c", script)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            observations[service] = bool(payload["active"])
        return observations

    def publish_ready_maintenance_probe(self) -> str:
        """Publish one task-owned ready signal without creating product data."""
        project_id = secrets.token_hex(16)
        body = json.dumps(
            {
                "project_id": project_id,
                "job_id": f"maintenance-{project_id}",
                "task_id": f"maintenance-{project_id}",
            }
        )
        script = (
            "from core.infrastructure.runtime.worker import queue_broker as queue;"
            "connection=queue._connect();"
            "channel=connection.channel();"
            "queue._declare_ready_queue(channel);"
            "channel.confirm_delivery();"
            f"channel.basic_publish(exchange='',routing_key=queue._READY_QUEUE,body={body!r}.encode(),"
            "properties=queue.pika.BasicProperties(delivery_mode=2,content_type='application/json'));"
            "connection.close();"
            "print('published')"
        )
        result = self._run_active_compose("exec", "-T", "fs-cli", "python", "-c", script)
        if result.stdout.strip().splitlines()[-1] != "published":
            raise AssertionError("maintenance ready probe was not published")
        return project_id

    def ready_queue_observation(self) -> dict[str, int]:
        """Return ready plus unacknowledged counts from the exact Rabbit queue."""
        result = _run(
            [
                "docker",
                "exec",
                f"{self.project_name}-rabbitmq-fs-1",
                "rabbitmqctl",
                "list_queues",
                "--formatter",
                "json",
                "name",
                "messages_ready",
                "messages_unacknowledged",
            ]
        )
        start = result.stdout.find("[")
        rows = json.loads(result.stdout[start:]) if start >= 0 else []
        row = next(
            (
                item
                for item in rows
                if isinstance(item, dict) and item.get("name") == "platform.projects.ready"
            ),
            {},
        )
        return {
            "ready": int(row.get("messages_ready") or 0),
            "unacknowledged": int(row.get("messages_unacknowledged") or 0),
        }

    def output_contains_external_credentials(self) -> bool:
        if self._last_result is None or not self._external_s3_secret:
            return False
        identity = self.external_s3_identity or {}
        return any(
            value and value in self._last_result.combined_output
            for value in (self._external_s3_secret, str(identity.get("access_key_id") or ""))
        )

    def has_compose_containers(self) -> bool:
        return bool(_container_names_for_project(self.project_name))

    def has_compose_resources(self) -> bool:
        return any(
            (
                self.has_compose_containers(),
                bool(_names_with_label("volume", self.project_name)),
                bool(_names_with_label("network", self.project_name)),
            )
        )

    def has_bundled_storage_resources(self) -> bool:
        resources = set(_container_names_for_project(self.project_name))
        resources.update(_names_with_label("volume", self.project_name))
        return any("seaweedfs" in name for name in resources)

    def cleanup(self) -> None:
        """Remove only parsed manifest identities and prove their exact absence."""
        if self.cleaned:
            return
        self._refresh_exact_docker_identities()
        manifest = self._manifest()
        active = manifest["active_resources"]
        for container in active["containers"]:
            self._remove_owned_container(str(container), manifest)
        for network in active["networks"]:
            self._remove_owned_network(str(network), manifest)
        for volume in active["volumes"]:
            self._remove_owned_volume(str(volume), manifest)
        for image in active["images"]:
            if str(image) in manifest.get("baseline_image_tags", {}):
                raise AssertionError(f"refusing to remove baseline Docker image tag: {image}")
            _run(["docker", "image", "rm", str(image)], check=False)
        manifest["dependency_image_cleanup"] = self._remove_unused_dependency_images(manifest)
        dangling_image_cleanup = self._remove_new_dangling_images(manifest)
        manifest["dangling_image_cleanup"] = dangling_image_cleanup
        anonymous_cleanup = self._remove_new_anonymous_volumes(manifest)
        manifest["anonymous_volume_cleanup"] = anonymous_cleanup
        build_cache_cleanup = self._remove_new_build_cache_records(manifest)
        manifest["build_cache_cleanup"] = build_cache_cleanup
        for raw_path in active["paths"]:
            path = Path(str(raw_path))
            if path == self.manifest_path or not path.is_relative_to(self.manifest_path.parent):
                raise AssertionError(f"refusing out-of-root cleanup target: {path}")
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
        self._verify_resources_absent(manifest)
        manifest["active_resources"] = {
            "containers": [],
            "networks": [],
            "volumes": [],
            "images": [],
            "paths": [],
        }
        _replace_owner_only_json(self.manifest_path, manifest)
        self.cleaned = True
        if anonymous_cleanup["post_cleanup_residue"]:
            raise AssertionError(
                f"new anonymous Docker volume residue: {anonymous_cleanup['post_cleanup_residue']}"
            )
        if dangling_image_cleanup["post_cleanup_residue"]:
            raise AssertionError(
                "new unreferenced dangling Docker image residue: "
                f"{dangling_image_cleanup['post_cleanup_residue']}"
            )
        if build_cache_cleanup["post_cleanup_residue"]:
            raise AssertionError(
                f"new Docker build cache residue: {build_cache_cleanup['post_cleanup_residue']}"
            )
        manifest["cleanup_inventory_evidence"] = self._verify_baseline_preserved(manifest)
        _replace_owner_only_json(self.manifest_path, manifest)

    def _remove_new_dangling_images(
        self,
        manifest: dict[str, Any],
    ) -> dict[str, list[str]]:
        """Remove only new, untagged image identities with no container links."""
        baseline = set(manifest.get("baseline_inventory", {}).get("images", ()))
        current = _docker_inventory()
        observed = sorted(set(current["images"]) - baseline)
        evidence: dict[str, list[str]] = {
            "observed_new": observed,
            "removed": [],
            "retained_tagged_or_in_use": [],
            "post_cleanup_residue": [],
        }
        linked_images = _image_ids_with_container_links()
        removable: list[tuple[str, str]] = []
        for image_id in observed:
            inspected = _inspect("image", image_id)
            if (
                _DOCKER_IMAGE_ID.fullmatch(image_id) is None
                or inspected is None
                or inspected.get("RepoTags")
                or image_id in linked_images
            ):
                evidence["retained_tagged_or_in_use"].append(image_id)
                continue
            removable.append((str(inspected.get("Created") or ""), image_id))
        for _created, image_id in sorted(removable, reverse=True):
            _run(["docker", "image", "rm", image_id], check=False)

        after = set(_docker_inventory()["images"])
        post_cleanup = after - baseline
        evidence["removed"] = sorted(set(observed) - post_cleanup)
        linked_images = _image_ids_with_container_links()
        for image_id in sorted(post_cleanup):
            inspected = _inspect("image", image_id)
            if inspected is None or inspected.get("RepoTags") or image_id in linked_images:
                evidence["retained_tagged_or_in_use"].append(image_id)
            else:
                evidence["post_cleanup_residue"].append(image_id)
        evidence["retained_tagged_or_in_use"] = sorted(set(evidence["retained_tagged_or_in_use"]))
        return evidence

    def _remove_new_build_cache_records(
        self,
        manifest: dict[str, Any],
    ) -> dict[str, list[str]]:
        """Remove only reclaimable BuildKit records absent from the pre-test inventory."""
        baseline_value = manifest.get("baseline_build_cache")
        evidence: dict[str, list[str]] = {
            "observed_new": [],
            "removed": [],
            "retained_in_use_or_unproven": [],
            "post_cleanup_residue": [],
        }
        if not isinstance(baseline_value, list):
            return evidence
        baseline = {str(cache_id) for cache_id in baseline_value}
        current = _docker_build_cache_records()
        observed = sorted(set(current) - baseline)
        evidence["observed_new"] = observed
        attempted: set[str] = set()
        for cache_id in _build_cache_prune_order(current, observed):
            record = current[cache_id]
            if (
                _BUILDKIT_CACHE_ID.fullmatch(cache_id) is None
                or record.get("Reclaimable") is not True
            ):
                evidence["retained_in_use_or_unproven"].append(cache_id)
                continue
            removed = _run(
                [
                    "docker",
                    "buildx",
                    "prune",
                    "--filter",
                    f"id={cache_id}",
                    "--force",
                ],
                check=False,
            )
            if removed.returncode == 0:
                attempted.add(cache_id)
            else:
                evidence["retained_in_use_or_unproven"].append(cache_id)
        after = _docker_build_cache_records()
        post_cleanup = set(after) - baseline
        evidence["removed"] = sorted(attempted - post_cleanup)
        for cache_id in sorted(post_cleanup):
            record = after[cache_id]
            if record.get("Shared") is True or record.get("Reclaimable") is not True:
                evidence["retained_in_use_or_unproven"].append(cache_id)
            else:
                evidence["post_cleanup_residue"].append(cache_id)
        evidence["retained_in_use_or_unproven"] = sorted(
            set(evidence["retained_in_use_or_unproven"])
        )
        return evidence

    def _remove_new_anonymous_volumes(
        self,
        manifest: dict[str, Any],
    ) -> dict[str, list[str]]:
        """Remove only unreferenced anonymous volume IDs absent from the pre-test inventory."""
        baseline = set(manifest.get("baseline_inventory", {}).get("volumes", ()))
        observed = sorted(set(_anonymous_volume_names()) - baseline)
        evidence: dict[str, list[str]] = {
            "observed_new": observed,
            "removed": [],
            "retained_in_use_or_unproven": [],
            "post_cleanup_residue": [],
        }
        removal_attempts: set[str] = set()
        for name in observed:
            inspected = _inspect("volume", name)
            labels = inspected.get("Labels") or {} if inspected is not None else {}
            if (
                inspected is None
                or "com.docker.volume.anonymous" not in labels
                or _volume_container_ids(name)
            ):
                evidence["retained_in_use_or_unproven"].append(name)
                continue
            removal_attempts.add(name)
            removed = _run(["docker", "volume", "rm", name], check=False)
            if removed.returncode == 0:
                evidence["removed"].append(name)
            else:
                evidence["retained_in_use_or_unproven"].append(name)
        evidence["post_cleanup_residue"] = sorted(
            set(_anonymous_volume_names()).intersection(removal_attempts)
        )
        return evidence

    def _remove_unused_dependency_images(
        self,
        manifest: dict[str, Any],
    ) -> dict[str, list[str]]:
        """Remove exact, newly materialized dependency refs when Docker says they are unused."""
        baseline_ids = set(manifest.get("baseline_inventory", {}).get("images", ()))
        observed_ids = manifest.get("observed_dependency_image_ids", {})
        if not isinstance(observed_ids, dict):
            raise AssertionError("observed dependency image identities are invalid")
        evidence: dict[str, list[str]] = {
            "removed": [],
            "retained_baseline": [],
            "retained_changed_or_in_use": [],
        }
        for raw_reference in dict.fromkeys(manifest.get("dependency_images", ())):
            reference = str(raw_reference)
            inspected = _inspect("image", reference)
            if inspected is None:
                continue
            current_id = str(inspected.get("Id") or "")
            expected_id = str(observed_ids.get(reference) or "")
            if current_id in baseline_ids:
                evidence["retained_baseline"].append(reference)
                continue
            if not expected_id or current_id != expected_id:
                evidence["retained_changed_or_in_use"].append(reference)
                continue
            removed = _run(["docker", "image", "rm", reference], check=False)
            if removed.returncode == 0:
                evidence["removed"].append(reference)
            else:
                evidence["retained_changed_or_in_use"].append(reference)
        return evidence

    def cleanup_proof(self) -> dict[str, list[str]]:
        value = self._manifest()["active_resources"]
        return {
            key: list(value[key])
            for key in ("containers", "networks", "volumes", "images", "paths")
        }

    def _materialize_distribution(self) -> None:
        if self.distribution_directory.exists():
            if not any(self.distribution_directory.iterdir()):
                return
            if (self.distribution_directory / "install.sh").is_file():
                return
            raise AssertionError("distribution directory was not empty before materialization")
        _run(
            [
                "git",
                "clone",
                "--no-local",
                "--no-hardlinks",
                str(self.source_root),
                str(self.distribution_directory),
            ]
        )
        manifest = self._manifest()
        revision = _git_revision(self.distribution_directory)
        if revision != manifest["source_revision"]:
            raise AssertionError("disposable clone revision differs from its manifest")
        _verify_vendored_wheel_hashes(self.distribution_directory)

    def _prepare_low_capacity_projection(self) -> tuple[Path, str, str]:
        real_docker = shutil.which("docker")
        if real_docker is None:
            raise DisposableScenarioUnavailable("Docker is required for low-disk acceptance")
        manifest = self._manifest()
        if manifest.get("low_capacity_filesystem") is not None:
            raise AssertionError("low-capacity filesystem was already prepared")
        wrapper_root = self.manifest_path.parent / "low-capacity-docker"
        wrapper = wrapper_root / "docker"
        bootstrap_container = f"{self.project_name}-low-disk-bootstrap"
        manifest["low_capacity_filesystem"] = {
            "kind": "docker_tmpfs",
            "capacity_bytes": _LOW_CAPACITY_FILESYSTEM_BYTES,
            "container_path": "/low-capacity",
            "wrapper_path": str(wrapper),
            "bootstrap_container": bootstrap_container,
            "real_docker": real_docker,
        }
        manifest["planned_resources"]["paths"].append(str(wrapper_root))
        manifest["active_resources"]["paths"].append(str(wrapper_root))
        manifest["planned_resources"]["containers"].append(bootstrap_container)
        manifest["active_resources"]["containers"].append(bootstrap_container)
        _replace_owner_only_json(self.manifest_path, manifest)
        _write_new_owner_only_text(wrapper, _LOW_CAPACITY_DOCKER_WRAPPER)
        wrapper.chmod(0o700)
        return wrapper, bootstrap_container, real_docker

    def _installer_args(
        self,
        *,
        environment: str,
        renderer: str,
        storage: str,
        tls_mode: str,
        endpoint: str,
        credentials_path: Path | None,
    ) -> list[str]:
        args = [
            "--environment",
            environment,
            "--file-storage",
            storage,
            "--tls-mode",
            tls_mode,
            "--public-host",
            "127.0.0.1",
            "--public-port",
            str(self.ports["public"]),
        ]
        if storage == "external_s3":
            assert credentials_path is not None
            args.extend(
                [
                    "--s3-endpoint",
                    endpoint,
                    "--s3-bucket",
                    self._bucket_name(),
                    "--s3-region",
                    "us-east-1",
                    "--s3-credentials-path",
                    str(credentials_path),
                ]
            )
            if environment == "development" and endpoint.startswith("http://"):
                args.append("--allow-insecure-s3-http")
        if tls_mode == "provided":
            args.extend(
                [
                    "--certificate-path",
                    str(self.distribution_directory / ".acceptance-tls/cert.pem"),
                    "--private-key-path",
                    str(self.distribution_directory / ".acceptance-tls/key.pem"),
                ]
            )
        if tls_mode == "reverse_proxy":
            args.extend(
                [
                    "--reverse-proxy-backend-host",
                    "127.0.0.1",
                    "--reverse-proxy-backend-port",
                    str(self.ports["reverse_proxy_backend"]),
                ]
            )
        if renderer == "plain":
            args.extend(("--plain", "--confirm"))
        elif renderer == "non_interactive":
            args.extend(("--non-interactive", "--plain", "--confirm"))
        elif renderer != "textual":
            raise ValueError(f"unsupported renderer: {renderer}")
        return args

    def _run_launcher(
        self,
        args: list[str],
        *,
        renderer: str,
        environment: dict[str, str] | None = None,
    ) -> InstallerProcessResult:
        command = [str(self.distribution_directory / "install.sh"), *args]
        launcher_environment = environment or self._launcher_environment()
        if renderer in {"textual", "plain"}:
            return _run_in_pty(
                command,
                cwd=self.distribution_directory,
                env=launcher_environment,
                textual=renderer == "textual",
            )
        process = subprocess.Popen(
            command,
            cwd=self.distribution_directory,
            env=launcher_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = _communicate_with_process_group_timeout(
            process, timeout_seconds=_INSTALL_TIMEOUT_SECONDS
        )
        result = InstallerProcessResult(
            args=tuple(command),
            returncode=int(process.returncode),
            stdout=stdout,
            stderr=stderr,
        )
        if result.returncode != 0:
            try:
                runtime_evidence = self._transition_runtime_diagnostic()
            except Exception:
                runtime_evidence = "unavailable"
            diagnostic = (
                "Launcher stdout tail:\n"
                + stdout[-1_800:]
                + "\nLauncher stderr tail:\n"
                + stderr[-800:]
                + "\nRuntime installation evidence:\n"
                + runtime_evidence[-4_096:]
            )
            self._record_install_failure_evidence(
                returncode=result.returncode,
                diagnostic=diagnostic,
            )
            result = InstallerProcessResult(
                args=result.args,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=(
                    result.stderr
                    + (
                        "Runtime installation evidence:\n" + runtime_evidence + "\n"
                        if runtime_evidence != "unavailable"
                        else ""
                    )
                ),
            )
        return result

    def _launcher_environment(self) -> dict[str, str]:
        manifest = self._manifest()
        installer_image = str(manifest["planned_resources"]["images"][0])
        tags = manifest["production_image_tags"]
        external_tls = bool((manifest.get("external_s3") or {}).get("certificate_directory"))
        inherited = {
            name: value
            for name, value in os.environ.items()
            if name not in _canonical_product_environment_names(self.source_root)
        }
        return {
            **inherited,
            "FS_COMPOSE_PROJECT_NAME": self.project_name,
            "FS_INSTALLER_STATE_DIR": str(self.state_directory),
            "FS_INSTALLER_IMAGE": installer_image,
            "APP_PORT": str(self.ports["public"]),
            "HTTP_PORT": str(self.ports["http"]),
            "POSTGRES_HOST_PORT": str(self.ports["postgres"]),
            "RABBITMQ_AMQP_HOST_PORT": str(self.ports["rabbitmq_amqp"]),
            "RABBITMQ_MANAGEMENT_HOST_PORT": str(self.ports["rabbitmq_management"]),
            "MAILPIT_SMTP_PORT": str(self.ports["mailpit_smtp"]),
            "MAILPIT_WEB_PORT": str(self.ports["mailpit_web"]),
            "MAILPIT_HTTP_PORT": str(self.ports["mailpit_web"]),
            "FRONTEND_HOST_PORT": str(self.ports["frontend"]),
            "MCP_HOST_PORT": str(self.ports["mcp"]),
            "SONAR_PORT": str(self.ports["sonarqube"]),
            "SEAWEEDFS_S3_HOST_PORT": str(self.ports["seaweedfs_s3"]),
            "SEAWEEDFS_MASTER_HOST_PORT": str(self.ports["seaweedfs_master"]),
            "FS_IMAGE": tags["app"],
            "MCP_IMAGE": tags["mcp"],
            "FS_CLI_IMAGE": tags["cli"],
            "POSTGRES_IMAGE": tags["postgres"],
            **({"AWS_CA_BUNDLE": self._external_container_ca_path()} if external_tls else {}),
            "FS_INSTALLER_HEARTBEAT_SECONDS": "15",
            "NO_COLOR": "1",
            "TERM": "xterm-256color",
        }

    def _installed_launcher_environment(self) -> dict[str, str]:
        """Run installed commands without overriding durable image provenance."""
        environment = self._launcher_environment()
        for name in ("FS_IMAGE", "MCP_IMAGE", "FS_CLI_IMAGE", "POSTGRES_IMAGE"):
            environment.pop(name, None)
        return environment

    def _start_external_s3(self, *, use_tls: bool) -> tuple[str, Path]:
        credentials_path = self._record_external_s3_identity(reachable=True, use_tls=use_tls)
        manifest = self._manifest()
        token = str(manifest["owner_token"])
        image = str(manifest["planned_resources"]["images"][1])
        container = f"fs-external-s3-{token}"
        volume = f"fs-external-s3-data-{token}"
        identity = self.external_s3_identity
        if identity is None:
            raise AssertionError("external S3 identity was not recorded")
        base_image = _MINIO_IMAGE
        _run(["docker", "pull", base_image])
        self._record_dependency_image_identity(base_image)
        _run(["docker", "tag", base_image, image])
        _run(
            [
                "docker",
                "volume",
                "create",
                "--label",
                f"com.flow-steward.disposable-owner={token}",
                "--label",
                f"com.flow-steward.disposable-project={self.project_name}",
                volume,
            ]
        )
        command = [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--label",
            f"com.flow-steward.disposable-owner={token}",
            "--label",
            f"com.flow-steward.disposable-project={self.project_name}",
            "-p",
            f"127.0.0.1:{self.ports['external_s3']}:9000",
            "-e",
            "MINIO_ROOT_USER",
            "-e",
            "MINIO_ROOT_PASSWORD",
            "-v",
            f"{volume}:/data",
        ]
        if use_tls:
            command.extend(("-v", f"{self._external_certificate_dir()}:/root/.minio/certs:ro"))
        command.extend((image, "server", "/data"))
        _run(
            command,
            env={
                **os.environ,
                "MINIO_ROOT_USER": str(identity["access_key_id"]),
                "MINIO_ROOT_PASSWORD": self._external_s3_secret,
            },
        )
        endpoint = (
            f"{'https' if use_tls else 'http'}://host.docker.internal:{self.ports['external_s3']}"
        )
        self._wait_for_external_s3(use_tls=use_tls)
        self._create_external_bucket(endpoint=endpoint, use_tls=use_tls)
        return endpoint, credentials_path

    def _record_dependency_image_identity(self, reference: str) -> None:
        """Record the exact late-materialized dependency before creating owned resources."""
        inspected = _inspect_image_with_retry(reference)
        if inspected is None or not str(inspected.get("Id") or ""):
            raise AssertionError(f"dependency image identity is unavailable: {reference}")
        manifest = self._manifest()
        dependencies = manifest.setdefault("dependency_images", [])
        observed = manifest.setdefault("observed_dependency_image_ids", {})
        if not isinstance(dependencies, list) or not isinstance(observed, dict):
            raise AssertionError("dependency image evidence manifest shape is invalid")
        dependencies[:] = list(dict.fromkeys((*dependencies, reference)))
        observed[reference] = str(inspected["Id"])
        _replace_owner_only_json(self.manifest_path, manifest)

    def _record_external_s3_identity(self, *, reachable: bool, use_tls: bool) -> Path:
        if self.external_s3_identity is not None:
            identity = self.external_s3_identity
            if bool(identity["reachable"]) is not reachable:
                raise AssertionError("external S3 reachability differs from the recorded manifest")
            if bool(identity["certificate_directory"]) is not use_tls:
                raise AssertionError("external S3 TLS mode differs from the recorded manifest")
            credentials_path = self._external_credentials_path()
            if not credentials_path.exists():
                _write_new_owner_only_json(
                    credentials_path,
                    {
                        "access_key": identity["access_key_id"],
                        "secret_key": self._external_s3_secret,
                    },
                )
            if use_tls and not self._external_ca_path().exists():
                self._create_external_tls_material()
            return credentials_path
        manifest = self._manifest()
        token = str(manifest["owner_token"])
        access_key = f"fstest{token[:20]}"
        self._external_s3_secret = secrets.token_urlsafe(32)
        credentials_path = self.manifest_path.parent / "external-s3-credentials.json"
        certificate_dir = self._external_certificate_dir()
        manifest["external_s3"] = {
            "service": "minio",
            "container": f"fs-external-s3-{token}",
            "volume": f"fs-external-s3-data-{token}",
            "image": str(manifest["planned_resources"]["images"][1]),
            "access_key_id": access_key,
            "secret_sha256": hashlib.sha256(self._external_s3_secret.encode()).hexdigest(),
            "bucket": self._bucket_name(),
            "credentials_path": str(credentials_path),
            "certificate_directory": str(certificate_dir) if use_tls else "",
            "reachable": reachable,
        }
        _replace_owner_only_json(self.manifest_path, manifest)
        _write_new_owner_only_json(
            credentials_path,
            {"access_key": access_key, "secret_key": self._external_s3_secret},
        )
        if use_tls:
            self._create_external_tls_material()
        return credentials_path

    def _create_external_tls_material(self) -> None:
        cert_dir = self._external_certificate_dir()
        cert_dir.mkdir(mode=0o700)
        _run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(cert_dir / "ca.key"),
                "-out",
                str(cert_dir / "ca.crt"),
                "-days",
                "2",
                "-subj",
                "/CN=Flow Steward disposable S3 CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
            ]
        )
        _run(
            [
                "openssl",
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(cert_dir / "private.key"),
                "-out",
                str(cert_dir / "server.csr"),
                "-subj",
                "/CN=host.docker.internal",
            ]
        )
        extensions = cert_dir / "server.ext"
        _write_new_owner_only_text(
            extensions,
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:host.docker.internal,DNS:localhost,IP:127.0.0.1\n",
        )
        _run(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(cert_dir / "server.csr"),
                "-CA",
                str(cert_dir / "ca.crt"),
                "-CAkey",
                str(cert_dir / "ca.key"),
                "-CAcreateserial",
                "-out",
                str(cert_dir / "public.crt"),
                "-days",
                "1",
                "-extfile",
                str(extensions),
            ]
        )
        (cert_dir / "ca.key").chmod(0o600)
        (cert_dir / "private.key").chmod(0o600)
        (cert_dir / "ca.crt").chmod(0o644)
        (cert_dir / "public.crt").chmod(0o644)
        _write_new_owner_only_bytes(
            self._external_host_ca_path(),
            self._external_ca_path().read_bytes(),
        )
        self._external_host_ca_path().chmod(0o644)

    def _create_provided_tls_material(self) -> None:
        target = self.distribution_directory / ".acceptance-tls"
        target.mkdir(mode=0o700, exist_ok=True)
        _run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(target / "key.pem"),
                "-out",
                str(target / "cert.pem"),
                "-days",
                "2",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ]
        )
        (target / "key.pem").chmod(0o600)

    def _wait_for_external_s3(self, *, use_tls: bool) -> None:
        scheme = "https" if use_tls else "http"
        context = (
            ssl.create_default_context(cafile=str(self._external_ca_path())) if use_tls else None
        )
        url = f"{scheme}://127.0.0.1:{self.ports['external_s3']}/minio/health/ready"
        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2, context=context) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.5)
        raise TimeoutError(f"external S3 did not become ready: {url}")

    def _create_external_bucket(self, *, endpoint: str, use_tls: bool) -> None:
        credentials = self._read_external_credentials()
        host_endpoint = endpoint.replace("host.docker.internal", "127.0.0.1")
        client = boto3.client(
            "s3",
            endpoint_url=host_endpoint,
            aws_access_key_id=credentials["access_key"],
            aws_secret_access_key=credentials["secret_key"],
            region_name="us-east-1",
            verify=str(self._external_ca_path()) if use_tls else True,
            config=Config(s3={"addressing_style": "path"}),
        )
        client.create_bucket(Bucket=self._bucket_name())

    def _bundled_credentials(self) -> dict[str, str]:
        env_name = (
            ".env.prod" if self._active_configuration()["environment"] == "production" else ".env"
        )
        values = _read_env(self.distribution_directory / env_name)
        return {
            "access_key": values["ARTIFACT_S3_ACCESS_KEY"],
            "secret_key": values["ARTIFACT_S3_SECRET_KEY"],
        }

    def _read_external_credentials(self) -> dict[str, str]:
        return json.loads(self._external_credentials_path().read_text(encoding="utf-8"))

    def _external_credentials_path(self) -> Path:
        identity = self.external_s3_identity
        if identity is None:
            raise RuntimeError("external S3 identity has not been recorded")
        return Path(identity["credentials_path"])

    def _external_certificate_dir(self) -> Path:
        return self.manifest_path.parent / "external-s3-certs"

    def _external_ca_path(self) -> Path:
        return self._external_certificate_dir() / "ca.crt"

    def _external_container_ca_path(self) -> str:
        token = self._manifest()["owner_token"]
        return f"/app/extensions/.disposable-installer-ca-{token}.crt"

    def _external_host_ca_path(self) -> Path:
        return self.distribution_directory / self._external_container_ca_path().removeprefix(
            "/app/"
        )

    def _external_endpoint(self) -> str:
        identity = self.external_s3_identity
        if identity is None:
            return ""
        scheme = "https" if identity.get("certificate_directory") else "http"
        return f"{scheme}://host.docker.internal:{self.ports['external_s3']}"

    def _bucket_name(self) -> str:
        return f"fs-installer-{self._manifest()['owner_token']}"

    def _manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _owned_resource_snapshot(self) -> dict[str, dict[str, str]]:
        manifest = self._manifest()
        snapshot: dict[str, dict[str, str]] = {}
        for kind in ("container", "network", "volume"):
            names = _names_with_owned_labels(
                kind,
                project_name=self.project_name,
                owner_token=str(manifest["owner_token"]),
            )
            identities: dict[str, str] = {}
            for name in names:
                inspected = _inspect(kind, name)
                if inspected is not None:
                    labels = inspected.get("Config", {}).get("Labels") or {}
                    if kind == "container" and str(
                        labels.get("com.docker.compose.oneoff") or ""
                    ).lower() in {"1", "true"}:
                        continue
                    identities[name] = str(
                        inspected.get("Id") or inspected.get("ID") or inspected.get("Name") or ""
                    )
            snapshot[f"{kind}s"] = identities
        task_tags = list(
            dict.fromkeys(
                (
                    *manifest.get("planned_resources", {}).get("images", ()),
                    *manifest.get("production_image_tags", {}).values(),
                )
            )
        )
        snapshot["images"] = {
            str(tag): str(inspected["Id"])
            for tag in task_tags
            if (inspected := _inspect("image", str(tag))) is not None
        }
        return snapshot

    def _refresh_exact_docker_identities(self) -> None:
        manifest = self._manifest()
        active = manifest["active_resources"]
        for kind, key in (
            ("container", "containers"),
            ("network", "networks"),
            ("volume", "volumes"),
        ):
            observed = _names_with_owned_labels(
                kind,
                project_name=self.project_name,
                owner_token=str(manifest["owner_token"]),
            )
            if kind == "container":
                observed = list(
                    dict.fromkeys((*observed, *self._installer_bootstrap_container_names(manifest)))
                )
            active[key] = list(dict.fromkeys((*active[key], *observed)))
        for tag in manifest["production_image_tags"].values():
            inspected = _inspect("image", str(tag))
            if inspected is not None:
                manifest["observed_image_ids"][str(tag)] = str(inspected["Id"])
        _replace_owner_only_json(self.manifest_path, manifest)

    def _remove_owned_container(self, name: str, manifest: dict[str, Any]) -> None:
        inspected = _inspect("container", name)
        if inspected is None:
            return
        labels = inspected.get("Config", {}).get("Labels") or {}
        if (
            labels.get("com.docker.compose.project") != self.project_name
            and labels.get("com.flow-steward.disposable-owner") != manifest["owner_token"]
            and not self._is_owned_installer_bootstrap(inspected, manifest)
        ):
            raise AssertionError(f"container ownership mismatch: {name}")
        _run(["docker", "container", "rm", "-f", name])

    def _installer_bootstrap_container_names(self, manifest: dict[str, Any]) -> list[str]:
        planned_images = manifest.get("planned_resources", {}).get("images", ())
        if not planned_images:
            return []
        installer_image = str(planned_images[0])
        names: list[str] = []
        for name in _container_names_for_ancestor(installer_image):
            inspected = _inspect("container", name)
            if inspected is not None and self._is_owned_installer_bootstrap(inspected, manifest):
                names.append(name)
        return names

    def _is_owned_installer_bootstrap(
        self,
        inspected: dict[str, Any],
        manifest: dict[str, Any],
    ) -> bool:
        planned_images = manifest.get("planned_resources", {}).get("images", ())
        if not planned_images:
            return False
        installer_image = str(planned_images[0])
        config = inspected.get("Config") or {}
        environment = set(config.get("Env") or ())
        distribution = str(manifest.get("distribution_directory") or "")
        state_directory = str(manifest.get("state_directory") or "")
        mounts = inspected.get("Mounts") or ()
        return (
            bool(distribution)
            and bool(state_directory)
            and str(config.get("Image") or "") == installer_image
            and f"FS_COMPOSE_PROJECT_NAME={self.project_name}" in environment
            and f"FS_INSTALLER_STATE_DIR={state_directory}" in environment
            and any(
                str(mount.get("Source") or "") == distribution
                and str(mount.get("Destination") or "") == distribution
                for mount in mounts
            )
        )

    def _remove_owned_network(self, name: str, manifest: dict[str, Any]) -> None:
        inspected = _inspect("network", name)
        if inspected is None:
            return
        labels = inspected.get("Labels") or {}
        if (
            labels.get("com.docker.compose.project") != self.project_name
            and labels.get("com.flow-steward.disposable-owner") != manifest["owner_token"]
        ):
            raise AssertionError(f"network ownership mismatch: {name}")
        _run(["docker", "network", "rm", name])

    def _remove_owned_volume(self, name: str, manifest: dict[str, Any]) -> None:
        inspected = _inspect("volume", name)
        if inspected is None:
            return
        labels = inspected.get("Labels") or {}
        if (
            labels.get("com.docker.compose.project") != self.project_name
            and labels.get("com.flow-steward.disposable-owner") != manifest["owner_token"]
        ):
            raise AssertionError(f"volume ownership mismatch: {name}")
        _run(["docker", "volume", "rm", name])

    def _verify_resources_absent(self, manifest: dict[str, Any]) -> None:
        planned = manifest["planned_resources"]
        for kind, key in (
            ("container", "containers"),
            ("network", "networks"),
            ("volume", "volumes"),
        ):
            residue = [name for name in planned[key] if _inspect(kind, str(name)) is not None]
            residue.extend(
                _names_with_owned_labels(
                    kind,
                    project_name=self.project_name,
                    owner_token=str(manifest["owner_token"]),
                )
            )
            if kind == "container":
                residue.extend(self._installer_bootstrap_container_names(manifest))
            residue = list(dict.fromkeys(residue))
            if residue:
                raise AssertionError(f"owned {kind} residue: {residue}")
        image_residue = [name for name in planned["images"] if _inspect("image", str(name))]
        if image_residue:
            raise AssertionError(f"owned image residue: {image_residue}")
        path_residue = [name for name in planned["paths"] if Path(str(name)).exists()]
        if path_residue:
            raise AssertionError(f"owned path residue: {path_residue}")

    def _verify_baseline_preserved(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Record unrelated concurrent Docker drift without claiming cleanup caused it."""
        current = _docker_inventory()
        missing: dict[str, list[str]] = {}
        for kind in ("containers", "networks", "volumes", "images"):
            absent = sorted(set(manifest["baseline_inventory"][kind]) - set(current[kind]))
            if absent:
                missing[kind] = absent
        current_tags = _docker_image_tag_mappings()
        changed_tags = {
            tag: {"before": image_id, "after": current_tags.get(tag, "")}
            for tag, image_id in manifest.get("baseline_image_tags", {}).items()
            if current_tags.get(tag) != image_id
        }
        return {
            "inventory_after_cleanup": current,
            "missing_baseline_ids": missing,
            "changed_baseline_tags": changed_tags,
        }


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=check,
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_SECONDS,
    )


def _git_revision(root: Path) -> str:
    revision = _run(["git", "-C", str(root.resolve()), "rev-parse", "HEAD^{commit}"]).stdout.strip()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise AssertionError("disposable source does not have a full Git revision")
    return revision


@cache
def _canonical_product_environment_names(source_root: Path) -> frozenset[str]:
    manifest_path = (
        source_root.resolve() / "core/infrastructure/config/flow_steward_env_manifest.json"
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    variables = payload.get("variables") if isinstance(payload, dict) else None
    if not isinstance(variables, list):
        raise AssertionError("canonical Flow Steward environment manifest is invalid")
    names = {
        str(variable.get("name") or "") for variable in variables if isinstance(variable, dict)
    }
    if "SONAR_SCANNER_IMAGE" not in names or any(not name for name in names):
        raise AssertionError("canonical Flow Steward environment names are invalid")
    return frozenset(names)


def _verify_vendored_wheel_hashes(distribution: Path) -> None:
    """Every bundle in the distribution that pins wheels by hash must match them.

    A rule over whatever shipped rather than a check on one named bundle: with no
    bundles it holds trivially, and with ten it checks all ten.
    """
    for manifest_path in sorted((distribution / "extensions").glob("*/extension.yaml")):
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        requirements = manifest.get("python_requirements") if isinstance(manifest, dict) else None
        if not isinstance(requirements, list) or not requirements:
            continue
        declared = {
            digest
            for requirement in requirements
            if isinstance(requirement, dict)
            for digest in (requirement.get("hashes") or [])
        }
        if not declared:
            continue
        actual = {
            f"sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}"
            for wheel in (manifest_path.parent / "wheels").glob("*.whl")
        }
        missing = declared - actual
        if missing:
            raise AssertionError(
                f"{manifest_path.parent.name}: vendored wheels do not match declared hashes: "
                f"{sorted(missing)}"
            )


def _write_new_owner_only_text(path: Path, value: str) -> None:
    _write_new_owner_only_bytes(path, value.encode())


def _write_new_owner_only_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, value)
    finally:
        os.close(descriptor)


def _replace_owner_only_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    _write_new_owner_only_text(temporary, value)
    os.replace(temporary, path)


def _run_in_pty(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    textual: bool,
    timeout_seconds: float = _INSTALL_TIMEOUT_SECONDS,
) -> InstallerProcessResult:
    pid, master_fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, _PTY_WINDOW)
        os.chdir(cwd)
        os.execve(command[0], command, env)
    output = bytearray()
    requested_review = False
    sent_review = False
    saw_any_review = False
    saw_form = False
    saw_review = False
    saw_textual_terminal = False
    reached_eof = False
    child_reaped = False
    form_ready_at: float | None = None
    answered_plain_prompts: set[bytes] = set()
    deadline = time.monotonic() + timeout_seconds
    try:
        os.write(master_fd, b"")
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, _PTY_WINDOW)
        while time.monotonic() < deadline:
            ready, _, _ = (
                select.select([master_fd], [], [], 0.25) if not reached_eof else ([], [], [])
            )
            if ready:
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    reached_eof = True
                    chunk = b""
                if not chunk:
                    reached_eof = True
                else:
                    output.extend(chunk)
                    saw_any_review = b"Review" in output
                    saw_form = _TEXTUAL_FORM_MARKER in output
                    saw_review = _TEXTUAL_REVIEW_MARKER in output
                    saw_textual_terminal = any(
                        marker in output for marker in _ALTERNATE_SCREEN_MARKERS
                    )
                    if textual and _PLAIN_FALLBACK_BANNER in output:
                        status = _terminate_process_group(pid)
                        if status is None:
                            _, status = os.waitpid(pid, 0)
                        return InstallerProcessResult(
                            args=tuple(command),
                            returncode=os.waitstatus_to_exitcode(status) or 1,
                            stdout=output.decode(errors="replace"),
                            stderr="installer Textual path entered the plain fallback wizard",
                        )
                    if not textual:
                        for marker in _PLAIN_OPTIONAL_PROMPT_MARKERS:
                            if marker in output and marker not in answered_plain_prompts:
                                os.write(master_fd, b"\r")
                                answered_plain_prompts.add(marker)
                    if (
                        textual
                        and not requested_review
                        and saw_textual_terminal
                        and saw_form
                        and form_ready_at is None
                    ):
                        form_ready_at = time.monotonic() + 2.0
            if (
                textual
                and not requested_review
                and form_ready_at is not None
                and time.monotonic() >= form_ready_at
            ):
                os.write(master_fd, b"\x1b[Z\x1b[Z\r")
                requested_review = True
            if textual and requested_review and not sent_review and saw_review:
                time.sleep(0.25)
                os.write(master_fd, b"\r")
                sent_review = True
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                child_reaped = True
                returncode = os.waitstatus_to_exitcode(status)
                stderr = ""
                if textual and not saw_any_review:
                    returncode = returncode or 1
                    stderr = "installer Textual interaction marker was not observed"
                elif textual and not saw_textual_terminal:
                    returncode = returncode or 1
                    stderr = "installer Textual terminal evidence was not observed"
                elif textual and not saw_review:
                    returncode = returncode or 1
                    stderr = "installer Textual interaction marker was not observed"
                elif textual and not sent_review:
                    returncode = returncode or 1
                    stderr = "installer Textual Review interaction was not completed"
                return InstallerProcessResult(
                    args=tuple(command),
                    returncode=returncode,
                    stdout=output.decode(errors="replace"),
                    stderr=stderr,
                )
            if reached_eof:
                time.sleep(0.01)
        status = _terminate_process_group(pid)
        if status is None:
            _, status = os.waitpid(pid, 0)
        return InstallerProcessResult(
            args=tuple(command),
            returncode=os.waitstatus_to_exitcode(status),
            stdout=output.decode(errors="replace"),
            stderr="installer PTY timed out",
        )
    except BaseException:
        if not child_reaped:
            _terminate_process_group(pid)
        raise
    finally:
        os.close(master_fd)


def _terminate_process_group(pid: int) -> int | None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return status
        time.sleep(0.05)
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    return status


def _communicate_with_process_group_timeout(
    process: subprocess.Popen[str], *, timeout_seconds: float, input_text: str | None = None
) -> tuple[str, str]:
    try:
        return process.communicate(input=input_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            return process.communicate()


def _reserve_unique_ports(keys: tuple[str, ...]) -> dict[str, int]:
    reservations: list[socket.socket] = []
    selected: dict[str, int] = {}
    try:
        for key in keys:
            for _ in range(_ACCEPTANCE_PORT_ATTEMPTS):
                port = _ACCEPTANCE_PORT_FIRST + secrets.randbelow(
                    _ACCEPTANCE_PORT_STOP - _ACCEPTANCE_PORT_FIRST
                )
                if port in selected.values():
                    continue
                candidates: list[socket.socket] = []
                try:
                    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                        candidate = socket.socket(socket.AF_INET, kind)
                        candidates.append(candidate)
                        candidate.bind(("0.0.0.0", port))
                except OSError:
                    for candidate in candidates:
                        candidate.close()
                    continue
                reservations.extend(candidates)
                selected[key] = port
                break
            else:
                raise RuntimeError(f"could not reserve a disposable port for {key}")
        return selected
    finally:
        for reservation in reservations:
            reservation.close()


def _write_new_owner_only_json(path: Path, value: dict[str, Any]) -> None:
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)


def _replace_owner_only_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    _write_new_owner_only_json(temporary, value)
    os.replace(temporary, path)


def _bounded_preservation_value(value: object) -> object:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= 2_048:
        return value
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
    }


def _docker_inventory() -> dict[str, list[str]]:
    commands = {
        "containers": ["docker", "container", "ls", "-a", "-q"],
        "networks": ["docker", "network", "ls", "-q"],
        "volumes": ["docker", "volume", "ls", "-q"],
        "images": ["docker", "image", "ls", "-a", "-q", "--no-trunc"],
    }
    return {
        kind: sorted(set(_run(command).stdout.splitlines())) for kind, command in commands.items()
    }


def _docker_build_cache_records() -> dict[str, dict[str, Any]]:
    result = _run(["docker", "buildx", "du", "--format", "json"])
    records: dict[str, dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        cache_id = str(record.get("ID") or "")
        if _BUILDKIT_CACHE_ID.fullmatch(cache_id) is None:
            raise AssertionError("Docker build cache inventory returned an invalid identity")
        records[cache_id] = record
    return records


def _build_cache_prune_order(
    records: dict[str, dict[str, Any]],
    cache_ids: list[str],
) -> list[str]:
    """Order exact BuildKit records so descendants are removed before parents."""
    remaining = set(cache_ids)
    children = {cache_id: set() for cache_id in remaining}
    for child_id in remaining:
        for parent_id in records[child_id].get("Parents") or ():
            if parent_id in remaining:
                children[parent_id].add(child_id)
    ordered: list[str] = []
    while remaining:
        leaves = sorted(
            cache_id for cache_id in remaining if not children[cache_id].intersection(remaining)
        )
        if not leaves:
            ordered.extend(sorted(remaining))
            break
        ordered.extend(leaves)
        remaining.difference_update(leaves)
    return ordered


def _anonymous_volume_names() -> list[str]:
    result = _run(
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            "label=com.docker.volume.anonymous",
        ]
    )
    return sorted(set(result.stdout.splitlines()))


def _volume_container_ids(name: str) -> list[str]:
    result = _run(
        [
            "docker",
            "container",
            "ls",
            "-a",
            "-q",
            "--filter",
            f"volume={name}",
        ]
    )
    return sorted(set(result.stdout.splitlines()))


def _docker_image_tag_mappings() -> dict[str, str]:
    result = _run(
        [
            "docker",
            "image",
            "ls",
            "-a",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ]
    )
    mappings: dict[str, str] = {}
    for line in result.stdout.splitlines():
        record = json.loads(line)
        repository = str(record.get("Repository") or "")
        tag = str(record.get("Tag") or "")
        image_id = str(record.get("ID") or "")
        if repository and repository != "<none>" and tag and tag != "<none>" and image_id:
            mappings[f"{repository}:{tag}"] = image_id
    return mappings


def _names_with_label(kind: str, project_name: str) -> list[str]:
    return _names_with_exact_label(kind, f"com.docker.compose.project={project_name}")


def _names_with_owner_label(kind: str, owner_token: str) -> list[str]:
    return _names_with_exact_label(kind, f"com.flow-steward.disposable-owner={owner_token}")


def _names_with_owned_labels(kind: str, *, project_name: str, owner_token: str) -> list[str]:
    return list(
        dict.fromkeys(
            (
                *_names_with_label(kind, project_name),
                *_names_with_owner_label(kind, owner_token),
            )
        )
    )


def _names_with_exact_label(kind: str, label: str) -> list[str]:
    noun = "container" if kind == "container" else kind
    command = ["docker", noun, "ls"]
    if noun == "container":
        command.append("-a")
    command.extend(
        [
            "--filter",
            f"label={label}",
            "--format",
            "{{.Names}}" if noun == "container" else "{{.Name}}",
        ]
    )
    result = _run(
        command,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise AssertionError(f"Docker label inventory query failed for {kind}: {detail}")
    return [line for line in result.stdout.splitlines() if line]


def _container_names_for_ancestor(image: str) -> list[str]:
    result = _run(
        [
            "docker",
            "container",
            "ls",
            "--all",
            "--filter",
            f"ancestor={image}",
            "--format",
            "{{.Names}}",
        ]
    )
    return [line for line in result.stdout.splitlines() if line]


def _image_ids_with_container_links() -> set[str]:
    containers = _run(
        ["docker", "container", "ls", "--all", "--quiet", "--no-trunc"]
    ).stdout.splitlines()
    if not containers:
        return set()
    result = _run(
        [
            "docker",
            "container",
            "inspect",
            *containers,
            "--format",
            "{{.Image}}",
        ]
    )
    return {line for line in result.stdout.splitlines() if line}


def _container_names_for_project(project_name: str) -> list[str]:
    return _names_with_label("container", project_name)


def _inspect(kind: str, identity: str) -> dict[str, Any] | None:
    result = _run(["docker", kind, "inspect", identity], check=False)
    if result.returncode != 0:
        return None
    payload = json.loads(result.stdout)
    return payload[0] if isinstance(payload, list) and payload else None


def _inspect_image_with_retry(identity: str) -> dict[str, Any] | None:
    for attempt in range(_IMAGE_INSPECT_ATTEMPTS):
        inspected = _inspect("image", identity)
        if inspected is not None:
            return inspected
        if attempt + 1 < _IMAGE_INSPECT_ATTEMPTS:
            time.sleep(_IMAGE_INSPECT_RETRY_SECONDS)
    return None


def _project_container_rows(project_name: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name in _container_names_for_project(project_name):
        record = _inspect("container", name)
        if record is None:
            continue
        labels = record.get("Config", {}).get("Labels") or {}
        state = record.get("State") or {}
        health = state.get("Health") or {}
        rows.append(
            {
                "container_id": str(record.get("Id") or ""),
                "service": str(labels.get("com.docker.compose.service") or ""),
                "state": str(state.get("Status") or ""),
                "health": str(health.get("Status") or ""),
            }
        )
    return rows


def _json_records(value: str) -> list[dict[str, Any]]:
    text = value.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = [json.loads(line) for line in text.splitlines() if line.strip()]
    records = payload if isinstance(payload, list) else [payload]
    if not all(isinstance(record, dict) for record in records):
        raise AssertionError("Docker Compose returned an invalid JSON record stream")
    return [dict(record) for record in records]


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("'\"")
    return values
