from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import ssl
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.extensions.oauth_contract import is_canonical_https_origin
from core.application.installer import (
    InstallConfig,
    InstallJournal,
    ReleaseIdentity,
    ReleaseImageIdentity,
    classify_installation,
)
from core.application.installer.errors import InstallerFailure
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.release.images import all_release_roles, required_product_roles
from core.infrastructure.config.env_registry import registry_default_value
from core.infrastructure.installer import gateway as gateway_module
from core.infrastructure.installer import (
    gateway_cli_runtime,
    gateway_health,
    gateway_image_provenance,
    registry_image_resolver,
)
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.registry_image_resolver import RegistryImageIdentity
from core.infrastructure.installer.state_store import InstallerStateStore

_FULL_REVISION = "0123456789abcdef0123456789abcdef01234567"
# The build argument is asserted as name plus value rather than as one written-out
# string: `FS_IMAGE_PLATFORM=linux/arm64` scores above the Base64 entropy limit and
# the secret scan reports it, which is a false positive best removed rather than
# suppressed -- the composed form also matches the BUILD_SHA assertion beside it.
_HOST_PLATFORM = "linux/arm64"
_APP_IMAGE = "flow-steward-app:installer-acceptance"
_WORKER_IMAGE = "flow-steward-worker:installer-acceptance"
_CONTROL_IMAGE = "flow-steward-control:installer-acceptance"
_MCP_IMAGE = "flow-steward-mcp:installer-acceptance"
_APP_IMAGE_ID = f"sha256:{'a' * 64}"
_WORKER_IMAGE_ID = f"sha256:{'c' * 64}"
_CONTROL_IMAGE_ID = f"sha256:{'d' * 64}"
_MCP_IMAGE_ID = f"sha256:{'b' * 64}"
_CLI_IMAGE_ID = f"sha256:{'e' * 64}"
_FRONTEND_IMAGE_ID = f"sha256:{'f' * 64}"
_APP_REVISION = "1" * 40
_WORKER_REVISION = "2" * 40
_CONTROL_REVISION = "3" * 40
_MCP_REVISION = "4" * 40
_CLI_REVISION = "5" * 40
_FRONTEND_REVISION = "6" * 40
_CLI_READINESS_FAILURE_PAYLOAD = {
    "checks": [
        {
            "id": "cli_runtime_readiness",
            "category": "core",
            "title": "CLI runtime readiness",
            "status": "fail",
            "detail": "The CLI runtime readiness probe failed.",
            "remediation": "Verify the retained CLI runtime and rerun ./install.sh.",
        }
    ]
}


def _image_inspection(
    image_id: str,
    revision: str = _FULL_REVISION,
    *,
    service: str | None = None,
    command: tuple[str, ...] = (),
    entrypoint: tuple[str, ...] = (),
    architecture: str = "arm64",
    descriptor_digest: str | None = None,
    embedded_revision: str | None = None,
) -> str:
    labels = {"org.opencontainers.image.revision": revision}
    if service is not None:
        labels["com.docker.compose.service"] = service
    config: dict[str, object] = {
        "Labels": labels,
        "Cmd": list(command),
        "Entrypoint": list(entrypoint),
    }
    if embedded_revision is not None:
        config["Env"] = [
            "PATH=/usr/local/bin:/usr/bin:/bin",
            f"FS_EMBEDDED_IMAGE_REVISION={embedded_revision}",
        ]
    details: dict[str, object] = {
        "Id": image_id,
        "Architecture": architecture,
        "Config": config,
    }
    if descriptor_digest is not None:
        details["Descriptor"] = {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "digest": descriptor_digest,
        }
    return json.dumps([details])


def _exact_prebuilt_values(*, include_frontend: bool = False) -> dict[str, str]:
    values = {
        "FS_APP_IMAGE": _APP_IMAGE_ID,
        "FS_WORKER_IMAGE": _WORKER_IMAGE_ID,
        "FS_CONTROL_IMAGE": _CONTROL_IMAGE_ID,
        "MCP_IMAGE": _MCP_IMAGE_ID,
        "FS_CLI_IMAGE": _CLI_IMAGE_ID,
    }
    if include_frontend:
        values["FS_FRONTEND_IMAGE"] = _FRONTEND_IMAGE_ID
    return values


# A Compact install consumes a prebuilt image (AC17-R81). Tests that use the compact
# profile to reach ingress, port, or network logic must still supply one, exactly as
# an operator does, or preflight now refuses before it gets that far.
_COMPACT_TEST_IMAGE = {"FS_COMPACT_IMAGE": "sha256:" + "a" * 64}


def _presence_probes(*images: str) -> list[tuple[str, ...]]:
    """AC17-R177b: prepare asks Docker whether each recorded image ID is still there."""
    return [("docker", "image", "inspect", "--format", "{{.Id}}", image) for image in images]


# The order prepare_core probes a Scalable installation's recorded image IDs in.
_SCALABLE_PROBED = (
    _APP_IMAGE_ID,
    _WORKER_IMAGE_ID,
    _CONTROL_IMAGE_ID,
    _MCP_IMAGE_ID,
    _CLI_IMAGE_ID,
)


def _write_env_values(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )


def _register_exact_prebuilt_inspections(
    process: FakeProcessExecutor,
    *,
    include_frontend: bool = False,
    mcp_revision: str = _MCP_REVISION,
    mcp_architecture: str = "arm64",
    mcp_service: str = "fs-mcp",
    mcp_command: tuple[str, ...] = ("python", "-m", "core.entrypoints.mcp.server"),
) -> dict[str, str]:
    inspections = {
        _APP_IMAGE_ID: _image_inspection(
            _APP_IMAGE_ID,
            _APP_REVISION,
            service="fs-app",
            command=("/app/core/entrypoints/cli/scripts/start_server.sh",),
        ),
        _WORKER_IMAGE_ID: _image_inspection(
            _WORKER_IMAGE_ID,
            _WORKER_REVISION,
            service="fs-worker",
            command=("python", "-m", "core.entrypoints.cli.worker_process"),
        ),
        _CONTROL_IMAGE_ID: _image_inspection(
            _CONTROL_IMAGE_ID,
            _CONTROL_REVISION,
            service="control-plane",
            command=("python", "-m", "core.entrypoints.cli.control_process"),
        ),
        _MCP_IMAGE_ID: _image_inspection(
            _MCP_IMAGE_ID,
            mcp_revision,
            service=mcp_service,
            command=mcp_command,
            architecture=mcp_architecture,
        ),
        _CLI_IMAGE_ID: _image_inspection(
            _CLI_IMAGE_ID,
            _CLI_REVISION,
            entrypoint=("python", "-m", "core.entrypoints.cli.lifecycle_click_app"),
        ),
    }
    if include_frontend:
        inspections[_FRONTEND_IMAGE_ID] = _image_inspection(
            _FRONTEND_IMAGE_ID,
            _FRONTEND_REVISION,
            service="fs-frontend",
            command=("/usr/local/bin/start_frontend_dev.sh",),
            entrypoint=("docker-entrypoint.sh",),
        )
    for image, payload in inspections.items():
        command = ("docker", "image", "inspect", image)
        process.outcomes[command] = ProcessOutcome(
            command=command,
            returncode=0,
            stdout=payload,
        )
    return inspections


def test_installer_configuration_extends_the_canonical_lifecycle_contract() -> None:
    assert issubclass(InstallConfig, DeploymentConfig)


def test_compact_cli_preflight_uses_runtime_owned_identity_without_cli_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(deployment_profile="compact", environment="production")
    monkeypatch.setattr(gateway, "_compose_prefix", lambda _config: ["docker", "compose"])
    log = gateway.state_store.open_log("compact-cli")

    gateway.ensure_cli_runtime(config, log=log)

    log.close()
    assert process.commands == [
        (
            "docker",
            "compose",
            "exec",
            "-T",
            "--user",
            "0:0",
            "compact-runtime",
            "python",
            "-m",
            "core.entrypoints.cli.ops.candidate_runtime_preflight",
        )
    ]
    assert all("up" not in command for command in process.commands)


def test_compact_cli_readiness_executes_inside_compact_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "checks": [
            {
                "id": "compact_runtime",
                "category": "core",
                "title": "Compact runtime",
                "status": "pass",
                "detail": "ready",
                "remediation": "none",
            }
        ]
    }
    command = (
        "docker",
        "compose",
        "exec",
        "-T",
        "compact-runtime",
        "python",
        "-c",
        gateway_module._COMPACT_CLI_READINESS_SCRIPT,
    )
    process = FakeProcessExecutor(
        {command: ProcessOutcome(command=command, returncode=0, stdout=json.dumps(payload))}
    )
    gateway = _gateway(tmp_path, process)
    monkeypatch.setattr(gateway, "_compose_prefix", lambda _config: ["docker", "compose"])

    assert (
        gateway.probe_cli_readiness(
            InstallConfig(deployment_profile="compact", environment="production")
        )
        == payload
    )
    assert process.commands == [command]


def test_compact_cli_readiness_retries_runtime_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starting = _readiness_payload("compact_runtime", "fail")
    ready = _readiness_payload("compact_runtime", "pass")
    command = (
        "docker",
        "compose",
        "exec",
        "-T",
        "compact-runtime",
        "python",
        "-c",
        gateway_module._COMPACT_CLI_READINESS_SCRIPT,
    )
    process = FakeProcessExecutor()
    process.queued_outcomes[command] = [
        ProcessOutcome(command=command, returncode=0, stdout=json.dumps(starting)),
        ProcessOutcome(command=command, returncode=0, stdout=json.dumps(ready)),
    ]
    gateway = _gateway(tmp_path, process)
    sleeps: list[float] = []
    gateway._sleep = sleeps.append
    monkeypatch.setattr(gateway, "_compose_prefix", lambda _config: ["docker", "compose"])

    assert (
        gateway.probe_cli_readiness(
            InstallConfig(deployment_profile="compact", environment="production")
        )
        == ready
    )
    assert process.commands == [command, command]
    assert sleeps == [gateway_module._READINESS_DELAY_SECONDS]


def test_compact_cli_readiness_returns_final_failure_after_bounded_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_health, "_READINESS_ATTEMPTS", 3)
    starting = _readiness_payload("compact_runtime", "fail")
    command = (
        "docker",
        "compose",
        "exec",
        "-T",
        "compact-runtime",
        "python",
        "-c",
        gateway_module._COMPACT_CLI_READINESS_SCRIPT,
    )
    process = FakeProcessExecutor()
    process.queued_outcomes[command] = [
        ProcessOutcome(command=command, returncode=0, stdout=json.dumps(starting)) for _ in range(3)
    ]
    gateway = _gateway(tmp_path, process)
    sleeps: list[float] = []
    gateway._sleep = sleeps.append
    monkeypatch.setattr(gateway, "_compose_prefix", lambda _config: ["docker", "compose"])

    assert (
        gateway.probe_cli_readiness(
            InstallConfig(deployment_profile="compact", environment="production")
        )
        == starting
    )
    assert process.commands == [command] * 3
    assert sleeps == [gateway_module._READINESS_DELAY_SECONDS] * 2


@pytest.mark.parametrize(
    "tls_mode",
    ("acme", "external_certificate", "external_resolver", "platform_managed"),
)
def test_external_tls_uses_system_trust_without_local_certificate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tls_mode: str
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig().model_copy(
        update={
            "tls_mode": tls_mode,
            "public_host": "flow.example.test",
            "public_port": 443,
        }
    )
    contexts: list[dict[str, object]] = []
    context = object()

    def create_context(**kwargs):
        contexts.append(kwargs)
        return context

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    monkeypatch.setattr(gateway, "_open_public_probe", lambda *_args, **_kwargs: Response())
    log = gateway.state_store.open_log(f"external-tls-{tls_mode}")

    gateway.verify_public_route(config, log=log)

    log.close()
    assert contexts == [{}]


class FakeProcessExecutor:
    def __init__(self, outcomes: dict[tuple[str, ...], ProcessOutcome] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.commands: list[tuple[str, ...]] = []
        self.invocations: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.queued_outcomes: dict[tuple[str, ...], list[ProcessOutcome]] = {}

    def run(self, command, **kwargs):
        key = tuple(command)
        self.commands.append(key)
        self.invocations.append((key, kwargs))
        if key[:2] == ("openssl", "req"):
            Path(key[key.index("-keyout") + 1]).touch(mode=0o600)
            Path(key[key.index("-out") + 1]).touch(mode=0o644)
        queued = self.queued_outcomes.get(key)
        if queued:
            return queued.pop(0)
        if (
            key[:2] == ("git", "-C")
            and key[-2:] == ("rev-parse", "HEAD^{commit}")
            and key not in self.outcomes
        ):
            return ProcessOutcome(command=key, returncode=0, stdout=_FULL_REVISION + "\n")
        if key[-3:] == ("config", "--format", "json") and key not in self.outcomes:
            return ProcessOutcome(
                command=key,
                returncode=0,
                stdout=json.dumps(
                    {
                        "services": {
                            name: {"image": f"test-{name}:present"}
                            for name in (
                                "traefik",
                                "rabbitmq-fs",
                                "pg-fs",
                                "seaweedfs",
                                "seaweedfs-config",
                            )
                        }
                    }
                ),
            )
        return self.outcomes.get(key, ProcessOutcome(command=key, returncode=0))


def _candidate_builds(commands) -> list[tuple[str, ...]]:
    """The Scalable candidate build invocations, as the installer issues them.

    AC17-R143 prefixed the command with `env FS_RELEASE_TLS_INPUTS=...`, so the script
    is matched at the end of the command rather than at its start.
    """
    return [
        command
        for command in commands
        if len(command) >= 2
        and command[-2] == "bash"
        and command[-1].endswith("scripts/release/build_candidate.sh")
    ]


class MissingDockerExecutor(FakeProcessExecutor):
    def run(self, command, **_kwargs):
        if command and command[0] == "docker":
            raise FileNotFoundError("docker")
        return super().run(command, **_kwargs)


class HostRouteSocket:
    def __init__(self, error: OSError | None = None) -> None:
        self.error = error
        self.timeout: float | None = None
        self.address: tuple[str, int] | None = None
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, address: tuple[str, int]) -> None:
        self.address = address
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


def _refused_host_route_socket(*_args: object, **_kwargs: object) -> HostRouteSocket:
    return HostRouteSocket(ConnectionRefusedError("unused Docker host port"))


class TLSValidationExecutor(FakeProcessExecutor):
    def __init__(self, *, matching: bool = True) -> None:
        super().__init__()
        self.matching = matching

    def run(self, command, **kwargs):
        outcome = super().run(command, **kwargs)
        key = tuple(command)
        if key[:2] == ("openssl", "x509") and "-pubkey" in key:
            return ProcessOutcome(command=key, returncode=0, stdout="public-key\n")
        if key[:2] == ("openssl", "pkey"):
            value = "public-key\n" if self.matching else "different-key\n"
            return ProcessOutcome(command=key, returncode=0, stdout=value)
        return outcome


class FailingStorageProbeExecutor(FakeProcessExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.storage_attempts = 0

    def run(self, command, **kwargs):
        key = tuple(command)
        if "exec" in key and "fs-app" in key and key[-3:-1] == ("python", "-c"):
            self.commands.append(key)
            self.invocations.append((key, kwargs))
            self.storage_attempts += 1
            return ProcessOutcome(command=key, returncode=137)
        return super().run(command, **kwargs)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "infra/docker").mkdir(parents=True)
    (tmp_path / "infra/docker/docker-compose.yaml").write_text("services: {}\n")
    (tmp_path / "infra/docker/docker-compose.prod.yaml").write_text("services: {}\n")
    overlays = tmp_path / "infra/docker/scalable"
    overlays.mkdir()
    source_overlays = Path(__file__).parents[3] / "infra/docker/scalable"
    for name in (
        "cli-host-directories.yaml",
        "cli-host-directories-artifacts.yaml",
        "data-compact_volume.yaml",
    ):
        shutil.copy2(source_overlays / name, overlays / name)
    # A real checkout always carries it, and AC17-R92 builds the Compact image from it.
    (tmp_path / "infra/docker/Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (tmp_path / "db/schema").mkdir(parents=True)
    (tmp_path / "db/schema/001.sql").write_text("SELECT 1;\n")
    (tmp_path / "core/infrastructure/cli_runtime").mkdir(parents=True)
    helper = tmp_path / "core/infrastructure/cli_runtime/runtime_identity.sh"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    (tmp_path / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    return tmp_path


def _gateway(
    tmp_path: Path,
    process: FakeProcessExecutor,
    **kwargs,
) -> DockerInstallerGateway:
    root = _repo(tmp_path)
    environment = kwargs.pop("environment", {})
    port_available = kwargs.pop("port_available", lambda _host, _port: True)
    sleep = kwargs.pop("sleep", lambda _seconds: None)
    return DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(root / ".flow-steward"),
        process=process,
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=port_available,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment=environment,
        sleep=sleep,
        **kwargs,
    )


@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_installer_readiness_reconciliation_uses_existing_installed_runtime_scripts(
    tmp_path: Path, profile: str
) -> None:
    from core.infrastructure.lifecycle import docker_runtime_support

    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(
        deployment_profile=profile, environment="production", file_storage="local_fs"
    )
    log = gateway.state_store.open_log("derived-readiness")
    gateway.reconcile_readiness_capabilities(
        config,
        check_ids=("security_posture", "workflow_knowledge_sync", "security_posture"),
        log=log,
    )
    log.close()

    runtime = "compact-runtime" if profile == "compact" else "control-plane"
    prefix = tuple(gateway._compose_prefix(config))
    assert process.commands == [
        (*prefix, "exec", "-T", runtime, "python", "-c", script)
        for script in (
            docker_runtime_support.security_posture_reconciliation_script(),
            docker_runtime_support.workflow_knowledge_reconciliation_script(),
        )
    ]
    assert all(invocation[1]["timeout_seconds"] == 300.0 for invocation in process.invocations)


@pytest.mark.parametrize("check_ids", [(), ("database",), ("security_posture", "database")])
def test_installer_readiness_reconciliation_rejects_unknown_ids_before_any_command(
    tmp_path: Path, check_ids: tuple[str, ...]
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    log = gateway.state_store.open_log("refused-derived-readiness")
    config = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        gateway.reconcile_readiness_capabilities(config, check_ids=check_ids, log=log)
    log.close()
    assert raised.value.code == "unsupported_readiness_reconciliation"
    assert process.commands == []


def test_installer_readiness_reconciliation_preserves_scan_failure(tmp_path: Path) -> None:
    from core.infrastructure.lifecycle import docker_runtime_support

    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig()
    command = (
        *gateway._compose_prefix(config),
        "exec",
        "-T",
        "control-plane",
        "python",
        "-c",
        docker_runtime_support.security_posture_reconciliation_script(),
    )
    process.outcomes[command] = ProcessOutcome(
        command=command, returncode=1, stdout="actual scan failed"
    )
    log = gateway.state_store.open_log("failed-derived-readiness")
    with pytest.raises(InstallerFailure) as raised:
        gateway.reconcile_readiness_capabilities(
            config, check_ids=("security_posture", "workflow_knowledge_sync"), log=log
        )
    log.close()
    assert raised.value.code == "readiness_reconciliation_failed"
    assert raised.value.step == "readiness"
    assert raised.value.exit_code == 4
    assert raised.value.cause == "actual scan failed"
    assert process.commands == [command]


def _stage_official_release(
    gateway: DockerInstallerGateway, deployment_profile: str, *, with_postgres: bool = False
) -> ReleaseIdentity:
    roles = list(required_product_roles(deployment_profile))
    if with_postgres:
        roles.append(next(role for role in all_release_roles() if role.role == "postgres"))
    version = "0.9.2" if with_postgres else "0.1.0"
    images = tuple(
        ReleaseImageIdentity(
            role=role.role,
            registry_ref=f"registry.example/official/{role.repository}@sha256:{index:064x}",
            index_digest=f"sha256:{index + 100:064x}",
            manifest_digest=f"sha256:{index:064x}",
            config_digest=f"sha256:{index + 200:064x}",
            revision=_FULL_REVISION,
            platform=_HOST_PLATFORM,
        )
        for index, role in enumerate(roles, start=1)
    )
    release = ReleaseIdentity(
        version=version,
        tag=f"v{version}",
        commit_sha=_FULL_REVISION,
        registry="registry.example/official",
        profile=deployment_profile,
        platform=_HOST_PLATFORM,
        images=images,
    )
    gateway.state_store.stage_pending_release(release)
    return release


def test_initial_install_rollback_removes_scoped_runtime_and_preserves_user_files(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(
        tmp_path,
        process,
        environment={"FS_COMPOSE_PROJECT_NAME": "fs-inst-rollback"},
    )
    config = InstallConfig()
    state_root = tmp_path / ".flow-steward"
    runtime = state_root / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "identity.json").write_text("{}\n", encoding="utf-8")
    for path in (
        state_root / "stack.toml",
        state_root / "compose.env",
        state_root / "initial-preservation-manifest.json",
    ):
        path.write_text("managed\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("FS_COMPOSE_PROJECT_NAME=fs-inst-rollback\n", encoding="utf-8")
    certificate = tmp_path / ".ssl/cert.pem"
    certificate.parent.mkdir()
    certificate.write_text("certificate\n", encoding="utf-8")
    credentials = tmp_path / "external-s3-credentials.json"
    credentials.write_text("{}\n", encoding="utf-8")
    volume_inventory = (
        "docker",
        "volume",
        "ls",
        "--filter",
        "label=com.docker.compose.project=fs-inst-rollback",
        "--format",
        "{{.Name}}",
    )
    container_inventory = (
        "docker",
        "ps",
        "-a",
        "--filter",
        "label=com.docker.compose.project=fs-inst-rollback",
        "--format",
        "{{.Names}}",
    )
    process.outcomes[volume_inventory] = ProcessOutcome(
        command=volume_inventory, returncode=0, stdout="fs-inst-rollback-pg-fs\n"
    )
    process.outcomes[container_inventory] = ProcessOutcome(
        command=container_inventory, returncode=0, stdout="fs-inst-rollback-fs-app-1\n"
    )
    journal = InstallJournal(
        operation_id="interrupted",
        project_identity="fs-inst-rollback",
        config=config,
        created_paths=(
            ".flow-steward/stack.toml",
            ".flow-steward/compose.env",
            ".flow-steward/runtime",
            ".flow-steward/initial-preservation-manifest.json",
            ".env",
            ".ssl/cert.pem",
            "external-s3-credentials.json",
        ),
    )
    log = gateway.state_store.open_log("rollback")

    gateway.rollback_initial_install(journal, log=log)
    log.close()

    assert process.commands[-1] == (
        *gateway._compose_prefix(config),
        "down",
        "--volumes",
        "--remove-orphans",
    )
    assert not (state_root / "stack.toml").exists()
    assert not (state_root / "compose.env").exists()
    assert not runtime.exists()
    assert not (state_root / "initial-preservation-manifest.json").exists()
    assert env_path.read_text(encoding="utf-8").startswith("FS_COMPOSE_PROJECT_NAME=")
    assert certificate.read_text(encoding="utf-8") == "certificate\n"
    assert credentials.read_text(encoding="utf-8") == "{}\n"


def test_early_install_rollback_without_env_skips_compose_and_removes_planned_state(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(
        tmp_path,
        process,
        environment={"FS_COMPOSE_PROJECT_NAME": "fs-inst-early"},
    )
    state_file = tmp_path / ".flow-steward/stack.toml"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("partial\n", encoding="utf-8")
    journal = InstallJournal(
        operation_id="early",
        project_identity="fs-inst-early",
        config=InstallConfig(),
        created_paths=(".flow-steward/stack.toml",),
    )
    log = gateway.state_store.open_log("early-rollback")

    gateway.rollback_initial_install(journal, log=log)
    log.close()

    assert not any("down" in command for command in process.commands)
    assert all(
        invocation["timeout_seconds"] == 30.0 for _command, invocation in process.invocations
    )
    assert not state_file.exists()


def test_install_rollback_runs_compose_down_for_materialized_network_only_state(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(
        tmp_path,
        process,
        environment={"FS_COMPOSE_PROJECT_NAME": "fs-inst-network"},
    )
    config = InstallConfig()
    (tmp_path / ".env").write_text("FS_COMPOSE_PROJECT_NAME=fs-inst-network\n")
    journal = InstallJournal(
        operation_id="network-only",
        project_identity="fs-inst-network",
        config=config,
    )
    log = gateway.state_store.open_log("network-only-rollback")

    gateway.rollback_initial_install(journal, log=log)
    log.close()

    assert process.commands[-1] == (
        *gateway._compose_prefix(config),
        "down",
        "--volumes",
        "--remove-orphans",
    )


def test_install_rollback_has_bounded_compose_deadline(tmp_path: Path) -> None:
    class TimeoutProcess(FakeProcessExecutor):
        def run(self, command, **kwargs):
            outcome = super().run(command, **kwargs)
            if tuple(command)[-3:] == ("down", "--volumes", "--remove-orphans"):
                assert kwargs["timeout_seconds"] == 30.0
                raise TimeoutError("Process exceeded 30 seconds")
            return outcome

    process = TimeoutProcess()
    gateway = _gateway(
        tmp_path,
        process,
        environment={"FS_COMPOSE_PROJECT_NAME": "fs-inst-timeout"},
    )
    (tmp_path / ".env").write_text("FS_COMPOSE_PROJECT_NAME=fs-inst-timeout\n")
    container_inventory = (
        "docker",
        "ps",
        "-a",
        "--filter",
        "label=com.docker.compose.project=fs-inst-timeout",
        "--format",
        "{{.Names}}",
    )
    process.outcomes[container_inventory] = ProcessOutcome(
        command=container_inventory, returncode=0, stdout="fs-inst-timeout-fs-app-1\n"
    )
    journal = InstallJournal(
        operation_id="timeout",
        project_identity="fs-inst-timeout",
        config=InstallConfig(),
    )
    log = gateway.state_store.open_log("timeout-rollback")

    with pytest.raises(InstallerFailure) as raised:
        gateway.rollback_initial_install(journal, log=log)

    log.close()
    assert raised.value.code == "installation_rollback_failed"
    assert "30 seconds" in raised.value.cause


def test_install_rollback_refuses_symlinked_state_ancestor(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(
        tmp_path,
        process,
        environment={"FS_COMPOSE_PROJECT_NAME": "fs-inst-symlink"},
    )
    log = gateway.state_store.open_log("symlink-rollback")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "identity.json"
    outside_file.write_text("preserve\n", encoding="utf-8")
    (tmp_path / ".flow-steward/runtime").symlink_to(outside, target_is_directory=True)
    journal = InstallJournal(
        operation_id="symlink",
        project_identity="fs-inst-symlink",
        config=InstallConfig(),
        created_paths=(".flow-steward/runtime/identity.json",),
    )

    with pytest.raises(InstallerFailure) as raised:
        gateway.rollback_initial_install(journal, log=log)

    log.close()
    assert raised.value.code == "installation_rollback_failed"
    assert "symbolic link" in raised.value.cause
    assert outside_file.read_text(encoding="utf-8") == "preserve\n"


def test_scalable_first_install_resolves_image_tags_compose_just_built(tmp_path: Path) -> None:
    """AC17-R95: a clean machine had no branch of its own.

    prepare_core runs `docker compose build` and then records provenance. The
    provenance step had a branch for "reuse what a previous install recorded" and a
    branch for "refuse", and none for "this host has never built them" -- so a fresh
    checkout stopped at "Production image tags are missing from the installer
    environment", moments after Compose had built exactly those images under the
    tags the Compose file declares.
    """
    config = InstallConfig(environment="production")
    built = {
        "fs-app": "flow-steward-app:0.2.0",
        "fs-worker": "flow-steward-worker:0.2.0",
        "control-plane": "flow-steward-control:0.2.0",
        "fs-mcp": "flow-steward-mcp:0.2.0",
    }
    compose_config = json.dumps(
        {"services": {name: {"image": image} for name, image in built.items()}}
    )

    class ComposeAwareExecutor(FakeProcessExecutor):
        def run(self, command, **kwargs):
            key = tuple(command)
            if key[:2] == ("docker", "compose") and key[-3:] == ("config", "--format", "json"):
                self.commands.append(key)
                return ProcessOutcome(command=key, returncode=0, stdout=compose_config)
            if key[:3] == ("docker", "image", "inspect") and key[3] in set(built.values()):
                self.commands.append(key)
                digest = f"sha256:{abs(hash(key[3])) % 10**10:064d}"
                return ProcessOutcome(
                    command=key,
                    returncode=0,
                    stdout=json.dumps(
                        [
                            {
                                "Id": digest,
                                "Config": {
                                    "Labels": {"org.opencontainers.image.revision": _FULL_REVISION}
                                },
                            }
                        ]
                    ),
                )
            return super().run(command, **kwargs)

    process = ComposeAwareExecutor()
    gateway = _gateway(tmp_path, process)
    env_path = tmp_path / ".env.prod"
    env_path.write_text("OPERATOR_VALUE=preserved\n", encoding="utf-8")
    log = gateway.state_store.open_log("scalable-first-install")

    gateway._prepare_production_image_provenance(config, log=log)
    log.close()

    values = gateway_module._read_env(env_path)
    assert values["FS_BUILD_SHA"] == _FULL_REVISION
    assert values["FS_APP_IMAGE"].startswith("sha256:")
    assert values["FS_WORKER_IMAGE"].startswith("sha256:")
    assert values["OPERATOR_VALUE"] == "preserved"


def test_scalable_first_install_names_what_compose_could_not_resolve(tmp_path: Path) -> None:
    """The refusal survives, but it now says resolution was attempted and failed."""
    config = InstallConfig(environment="production")
    compose_command_tail = ("config", "--format", "json")

    class EmptyComposeExecutor(FakeProcessExecutor):
        def run(self, command, **kwargs):
            key = tuple(command)
            if key[:2] == ("docker", "compose") and key[-3:] == compose_command_tail:
                self.commands.append(key)
                return ProcessOutcome(command=key, returncode=0, stdout='{"services": {}}')
            return super().run(command, **kwargs)

    process = EmptyComposeExecutor()
    gateway = _gateway(tmp_path, process)
    (tmp_path / ".env.prod").write_text("OPERATOR_VALUE=preserved\n", encoding="utf-8")
    log = gateway.state_store.open_log("scalable-unresolvable")

    with pytest.raises(InstallerFailure) as raised:
        gateway._prepare_production_image_provenance(config, log=log)

    log.close()
    assert "could not be resolved from the Compose definition" in raised.value.cause
    assert "FS_APP_IMAGE" in raised.value.cause


def _production_provenance_gateway(
    tmp_path: Path,
    process: FakeProcessExecutor,
    *,
    environment: str = "production",
    git_revision: str = _FULL_REVISION,
    mcp_image_id: str = _MCP_IMAGE_ID,
    mcp_revision: str = _FULL_REVISION,
    extra_env: str = "",
) -> tuple[DockerInstallerGateway, InstallConfig, Path, dict[str, tuple[str, ...]]]:
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment=environment)
    env_path = tmp_path / (".env.prod" if environment == "production" else ".env")
    image_values = (
        {
            "FS_APP_IMAGE": _APP_IMAGE_ID,
            "FS_WORKER_IMAGE": _WORKER_IMAGE_ID,
            "FS_CONTROL_IMAGE": _CONTROL_IMAGE_ID,
            "MCP_IMAGE": mcp_image_id,
            "FS_CLI_IMAGE": _CLI_IMAGE_ID,
        }
        if environment == "production"
        else {
            "FS_APP_IMAGE": _APP_IMAGE,
            "FS_WORKER_IMAGE": _WORKER_IMAGE,
            "FS_CONTROL_IMAGE": _CONTROL_IMAGE,
            "MCP_IMAGE": _MCP_IMAGE,
        }
    )
    env_path.write_text(
        "# operator-owned line\n"
        + "".join(f"{key}={value}\n" for key, value in image_values.items())
        + extra_env
        + "OPERATOR_VALUE=preserved\n",
        encoding="utf-8",
    )
    inspected_images = (
        (_APP_IMAGE_ID, _WORKER_IMAGE_ID, _CONTROL_IMAGE_ID, mcp_image_id)
        if environment == "production"
        else (_APP_IMAGE, _WORKER_IMAGE, _CONTROL_IMAGE, _MCP_IMAGE)
    )
    commands = {
        "git": ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD^{commit}"),
        "app_inspect": ("docker", "image", "inspect", inspected_images[0]),
        "worker_inspect": ("docker", "image", "inspect", inspected_images[1]),
        "control_inspect": ("docker", "image", "inspect", inspected_images[2]),
        "mcp_inspect": ("docker", "image", "inspect", inspected_images[3]),
        "cli_inspect": ("docker", "image", "inspect", _CLI_IMAGE_ID),
        "app_reinspect": ("docker", "image", "inspect", _APP_IMAGE_ID),
        "worker_reinspect": ("docker", "image", "inspect", _WORKER_IMAGE_ID),
        "control_reinspect": ("docker", "image", "inspect", _CONTROL_IMAGE_ID),
        "mcp_reinspect": ("docker", "image", "inspect", _MCP_IMAGE_ID),
        "compose": (*gateway._compose_prefix(config), "build"),
    }
    process.outcomes.update(
        {
            commands["git"]: ProcessOutcome(
                command=commands["git"], returncode=0, stdout=git_revision + "\n"
            ),
            commands["app_inspect"]: ProcessOutcome(
                command=commands["app_inspect"],
                returncode=0,
                stdout=_image_inspection(
                    _APP_IMAGE_ID,
                    service="fs-app",
                    command=("/app/core/entrypoints/cli/scripts/start_server.sh",),
                ),
            ),
            commands["worker_inspect"]: ProcessOutcome(
                command=commands["worker_inspect"],
                returncode=0,
                stdout=_image_inspection(
                    _WORKER_IMAGE_ID,
                    service="fs-worker",
                    command=("python", "-m", "core.entrypoints.cli.worker_process"),
                ),
            ),
            commands["control_inspect"]: ProcessOutcome(
                command=commands["control_inspect"],
                returncode=0,
                stdout=_image_inspection(
                    _CONTROL_IMAGE_ID,
                    service="control-plane",
                    command=("python", "-m", "core.entrypoints.cli.control_process"),
                ),
            ),
            commands["mcp_inspect"]: ProcessOutcome(
                command=commands["mcp_inspect"],
                returncode=0,
                stdout=_image_inspection(
                    mcp_image_id,
                    mcp_revision,
                    service="fs-mcp",
                    command=("python", "-m", "core.entrypoints.mcp.server"),
                ),
            ),
            commands["cli_inspect"]: ProcessOutcome(
                command=commands["cli_inspect"],
                returncode=0,
                stdout=_image_inspection(
                    _CLI_IMAGE_ID,
                    entrypoint=("python", "-m", "core.entrypoints.cli.lifecycle_click_app"),
                ),
            ),
            commands["app_reinspect"]: ProcessOutcome(
                command=commands["app_reinspect"],
                returncode=0,
                stdout=_image_inspection(_APP_IMAGE_ID),
            ),
            commands["worker_reinspect"]: ProcessOutcome(
                command=commands["worker_reinspect"],
                returncode=0,
                stdout=_image_inspection(_WORKER_IMAGE_ID),
            ),
            commands["control_reinspect"]: ProcessOutcome(
                command=commands["control_reinspect"],
                returncode=0,
                stdout=_image_inspection(_CONTROL_IMAGE_ID),
            ),
            commands["mcp_reinspect"]: ProcessOutcome(
                command=commands["mcp_reinspect"],
                returncode=0,
                stdout=_image_inspection(_MCP_IMAGE_ID, mcp_revision),
            ),
        }
    )
    if environment == "production":
        process.outcomes.update(
            {
                commands["app_inspect"]: ProcessOutcome(
                    command=commands["app_inspect"],
                    returncode=0,
                    stdout=_image_inspection(
                        _APP_IMAGE_ID,
                        service="fs-app",
                        command=("/app/core/entrypoints/cli/scripts/start_server.sh",),
                    ),
                ),
                commands["worker_inspect"]: ProcessOutcome(
                    command=commands["worker_inspect"],
                    returncode=0,
                    stdout=_image_inspection(
                        _WORKER_IMAGE_ID,
                        service="fs-worker",
                        command=("python", "-m", "core.entrypoints.cli.worker_process"),
                    ),
                ),
                commands["control_inspect"]: ProcessOutcome(
                    command=commands["control_inspect"],
                    returncode=0,
                    stdout=_image_inspection(
                        _CONTROL_IMAGE_ID,
                        service="control-plane",
                        command=("python", "-m", "core.entrypoints.cli.control_process"),
                    ),
                ),
                commands["mcp_inspect"]: ProcessOutcome(
                    command=commands["mcp_inspect"],
                    returncode=0,
                    stdout=_image_inspection(
                        mcp_image_id,
                        mcp_revision,
                        service="fs-mcp",
                        command=("python", "-m", "core.entrypoints.mcp.server"),
                    ),
                ),
            }
        )
    return gateway, config, env_path, commands


@pytest.mark.parametrize(
    ("command", "code"),
    [
        (("docker", "info", "--format", "{{json .}}"), "docker_daemon_unavailable"),
        (("docker", "compose", "version", "--short"), "docker_compose_unavailable"),
    ],
)
def test_preflight_maps_docker_failures(
    tmp_path: Path, command: tuple[str, ...], code: str
) -> None:
    process = FakeProcessExecutor(
        {command: ProcessOutcome(command=command, returncode=1, stderr="permission denied")}
    )
    gateway = _gateway(tmp_path, process)
    log = gateway.state_store.open_log("preflight")

    _raises_input_611_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(_raises_input_611_1, log=log)

    log.close()
    assert raised.value.code == code
    assert raised.value.remediation
    assert "permission denied" in raised.value.cause
    if code == "docker_compose_unavailable":
        assert "https://docs.docker.com/compose/install/linux/" in raised.value.remediation
        assert "https://docs.docker.com/desktop/" in raised.value.remediation


def test_preflight_rejects_remote_docker_before_mutation(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        environment={"DOCKER_HOST": "tcp://builder.example:2376"},
    )
    log = gateway.state_store.open_log("preflight")

    _raises_input_628_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(_raises_input_628_1, log=log)

    log.close()
    assert raised.value.code == "remote_docker_unsupported"
    assert raised.value.exit_code == 7


def test_preflight_reports_architecture_disk_and_port_blockers(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    gateway = DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(root / ".flow-steward"),
        process=FakeProcessExecutor(),
        disk_free_bytes=lambda _path: 1,
        port_available=lambda _host, _port: False,
        architecture=lambda: "riscv64",
        environment={},
    )
    log = gateway.state_store.open_log("preflight")

    _raises_input_669_1 = InstallConfig(public_port=8099)
    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(_raises_input_669_1, log=log)

    log.close()
    assert raised.value.code == "unsupported_architecture"


def test_local_tls_contains_sans_and_precedes_start(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    log = gateway.state_store.open_log("tls")
    config = InstallConfig(public_host="127.0.0.1")

    gateway.ensure_tls(config, log=log)
    gateway.start_core(config, log=log)

    log.close()
    openssl = next(command for command in process.commands if command[:2] == ("openssl", "req"))
    compose = next(command for command in process.commands if "compose" in command)
    assert process.commands.index(openssl) < process.commands.index(compose)
    assert "subjectAltName=IP:127.0.0.1,DNS:localhost" in openssl
    assert oct((tmp_path / config.private_key_path).stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize(
    ("ingress_mode", "tls_mode"),
    (
        ("bundled_traefik", "acme"),
        ("external_traefik", "external_resolver"),
        ("external_traefik", "external_certificate"),
        ("external_proxy", "reverse_proxy"),
        ("platform", "platform_managed"),
    ),
)
def test_externally_owned_tls_does_not_generate_local_certificates(
    tmp_path: Path, ingress_mode: str, tls_mode: str
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig.model_validate(
        {
            "deployment_profile": "compact",
            "environment": "production",
            "file_storage": "local_fs",
            "ingress_mode": ingress_mode,
            "tls_mode": tls_mode,
            "traefik_network": "shared-ingress",
            "traefik_cert_resolver": "staging",
            "acme_contact": "ops@example.test",
            "acme_directory_url": "https://ca.example.test/directory",
        }
    )
    log = gateway.state_store.open_log("external-tls")

    gateway.ensure_tls(config, log=log)

    log.close()
    assert not any(command[:2] == ("openssl", "req") for command in process.commands)
    assert gateway.postcondition_holds("tls", config) is True


def test_start_runtime_normalizes_complete_development_preset(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    log = gateway.state_store.open_log("development-tools")

    gateway.start_runtime(
        InstallConfig(development_tools=("test_runner",)),
        log=log,
    )
    log.close()

    command = process.commands[-1]
    assert "fs-frontend" in command
    assert "fs-test-runner" in command
    assert "fs-audit" in command
    assert "mailpit" not in command
    assert "sonarqube" not in command
    assert "sonar-scanner" not in command
    assert {command[index + 1] for index, item in enumerate(command) if item == "--profile"} >= {
        "cli",
        "development",
        "test",
        "audit",
        "quality",
        "storage-seaweedfs",
    }


def _without_name_listings(commands):
    """What a start issues besides F2's listing of the project's containers, which comes
    before each `up` to finish a recreate a kill left (the listing and what follows it
    are tested in test_start_finishes_interrupted_recreates.py)."""
    return [command for command in commands if tuple(command[:3]) != ("docker", "ps", "--all")]


def test_exact_prebuilt_start_never_builds_or_pulls_and_keeps_one_project(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production")
    _write_env_values(
        tmp_path / ".env.prod",
        {"FS_COMPOSE_PROJECT_NAME": "flow-steward", **_exact_prebuilt_values()},
    )
    log = gateway.state_store.open_log("exact-start")

    gateway.start_core(config, log=log)
    gateway.start_runtime(config, log=log)

    log.close()
    commands = _without_name_listings(process.commands)
    assert len(process.commands) - len(commands) == 2  # one listing before each start
    assert len(commands) == 2
    for command in commands:
        assert command[command.index("-p") + 1] == "flow-steward"
        assert "--no-build" in command
        assert command[command.index("--pull") + 1] == "never"
        assert "sonar-scanner" not in command


def test_exact_prebuilt_development_starts_frontend_in_same_project_without_sonar(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="development")
    _write_env_values(
        tmp_path / ".env",
        {
            "FS_COMPOSE_PROJECT_NAME": "flow-steward",
            **_exact_prebuilt_values(include_frontend=True),
        },
    )
    log = gateway.state_store.open_log("exact-development-start")

    gateway.start_runtime(config, log=log)

    log.close()
    command = process.commands[-1]
    assert command[command.index("-p") + 1] == "flow-steward"
    assert "fs-frontend" in command
    assert "--no-build" in command
    assert command[command.index("--pull") + 1] == "never"
    assert "sonar-scanner" not in command


def test_exact_prebuilt_development_prepare_only_inspects_existing_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="development")
    _write_env_values(tmp_path / ".env", _exact_prebuilt_values(include_frontend=True))
    monkeypatch.setattr(gateway, "_prepare_exact_prebuilt_provenance", lambda *_a, **_kw: None)
    log = gateway.state_store.open_log("exact-development-prepare")

    gateway.prepare_core(config, log=log)

    log.close()
    probes = _presence_probes(*_SCALABLE_PROBED)
    assert process.commands[: len(probes)] == probes
    assert process.commands[len(probes) :]
    assert all(
        command[-3:] == ("config", "--format", "json")
        or command[:3] == ("docker", "image", "inspect")
        for command in process.commands[len(probes) :]
    )


@pytest.mark.parametrize(
    "values",
    (
        {},
        {
            "FS_APP_IMAGE": "example/app:latest",
            "FS_WORKER_IMAGE": "example/worker:latest",
            "FS_CONTROL_IMAGE": "example/control:latest",
            "FS_CLI_IMAGE": "example/cli:latest",
            "MCP_IMAGE": "example/mcp:latest",
        },
    ),
)
def test_production_prepare_without_official_identity_never_builds_product_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str],
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment=values)
    config = InstallConfig(environment="production", deployment_profile="scalable")
    monkeypatch.setattr(
        gateway,
        "_build_scalable_candidate_images",
        lambda *_args, **_kwargs: pytest.fail("production must not build product images"),
    )
    monkeypatch.setattr(
        gateway,
        "_build_compact_runtime_image",
        lambda *_args, **_kwargs: pytest.fail("production must not build product images"),
    )
    log = gateway.state_store.open_log("official-release-required")

    with pytest.raises(InstallerFailure) as raised:
        gateway.prepare_core(config, log=log)

    log.close()
    assert raised.value.code == "official_release_missing"
    assert not any("build" in command for command in process.commands)


def test_start_core_does_not_start_development_tools_in_production(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    log = gateway.state_store.open_log("production-core")

    gateway.start_runtime(
        InstallConfig(
            environment="production",
            development_tools=("test_runner", "audit_tools", "sonarqube"),
        ),
        log=log,
    )
    log.close()

    command = process.commands[-1]
    assert "fs-test-runner" not in command
    assert "fs-audit" not in command
    assert "sonarqube" not in command


def test_compose_bind_sources_use_the_host_state_dir_inside_the_cli_container(
    tmp_path: Path,
) -> None:
    """The release handoff sees the state directory at a container mount; Compose
    interpolates bind sources from it and the Docker host must find them."""
    process = FakeProcessExecutor()
    root = _repo(tmp_path)
    host_state = root / ".flow-steward"
    host_state.mkdir(exist_ok=True)
    mounted_state = tmp_path / "run-flow-steward-candidate-state"
    mounted_state.symlink_to(host_state, target_is_directory=True)
    (root / ".env.prod").write_text(f"FS_CANDIDATE_STATE_DIR={host_state}\n", encoding="utf-8")
    gateway = DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(mounted_state),
        process=process,
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment={},
        sleep=lambda _seconds: None,
    )
    log = gateway.state_store.open_log("handoff-runtime")

    gateway.start_runtime(InstallConfig(environment="production"), log=log)
    log.close()

    _command, kwargs = process.invocations[-1]
    environment = kwargs["environment"]
    assert isinstance(environment, dict)
    assert environment["FS_INSTALLER_STATE_DIR"] == str(host_state)


def test_compose_environment_is_untouched_when_the_state_dir_is_the_host_path(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    state_root = gateway.state_store.root
    (gateway.repo_root / ".env.prod").write_text(
        f"FS_CANDIDATE_STATE_DIR={state_root}\n", encoding="utf-8"
    )
    log = gateway.state_store.open_log("host-runtime")

    gateway.start_runtime(InstallConfig(environment="production"), log=log)
    log.close()

    _command, kwargs = process.invocations[-1]
    assert kwargs.get("environment") is None


def test_production_prepare_persists_exact_image_provenance_before_compose(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway, config, env_path, commands = _production_provenance_gateway(tmp_path, process)
    log = gateway.state_store.open_log("production-provenance")

    gateway.prepare_core(config, log=log)

    log.close()
    assert process.commands == [
        commands["git"],
        commands["app_inspect"],
        commands["worker_inspect"],
        commands["control_inspect"],
        commands["mcp_inspect"],
        commands["cli_inspect"],
        (*gateway._compose_prefix(config), "config", "--format", "json"),
        *[
            ("docker", "image", "inspect", f"test-{name}:present")
            for name in ("traefik", "rabbitmq-fs", "pg-fs", "seaweedfs", "seaweedfs-config")
        ],
    ]
    assert gateway.state_store.read_env_values(env_path) == {
        "FS_BUILD_SHA": _FULL_REVISION,
        "FS_APP_IMAGE": _APP_IMAGE_ID,
        "FS_APP_IMAGE_DIGEST": _APP_IMAGE_ID,
        "FS_APP_IMAGE_REVISION": _FULL_REVISION,
        "FS_WORKER_IMAGE": _WORKER_IMAGE_ID,
        "FS_WORKER_IMAGE_DIGEST": _WORKER_IMAGE_ID,
        "FS_WORKER_IMAGE_REVISION": _FULL_REVISION,
        "FS_CONTROL_IMAGE": _CONTROL_IMAGE_ID,
        "FS_CONTROL_IMAGE_DIGEST": _CONTROL_IMAGE_ID,
        "FS_CONTROL_IMAGE_REVISION": _FULL_REVISION,
        "MCP_IMAGE": _MCP_IMAGE_ID,
        "MCP_IMAGE_DIGEST": _MCP_IMAGE_ID,
        "MCP_IMAGE_REVISION": _FULL_REVISION,
        "FS_CLI_IMAGE": _CLI_IMAGE_ID,
        "FS_CLI_IMAGE_REVISION": _FULL_REVISION,
        "OPERATOR_VALUE": "preserved",
    }
    assert env_path.read_text(encoding="utf-8").startswith("# operator-owned line\n")
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_exact_prebuilt_prepare_rejects_a_label_that_disagrees_with_the_baked_revision(
    tmp_path: Path,
) -> None:
    """AC17-R91: a `--label` override hides a missing `--build-arg BUILD_SHA`.

    The label then satisfies the installer while FS_EMBEDDED_IMAGE_REVISION stays
    `unknown`, so the runtime rejects the image, exits EX_CONFIG and restarts --
    after configuration, secrets, the certificate and the schema are on disk.
    """
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production")
    _write_env_values(tmp_path / ".env.prod", _exact_prebuilt_values())
    _register_exact_prebuilt_inspections(process)
    process.outcomes[("docker", "image", "inspect", _APP_IMAGE_ID)] = ProcessOutcome(
        command=("docker", "image", "inspect", _APP_IMAGE_ID),
        returncode=0,
        stdout=_image_inspection(
            _APP_IMAGE_ID,
            _APP_REVISION,
            service="fs-app",
            command=("/app/core/entrypoints/cli/scripts/start_server.sh",),
            embedded_revision="unknown",
        ),
    )
    git_command = ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD^{commit}")
    process.outcomes[git_command] = ProcessOutcome(
        command=git_command, returncode=0, stdout=_FULL_REVISION + "\n"
    )
    log = gateway.state_store.open_log("label-vs-embedded")

    with pytest.raises(InstallerFailure) as raised:
        gateway.prepare_core(config, log=log)

    log.close()
    assert _APP_REVISION in raised.value.cause
    assert "unknown" in raised.value.cause
    assert "BUILD_SHA" in raised.value.cause


def test_exact_prebuilt_prepare_accepts_a_label_matching_the_baked_revision(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production")
    _write_env_values(tmp_path / ".env.prod", _exact_prebuilt_values())
    _register_exact_prebuilt_inspections(process)
    process.outcomes[("docker", "image", "inspect", _APP_IMAGE_ID)] = ProcessOutcome(
        command=("docker", "image", "inspect", _APP_IMAGE_ID),
        returncode=0,
        stdout=_image_inspection(
            _APP_IMAGE_ID,
            _APP_REVISION,
            service="fs-app",
            command=("/app/core/entrypoints/cli/scripts/start_server.sh",),
            embedded_revision=_APP_REVISION,
        ),
    )
    git_command = ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD^{commit}")
    process.outcomes[git_command] = ProcessOutcome(
        command=git_command, returncode=0, stdout=_FULL_REVISION + "\n"
    )
    log = gateway.state_store.open_log("label-matches-embedded")

    gateway.prepare_core(config, log=log)
    log.close()


def test_exact_prebuilt_prepare_uses_only_local_inventory_and_persists_each_revision(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production")
    env_path = tmp_path / ".env.prod"
    _write_env_values(
        env_path,
        {
            **_exact_prebuilt_values(include_frontend=True),
            "OPERATOR_VALUE": "preserved",
        },
    )
    inspections = _register_exact_prebuilt_inspections(process, include_frontend=True)
    git_command = ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD^{commit}")
    process.outcomes[git_command] = ProcessOutcome(
        command=git_command,
        returncode=0,
        stdout=_FULL_REVISION + "\n",
    )
    log = gateway.state_store.open_log("exact-prebuilt-provenance")

    gateway.prepare_core(config, log=log)

    log.close()
    assert process.commands == [
        git_command,
        *[("docker", "image", "inspect", image) for image in inspections],
        (*gateway._compose_prefix(config), "config", "--format", "json"),
        *[
            ("docker", "image", "inspect", f"test-{name}:present")
            for name in ("traefik", "rabbitmq-fs", "pg-fs", "seaweedfs", "seaweedfs-config")
        ],
    ]
    assert not any("pull" in command or "build" in command for command in process.commands)
    values = gateway.state_store.read_env_values(env_path)
    assert values == {
        "FS_BUILD_SHA": _FULL_REVISION,
        "FS_APP_IMAGE": _APP_IMAGE_ID,
        "FS_APP_IMAGE_DIGEST": _APP_IMAGE_ID,
        "FS_APP_IMAGE_REVISION": _APP_REVISION,
        "FS_WORKER_IMAGE": _WORKER_IMAGE_ID,
        "FS_WORKER_IMAGE_DIGEST": _WORKER_IMAGE_ID,
        "FS_WORKER_IMAGE_REVISION": _WORKER_REVISION,
        "FS_CONTROL_IMAGE": _CONTROL_IMAGE_ID,
        "FS_CONTROL_IMAGE_DIGEST": _CONTROL_IMAGE_ID,
        "FS_CONTROL_IMAGE_REVISION": _CONTROL_REVISION,
        "MCP_IMAGE": _MCP_IMAGE_ID,
        "MCP_IMAGE_DIGEST": _MCP_IMAGE_ID,
        "MCP_IMAGE_REVISION": _MCP_REVISION,
        "FS_CLI_IMAGE": _CLI_IMAGE_ID,
        "FS_CLI_IMAGE_REVISION": _CLI_REVISION,
        "FS_FRONTEND_IMAGE": _FRONTEND_IMAGE_ID,
        "FS_FRONTEND_IMAGE_REVISION": _FRONTEND_REVISION,
        "OPERATOR_VALUE": "preserved",
    }


@pytest.mark.parametrize("separate_images", [False, True])
def test_compact_exact_prebuilt_persists_distinct_oci_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, separate_images: bool
) -> None:
    # Inspect the entrypoint actually shipped by the Compact image, rather than
    # duplicating an installer expectation that can drift from the Dockerfile.
    dockerfile = Path(__file__).resolve().parents[3] / "infra/docker/Dockerfile"
    compact_stage = dockerfile.read_text(encoding="utf-8").split("FROM audit-tooling AS audit")[0]
    entrypoint_line = [
        line for line in compact_stage.splitlines() if line.startswith("ENTRYPOINT ")
    ][-1]
    compact_entrypoint = tuple(json.loads(entrypoint_line.removeprefix("ENTRYPOINT ")))
    image_id = f"sha256:{'7' * 64}"
    manifest_digest = f"sha256:{'8' * 64}"
    config_digest = f"sha256:{'9' * 64}"
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production", deployment_profile="compact")
    env_path = tmp_path / ".env.prod"
    _write_env_values(
        env_path,
        {
            "FS_COMPACT_IMAGE": image_id,
            "FS_COMPACT_IMAGE_INDEX_DIGEST": image_id,
            "FS_COMPACT_IMAGE_MANIFEST_DIGEST": manifest_digest,
            "FS_COMPACT_IMAGE_CONFIG_DIGEST": config_digest,
            "FS_COMPACT_IMAGE_PLATFORM": "linux/arm64",
            "FS_COMPACT_IMAGE_ROLE": "compact-runtime",
        },
    )
    git_command = ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD^{commit}")
    inspect_command = ("docker", "image", "inspect", image_id)
    process.outcomes[git_command] = ProcessOutcome(
        command=git_command, returncode=0, stdout=_FULL_REVISION + "\n"
    )
    process.outcomes[inspect_command] = ProcessOutcome(
        command=inspect_command,
        returncode=0,
        stdout=_image_inspection(
            image_id,
            service="compact-runtime",
            entrypoint=compact_entrypoint,
            descriptor_digest=image_id,
        ),
    )
    if separate_images:
        _write_env_values(
            env_path,
            {
                **gateway_module._read_env(env_path),
                "FS_CLI_IMAGE": _CLI_IMAGE_ID,
                "MCP_IMAGE": _MCP_IMAGE_ID,
            },
        )
        for role, image in (("fs-cli", _CLI_IMAGE_ID), ("fs-mcp", _MCP_IMAGE_ID)):
            command = ("docker", "image", "inspect", image)
            process.outcomes[command] = ProcessOutcome(
                command=command,
                returncode=0,
                stdout=_image_inspection(
                    image,
                    service=role,
                    entrypoint=(
                        "python",
                        "-m",
                        "core.entrypoints.cli.lifecycle_click_app"
                        if role == "fs-cli"
                        else "core.entrypoints.mcp.server",
                    ),
                ),
            )
    monkeypatch.setattr(
        gateway_image_provenance,
        "resolve_local_image_archive",
        lambda **_kwargs: gateway_module.LocalImageContentIdentity(
            index_digest=image_id,
            manifest_digest=manifest_digest,
            config_digest=config_digest,
        ),
    )
    log = gateway.state_store.open_log("compact-exact-provenance")

    gateway.prepare_core(config, log=log)

    log.close()
    values = gateway.state_store.read_env_values(env_path)
    assert values["FS_COMPACT_IMAGE"] == image_id
    assert values["FS_COMPACT_IMAGE_INDEX_DIGEST"] == image_id
    assert values["FS_COMPACT_IMAGE_MANIFEST_DIGEST"] == manifest_digest
    assert values["FS_COMPACT_IMAGE_CONFIG_DIGEST"] == config_digest
    assert values["FS_COMPACT_IMAGE_PLATFORM"] == "linux/arm64"
    if separate_images:
        assert values["FS_CLI_IMAGE_REVISION"] == _FULL_REVISION
        assert values["MCP_IMAGE_REVISION"] == _FULL_REVISION
    # AC17-R102 with AC17-R109: dating the recorded image now comes first. The env
    # file here pins digests and no revision, and that no longer means "undatable" --
    # the image carries org.opencontainers.image.revision, so it is asked before the
    # provenance path runs.
    assert process.commands[:2] == [git_command, inspect_command]
    assert process.commands[2][:4] == ("docker", "image", "save", "--output")


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_official_transition_materializes_registry_refs_despite_local_source_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    source_ids = {
        "FS_COMPACT_IMAGE": _APP_IMAGE_ID,
        "FS_APP_IMAGE": _APP_IMAGE_ID,
        "FS_WORKER_IMAGE": _WORKER_IMAGE_ID,
        "FS_CONTROL_IMAGE": _CONTROL_IMAGE_ID,
        "FS_CLI_IMAGE": _CLI_IMAGE_ID,
        "MCP_IMAGE": _MCP_IMAGE_ID,
    }
    gateway = _gateway(tmp_path, FakeProcessExecutor(), environment=source_ids)
    release = _stage_official_release(gateway, profile)
    _write_env_values(tmp_path / ".env.prod", source_ids)

    def resolve(**values):
        selected = next(image for image in release.images if image.role == values["expected_role"])
        assert values["registry_ref"] == (
            f"{selected.registry_ref.partition('@')[0]}@{selected.index_digest}"
        )
        assert values["platform"] == selected.platform
        assert values["expected_revision"] == selected.revision
        return RegistryImageIdentity(
            registry_ref=selected.registry_ref,
            index_digest=selected.index_digest,
            manifest_digest=selected.manifest_digest,
            config_digest=selected.config_digest,
            revision=selected.revision,
            role=selected.role,
            platform=selected.platform,
            local_image_id=selected.manifest_digest,
        )

    monkeypatch.setattr(registry_image_resolver, "resolve_digest_pinned_image", resolve)
    log = gateway.state_store.open_log("source-to-official-images")
    try:
        gateway._materialize_official_release(
            InstallConfig(environment="production", deployment_profile=profile), log=log
        )
    finally:
        log.close()

    persisted = gateway.state_store.read_env_values(tmp_path / ".env.prod")
    image_keys = {
        "compact-runtime": "FS_COMPACT_IMAGE",
        "fs-app": "FS_APP_IMAGE",
        "fs-worker": "FS_WORKER_IMAGE",
        "control-plane": "FS_CONTROL_IMAGE",
        "fs-cli": "FS_CLI_IMAGE",
        "fs-mcp": "MCP_IMAGE",
    }
    for image in release.images:
        assert persisted[image_keys[image.role]] == image.registry_ref
    assert persisted["FS_BUILD_SHA"] == _FULL_REVISION


def _official_identity_resolver(release: ReleaseIdentity):
    def resolve(**values):
        selected = next(image for image in release.images if image.role == values["expected_role"])
        assert values["registry_ref"] == (
            f"{selected.registry_ref.partition('@')[0]}@{selected.index_digest}"
        )
        return RegistryImageIdentity(
            registry_ref=selected.registry_ref,
            index_digest=selected.index_digest,
            manifest_digest=selected.manifest_digest,
            config_digest=selected.config_digest,
            revision=selected.revision,
            role=selected.role,
            platform=selected.platform,
            local_image_id=selected.manifest_digest,
        )

    return resolve


class _ComposeReadsPostgresImage(FakeProcessExecutor):
    """Compose resolves pg-fs from .env.prod; the locally built default is absent."""

    def __init__(self, env_path: Path) -> None:
        super().__init__()
        self.env_path = env_path
        self.read_env = None

    def run(self, command, **kwargs):
        key = tuple(command)
        if key[-3:] == ("config", "--format", "json"):
            self.commands.append(key)
            values = self.read_env(self.env_path)
            images = {"rabbitmq-fs": "rabbit:test", "pg-fs": values["POSTGRES_IMAGE"]}
            return ProcessOutcome(
                command=key,
                returncode=0,
                stdout=json.dumps({"services": {k: {"image": v} for k, v in images.items()}}),
            )
        if key == ("docker", "image", "inspect", registry_default_value("POSTGRES_IMAGE")):
            self.commands.append(key)
            return ProcessOutcome(command=key, returncode=1)
        return super().run(command, **kwargs)


@pytest.mark.parametrize("profile", ("compact", "scalable"))
@pytest.mark.parametrize("with_postgres", (True, False))
def test_official_install_pulls_the_published_postgres_image_instead_of_building_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, with_postgres: bool
) -> None:
    env_path = tmp_path / ".env.prod"
    process = _ComposeReadsPostgresImage(env_path)
    gateway = _gateway(tmp_path, process)
    process.read_env = gateway.state_store.read_env_values
    release = _stage_official_release(gateway, profile, with_postgres=with_postgres)
    local_default = registry_default_value("POSTGRES_IMAGE")
    _write_env_values(env_path, {"POSTGRES_IMAGE": local_default})
    monkeypatch.setattr(
        registry_image_resolver, "resolve_digest_pinned_image", _official_identity_resolver(release)
    )
    monkeypatch.setattr(gateway, "_prepare_exact_prebuilt_provenance", lambda *_a, **_kw: None)
    monkeypatch.setattr(gateway, "_resolve_compact_registry_image", lambda *_a, **_kw: None)
    config = InstallConfig(
        environment="production",
        deployment_profile=profile,
        file_storage="local_fs",
        ingress_mode="external_proxy",
        tls_mode="reverse_proxy",
    )
    log = gateway.state_store.open_log("official-postgres")
    try:
        gateway.prepare_core(config, log=log)
    finally:
        log.close()

    persisted = gateway.state_store.read_env_values(env_path)
    builds = [
        command for command in process.commands if command[:3] == ("docker", "buildx", "build")
    ]
    text = log.path.read_text(encoding="utf-8")
    if with_postgres:
        postgres = next(image for image in release.images if image.role == "postgres")
        assert persisted["POSTGRES_IMAGE"] == postgres.registry_ref
        assert builds == []
        assert "names no official PostgreSQL image" not in text
    else:
        # A release that predates the published image (0.9.0/0.9.1, or one resolved by
        # an updater that predates it) keeps the installation's own image and builds it
        # locally when absent, exactly as before.
        assert persisted["POSTGRES_IMAGE"] == local_default
        assert [command[command.index("--tag") + 1] for command in builds] == [local_default]
        assert f"Release {release.tag} names no official PostgreSQL image" in text
    assert not any(command[:2] == ("docker", "pull") for command in process.commands)


def test_compact_registry_prepare_preserves_validated_parent_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    release = _stage_official_release(gateway, "compact")
    release_image = release.images[0]
    index_digest = release_image.index_digest
    manifest_digest = release_image.manifest_digest
    config_digest = release_image.config_digest
    index_ref = f"registry.example/official/{required_product_roles('compact')[0].repository}@{index_digest}"
    registry_ref = release_image.registry_ref
    config = InstallConfig(environment="production", deployment_profile="compact")
    env_path = tmp_path / ".env.prod"
    _write_env_values(
        env_path,
        {
            "FS_COMPACT_IMAGE_REGISTRY_REF": index_ref,
            "FS_COMPACT_IMAGE_REVISION": _FULL_REVISION,
            "FS_COMPACT_IMAGE_PLATFORM": "linux/arm64",
        },
    )
    git_command = ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD^{commit}")
    inspect_command = ("docker", "image", "inspect", manifest_digest)
    process.outcomes[git_command] = ProcessOutcome(
        command=git_command, returncode=0, stdout=_FULL_REVISION + "\n"
    )
    process.outcomes[inspect_command] = ProcessOutcome(
        command=inspect_command,
        returncode=0,
        stdout=_image_inspection(
            manifest_digest,
            service="compact-runtime",
            entrypoint=("python", "-m", "core.entrypoints.cli.compact_provider_runtime"),
            descriptor_digest=manifest_digest,
        ),
    )

    def registry_identity(**values):
        selected = next(image for image in release.images if image.role == values["expected_role"])
        if selected.role in {"fs-cli", "fs-mcp"}:
            command = ("docker", "image", "inspect", selected.registry_ref)
            process.outcomes[command] = ProcessOutcome(
                command=command,
                returncode=0,
                stdout=_image_inspection(
                    selected.config_digest,
                    service=selected.role,
                    descriptor_digest=selected.manifest_digest,
                    entrypoint=(
                        "python",
                        "-m",
                        "core.entrypoints.cli.lifecycle_click_app"
                        if selected.role == "fs-cli"
                        else "core.entrypoints.mcp.server",
                    ),
                ),
            )
        return RegistryImageIdentity(
            registry_ref=selected.registry_ref,
            index_digest=selected.index_digest,
            manifest_digest=selected.manifest_digest,
            config_digest=selected.config_digest,
            revision=selected.revision,
            role=selected.role,
            platform=selected.platform,
            local_image_id=selected.manifest_digest,
        )

    monkeypatch.setattr(
        registry_image_resolver,
        "resolve_digest_pinned_image",
        registry_identity,
    )
    monkeypatch.setattr(
        gateway_image_provenance,
        "resolve_local_image_archive",
        lambda **_kwargs: gateway_module.LocalImageContentIdentity(
            index_digest=manifest_digest,
            manifest_digest=manifest_digest,
            config_digest=config_digest,
        ),
    )
    log = gateway.state_store.open_log("compact-registry-provenance")

    gateway.prepare_core(config, log=log)

    log.close()
    # A fresh tree holding only the staged release installs it without any build.
    assert gateway.state_store.load_installed_release() is None
    assert not any("build" in command for command in process.commands)
    assert (
        f"Install mode: official release {release.tag} from {release.registry}; "
        "product images are pulled by digest, not built."
    ) in log.path.read_text(encoding="utf-8")
    values = gateway.state_store.read_env_values(env_path)
    assert values["FS_COMPACT_IMAGE"] == registry_ref
    assert values["FS_COMPACT_IMAGE_REGISTRY_REF"] == registry_ref
    assert values["FS_COMPACT_IMAGE_INDEX_DIGEST"] == index_digest
    assert values["FS_COMPACT_IMAGE_MANIFEST_DIGEST"] == manifest_digest
    assert values["FS_COMPACT_IMAGE_CONFIG_DIGEST"] == config_digest

    monkeypatch.setattr(
        gateway_image_provenance,
        "resolve_local_image_archive",
        lambda **_kwargs: gateway_module.LocalImageContentIdentity(
            index_digest=f"sha256:{'a' * 64}",
            manifest_digest=f"sha256:{'a' * 64}",
            config_digest=config_digest,
        ),
    )
    mismatch_log = gateway.state_store.open_log("compact-registry-mismatch")
    with pytest.raises(
        InstallerFailure,
        match="exact compact image descriptor does not match its own image content",
    ):
        gateway.prepare_core(config, log=mismatch_log)
    mismatch_log.close()

    # On a classic overlay2 store there is no descriptor to disagree, so the registry
    # identity comparison is what must still refuse a substituted archive.
    process.outcomes[inspect_command] = ProcessOutcome(
        command=inspect_command,
        returncode=0,
        stdout=_image_inspection(
            manifest_digest,
            service="compact-runtime",
            entrypoint=("python", "-m", "core.entrypoints.cli.compact_provider_runtime"),
            descriptor_digest=None,
        ),
    )
    monkeypatch.setattr(
        gateway_image_provenance,
        "resolve_local_image_archive",
        lambda **_kwargs: gateway_module.LocalImageContentIdentity(
            index_digest=manifest_digest,
            manifest_digest=manifest_digest,
            config_digest=f"sha256:{'b' * 64}",
        ),
    )
    overlay_log = gateway.state_store.open_log("compact-registry-overlay-mismatch")
    with pytest.raises(
        InstallerFailure,
        match="local Compact image config digest does not match the validated registry identity",
    ):
        gateway.prepare_core(config, log=overlay_log)
    overlay_log.close()


@pytest.mark.parametrize(
    "selection",
    (
        {"FS_APP_IMAGE": _APP_IMAGE_ID},
        {
            "FS_APP_IMAGE": _APP_IMAGE_ID,
            "FS_WORKER_IMAGE": _WORKER_IMAGE_ID,
            "FS_CONTROL_IMAGE": _CONTROL_IMAGE_ID,
            "MCP_IMAGE": _MCP_IMAGE,
            "FS_CLI_IMAGE": _CLI_IMAGE_ID,
        },
    ),
    ids=("partial", "mixed"),
)
def test_preflight_rejects_partial_or_mixed_exact_selection_before_docker_commands(
    tmp_path: Path,
    selection: dict[str, str],
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment=selection)
    log = gateway.state_store.open_log("invalid-exact-selection")

    _raises_input_925_1 = InstallConfig(environment="production")
    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(_raises_input_925_1, log=log)

    log.close()
    assert raised.value.code == "invalid_exact_image_selection"
    assert process.commands == []


def test_preflight_rejects_explicit_tagged_frontend_with_exact_production_images(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(
        tmp_path,
        process,
        environment={
            **_exact_prebuilt_values(),
            "FS_FRONTEND_IMAGE": "flow-steward-frontend:0.2.0",
        },
    )
    log = gateway.state_store.open_log("invalid-tagged-frontend")

    config = InstallConfig(environment="production")

    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(config, log=log)

    log.close()
    assert raised.value.code == "invalid_exact_image_selection"
    assert "frontend image" in str(raised.value).lower()
    assert process.commands == []


@pytest.mark.parametrize(
    ("mcp_revision", "mcp_architecture", "mcp_service", "mcp_command"),
    (
        ("unknown", "arm64", "fs-mcp", ("python", "-m", "core.entrypoints.mcp.server")),
        (_MCP_REVISION, "amd64", "fs-mcp", ("python", "-m", "core.entrypoints.mcp.server")),
        (_MCP_REVISION, "arm64", "fs-worker", ("python", "-m", "core.entrypoints.mcp.server")),
        (_MCP_REVISION, "arm64", "fs-mcp", ("python", "wrong-server.py")),
    ),
    ids=("unknown-revision", "wrong-platform", "wrong-role", "wrong-entrypoint"),
)
def test_exact_prebuilt_prepare_rejects_invalid_identity_without_env_mutation(
    tmp_path: Path,
    mcp_revision: str,
    mcp_architecture: str,
    mcp_service: str,
    mcp_command: tuple[str, ...],
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production")
    env_path = tmp_path / ".env.prod"
    _write_env_values(env_path, _exact_prebuilt_values())
    before = env_path.read_text(encoding="utf-8")
    _register_exact_prebuilt_inspections(
        process,
        mcp_revision=mcp_revision,
        mcp_architecture=mcp_architecture,
        mcp_service=mcp_service,
        mcp_command=mcp_command,
    )
    log = gateway.state_store.open_log("invalid-exact-identity")

    with pytest.raises(InstallerFailure) as raised:
        gateway.prepare_core(config, log=log)

    log.close()
    assert raised.value.code == "core_image_preparation_failed"
    assert env_path.read_text(encoding="utf-8") == before
    assert not any(
        "compose" in command or "pull" in command or "build" in command
        for command in process.commands
    )


def test_production_runtime_up_consumes_durable_exact_image_ids(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway, config, env_path, _commands = _production_provenance_gateway(tmp_path, process)
    log = gateway.state_store.open_log("production-runtime-provenance")

    gateway.prepare_core(config, log=log)
    gateway.start_runtime(config, log=log)

    log.close()
    runtime_command = process.commands[-1]
    runtime_env_path = Path(runtime_command[runtime_command.index("--env-file") + 1])
    assert runtime_env_path == env_path
    runtime_values = gateway.state_store.read_env_values(runtime_env_path)
    assert runtime_values["FS_APP_IMAGE"] == runtime_values["FS_APP_IMAGE_DIGEST"] == _APP_IMAGE_ID
    assert (
        runtime_values["FS_WORKER_IMAGE"]
        == runtime_values["FS_WORKER_IMAGE_DIGEST"]
        == _WORKER_IMAGE_ID
    )
    assert (
        runtime_values["FS_CONTROL_IMAGE"]
        == runtime_values["FS_CONTROL_IMAGE_DIGEST"]
        == _CONTROL_IMAGE_ID
    )
    assert runtime_values["MCP_IMAGE"] == runtime_values["MCP_IMAGE_DIGEST"] == _MCP_IMAGE_ID


@pytest.mark.parametrize(
    ("git_revision", "mcp_image_id", "mcp_revision", "error_code"),
    [
        ("0123456789ab", _MCP_IMAGE_ID, _FULL_REVISION, "core_image_preparation_failed"),
        (_FULL_REVISION, "sha256:truncated", _FULL_REVISION, "official_release_missing"),
    ],
    ids=("short-git-revision", "invalid-mcp-image-id"),
)
def test_production_prepare_never_persists_partial_provenance(
    tmp_path: Path,
    git_revision: str,
    mcp_image_id: str,
    mcp_revision: str,
    error_code: str,
) -> None:
    process = FakeProcessExecutor()
    gateway, config, env_path, commands = _production_provenance_gateway(
        tmp_path,
        process,
        git_revision=git_revision,
        mcp_image_id=mcp_image_id,
        mcp_revision=mcp_revision,
    )
    log = gateway.state_store.open_log("invalid-production-provenance")

    with pytest.raises(InstallerFailure) as raised:
        gateway.prepare_core(config, log=log)

    log.close()
    assert raised.value.code == error_code
    assert gateway.state_store.read_env_values(env_path) == {
        "FS_APP_IMAGE": _APP_IMAGE_ID,
        "FS_WORKER_IMAGE": _WORKER_IMAGE_ID,
        "FS_CONTROL_IMAGE": _CONTROL_IMAGE_ID,
        "MCP_IMAGE": mcp_image_id,
        "FS_CLI_IMAGE": _CLI_IMAGE_ID,
        "OPERATOR_VALUE": "preserved",
    }
    assert env_path.read_text(encoding="utf-8").startswith("# operator-owned line\n")
    assert process.commands.count(commands["compose"]) == 0
    assert not any(command[:2] == ("docker", "build") for command in process.commands)


def test_production_prepare_names_git_as_the_failing_subsystem(tmp_path: Path) -> None:
    """AC17-R90: a git failure used to be reported as an image failure.

    The remediation sent the operator to the image inventory, a different
    subsystem from the one that actually failed.
    """
    process = FakeProcessExecutor()
    gateway, config, _env_path, commands = _production_provenance_gateway(tmp_path, process)
    process.outcomes[commands["git"]] = ProcessOutcome(
        command=commands["git"], returncode=1, stderr="not a Git checkout"
    )
    log = gateway.state_store.open_log("production-git-failure")

    with pytest.raises(InstallerFailure) as raised:
        gateway.prepare_core(config, log=log)

    log.close()
    assert raised.value.code == "source_revision_unavailable"
    assert raised.value.step == "prepare"
    assert "git" in raised.value.remediation.lower()
    assert "image inventory" not in raised.value.remediation.lower()


def test_production_prepare_resume_revalidates_exact_images_without_build(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway, config, _env_path, commands = _production_provenance_gateway(tmp_path, process)
    log = gateway.state_store.open_log("production-provenance-resume")

    gateway.prepare_core(config, log=log)
    gateway.prepare_core(config, log=log)

    log.close()
    assert not any(command[:2] == ("docker", "build") for command in process.commands)
    assert process.commands.count(commands["app_inspect"]) == 2
    assert process.commands.count(commands["worker_inspect"]) == 2
    assert process.commands.count(commands["control_inspect"]) == 2
    assert process.commands.count(commands["mcp_inspect"]) == 2
    assert (
        gateway.state_store.read_env_values(tmp_path / ".env.prod")["FS_APP_IMAGE"] == _APP_IMAGE_ID
    )


def test_development_prepare_materializes_external_on_demand_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="development")
    scanner_pull = (
        *gateway._compose_prefix(config),
        "pull",
        "--ignore-buildable",
        "--policy",
        "missing",
        "sonar-scanner",
    )
    compose_build = (*gateway._compose_prefix(config), "build")
    log = gateway.state_store.open_log("development-prepare")
    monkeypatch.setattr(
        gateway, "_prepare_production_image_provenance", lambda *_args, **_kwargs: None
    )

    gateway.prepare_core(config, log=log)

    log.close()
    assert process.commands == [scanner_pull, compose_build]
    assert all("environment" not in invocation for _, invocation in process.invocations)


def test_development_prepare_resolves_compose_owned_mcp_build_without_image(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway, config, env_path, _commands = _production_provenance_gateway(
        tmp_path, process, environment="development"
    )
    env_path.write_text(env_path.read_text().replace(f"MCP_IMAGE={_MCP_IMAGE}\n", ""))
    compose_config = (*gateway._compose_prefix(config), "config", "--format", "json")
    process.outcomes[compose_config] = ProcessOutcome(
        command=compose_config,
        returncode=0,
        stdout=json.dumps(
            {
                "name": "owned-development",
                "services": {"fs-mcp": {"build": {"context": "."}}},
            }
        ),
    )
    mcp_inspect = ("docker", "image", "inspect", "owned-development-fs-mcp")
    process.outcomes[mcp_inspect] = ProcessOutcome(
        command=mcp_inspect,
        returncode=0,
        stdout=_image_inspection(_MCP_IMAGE_ID),
    )
    log = gateway.state_store.open_log("development-compose-owned-mcp")

    gateway.prepare_core(config, log=log)

    log.close()
    values = gateway.state_store.read_env_values(env_path)
    assert values["MCP_IMAGE"] == values["MCP_IMAGE_DIGEST"] == _MCP_IMAGE_ID
    assert values["MCP_IMAGE_REVISION"] == values["FS_BUILD_SHA"] == _FULL_REVISION
    assert mcp_inspect in process.commands
    assert ("docker", "image", "inspect", _MCP_IMAGE) not in process.commands


def test_production_provenance_refuses_build_only_mcp_without_explicit_image(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway, _config, development_env, _commands = _production_provenance_gateway(
        tmp_path, process, environment="development"
    )
    config = InstallConfig(environment="production")
    env_path = tmp_path / ".env.prod"
    env_path.write_text(development_env.read_text().replace(f"MCP_IMAGE={_MCP_IMAGE}\n", ""))
    before = env_path.read_bytes()
    compose_config = (*gateway._compose_prefix(config), "config", "--format", "json")
    process.outcomes[compose_config] = ProcessOutcome(
        command=compose_config,
        returncode=0,
        stdout=json.dumps(
            {
                "name": "owned-production",
                "services": {"fs-mcp": {"build": {"context": "."}}},
            }
        ),
    )
    log = gateway.state_store.open_log("production-missing-explicit-mcp")

    with pytest.raises(InstallerFailure, match="MCP_IMAGE") as raised:
        gateway._prepare_production_image_provenance(config, log=log)

    log.close()
    assert raised.value.code == "core_image_preparation_failed"
    assert env_path.read_bytes() == before
    assert not any(command[:3] == ("docker", "image", "inspect") for command in process.commands)


def test_development_prepare_persists_truthful_production_provenance_for_later_switch(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway, config, env_path, commands = _production_provenance_gateway(
        tmp_path,
        process,
        environment="development",
    )
    scanner_pull = (
        *gateway._compose_prefix(config),
        "pull",
        "--ignore-buildable",
        "--policy",
        "missing",
        "sonar-scanner",
    )
    log = gateway.state_store.open_log("development-production-provenance")

    gateway.prepare_core(config, log=log)

    log.close()
    assert process.commands[0] == scanner_pull
    assert process.commands[1:] == [
        commands["compose"],
        commands["git"],
        commands["app_inspect"],
        commands["worker_inspect"],
        commands["control_inspect"],
        commands["mcp_inspect"],
    ]
    assert {
        "FS_BUILD_SHA": _FULL_REVISION,
        "FS_APP_IMAGE": _APP_IMAGE_ID,
        "FS_APP_IMAGE_DIGEST": _APP_IMAGE_ID,
        "FS_APP_IMAGE_REVISION": _FULL_REVISION,
        "FS_WORKER_IMAGE": _WORKER_IMAGE_ID,
        "FS_WORKER_IMAGE_DIGEST": _WORKER_IMAGE_ID,
        "FS_WORKER_IMAGE_REVISION": _FULL_REVISION,
        "FS_CONTROL_IMAGE": _CONTROL_IMAGE_ID,
        "FS_CONTROL_IMAGE_DIGEST": _CONTROL_IMAGE_ID,
        "FS_CONTROL_IMAGE_REVISION": _FULL_REVISION,
        "MCP_IMAGE": _MCP_IMAGE_ID,
        "MCP_IMAGE_DIGEST": _MCP_IMAGE_ID,
        "MCP_IMAGE_REVISION": _FULL_REVISION,
    }.items() <= gateway.state_store.read_env_values(env_path).items()


def test_development_prepare_expands_seeded_source_revision_into_image_provenance(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway, config, env_path, commands = _production_provenance_gateway(
        tmp_path,
        process,
        environment="development",
        extra_env=f"FS_BUILD_SHA={_FULL_REVISION}\n",
    )
    scanner_pull = (
        *gateway._compose_prefix(config),
        "pull",
        "--ignore-buildable",
        "--policy",
        "missing",
        "sonar-scanner",
    )
    log = gateway.state_store.open_log("development-seeded-source-revision")

    gateway.prepare_core(config, log=log)

    log.close()
    assert process.commands[0] == scanner_pull
    assert process.commands[1:] == [
        commands["compose"],
        commands["git"],
        commands["app_inspect"],
        commands["worker_inspect"],
        commands["control_inspect"],
        commands["mcp_inspect"],
    ]
    assert {
        "FS_BUILD_SHA": _FULL_REVISION,
        "FS_APP_IMAGE": _APP_IMAGE_ID,
        "FS_APP_IMAGE_DIGEST": _APP_IMAGE_ID,
        "FS_APP_IMAGE_REVISION": _FULL_REVISION,
        "FS_WORKER_IMAGE": _WORKER_IMAGE_ID,
        "FS_WORKER_IMAGE_DIGEST": _WORKER_IMAGE_ID,
        "FS_WORKER_IMAGE_REVISION": _FULL_REVISION,
        "FS_CONTROL_IMAGE": _CONTROL_IMAGE_ID,
        "FS_CONTROL_IMAGE_DIGEST": _CONTROL_IMAGE_ID,
        "FS_CONTROL_IMAGE_REVISION": _FULL_REVISION,
        "MCP_IMAGE": _MCP_IMAGE_ID,
        "MCP_IMAGE_DIGEST": _MCP_IMAGE_ID,
        "MCP_IMAGE_REVISION": _FULL_REVISION,
    }.items() <= gateway.state_store.read_env_values(env_path).items()


def test_development_prepare_types_scanner_pull_failure_before_build(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="development")
    scanner_pull = (
        *gateway._compose_prefix(config),
        "pull",
        "--ignore-buildable",
        "--policy",
        "missing",
        "sonar-scanner",
    )
    process.outcomes[scanner_pull] = ProcessOutcome(
        command=scanner_pull,
        returncode=1,
        stderr="scanner registry unavailable",
    )
    log = gateway.state_store.open_log("development-scanner-pull-failure")

    with pytest.raises(InstallerFailure) as raised:
        gateway.prepare_core(config, log=log)

    log.close()
    assert raised.value.code == "core_image_preparation_failed"
    assert raised.value.step == "prepare"
    assert process.commands == [scanner_pull]


def test_prepare_postcondition_requires_each_compose_built_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="development")
    compose_config = (*gateway._compose_prefix(config), "config", "--format", "json")
    process.outcomes[compose_config] = ProcessOutcome(
        command=compose_config,
        returncode=0,
        stdout=json.dumps(
            {
                "services": {
                    "app": {"build": {"context": "."}, "image": None},
                    "mcp": {"build": {"context": "."}, "image": "custom-mcp"},
                    "postgres": {"image": "postgres:17"},
                    "sonar-scanner": {"image": "scanner@sha256:1234"},
                }
            }
        ),
    )
    app_inspect = ("docker", "image", "inspect", "flow-steward-app")
    mcp_inspect = ("docker", "image", "inspect", "custom-mcp")
    scanner_inspect = ("docker", "image", "inspect", "scanner@sha256:1234")
    process.outcomes[app_inspect] = ProcessOutcome(command=app_inspect, returncode=0)
    process.outcomes[mcp_inspect] = ProcessOutcome(command=mcp_inspect, returncode=0)
    process.outcomes[scanner_inspect] = ProcessOutcome(command=scanner_inspect, returncode=0)
    monkeypatch.setattr(gateway, "_production_provenance_postcondition_holds", lambda _config: True)

    assert gateway.postcondition_holds("prepare", config) is True

    process.outcomes[scanner_inspect] = ProcessOutcome(command=scanner_inspect, returncode=1)
    assert gateway.postcondition_holds("prepare", config) is False
    assert ("docker", "image", "inspect", "postgres:17") not in process.commands


def test_development_prepare_postcondition_requires_scanner_service(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="development")
    compose_config = (*gateway._compose_prefix(config), "config", "--format", "json")
    process.outcomes[compose_config] = ProcessOutcome(
        command=compose_config,
        returncode=0,
        stdout=json.dumps({"services": {"app": {"build": {"context": "."}, "image": "app-image"}}}),
    )
    app_inspect = ("docker", "image", "inspect", "app-image")
    process.outcomes[app_inspect] = ProcessOutcome(command=app_inspect, returncode=0)

    assert gateway.postcondition_holds("prepare", config) is False


def test_development_prepare_postcondition_requires_truthful_production_provenance(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="development")
    env_path = tmp_path / ".env"
    _write_env_values(
        env_path,
        {
            "FS_BUILD_SHA": _FULL_REVISION,
            **_exact_prebuilt_values(include_frontend=True),
            "FS_APP_IMAGE_DIGEST": _APP_IMAGE_ID,
            "FS_APP_IMAGE_REVISION": _APP_REVISION,
            "FS_WORKER_IMAGE_DIGEST": _WORKER_IMAGE_ID,
            "FS_WORKER_IMAGE_REVISION": _WORKER_REVISION,
            "FS_CONTROL_IMAGE_DIGEST": _CONTROL_IMAGE_ID,
            "FS_CONTROL_IMAGE_REVISION": _CONTROL_REVISION,
            "MCP_IMAGE_DIGEST": _MCP_IMAGE_ID,
            "MCP_IMAGE_REVISION": _MCP_REVISION,
            "FS_CLI_IMAGE_REVISION": _CLI_REVISION,
            "FS_FRONTEND_IMAGE_REVISION": _FRONTEND_REVISION,
        },
    )
    git_revision = ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD^{commit}")
    process.outcomes[git_revision] = ProcessOutcome(
        command=git_revision,
        returncode=0,
        stdout=_FULL_REVISION + "\n",
    )
    _register_exact_prebuilt_inspections(process, include_frontend=True)

    assert gateway.postcondition_holds("prepare", config) is True

    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            f"FS_BUILD_SHA={_FULL_REVISION}\n",
            "",
        ),
        encoding="utf-8",
    )
    assert gateway.postcondition_holds("prepare", config) is False

    env_path.write_text(
        env_path.read_text(encoding="utf-8") + f"FS_BUILD_SHA={_FULL_REVISION}\n",
        encoding="utf-8",
    )
    process.outcomes[git_revision] = ProcessOutcome(
        command=git_revision,
        returncode=0,
        stdout="f" * 40 + "\n",
    )
    assert gateway.postcondition_holds("prepare", config) is False


def test_production_prepare_postcondition_does_not_require_scanner(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production")
    compose_config = (*gateway._compose_prefix(config), "config", "--format", "json")
    process.outcomes[compose_config] = ProcessOutcome(
        command=compose_config,
        returncode=0,
        stdout=json.dumps({"services": {"app": {"build": {"context": "."}, "image": "app-image"}}}),
    )
    app_inspect = ("docker", "image", "inspect", "app-image")
    process.outcomes[app_inspect] = ProcessOutcome(command=app_inspect, returncode=0)

    assert gateway.postcondition_holds("prepare", config) is True


def test_prepare_postcondition_rejects_malformed_compose_service(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="development")
    compose_config = (*gateway._compose_prefix(config), "config", "--format", "json")
    process.outcomes[compose_config] = ProcessOutcome(
        command=compose_config,
        returncode=0,
        stdout=json.dumps(
            {
                "services": {
                    "app": {"build": {"context": "."}, "image": "flow-steward-app"},
                    "sonar-scanner": {"image": "scanner@sha256:1234"},
                    "malformed": [],
                }
            }
        ),
    )
    app_inspect = ("docker", "image", "inspect", "flow-steward-app")
    process.outcomes[app_inspect] = ProcessOutcome(command=app_inspect, returncode=0)

    assert gateway.postcondition_holds("prepare", config) is False


def test_schema_is_verified_and_baselined_before_runtime_services_start(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    log = gateway.state_store.open_log("schema-order")
    config = InstallConfig()

    gateway.start_core(config, log=log)
    gateway.verify_schema(config, log=log)
    gateway.ensure_worker_identity(config, log=log)
    gateway.start_runtime(config, log=log)
    log.close()

    commands = _without_name_listings(process.commands)
    infrastructure = commands[0]
    extension_schema = commands[1]
    baseline = commands[2]
    canonical_seed = commands[3]
    workflow_knowledge_graph = commands[4]
    worker_identity = commands[5]
    runtime = commands[6]
    assert "pg-fs" in infrastructure
    assert "fs-app" not in infrastructure
    assert "memory-postgres-extensions" in extension_schema
    assert baseline[-3:] == (
        "core.application.tooling.db.upgrade_db",
        "--baseline-migration-history",
        "--author=installer",
    )
    assert baseline[-7:-4] == (
        "--entrypoint",
        "python",
        "fs-app",
    )
    assert canonical_seed[-6:] == (
        "--entrypoint",
        "python",
        "fs-app",
        "-m",
        "core.application.tooling.db.bootstrap_seed",
        "--author=installer",
    )
    assert workflow_knowledge_graph[-5:-1] == (
        "--entrypoint",
        "python",
        "fs-app",
        "-c",
    )
    assert workflow_knowledge_graph[-1] == (
        "from core.application.tooling.db.init_db import "
        "init_workflow_knowledge_graph; init_workflow_knowledge_graph()"
    )
    assert "fs-cli" not in baseline
    assert "fs-cli" not in canonical_seed
    assert "fs-worker" not in baseline
    assert "fs-worker" not in canonical_seed
    assert "fs-worker" not in workflow_knowledge_graph
    assert "fs-app" in worker_identity
    assert "fs-cli" not in worker_identity
    assert worker_identity[worker_identity.index("--volume") + 1] == (
        f"{tmp_path}:/run/flow-steward-installer:rw"
    )
    assert worker_identity[worker_identity.index("--user") + 1] == (f"{os.getuid()}:{os.getgid()}")
    assert worker_identity[-5:] == (
        "core.entrypoints.cli.ops.worker_identity_bootstrap",
        "--env-file",
        "/run/flow-steward-installer/.env",
        "--account-id",
        "00000000-0000-0000-0000-000000000002",
    )
    assert "fs-app" in runtime
    assert "fs-worker" in runtime
    assert "control-plane" in runtime


def test_worker_identity_activation_uses_non_forcing_compose_convergence(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig()
    log = gateway.state_store.open_log("worker-identity-activation")

    gateway.activate_worker_identity(config, log=log)

    log.close()
    assert process.commands == [
        (*gateway._compose_prefix(config), "up", "-d", "--no-deps", "fs-worker")
    ]


def test_compact_worker_identity_activation_uses_non_forcing_compose_convergence(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production", deployment_profile="compact")
    log = gateway.state_store.open_log("compact-worker-identity-activation")

    gateway.activate_worker_identity(config, log=log)

    log.close()
    assert process.commands == [
        (*gateway._compose_prefix(config), "up", "-d", "--no-deps", "compact-runtime")
    ]


def test_exact_prebuilt_installer_one_offs_and_activation_never_pull_or_build(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production")
    _write_env_values(tmp_path / ".env.prod", _exact_prebuilt_values())
    log = gateway.state_store.open_log("exact-installer-one-offs")

    gateway.verify_schema(config, log=log)
    gateway.ensure_worker_identity(config, log=log)
    gateway.activate_worker_identity(config, log=log)

    log.close()
    assert len(process.commands) == 6
    for command in process.commands[:5]:
        assert "run" in command
        assert command[command.index("--pull") + 1] == "never"
        assert "build" not in command
    worker_identity = process.commands[4]
    assert "fs-app" in worker_identity
    assert "fs-cli" not in worker_identity
    assert worker_identity[worker_identity.index("--volume") + 1] == (
        f"{tmp_path}:/run/flow-steward-installer:rw"
    )
    assert worker_identity[worker_identity.index("--user") + 1] == (f"{os.getuid()}:{os.getgid()}")
    activation = process.commands[-1]
    assert "--no-build" in activation
    assert activation[activation.index("--pull") + 1] == "never"


def test_canonical_schema_seed_failure_stops_before_worker_and_runtime(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig()
    canonical_seed = (
        *gateway._compose_prefix(config),
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "python",
        "fs-app",
        "-m",
        "core.application.tooling.db.bootstrap_seed",
        "--author=installer",
    )
    process.outcomes[canonical_seed] = ProcessOutcome(
        command=canonical_seed,
        returncode=1,
        stderr="canonical seed refused",
    )
    log = gateway.state_store.open_log("schema-seed-failure")

    def _raises_operation_1559():
        gateway.start_core(config, log=log)
        gateway.verify_schema(config, log=log)
        gateway.ensure_worker_identity(config, log=log)
        gateway.start_runtime(config, log=log)

    with pytest.raises(InstallerFailure) as raised:
        _raises_operation_1559()

    log.close()
    assert raised.value.code == "schema_seed_failed"
    assert raised.value.step == "schema"
    assert canonical_seed in process.commands
    assert not any(
        "core.entrypoints.cli.ops.worker_identity_bootstrap" in command
        for command in process.commands
    )
    assert not any("fs-app" in command and "up" in command for command in process.commands)


def test_workflow_graph_bootstrap_failure_stops_before_worker_and_runtime(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig()
    workflow_graph = (
        *gateway._compose_prefix(config),
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "python",
        "fs-app",
        "-c",
        (
            "from core.application.tooling.db.init_db import "
            "init_workflow_knowledge_graph; init_workflow_knowledge_graph()"
        ),
    )
    process.outcomes[workflow_graph] = ProcessOutcome(
        command=workflow_graph,
        returncode=1,
        stderr="workflow graph bootstrap refused",
    )
    log = gateway.state_store.open_log("workflow-graph-failure")

    gateway.start_core(config, log=log)

    # AC17: verify_schema is the step that raises here -- the assertions below pin
    # `step == "schema"` -- so the two calls that used to follow it inside this block
    # were unreachable.
    with pytest.raises(InstallerFailure) as raised:
        gateway.verify_schema(config, log=log)

    log.close()
    assert raised.value.code == "workflow_knowledge_graph_bootstrap_failed"
    assert raised.value.step == "schema"
    assert workflow_graph in process.commands
    assert not any(
        "core.entrypoints.cli.ops.worker_identity_bootstrap" in command
        for command in process.commands
    )
    assert not any("fs-app" in command and "up" in command for command in process.commands)


def test_configuration_pins_compose_project_identity(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    log = gateway.state_store.open_log("identity")

    gateway.ensure_configuration(InstallConfig(), log=log)
    log.close()

    compose_env = (tmp_path / ".flow-steward/compose.env").read_text(encoding="utf-8")
    assert "FS_COMPOSE_PROJECT_NAME=flow-steward" in compose_env


def test_compact_installer_uses_canonical_target_for_fresh_runtime_steps(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
        ingress_mode="bundled_traefik",
        tls_mode="local",
    )
    log = gateway.state_store.open_log("compact-canonical-target")

    gateway.ensure_configuration(config, log=log)
    gateway.start_core(config, log=log)
    gateway.verify_schema(config, log=log)
    gateway.ensure_worker_identity(config, log=log)
    gateway.start_runtime(config, log=log)
    log.close()

    compose_names = tuple(path.name for path in gateway._compose_files(config))
    assert compose_names == (
        "docker-compose.compact.yaml",
        "ingress-bundled_traefik.yaml",
        "tls-bundled-files.yaml",
    )
    commands = _without_name_listings(process.commands)
    assert "compact-runtime" not in commands[0]
    assert "pg-fs" in commands[0]
    assert "traefik" in commands[0]
    assert commands[2][commands[2].index("--entrypoint") + 2] == ("compact-runtime")
    # AC17-R142: the Compact runtime issues its own worker identity; the installer does not.
    assert not any(
        "core.entrypoints.cli.ops.worker_identity_bootstrap" in command
        for command in process.commands
    )
    assert process.commands[-1][-1] == "compact-runtime"


@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_local_production_installer_persists_source_mode_and_revision(
    tmp_path: Path, profile: str
) -> None:
    process = FakeProcessExecutor()
    revision_command = ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD^{commit}")
    process.outcomes[revision_command] = ProcessOutcome(
        command=revision_command, returncode=0, stdout=_FULL_REVISION + "\n"
    )
    gateway = _gateway(tmp_path, process, environment={"FS_INSTALL_MODE": "source"})
    config = InstallConfig(environment="production", deployment_profile=profile)
    log = gateway.state_store.open_log("local-source-provenance")
    gateway.ensure_secrets(config, log=log)
    log.close()
    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert values["FS_INSTALL_MODE"] == "source"
    assert values["FS_BUILD_SHA"] == _FULL_REVISION
    assert gateway._official_pending_release(config) is None


@pytest.mark.parametrize(
    ("profile", "persisted", "explicit"),
    (
        ("compact", "", ""),
        ("compact", _MCP_IMAGE_ID, ""),
        ("compact", _MCP_IMAGE_ID, _CLI_IMAGE_ID),
        ("scalable", "", ""),
    ),
)
def test_source_installer_preserves_only_selected_compact_mcp_image(
    tmp_path: Path, profile: str, persisted: str, explicit: str
) -> None:
    process = FakeProcessExecutor()
    command = ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD^{commit}")
    process.outcomes[command] = ProcessOutcome(
        command=command, returncode=0, stdout=_FULL_REVISION + "\n"
    )
    if persisted:
        _write_env_values(tmp_path / ".env.prod", {"MCP_IMAGE": persisted})
    gateway = _gateway(
        tmp_path,
        process,
        environment={"FS_INSTALL_MODE": "source", **({"MCP_IMAGE": explicit} if explicit else {})},
    )
    log = gateway.state_store.open_log("compact-optional-mcp")
    try:
        gateway.ensure_secrets(
            InstallConfig(environment="production", deployment_profile=profile), log=log
        )
    finally:
        log.close()
    values = gateway_module._read_env(tmp_path / ".env.prod")
    if explicit or persisted:
        assert values["MCP_IMAGE"] == (explicit or persisted)
    elif profile == "compact":
        assert "MCP_IMAGE" not in values
    else:
        assert values["MCP_IMAGE"] == registry_default_value("MCP_IMAGE", environment="production")


def test_local_production_prepare_builds_compact_from_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment={"FS_INSTALL_MODE": "source"})
    config = InstallConfig(environment="production", deployment_profile="compact")
    tag = f"flow-steward-compact:flow-steward-{_FULL_REVISION[:12]}"
    command = ("docker", "image", "inspect", "--format", "{{.Id}}", tag)
    process.outcomes[command] = ProcessOutcome(
        command=command, returncode=0, stdout=f"sha256:{'e' * 64}\n"
    )
    cli_command = (
        "docker",
        "image",
        "inspect",
        f"flow-steward-cli:compact-flow-steward-{_FULL_REVISION[:12]}",
    )
    process.outcomes[cli_command] = ProcessOutcome(
        command=cli_command,
        returncode=0,
        stdout=_image_inspection(
            _FRONTEND_IMAGE_ID,
            entrypoint=("python", "-m", "core.entrypoints.cli.lifecycle_click_app"),
        ),
    )
    monkeypatch.setattr(gateway, "_prepare_exact_prebuilt_provenance", lambda *_a, **_kw: None)
    log = gateway.state_store.open_log("local-production-build")
    gateway.prepare_core(config, log=log)
    log.close()
    targets = {
        command[command.index("--target") + 1]
        for command in process.commands
        if command[:3] == ("docker", "buildx", "build")
    }
    assert targets == {"compact-runtime", "cli"}
    assert not any("imagetools" in command for command in process.commands)


def test_official_pending_identity_cannot_be_overridden_by_source_mode(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor(), environment={"FS_INSTALL_MODE": "source"})
    release = _stage_official_release(gateway, "compact")
    config = InstallConfig(environment="production", deployment_profile="compact")
    assert gateway._official_pending_release(config) == release


def test_development_installer_persists_source_mode_and_exact_git_revision(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    revision_command = ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD^{commit}")
    process.outcomes[revision_command] = ProcessOutcome(
        command=revision_command,
        returncode=0,
        stdout=_FULL_REVISION + "\n",
    )
    gateway = _gateway(tmp_path, process)
    log = gateway.state_store.open_log("source-provenance")

    gateway.ensure_secrets(InstallConfig(environment="development"), log=log)
    log.close()

    values = gateway_module._read_env(tmp_path / ".env")
    assert values["FS_INSTALL_MODE"] == "source"
    assert values["FS_BUILD_SHA"] == _FULL_REVISION


def test_configuration_normalizes_development_profiles_and_compatibility_flags(
    tmp_path: Path,
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    log = gateway.state_store.open_log("development-preset")

    gateway.ensure_configuration(
        InstallConfig(environment="development", development_tools=("test_runner",)),
        log=log,
    )
    log.close()

    compose_env = gateway_module._read_env(tmp_path / ".flow-steward/compose.env")
    assert compose_env["FS_COMPOSE_PROFILES"] == (
        "storage-seaweedfs cli development test audit quality"
    )
    stack = (tmp_path / ".flow-steward/stack.toml").read_text(encoding="utf-8")
    assert 'development_tools = ["test_runner"]' in stack


def test_inspect_reports_owned_resources_and_never_mutates(tmp_path: Path) -> None:
    volumes = ("flow-steward_pg-fs", "flow-steward_rabbitmq-fs")
    process = FakeProcessExecutor(
        {
            (
                "docker",
                "volume",
                "ls",
                "--filter",
                "label=com.docker.compose.project=flow-steward",
                "--format",
                "{{.Name}}",
            ): ProcessOutcome(command=(), returncode=0, stdout="\n".join(volumes)),
            (
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.project=flow-steward",
                "--format",
                "{{.Names}}",
            ): ProcessOutcome(command=(), returncode=0, stdout="flow-steward-pg-fs-1\n"),
        }
    )
    gateway = _gateway(tmp_path, process)
    (tmp_path / ".env").write_text(
        "FS_SECRET_KEY=x\nENCRYPTION_KEY=y\nFS_BACKUP_ENCRYPTION_KEY=z\nFS_METRICS_TOKEN=metrics\n",
        encoding="utf-8",
    )
    (tmp_path / ".flow-steward").mkdir()
    (tmp_path / ".flow-steward/stack.toml").write_text(
        'environment = "local_development"\n', encoding="utf-8"
    )

    evidence = gateway.inspect()

    assert evidence.owned_volumes == volumes
    assert evidence.durable_secrets_present is True
    assert all(command[1] not in {"rm", "run"} for command in process.commands)


def test_process_failure_output_is_retained_in_log(tmp_path: Path) -> None:
    command = ("docker", "compose", "version", "--short")
    process = FakeProcessExecutor(
        {
            command: ProcessOutcome(
                command=command, returncode=1, stdout="full stdout", stderr="full stderr"
            )
        }
    )
    gateway = _gateway(tmp_path, process)
    log = gateway.state_store.open_log("output")

    _raises_input_1676_1 = InstallConfig()
    with pytest.raises(InstallerFailure):
        gateway.preflight(_raises_input_1676_1, log=log)
    log.close()

    text = log.path.read_text()
    assert "full stdout" in text
    assert "full stderr" in text
    assert json.dumps(list(command)) not in text
    assert os.path.exists(log.path)


def test_inspect_treats_missing_docker_as_no_observed_resources(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, MissingDockerExecutor())

    evidence = gateway.inspect()

    assert evidence.owned_containers == ()
    assert evidence.owned_volumes == ()


def test_inspect_marks_corrupt_journal_invalid_instead_of_crashing(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    gateway.state_store.root.mkdir()
    gateway.state_store.journal_path.write_text("{not-json", encoding="utf-8")

    evidence = gateway.inspect()

    assert evidence.journal_exists is True
    assert evidence.journal_valid is False


def test_inspect_marks_failed_docker_inventory_invalid(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    volume_command = (
        "docker",
        "volume",
        "ls",
        "--filter",
        "label=com.docker.compose.project=flow-steward",
        "--format",
        "{{.Name}}",
    )
    process.outcomes[volume_command] = ProcessOutcome(
        command=volume_command, returncode=1, stderr="permission denied"
    )

    evidence = gateway.inspect()

    assert evidence.resource_inventory_valid is False


def test_failed_inventory_output_never_becomes_owned_resources(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    volume_command = (
        "docker",
        "volume",
        "ls",
        "--filter",
        "label=com.docker.compose.project=flow-steward",
        "--format",
        "{{.Name}}",
    )
    container_command = (
        "docker",
        "ps",
        "-a",
        "--filter",
        "label=com.docker.compose.project=flow-steward",
        "--format",
        "{{.Names}}",
    )
    failed = "Cannot connect to Docker daemon\n"
    process.outcomes[volume_command] = ProcessOutcome(
        command=volume_command, returncode=1, stdout=failed
    )
    process.outcomes[container_command] = ProcessOutcome(
        command=container_command, returncode=0, stdout="flow-steward-fs-app-1\n"
    )

    evidence = gateway.inspect()

    assert evidence.resource_inventory_valid is False
    assert evidence.owned_containers == ()
    assert evidence.owned_volumes == ()
    classification = classify_installation(evidence)
    assert classification.reasons == ("Docker resource inventory could not be read safely.",)


def test_preflight_refuses_an_unreadable_checkout_before_writing_anything(
    tmp_path: Path,
) -> None:
    """AC17-R90: this used to surface during prepare_core, after writes.

    By then configuration, generated secrets and an issued TLS certificate were
    already on disk, and the message named the image inventory rather than git.
    """
    root = _repo(tmp_path)
    process = FakeProcessExecutor()
    git_command = ("git", "-C", str(root), "rev-parse", "HEAD^{commit}")
    process.outcomes[git_command] = ProcessOutcome(
        command=git_command, returncode=128, stderr="fatal: not a git repository"
    )
    state_dir = root / ".flow-steward"
    gateway = DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(state_dir),
        process=process,
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment={},
    )
    log = gateway.state_store.open_log("unreadable-checkout")
    written_before = sorted(path.name for path in state_dir.iterdir())

    config = InstallConfig()

    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(config, log=log)

    log.close()
    assert raised.value.code == "source_revision_unavailable"
    assert raised.value.step == "preflight"
    assert "git" in raised.value.remediation.lower()
    assert "image inventory" not in raised.value.remediation.lower()
    assert not (root / ".env").exists()
    assert not (root / ".ssl").exists()
    assert sorted(path.name for path in state_dir.iterdir()) == written_before


def test_preflight_rejects_a_revision_that_is_not_a_full_sha(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    process = FakeProcessExecutor()
    git_command = ("git", "-C", str(root), "rev-parse", "HEAD^{commit}")
    process.outcomes[git_command] = ProcessOutcome(
        command=git_command, returncode=0, stdout="HEAD\n"
    )
    gateway = DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(root / ".flow-steward"),
        process=process,
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment={},
    )
    log = gateway.state_store.open_log("short-revision")

    config = InstallConfig()

    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(config, log=log)

    log.close()
    assert raised.value.code == "source_revision_unavailable"
    assert raised.value.step == "preflight"
    assert "HEAD" in raised.value.cause


def test_preflight_reports_missing_docker_cli_before_daemon_probe(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    process = FakeProcessExecutor()
    gateway = DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(root / ".flow-steward"),
        process=process,
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: False,
        environment={},
    )
    log = gateway.state_store.open_log("missing-docker")

    _raises_input_1781_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(_raises_input_1781_1, log=log)

    log.close()
    assert raised.value.code == "docker_cli_missing"
    assert "https://docs.docker.com/engine/install/" in raised.value.remediation
    assert "https://docs.docker.com/desktop/" in raised.value.remediation
    assert process.commands == []


def test_preflight_allows_public_port_owned_by_resumable_stack(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ports_command = (
        "docker",
        "ps",
        "--filter",
        "label=com.docker.compose.project=flow-steward",
        "--format",
        "{{.Ports}}",
    )
    process = FakeProcessExecutor(
        {
            ports_command: ProcessOutcome(
                command=ports_command,
                returncode=0,
                # bundled Traefik publishes both the HTTPS entrypoint and the plain-HTTP
                # redirect, so a resumable stack owns both (AC17-R82)
                stdout="0.0.0.0:8080->8080/tcp, 0.0.0.0:8099->8099/tcp\n",
            )
        }
    )
    gateway = DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(root / ".flow-steward"),
        process=process,
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: False,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment={},
    )
    log = gateway.state_store.open_log("owned-port")

    gateway.preflight(InstallConfig(public_port=8099), log=log)
    log.close()
    assert ports_command in process.commands


def test_bundled_traefik_preflight_probes_the_redirect_port_it_also_binds(
    tmp_path: Path,
) -> None:
    # AC17-R82: the operator asked for one public port and preflight checked exactly that
    # one. bundled_traefik publishes two -- the HTTPS entrypoint and the plain-HTTP
    # redirect on ${HTTP_PORT:-8080} -- so an install died at "Start core services", after
    # the network, both volumes and three containers existed, on a raw
    # "Bind for 0.0.0.0:8080 failed: port is already allocated". 8080 is busy on any host
    # already running an installation of this product.
    probed: list[int] = []
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        port_available=lambda _host, port: probed.append(port) or port != 8080,
        environment=_COMPACT_TEST_IMAGE,
    )
    _stage_official_release(gateway, "compact")
    log = gateway.state_store.open_log("bundled-redirect-port")
    config = InstallConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
        ingress_mode="bundled_traefik",
        tls_mode="self_signed",
        public_host="192.168.0.168",
        public_port=8399,
    )

    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(config, log=log)

    log.close()
    assert probed == [8399, 8080]
    assert raised.value.code == "port_conflict"
    assert "8080" in raised.value.cause
    # and it names the knob that moves it, which the Docker bind error never did.
    # AC17-R92: that knob is now a questionnaire answer. It used to be the HTTP_PORT
    # environment variable, so an install on a host where 8080 was taken could not be
    # completed by answering questions alone -- and the owner's criterion is that the
    # operator exports nothing.
    assert "--redirect-http-port" in raised.value.remediation


def test_the_redirect_port_answer_is_probed_instead_of_the_environment(
    tmp_path: Path,
) -> None:
    """AC17-R92: answering the question must move the port Compose actually binds.

    The redirect entrypoint used to be reachable only through the HTTP_PORT
    environment variable. On a host where 8080 is taken -- any host already running
    this product -- `./install.sh` with default answers therefore could not be
    completed at all, against the owner's criterion that the operator exports
    nothing. Probing the ambient variable while Compose binds the answer would
    clear a port nobody is going to use.
    """
    probed: list[int] = []
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        port_available=lambda _host, port: probed.append(port) or port != 8080,
        environment=_COMPACT_TEST_IMAGE,
    )
    _stage_official_release(gateway, "compact")
    log = gateway.state_store.open_log("redirect-port-answer")
    config = InstallConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
        ingress_mode="bundled_traefik",
        tls_mode="self_signed",
        public_host="192.168.0.168",
        public_port=8399,
        redirect_http_port=8390,
    )

    gateway.preflight(config, log=log)
    log.close()

    # 8080 is never probed: the answer replaced it, and the answer is what binds.
    assert probed == [8399, 8390]


def test_the_redirect_port_answer_reaches_the_environment_file(tmp_path: Path) -> None:
    """AC17-R103: the answer was probed on one port and then recorded as another.

    AC17-R92 taught the preflight probe that the answer wins over the ambient
    HTTP_PORT. It did not teach the environment file, which kept resolving the port
    from the environment on its own -- so `--redirect-http-port 8472` was probed on
    8472, written as 8080, and bound on 8080. The install was accepted and then died
    on a busy port, with .env.prod still claiming 8080 afterwards.
    """
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    config = InstallConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
        ingress_mode="bundled_traefik",
        tls_mode="self_signed",
        public_host="192.168.0.168",
        public_port=8399,
        redirect_http_port=8472,
    )
    log = gateway.state_store.open_log("redirect-port-recorded")

    gateway.ensure_secrets(config, log=log)

    log.close()
    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert values["HTTP_PORT"] == "8472"


def test_release_source_overrides_are_persisted_for_later_cli_commands(
    tmp_path: Path,
) -> None:
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        secret_factory=lambda: "generated-secret",
        environment={
            **_COMPACT_TEST_IMAGE,
            "FS_RELEASE_REPOSITORY": "acme/flow-steward",
            "FS_RELEASE_REGISTRIES": "registry.example/acme,mirror.example/acme",
        },
    )
    log = gateway.state_store.open_log("release-sources")

    gateway.ensure_secrets(
        InstallConfig(environment="production", deployment_profile="compact"),
        log=log,
    )
    log.close()
    gateway._environment.pop("FS_RELEASE_REPOSITORY")
    gateway._environment.pop("FS_RELEASE_REGISTRIES")
    resume_log = gateway.state_store.open_log("release-sources-resume")
    gateway.ensure_secrets(
        InstallConfig(environment="production", deployment_profile="compact"),
        log=resume_log,
    )
    resume_log.close()

    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert values["FS_RELEASE_REPOSITORY"] == "acme/flow-steward"
    assert values["FS_RELEASE_REGISTRIES"] == ("registry.example/acme,mirror.example/acme")


def test_empty_installer_environment_writes_the_official_release_repository_default(
    tmp_path: Path,
) -> None:
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        secret_factory=lambda: "generated-secret",
        environment={},
    )
    log = gateway.state_store.open_log("release-sources-default")

    gateway.ensure_secrets(
        InstallConfig(
            environment="production",
            deployment_profile="compact",
            file_storage="local_fs",
        ),
        log=log,
    )
    log.close()

    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert values["FS_RELEASE_REPOSITORY"] == "Flow-Steward/flow-steward-app"


def test_the_ambient_redirect_port_is_recorded_when_the_answer_is_left_alone(
    tmp_path: Path,
) -> None:
    """The one case the two sources cannot be told apart from here keeps its old answer.

    An operator who exported HTTP_PORT and never touched --redirect-http-port must
    not have their port replaced by the default, so the probe and the file agree on
    deferring to the environment while the answer sits at 8080.
    """
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        secret_factory=lambda: "generated-secret",
        environment={**_COMPACT_TEST_IMAGE, "HTTP_PORT": "8398"},
    )
    config = InstallConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
        ingress_mode="bundled_traefik",
        tls_mode="self_signed",
        public_host="192.168.0.168",
        public_port=8399,
    )
    log = gateway.state_store.open_log("redirect-port-ambient")

    gateway.ensure_secrets(config, log=log)

    log.close()
    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert values["HTTP_PORT"] == "8398"


def test_an_exported_http_port_overrides_even_an_answered_redirect_port(
    tmp_path: Path,
) -> None:
    """The last word belongs to the bootstrap isolation overrides, by design.

    Written down because it cost two readers an hour: HTTP_PORT is one of the
    _BOOTSTRAP_ISOLATION_ENV_NAMES, and `managed.update(isolation)` runs after the
    resolved value is placed. So an exported HTTP_PORT beats the answer, which looks
    like AC17-R103 reopening and is not -- R103 was about the answer losing to an
    environment that said nothing at all. Anyone tempted to make the answer win here
    should change the isolation contract deliberately, not this line.
    """
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        secret_factory=lambda: "generated-secret",
        environment={**_COMPACT_TEST_IMAGE, "HTTP_PORT": "9000"},
    )
    config = InstallConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
        ingress_mode="bundled_traefik",
        tls_mode="self_signed",
        public_host="192.168.0.168",
        public_port=8399,
        redirect_http_port=8472,
    )
    log = gateway.state_store.open_log("redirect-port-isolation")

    gateway.ensure_secrets(config, log=log)

    log.close()
    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert values["HTTP_PORT"] == "9000"


def test_bundled_traefik_preflight_honours_a_redirect_port_override(tmp_path: Path) -> None:
    probed: list[int] = []
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        port_available=lambda _host, port: probed.append(port) or port != 8080,
        environment={**_COMPACT_TEST_IMAGE, "HTTP_PORT": "8398"},
    )
    _stage_official_release(gateway, "compact")
    log = gateway.state_store.open_log("bundled-redirect-override")

    gateway.preflight(
        InstallConfig(
            environment="production",
            deployment_profile="compact",
            file_storage="local_fs",
            ingress_mode="bundled_traefik",
            tls_mode="self_signed",
            public_host="192.168.0.168",
            public_port=8399,
        ),
        log=log,
    )

    log.close()
    assert probed == [8399, 8398]


def test_non_publishing_ingress_modes_do_not_gain_a_redirect_port_probe(
    tmp_path: Path,
) -> None:
    """The second probe belongs to bundled Traefik only; it must not spread."""
    probed: list[int] = []
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        port_available=lambda _host, port: probed.append(port) or True,
        environment=_COMPACT_TEST_IMAGE,
    )
    _stage_official_release(gateway, "compact")
    log = gateway.state_store.open_log("external-proxy-single-port")

    gateway.preflight(
        InstallConfig(
            environment="production",
            deployment_profile="compact",
            file_storage="local_fs",
            ingress_mode="external_proxy",
            tls_mode="reverse_proxy",
            proxy_placement="host",
            public_host="192.168.0.168",
            public_port=8399,
        ),
        log=log,
    )

    log.close()
    assert probed == [18099]


def test_bootstrap_preflight_detects_docker_host_route_listener_before_bind_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[int, int]] = []
    route_socket = HostRouteSocket()

    monkeypatch.setattr(
        gateway_module.socket,
        "socket",
        lambda family, kind: observed.append((family, kind)) or route_socket,
    )
    monkeypatch.setattr(
        gateway_module.socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("dual-stack create_connection must not be used"),
    )
    process = FakeProcessExecutor()
    gateway = _gateway(
        tmp_path,
        process,
        port_available=None,
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
    )
    log = gateway.state_store.open_log("bootstrap-host-route-listener")

    _raises_input_1851_1 = InstallConfig(public_host="127.0.0.1", public_port=8099)
    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(_raises_input_1851_1, log=log)

    log.close()
    assert raised.value.code == "port_conflict"
    assert observed == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert route_socket.timeout == 1.0
    assert route_socket.address == ("host.docker.internal", 8099)
    assert route_socket.closed
    assert not any(
        command[:3] == ("docker", "container", "inspect") for command in process.commands
    )


def test_bootstrap_preflight_fails_closed_when_docker_host_route_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route that resolves nowhere is still fatal, and deliberately.

    AC17-R78 narrowed this from "any error" to "the name does not resolve". A timeout used to
    land here too, which reported a healthy free port as a fatal error on any host that drops
    packets to unlistened ports. Resolution failure is different: `_open_public_probe` dials
    this same route to verify the published URL once the stack is up, so a name that resolves
    nowhere cannot be verified either.
    """
    route_socket = HostRouteSocket(gateway_module.socket.gaierror(-2, "Name or service not known"))
    monkeypatch.setattr(gateway_module.socket, "socket", lambda *_args: route_socket)
    process = FakeProcessExecutor()
    gateway = _gateway(
        tmp_path,
        process,
        port_available=None,
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
    )
    log = gateway.state_store.open_log("bootstrap-host-route-unavailable")

    _raises_input_1880_1 = InstallConfig(public_host="127.0.0.1", public_port=8099)
    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(_raises_input_1880_1, log=log)

    log.close()
    assert raised.value.code == "host_port_route_probe_failed"
    assert raised.value.exit_code == 7
    assert route_socket.closed
    assert not any(
        command[:3] == ("docker", "container", "inspect") for command in process.commands
    )


def test_bootstrap_preflight_checks_public_port_with_ephemeral_docker_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module.socket, "gethostname", lambda: "bootstrap-container")
    monkeypatch.setattr(
        gateway_module.socket,
        "socket",
        _refused_host_route_socket,
    )
    inspect_command = (
        "docker",
        "container",
        "inspect",
        "bootstrap-container",
        "--format",
        "{{.Image}}",
    )
    probe_command = (
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--publish",
        "8099:1/tcp",
        "--publish",
        "8099:1/udp",
        "--entrypoint",
        "python3",
        _APP_IMAGE_ID,
        "-c",
        "pass",
    )
    process = FakeProcessExecutor(
        {
            inspect_command: ProcessOutcome(
                command=inspect_command,
                returncode=0,
                stdout=f"{_APP_IMAGE_ID}\n",
            ),
            probe_command: ProcessOutcome(
                command=probe_command,
                returncode=125,
                stderr="listen tcp4 127.0.0.1:8099: bind: address already in use",
            ),
        }
    )
    gateway = _gateway(
        tmp_path,
        process,
        port_available=None,
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
    )
    log = gateway.state_store.open_log("bootstrap-host-bind")

    _raises_input_1948_1 = InstallConfig(public_host="127.0.0.1", public_port=8099)
    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(_raises_input_1948_1, log=log)

    log.close()
    assert raised.value.code == "port_conflict"
    assert inspect_command in process.commands
    assert probe_command in process.commands
    assert "--network" not in probe_command


def test_bootstrap_preflight_accepts_successful_ephemeral_docker_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module.socket, "gethostname", lambda: "bootstrap-container")
    monkeypatch.setattr(
        gateway_module.socket,
        "socket",
        _refused_host_route_socket,
    )
    inspect_command = (
        "docker",
        "container",
        "inspect",
        "bootstrap-container",
        "--format",
        "{{.Image}}",
    )
    process = FakeProcessExecutor(
        {
            inspect_command: ProcessOutcome(
                command=inspect_command,
                returncode=0,
                stdout=f"{_APP_IMAGE_ID}\n",
            )
        }
    )
    gateway = _gateway(
        tmp_path,
        process,
        port_available=None,
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
    )
    log = gateway.state_store.open_log("bootstrap-free-host-bind")

    gateway.preflight(InstallConfig(public_host="127.0.0.1", public_port=8099), log=log)

    log.close()


def test_bootstrap_preflight_fails_closed_when_docker_bind_probe_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module.socket, "gethostname", lambda: "bootstrap-container")
    monkeypatch.setattr(
        gateway_module.socket,
        "socket",
        _refused_host_route_socket,
    )
    inspect_command = (
        "docker",
        "container",
        "inspect",
        "bootstrap-container",
        "--format",
        "{{.Image}}",
    )
    process = FakeProcessExecutor(
        {
            inspect_command: ProcessOutcome(
                command=inspect_command,
                returncode=0,
                stdout="not-an-image-id\n",
            )
        }
    )
    gateway = _gateway(
        tmp_path,
        process,
        port_available=None,
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
    )
    log = gateway.state_store.open_log("bootstrap-invalid-bind-probe")

    _raises_input_2033_1 = InstallConfig(public_host="127.0.0.1", public_port=8099)
    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(_raises_input_2033_1, log=log)

    log.close()
    assert raised.value.code == "host_port_bind_probe_failed"
    assert raised.value.exit_code == 7


def test_bootstrap_reverse_proxy_bind_probe_matches_loopback_tcp_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module.socket, "gethostname", lambda: "bootstrap-container")
    monkeypatch.setattr(
        gateway_module.socket,
        "socket",
        _refused_host_route_socket,
    )
    inspect_command = (
        "docker",
        "container",
        "inspect",
        "bootstrap-container",
        "--format",
        "{{.Image}}",
    )
    probe_command = (
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--publish",
        "127.0.0.1:18099:1/tcp",
        "--entrypoint",
        "python3",
        _APP_IMAGE_ID,
        "-c",
        "pass",
    )
    process = FakeProcessExecutor(
        {
            inspect_command: ProcessOutcome(
                command=inspect_command,
                returncode=0,
                stdout=f"{_APP_IMAGE_ID}\n",
            )
        }
    )
    gateway = _gateway(
        tmp_path,
        process,
        port_available=None,
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
    )
    log = gateway.state_store.open_log("bootstrap-reverse-proxy-bind")

    gateway.preflight(
        InstallConfig(
            tls_mode="reverse_proxy",
            public_host="flow.example.test",
            public_port=443,
            reverse_proxy_backend_port=18099,
        ),
        log=log,
    )

    log.close()
    assert probe_command in process.commands


def test_reverse_proxy_preflight_checks_private_backend_port(tmp_path: Path) -> None:
    checked: list[tuple[str, int]] = []
    process = FakeProcessExecutor()
    gateway = _gateway(
        tmp_path,
        process,
        port_available=lambda host, port: checked.append((host, port)) or True,
    )
    log = gateway.state_store.open_log("reverse-proxy-preflight")

    gateway.preflight(
        InstallConfig(
            tls_mode="reverse_proxy",
            public_host="flow.example.test",
            public_port=443,
            reverse_proxy_backend_port=18099,
        ),
        log=log,
    )

    log.close()
    assert checked == [("127.0.0.1", 18099)]


def test_docker_reverse_proxy_preflight_does_not_claim_a_host_port(tmp_path: Path) -> None:
    checked: list[tuple[str, int]] = []
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        port_available=lambda host, port: checked.append((host, port)) or True,
        environment=_COMPACT_TEST_IMAGE,
    )
    _stage_official_release(gateway, "compact")
    log = gateway.state_store.open_log("docker-reverse-proxy-preflight")

    gateway.preflight(
        InstallConfig(
            environment="production",
            deployment_profile="compact",
            file_storage="local_fs",
            ingress_mode="external_proxy",
            tls_mode="reverse_proxy",
            proxy_placement="docker",
            proxy_network="shared-proxy",
        ),
        log=log,
    )

    log.close()
    assert checked == []


def test_docker_reverse_proxy_uses_static_network_overlay_without_host_override(
    tmp_path: Path,
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    log = gateway.state_store.open_log("docker-reverse-proxy-config")
    config = InstallConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
        ingress_mode="external_proxy",
        tls_mode="reverse_proxy",
        proxy_placement="docker",
        proxy_network="shared-proxy",
    )

    gateway.ensure_configuration(config, log=log)

    log.close()
    assert not (tmp_path / ".flow-steward/installer.compose.override.yaml").exists()
    assert gateway._compose_files(config)[-1].name == "ingress-external_proxy-docker.yaml"
    assert gateway.postcondition_holds("configuration", config) is True


def test_reverse_proxy_writes_loopback_override_and_omits_traefik(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    log = gateway.state_store.open_log("reverse-proxy-config")
    config = InstallConfig(
        tls_mode="reverse_proxy",
        public_host="flow.example.test",
        public_port=443,
        reverse_proxy_backend_port=18099,
    )

    gateway.ensure_configuration(config, log=log)
    gateway.start_core(config, log=log)

    override = tmp_path / ".flow-steward/installer.compose.override.yaml"
    override_text = override.read_text(encoding="utf-8")
    assert '"127.0.0.1:18099:8099"' in override_text
    assert 'profiles: ["installer-disabled-traefik"]' in override_text
    assert "required: false" in override_text
    command = process.commands[-1]
    assert str(override) in command
    assert "traefik" not in command
    assert gateway.product_url(config) == "https://flow.example.test"

    log.close()


def test_reverse_proxy_external_state_uses_canonical_override_and_rejects_stale_resume(
    tmp_path: Path,
) -> None:
    repo_root = _repo(tmp_path / "distribution")
    external_state = InstallerStateStore(tmp_path / "installer-state")
    gateway = DockerInstallerGateway(
        repo_root=repo_root,
        state_store=external_state,
        process=FakeProcessExecutor(),
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment={},
    )
    config = InstallConfig(
        tls_mode="reverse_proxy",
        public_host="flow.example.test",
        public_port=443,
        reverse_proxy_backend_port=18099,
    )
    log = gateway.state_store.open_log("external-state-reverse-proxy")

    gateway.ensure_configuration(config, log=log)

    canonical_override = repo_root / ".flow-steward/installer.compose.override.yaml"
    stale_override = external_state.root / "installer.compose.override.yaml"
    assert canonical_override.is_file()
    assert not stale_override.exists()
    assert canonical_override.resolve() in gateway._compose_files(config)
    assert gateway.postcondition_holds("configuration", config) is True

    stale_override.parent.mkdir(parents=True, exist_ok=True)
    stale_override.write_text(canonical_override.read_text(encoding="utf-8"), encoding="utf-8")
    canonical_override.unlink()

    assert gateway.postcondition_holds("configuration", config) is False
    log.close()


@pytest.mark.parametrize("tls_mode", ["local", "self_signed"])
def test_generated_certificate_is_trusted_for_public_health_probe(
    tmp_path: Path,
    monkeypatch,
    tls_mode: str,
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig(tls_mode=tls_mode)
    certificate = tmp_path / config.certificate_path
    certificate.parent.mkdir(parents=True)
    certificate.write_text("certificate", encoding="utf-8")
    sentinel_context = object()
    observed: dict[str, object] = {}

    def fake_context(*, cafile=None):
        observed["cafile"] = cafile
        return sentinel_context

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(url, *, timeout, context=None):
        observed.update(url=url, timeout=timeout, context=context)
        return Response()

    monkeypatch.setattr(ssl, "create_default_context", fake_context)
    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", fake_urlopen)
    log = gateway.state_store.open_log("public-route")

    gateway.verify_public_route(config, log=log)

    log.close()
    assert observed["cafile"] == str(certificate)
    assert observed["context"] is sentinel_context


def test_bootstrap_route_dials_host_with_original_request_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
    )
    config = InstallConfig(public_host="127.0.0.1", public_port=18443)
    certificate = tmp_path / config.certificate_path
    certificate.parent.mkdir(parents=True)
    certificate.write_text("certificate", encoding="utf-8")
    observed: list[dict[str, object]] = []

    class Context:
        check_hostname = True

    context = Context()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "ok", "auth": {"bootstrap_required": True}}).encode()

    def routed_urlopen(
        request,
        *,
        dial_host,
        dial_port,
        server_hostname,
        timeout,
        context,
    ):
        observed.append(
            {
                "url": request.full_url,
                "host": request.get_header("Host"),
                "dial_host": dial_host,
                "dial_port": dial_port,
                "server_hostname": server_hostname,
                "timeout": timeout,
                "context": context,
            }
        )
        return Response()

    trusted_ca_files: list[str] = []

    def create_context(*, cafile):
        trusted_ca_files.append(cafile)
        return context

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    monkeypatch.setattr(gateway_health, "_routed_urlopen", routed_urlopen, raising=False)
    monkeypatch.setattr(
        gateway_module.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("bootstrap route must not dial container loopback"),
    )
    log = gateway.state_store.open_log("routed-public-route")

    gateway.verify_public_route(config, log=log)
    bootstrap = gateway.verify_bootstrap(config, log=log)

    log.close()
    assert bootstrap.bootstrap_required is True
    assert gateway.product_url(config) == "https://127.0.0.1:18443"
    assert trusted_ca_files == [str(certificate), str(certificate)]
    assert observed == [
        {
            "url": "https://127.0.0.1:18443/health",
            "host": "127.0.0.1:18443",
            "dial_host": "host.docker.internal",
            "dial_port": 18443,
            "server_hostname": "127.0.0.1",
            "timeout": 10,
            "context": context,
        },
        {
            "url": "https://127.0.0.1:18443/",
            "host": "127.0.0.1:18443",
            "dial_host": "host.docker.internal",
            "dial_port": 18443,
            "server_hostname": "127.0.0.1",
            "timeout": 10,
            "context": context,
        },
        {
            "url": "https://127.0.0.1:18443/api/app/bootstrap",
            "host": "127.0.0.1:18443",
            "dial_host": "host.docker.internal",
            "dial_port": 18443,
            "server_hostname": "127.0.0.1",
            "timeout": 10,
            "context": context,
        },
    ]
    assert context.check_hostname is True


def test_routed_https_connection_preserves_strict_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class RawSocket:
        def close(self) -> None:
            observed["raw_closed"] = True

    raw_socket = RawSocket()

    class Context:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED

        def wrap_socket(self, sock, *, server_hostname):
            observed.update(wrapped_socket=sock, server_hostname=server_hostname)
            return sock

    def create_connection(address, timeout, source_address):
        observed.update(
            address=address,
            timeout=timeout,
            source_address=source_address,
        )
        return raw_socket

    def request(connection, method, target, *, headers):
        observed.update(
            request_host=connection.host,
            method=method,
            target=target,
            headers=headers,
        )
        connection.connect()

    response = object()

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(gateway_module._RoutedHTTPSConnection, "request", request)
    monkeypatch.setattr(
        gateway_module._RoutedHTTPSConnection,
        "getresponse",
        lambda _connection: response,
    )
    context = Context()
    request_identity = gateway_module.urlrequest.Request(
        "https://flow.example.test:443/health",
        headers={"Host": "flow.example.test:443"},
    )

    result = gateway_module._routed_urlopen(
        request_identity,
        dial_host="host.docker.internal",
        dial_port=18443,
        server_hostname="flow.example.test",
        timeout=10,
        context=context,
    )

    assert result is response
    assert observed == {
        "request_host": "flow.example.test",
        "method": "GET",
        "target": "/health",
        "headers": {"Host": "flow.example.test:443"},
        "address": ("host.docker.internal", 18443),
        "timeout": 10,
        "source_address": None,
        "wrapped_socket": raw_socket,
        "server_hostname": "flow.example.test",
    }
    assert context.check_hostname is True


@pytest.mark.parametrize("signal", (None, "", "1", "unexpected", " 0"))
def test_absent_or_invalid_bootstrap_route_keeps_direct_public_probe(
    tmp_path: Path,
    monkeypatch,
    signal: str | None,
) -> None:
    environment = {} if signal is None else {"FS_INSTALLER_USE_HOST_PYTHON": signal}
    gateway = _gateway(tmp_path, FakeProcessExecutor(), environment=environment)
    config = InstallConfig(public_host="127.0.0.1", public_port=18443)
    certificate = tmp_path / config.certificate_path
    certificate.parent.mkdir(parents=True)
    certificate.write_text("certificate", encoding="utf-8")
    observed: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def direct_urlopen(url, *, timeout, context=None):
        observed.append(url)
        return Response()

    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", direct_urlopen)
    monkeypatch.setattr(
        gateway_health,
        "_routed_urlopen",
        lambda *_args, **_kwargs: pytest.fail("invalid signal must not enable host routing"),
        raising=False,
    )
    log = gateway.state_store.open_log("direct-public-route")

    gateway.verify_public_route(config, log=log)

    log.close()
    assert observed == [
        "https://127.0.0.1:18443/health",
        "https://127.0.0.1:18443/",
    ]


def test_reverse_proxy_probes_use_published_backend_without_changing_public_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
    )
    config = InstallConfig(
        tls_mode="reverse_proxy",
        public_host="flow.example.test",
        public_port=443,
        reverse_proxy_backend_port=18099,
    )
    observed: list[dict[str, object]] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "ok", "auth": {"bootstrap_required": True}}).encode()

    def routed_urlopen(request, **kwargs):
        observed.append(
            {
                "url": request.full_url,
                "host": request.get_header("Host"),
                **kwargs,
            }
        )
        return Response()

    monkeypatch.setattr(gateway_health, "_routed_urlopen", routed_urlopen, raising=False)
    monkeypatch.setattr(
        gateway_module.urlrequest,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("reverse-proxy probe must use published backend"),
    )
    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda **_kwargs: pytest.fail("published backend probe must remain plain HTTP"),
    )
    log = gateway.state_store.open_log("reverse-proxy-probes")

    gateway.verify_public_route(config, log=log)
    bootstrap = gateway.verify_bootstrap(config, log=log)

    log.close()
    assert bootstrap.bootstrap_required is True
    assert gateway.product_url(config) == "https://flow.example.test"
    assert observed == [
        {
            "url": "http://flow.example.test/health",
            "host": "flow.example.test",
            "dial_host": "host.docker.internal",
            "dial_port": 18099,
            "server_hostname": None,
            "timeout": 10,
            "context": None,
        },
        {
            "url": "http://flow.example.test/",
            "host": "flow.example.test",
            "dial_host": "host.docker.internal",
            "dial_port": 18099,
            "server_hostname": None,
            "timeout": 10,
            "context": None,
        },
        {
            "url": "http://flow.example.test/api/app/bootstrap",
            "host": "flow.example.test",
            "dial_host": "host.docker.internal",
            "dial_port": 18099,
            "server_hostname": None,
            "timeout": 10,
            "context": None,
        },
    ]


def test_public_route_retries_startup_race_before_succeeding(tmp_path: Path, monkeypatch) -> None:
    waits: list[float] = []
    gateway = _gateway(tmp_path, FakeProcessExecutor(), sleep=waits.append)
    config = InstallConfig()
    certificate = tmp_path / ".ssl/cert.pem"
    certificate.parent.mkdir(parents=True)
    certificate.write_text("certificate", encoding="utf-8")
    attempts = 0

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def eventually_ready(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OSError("connection refused")
        return Response()

    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", eventually_ready)
    log = gateway.state_store.open_log("public-retry")

    gateway.verify_public_route(config, log=log)
    log.close()

    assert waits == [2.0, 2.0]
    assert attempts == 4
    assert "Waiting for public route readiness" in log.path.read_text(encoding="utf-8")


def test_public_route_budget_covers_first_development_frontend_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    gateway = _gateway(tmp_path, FakeProcessExecutor(), sleep=waits.append)
    config = InstallConfig()
    certificate = tmp_path / ".ssl/cert.pem"
    certificate.parent.mkdir(parents=True)
    certificate.write_text("certificate", encoding="utf-8")
    attempts = 0

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def ready_after_core_budget(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= gateway_module._READINESS_ATTEMPTS:
            raise OSError("frontend build still in progress")
        return Response()

    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", ready_after_core_budget)
    log = gateway.state_store.open_log("public-first-build")

    gateway.verify_public_route(config, log=log)
    log.close()

    assert attempts == gateway_module._READINESS_ATTEMPTS + 2
    assert waits == [gateway_module._READINESS_DELAY_SECONDS] * gateway_module._READINESS_ATTEMPTS


def test_provided_tls_is_validated_and_copied_to_traefik_runtime_paths(tmp_path: Path) -> None:
    source = tmp_path / "operator-tls"
    source.mkdir()
    (source / "site.crt").write_text("certificate", encoding="utf-8")
    (source / "site.key").write_text("private-key", encoding="utf-8")
    gateway = _gateway(tmp_path, TLSValidationExecutor())
    config = InstallConfig(
        tls_mode="provided",
        public_host="flow.example.test",
        certificate_path="operator-tls/site.crt",
        private_key_path="operator-tls/site.key",
    )
    log = gateway.state_store.open_log("provided-tls")

    gateway.ensure_tls(config, log=log)
    log.close()

    runtime_cert = tmp_path / ".ssl/cert.pem"
    runtime_key = tmp_path / ".ssl/key.pem"
    assert runtime_cert.read_text(encoding="utf-8") == "certificate"
    assert runtime_key.read_text(encoding="utf-8") == "private-key"
    assert runtime_cert.stat().st_mode & 0o777 == 0o644
    assert runtime_key.stat().st_mode & 0o777 == 0o600


def test_provided_tls_rejects_mismatched_private_key(tmp_path: Path) -> None:
    source = tmp_path / "operator-tls"
    source.mkdir()
    (source / "site.crt").write_text("certificate", encoding="utf-8")
    (source / "site.key").write_text("private-key", encoding="utf-8")
    gateway = _gateway(tmp_path, TLSValidationExecutor(matching=False))
    config = InstallConfig(
        tls_mode="provided",
        public_host="flow.example.test",
        certificate_path="operator-tls/site.crt",
        private_key_path="operator-tls/site.key",
    )
    log = gateway.state_store.open_log("mismatched-tls")

    with pytest.raises(InstallerFailure) as raised:
        gateway.ensure_tls(config, log=log)
    log.close()

    assert raised.value.code == "tls_material_invalid"


def test_default_certificate_paths_match_traefik_dynamic_configuration() -> None:
    config = InstallConfig()
    dynamic = (Path(__file__).resolve().parents[3] / "infra/traefik/fs.dynamic.yaml").read_text(
        encoding="utf-8"
    )

    assert f"/etc/traefik/certs/{Path(config.certificate_path).name}" in dynamic
    assert f"/etc/traefik/certs/{Path(config.private_key_path).name}" in dynamic


def test_ipv6_product_url_uses_bracketed_authority(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())

    assert gateway.product_url(InstallConfig(public_host="::1")) == "https://[::1]:8099"


def test_secret_step_writes_complete_core_env_and_preserves_operator_lines(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    env_path = tmp_path / ".env"
    env_path.write_text("# keep this comment\nOPENAI_API_KEY=\n", encoding="utf-8")
    log = gateway.state_store.open_log("core-env")

    gateway.ensure_secrets(InstallConfig(), log=log)

    log.close()
    values = gateway_module._read_env(env_path)
    assert values["FS_SECRET_KEY"] == "generated-secret"
    assert values["FS_BACKUP_ENCRYPTION_KEY"] == "generated-secret"
    assert values["FS_METRICS_TOKEN"] == "generated-secret"
    assert values["FS_WORKER_MCP_BEARER_TOKEN"] == "generated-secret"
    assert values["APP_PORT"] == "8099"
    assert values["FS_PUBLIC_BASE_URL"] == "https://localhost:8099"
    assert values["FS_AUTH_BOOTSTRAP_ENABLED"] == "1"
    assert values["FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY"] == "1"
    assert "FS_RELEASE_VERSION" not in values
    assert "# keep this comment" in env_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("profile", ("compact", "scalable"))
@pytest.mark.parametrize(
    ("host", "port", "url"),
    (
        ("cli.example.test", 443, "https://cli.example.test"),
        ("CLI.Example.TEST", 443, "https://cli.example.test"),
        ("::1", 443, "https://[::1]"),
        ("2001:0DB8:0000:0000:0000:0000:0000:0001", 443, "https://[2001:db8::1]"),
        ("localhost", 8099, "https://localhost:8099"),
    ),
)
def test_install_public_address_is_written_before_first_start(
    tmp_path: Path, profile: str, host: str, port: int, url: str
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    config = InstallConfig(
        environment="production", deployment_profile=profile, public_host=host, public_port=port
    )
    log = gateway.state_store.open_log("public-address")
    try:
        gateway.ensure_secrets(config, log=log)
    finally:
        log.close()

    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert values["FS_PUBLIC_BASE_URL"] == url
    assert values["APP_PORT"] == str(port)
    assert values["FS_AUTH_TRUSTED_ORIGINS"] == url
    assert values["FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY"] == (
        "1" if host in {"localhost", "::1"} else "0"
    )
    assert is_canonical_https_origin(values["FS_PUBLIC_BASE_URL"]) is True
    assert gateway.postcondition_holds("secrets", config) is True


def test_install_rejects_scoped_ipv6_before_writing_secrets(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    config = InstallConfig.model_construct(
        environment="production",
        public_host="fe80::1%eth0",
        public_port=443,
    )
    log = gateway.state_store.open_log("scoped-ipv6")
    try:
        with pytest.raises(ValueError, match="scope"):
            gateway.ensure_secrets(config, log=log)
    finally:
        log.close()

    assert not (tmp_path / ".env.prod").exists()


def test_scalable_secret_step_uses_shared_logs_artifact_root(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    config = InstallConfig(
        environment="production",
        deployment_profile="scalable",
        file_storage="local_fs",
    )
    log = gateway.state_store.open_log("scalable-local-artifacts")

    gateway.ensure_secrets(config, log=log)

    log.close()
    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert values["ARTIFACT_LOCAL_ROOT_DIR"] == "/app/logs/artifacts"


def test_compact_secret_step_uses_single_volume_artifact_root(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    config = InstallConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
    )
    log = gateway.state_store.open_log("compact-local-artifacts")

    gateway.ensure_secrets(config, log=log)

    log.close()
    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert values["ARTIFACT_LOCAL_ROOT_DIR"] == "/data/artifacts"


def test_secret_step_persists_only_controlled_isolation_overrides_and_candidate_paths(
    tmp_path: Path,
) -> None:
    approved = {
        "FS_COMPOSE_PROJECT_NAME": "fs-inst-controlled-test",
        "HTTP_PORT": "18080",
        "POSTGRES_HOST_PORT": "15432",
        "RABBITMQ_AMQP_HOST_PORT": "15672",
        "RABBITMQ_MANAGEMENT_HOST_PORT": "15673",
        "MAILPIT_SMTP_PORT": "11025",
        "MAILPIT_WEB_PORT": "18025",
        "MAILPIT_HTTP_PORT": "18026",
        "FRONTEND_HOST_PORT": "15173",
        "MCP_HOST_PORT": "18082",
        "SONAR_PORT": "19000",
        "SEAWEEDFS_S3_HOST_PORT": "18333",
        "SEAWEEDFS_MASTER_HOST_PORT": "19333",
        "FS_APP_IMAGE": "task-app:test",
        "FS_WORKER_IMAGE": "task-worker:test",
        "FS_CONTROL_IMAGE": "task-control:test",
        "MCP_IMAGE": "task-mcp:test",
        "FS_CLI_IMAGE": "task-cli:test",
        "POSTGRES_IMAGE": "task-postgres:test",
        "AWS_CA_BUNDLE": "/app/extensions/task-ca.crt",
    }
    forbidden = {
        "ARTIFACT_S3_ACCESS_KEY": "host-access-must-not-persist",
        "ARTIFACT_S3_SECRET_KEY": "host-secret-must-not-persist",
        "FS_BUILD_SHA": "0" * 40,
        "FS_APP_IMAGE_DIGEST": f"sha256:{'a' * 64}",
        "FS_WORKER_IMAGE_DIGEST": f"sha256:{'c' * 64}",
        "FS_CONTROL_IMAGE_DIGEST": f"sha256:{'d' * 64}",
        "MCP_IMAGE_DIGEST": f"sha256:{'b' * 64}",
        "FS_CANDIDATE_STATE_DIR": "/hostile/state",
        "FS_CANDIDATE_PROD_ENV": "/hostile/.env.prod",
    }
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        environment={**approved, **forbidden},
        secret_factory=lambda: "generated-secret",
    )
    config = InstallConfig(environment="production")
    log = gateway.state_store.open_log("controlled-overrides")

    gateway.ensure_configuration(config, log=log)
    gateway.ensure_secrets(config, log=log)
    log.close()

    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert {name: values[name] for name in approved} == approved
    assert values["FS_CANDIDATE_STATE_DIR"] == str(gateway.state_store.root.resolve())
    assert values["FS_CANDIDATE_PROD_ENV"] == str((tmp_path / ".env.prod").resolve())
    assert values["ARTIFACT_S3_ACCESS_KEY"] != forbidden["ARTIFACT_S3_ACCESS_KEY"]
    assert values["ARTIFACT_S3_SECRET_KEY"] != forbidden["ARTIFACT_S3_SECRET_KEY"]
    assert "FS_BUILD_SHA" not in values
    assert "FS_APP_IMAGE_DIGEST" not in values
    assert "FS_WORKER_IMAGE_DIGEST" not in values
    assert "FS_CONTROL_IMAGE_DIGEST" not in values
    assert "MCP_IMAGE_DIGEST" not in values
    compose_values = gateway.state_store.read_env_values(gateway.state_store.root / "compose.env")
    assert compose_values["FS_COMPOSE_PROJECT_NAME"] == approved["FS_COMPOSE_PROJECT_NAME"]
    assert "FS_PROJECTS_ROOT" not in compose_values
    assert "FS_TEST_PROJECT_ROOT" not in compose_values


@pytest.mark.parametrize("invalid_character", ("\n", "\r", "\0", "\t", "\x1b", "\x7f"))
@pytest.mark.parametrize("environment_name", ("HTTP_PORT", "FS_COMPOSE_PROJECT_NAME"))
@pytest.mark.parametrize(
    ("method_name", "expected_step"),
    (("ensure_configuration", "configuration"), ("ensure_secrets", "secrets")),
)
def test_persisted_bootstrap_values_reject_all_control_characters(
    tmp_path: Path,
    invalid_character: str,
    environment_name: str,
    method_name: str,
    expected_step: str,
) -> None:
    hostile_value = f"safe{invalid_character}FS_SECRET_KEY=leaked-value"
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        environment={environment_name: hostile_value},
    )
    log = gateway.state_store.open_log("invalid-controlled-override")

    _raises_callable_2798_1 = getattr(gateway, method_name)
    _raises_input_2798_2 = InstallConfig()
    with pytest.raises(InstallerFailure) as captured:
        _raises_callable_2798_1(_raises_input_2798_2, log=log)
    log.close()

    assert captured.value.code == "invalid_bootstrap_override"
    assert captured.value.step == expected_step
    assert hostile_value not in str(captured.value)
    assert "leaked-value" not in log.path.read_text(encoding="utf-8")
    compose_env = gateway.state_store.root / "compose.env"
    assert not compose_env.exists() or "leaked-value" not in compose_env.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("method_name", "expected_step"),
    (("ensure_configuration", "configuration"), ("ensure_secrets", "secrets")),
)
def test_persisted_bootstrap_rejects_invalid_compose_project_name(
    tmp_path: Path,
    method_name: str,
    expected_step: str,
) -> None:
    hostile_value = "Uppercase.project/name"
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        environment={"FS_COMPOSE_PROJECT_NAME": hostile_value},
    )
    log = gateway.state_store.open_log("invalid-project-identity")

    _raises_callable_2827_1 = getattr(gateway, method_name)
    _raises_input_2827_2 = InstallConfig()
    with pytest.raises(InstallerFailure) as captured:
        _raises_callable_2827_1(_raises_input_2827_2, log=log)
    log.close()

    assert captured.value.code == "invalid_bootstrap_override"
    assert captured.value.step == expected_step
    assert hostile_value not in str(captured.value)
    assert hostile_value not in log.path.read_text(encoding="utf-8")
    assert not (gateway.state_store.root / "compose.env").exists()


def test_non_loopback_install_creates_expiring_owner_only_bootstrap_token(
    tmp_path: Path, monkeypatch
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    config = InstallConfig(public_host="flow.example.test")
    log = gateway.state_store.open_log("bootstrap")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "ok", "auth": {"bootstrap_required": True}}).encode()

    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())

    gateway.ensure_secrets(config, log=log)
    bootstrap = gateway.verify_bootstrap(config, log=log)

    log.close()
    values = gateway_module._read_env(tmp_path / ".env")
    assert len(values["FS_AUTH_BOOTSTRAP_TOKEN"]) >= 43
    assert values["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"]
    assert (
        values["FS_AUTH_BOOTSTRAP_STATE_FILE"]
        == "/run/flow-steward-runtime/bootstrap-token-state.json"
    )
    assert values["FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY"] == "0"
    assert values["FS_PUBLIC_BIND_HOST"] == "0.0.0.0"
    assert bootstrap.bootstrap_required is True
    assert bootstrap.token_path == ".flow-steward/secret-output/bootstrap-token"
    token_file = tmp_path / bootstrap.token_path
    assert token_file.read_text(encoding="utf-8").strip() == values["FS_AUTH_BOOTSTRAP_TOKEN"]
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"
    assert values["FS_AUTH_BOOTSTRAP_TOKEN"] not in log.path.read_text(encoding="utf-8")


def test_loopback_install_uses_authoritative_bootstrap_token_state(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    log = gateway.state_store.open_log("loopback-bootstrap")

    gateway.ensure_secrets(InstallConfig(public_host="127.0.0.1"), log=log)

    log.close()
    values = gateway_module._read_env(tmp_path / ".env")
    assert len(values["FS_AUTH_BOOTSTRAP_TOKEN"]) >= 43
    assert values["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"]
    assert (
        values["FS_AUTH_BOOTSTRAP_STATE_FILE"]
        == "/run/flow-steward-runtime/bootstrap-token-state.json"
    )
    assert values["FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY"] == "0"
    assert values["FS_PUBLIC_BIND_HOST"] == "127.0.0.1"
    assert (tmp_path / ".flow-steward/secret-output/bootstrap-token").read_text(
        encoding="utf-8"
    ).strip() == values["FS_AUTH_BOOTSTRAP_TOKEN"]


def test_non_loopback_bootstrap_returns_absolute_token_path_for_external_state_root(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = _repo(tmp_path / "repo")
    state_root = tmp_path / "external-state"
    gateway = DockerInstallerGateway(
        repo_root=repo_root,
        state_store=InstallerStateStore(state_root),
        process=FakeProcessExecutor(),
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment={},
        sleep=lambda _seconds: None,
        secret_factory=lambda: "generated-secret",
    )
    config = InstallConfig(public_host="flow.example.test")
    log = gateway.state_store.open_log("bootstrap-external-state")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "ok", "auth": {"bootstrap_required": True}}).encode()

    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())

    gateway.ensure_secrets(config, log=log)
    bootstrap = gateway.verify_bootstrap(config, log=log)

    log.close()
    expected = state_root / "secret-output/bootstrap-token"
    assert bootstrap.token_path == str(expected)
    assert expected.is_file()
    assert oct(expected.stat().st_mode & 0o777) == "0o600"


def test_external_s3_imports_credentials_from_owner_only_file(tmp_path: Path) -> None:
    credentials = tmp_path / "s3-credentials.json"
    credentials.write_text(
        json.dumps({"access_key": "access", "secret_key": "very-secret"}),
        encoding="utf-8",
    )
    credentials.chmod(0o600)
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated")
    config = InstallConfig(
        file_storage="external_s3",
        s3_endpoint="https://objects.example.test",
        s3_bucket="flow-artifacts",
        s3_region="eu-test-1",
        s3_credentials_path=str(credentials),
    )
    log = gateway.state_store.open_log("external-s3")

    gateway.ensure_secrets(config, log=log)

    values = gateway_module._read_env(tmp_path / ".env")
    assert values["ARTIFACT_S3_ENDPOINT_URL"] == "https://objects.example.test"
    assert values["ARTIFACT_S3_BUCKET"] == "flow-artifacts"
    assert values["ARTIFACT_S3_REGION"] == "eu-test-1"
    assert values["ARTIFACT_S3_ACCESS_KEY"] == "access"
    assert values["ARTIFACT_S3_SECRET_KEY"] == "very-secret"
    gateway.verify_storage(config, log=log)
    log.close()
    storage_command = gateway.process.commands[-1]
    assert storage_command[-3:-1] == ("python", "-c")
    compile(storage_command[-1], "<storage-probe>", "exec")
    assert "access_key': 'access" not in " ".join(storage_command)
    assert "very-secret" not in " ".join(storage_command)
    assert "very-secret" not in log.path.read_text(encoding="utf-8")


def test_production_external_s3_requires_https() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        InstallConfig(
            environment="production",
            file_storage="external_s3",
            s3_endpoint="http://objects.example.test",
            s3_credentials_path="/run/secrets/s3.json",
        )
    with pytest.raises(ValueError, match="HTTPS"):
        InstallConfig(
            environment="production",
            file_storage="external_s3",
            s3_endpoint="ftp://objects.example.test",
            s3_credentials_path="/run/secrets/s3.json",
        )


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://user:password@objects.example.test",
        "https://objects.example.test?token=secret",
        "https://objects.example.test#secret",
        "https:///missing-host",
    ),
)
def test_external_s3_endpoint_rejects_credential_bearing_or_hostless_urls(endpoint: str) -> None:
    with pytest.raises(ValueError, match=r"hostname|credentials|query|fragment"):
        InstallConfig(
            file_storage="external_s3",
            s3_endpoint=endpoint,
            s3_credentials_path="/run/secrets/s3.json",
        )


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://objects.example.test:abc",
        "https://objects.example.test:99999",
        "https://objects.example.test%2fattack",
        "https://objects.example.test\\attack",
        "https://objects.example.test/\nattack",
    ),
)
def test_external_s3_endpoint_rejects_malformed_authorities(endpoint: str) -> None:
    with pytest.raises(ValueError, match="S3 endpoint"):
        InstallConfig(
            file_storage="external_s3",
            s3_endpoint=endpoint,
            s3_credentials_path="/run/secrets/s3.json",
        )


def test_bundled_storage_discards_hidden_external_s3_values() -> None:
    config = InstallConfig(
        file_storage="bundled",
        s3_endpoint="https://user:password@objects.example.test",
        s3_bucket="secret-bucket",
        s3_region="secret-region",
        s3_credentials_path="/run/secrets/s3.json",
        allow_insecure_s3_http=True,
    )

    assert config.s3_endpoint == ""
    assert config.s3_credentials_path == ""
    assert "user:password" not in config.model_dump_json()


def test_development_external_s3_http_requires_explicit_unsafe_opt_in() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        InstallConfig(
            file_storage="external_s3",
            s3_endpoint="http://minio.test:9000",
            s3_credentials_path="/run/secrets/s3.json",
        )

    config = InstallConfig(
        file_storage="external_s3",
        s3_endpoint="http://minio.test:9000",
        s3_credentials_path="/run/secrets/s3.json",
        allow_insecure_s3_http=True,
    )
    assert config.allow_insecure_s3_http is True


def test_production_compose_authenticates_bundled_seaweedfs() -> None:
    compose = (Path("infra/docker/docker-compose.prod.yaml")).read_text(encoding="utf-8")

    # AC17-R116: the server reads a copy handed to it by a root step, not the
    # operator's own file -- the image drops to its own user and could not read a
    # 0600 file owned by anyone else. What matters here is unchanged: it is given an
    # identity file at all, and the only source of that file is the operator's.
    assert "-s3.config=/config/seaweedfs-s3.json" in compose
    assert "seaweedfs-s3.json:/run/flow-steward-runtime/seaweedfs-s3.json:ro" in compose
    assert "artifact-storage:" in compose
    assert "internal: true" in compose


def test_development_compose_authenticates_reachable_bundled_seaweedfs() -> None:
    compose = (Path("infra/docker/docker-compose.yaml")).read_text(encoding="utf-8")

    assert "container_name: fs-seaweedfs" not in compose
    assert "-s3.ip.bind=0.0.0.0" in compose
    assert "-s3.config=/config/seaweedfs-s3.json" in compose
    assert "seaweedfs-s3.json:/run/flow-steward-runtime/seaweedfs-s3.json:ro" in compose


def test_development_bundled_storage_writes_owner_only_identity_config(
    tmp_path: Path,
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-key")
    log = gateway.state_store.open_log("seaweed-development-identity")

    gateway.ensure_secrets(InstallConfig(environment="development"), log=log)
    log.close()

    identity_path = tmp_path / ".flow-steward/runtime/seaweedfs-s3.json"
    payload = json.loads(identity_path.read_text(encoding="utf-8"))
    credential = payload["identities"][0]["credentials"][0]
    assert credential == {"accessKey": "generated-key", "secretKey": "generated-key"}
    assert oct(identity_path.stat().st_mode & 0o777) == "0o600"
    env = gateway_module._read_env(tmp_path / ".env")
    assert env["ARTIFACT_S3_ACCESS_KEY"] == "generated-key"
    assert env["ARTIFACT_S3_SECRET_KEY"] == "generated-key"


def test_production_bundled_storage_writes_owner_only_identity_config(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-key")
    log = gateway.state_store.open_log("seaweed-identity")

    gateway.ensure_secrets(InstallConfig(environment="production"), log=log)
    log.close()

    identity_path = tmp_path / ".flow-steward/runtime/seaweedfs-s3.json"
    payload = json.loads(identity_path.read_text(encoding="utf-8"))
    credential = payload["identities"][0]["credentials"][0]
    assert credential == {"accessKey": "generated-key", "secretKey": "generated-key"}
    assert oct(identity_path.stat().st_mode & 0o777) == "0o600"
    env = gateway_module._read_env(tmp_path / ".env.prod")
    assert env["ARTIFACT_S3_ACCESS_KEY"] == "generated-key"
    assert env["ARTIFACT_S3_SECRET_KEY"] == "generated-key"


def test_production_external_install_preprovisions_distinct_bundled_identity(
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "external-s3.json"
    credentials.write_text(
        json.dumps({"access_key": "external-access", "secret_key": "external-secret"}),
        encoding="utf-8",
    )
    credentials.chmod(0o600)
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "bundled-key")
    config = InstallConfig(
        environment="production",
        file_storage="external_s3",
        s3_endpoint="https://objects.example.test",
        s3_bucket="artifacts",
        s3_region="eu-test-1",
        s3_credentials_path=str(credentials),
    )
    log = gateway.state_store.open_log("external-with-bundled-identity")

    gateway.ensure_secrets(config, log=log)
    log.close()

    identity_path = tmp_path / ".flow-steward/runtime/seaweedfs-s3.json"
    bundled = json.loads(identity_path.read_text(encoding="utf-8"))
    assert bundled["identities"][0]["credentials"][0] == {
        "accessKey": "bundled-key",
        "secretKey": "bundled-key",
    }
    env = gateway_module._read_env(tmp_path / ".env.prod")
    assert env["ARTIFACT_S3_ACCESS_KEY"] == "external-access"
    assert env["ARTIFACT_S3_SECRET_KEY"] == "external-secret"


@pytest.mark.parametrize(
    ("read_only", "expected_calls"),
    [
        (False, ["head", "put", "delete"]),
        (True, ["head"]),
    ],
)
def test_development_bundled_storage_probe_uses_generated_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
    expected_calls: list[str],
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    log = gateway.state_store.open_log("bundled-storage")
    calls: list[str] = []

    class StorageClient:
        def head_bucket(self, **_kwargs):
            calls.append("head")

        def put_object(self, **_kwargs):
            calls.append("put")

        def delete_object(self, **_kwargs):
            calls.append("delete")

    import boto3

    observed_options: dict[str, object] = {}

    def fake_client(_service: str, **options: object) -> StorageClient:
        observed_options.update(options)
        return StorageClient()

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setenv("ARTIFACT_S3_BUCKET", "flow-steward-artifacts")
    monkeypatch.setenv("ARTIFACT_S3_ENDPOINT_URL", "http://seaweedfs:8333")
    monkeypatch.setenv("ARTIFACT_S3_ACCESS_KEY", "bundled-access")
    monkeypatch.setenv("ARTIFACT_S3_SECRET_KEY", "bundled-secret")

    gateway.verify_storage(InstallConfig(), log=log, read_only=read_only)
    log.close()
    command = process.commands[-1]
    script = process.commands[-1][-1]
    exec(compile(script, "<bundled-storage-probe>", "exec"), {})

    assert command[-4:-2] == ("fs-app", "python")
    assert "fs-cli" not in command
    assert calls == expected_calls
    assert f"create_bucket={'False' if read_only else 'True'}" in script
    assert observed_options["aws_access_key_id"] == "bundled-access"
    assert (
        observed_options["aws_secret_access_key"]
        == "bundled-secret"  # pragma: allowlist secret - fixture literal
    )  # pragma: allowlist secret -- test fixture
    assert "config" not in observed_options


def test_production_bundled_storage_probe_uses_generated_credentials(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    log = gateway.state_store.open_log("production-bundled-storage")

    gateway.verify_storage(InstallConfig(environment="production"), log=log, read_only=True)
    log.close()

    script = process.commands[-1][-1]
    assert "aws_access_key_id=os.environ['ARTIFACT_S3_ACCESS_KEY']" in script
    assert "aws_secret_access_key=os.environ['ARTIFACT_S3_SECRET_KEY']" in script
    assert "signature_version=UNSIGNED" not in script


@pytest.mark.parametrize("read_only", (False, True))
def test_local_fs_storage_probe_uses_persistent_artifact_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    artifact_root = tmp_path / "artifacts"
    if read_only:
        artifact_root.mkdir()
    monkeypatch.setenv("ARTIFACT_LOCAL_ROOT_DIR", str(artifact_root))
    log = gateway.state_store.open_log("local-fs-storage")

    gateway.verify_storage(
        InstallConfig(
            environment="production",
            deployment_profile="compact",
            file_storage="local_fs",
        ),
        log=log,
        read_only=read_only,
    )
    log.close()

    command = process.commands[-1]
    script = command[-1]
    exec(compile(script, "<local-fs-storage-probe>", "exec"), {})
    assert command[-4:-2] == ("compact-runtime", "python")
    assert "ARTIFACT_LOCAL_ROOT_DIR" in script
    assert "boto3" not in script
    assert artifact_root.is_dir()
    assert list(artifact_root.iterdir()) == []


def test_storage_probe_fails_closed_without_retrying_app_exec_137(tmp_path: Path) -> None:
    process = FailingStorageProbeExecutor()
    sleeps: list[float] = []
    gateway = _gateway(tmp_path, process, sleep=sleeps.append)
    log = gateway.state_store.open_log("storage-probe-137")

    _raises_input_3236_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        gateway.verify_storage(_raises_input_3236_1, log=log, read_only=True)
    log.close()

    assert raised.value.code == "storage_verification_failed"
    assert raised.value.cause == "exit code 137"
    assert process.storage_attempts == 1
    assert sleeps == []


def test_cli_readiness_probe_executes_constant_script_in_exact_cli_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, process, config, _helper_command, _ps_command, _inspect_command = (
        _configure_runtime_contract(
            tmp_path,
            monkeypatch,
            environment="production",
            cli_image="fs-inst-production-cli:acceptance",
        )
    )
    credentials_path = tmp_path / "external-s3.json"
    credential_secret = "credential-content-must-stay-private"
    credentials_path.write_text(
        json.dumps({"secret_access_key": credential_secret}),
        encoding="utf-8",
    )
    config = config.model_copy(
        update={
            "file_storage": "external_s3",
            "s3_endpoint": "https://objects.example.test",
            "s3_credentials_path": str(credentials_path),
        }
    )
    payload = {
        "checks": [
            {
                "id": "database",
                "category": "core",
                "title": "Database",
                "status": "pass",
                "detail": "Database is ready.",
                "remediation": "No action required.",
            }
        ]
    }
    context = gateway._cli_runtime_context(config, log=None)
    command = (
        *context.compose_prefix,
        "exec",
        "-T",
        "fs-cli",
        "python",
        "-c",
        gateway_module._CLI_READINESS_SCRIPT,
    )
    process.outcomes[command] = ProcessOutcome(
        command=command,
        returncode=0,
        stdout=json.dumps(payload, separators=(",", ":")) + "\n",
    )
    process.invocations.clear()

    assert gateway.probe_cli_readiness(config) == payload
    assert process.invocations[-1] == (
        command,
        {
            "cwd": tmp_path.resolve(),
            "environment": context.environment,
            "input_text": config.model_dump_json(),
            "separate_stderr": True,
        },
    )
    assert "fs-app" not in command
    assert "probe_dispatch_queue_depth" not in gateway_module._CLI_READINESS_SCRIPT
    assert (
        "from core.entrypoints.cli.lifecycle_runtime import LifecycleCLIRuntime"
        in gateway_module._CLI_READINESS_SCRIPT
    )
    assert "core.entrypoints.cli.runtime" not in gateway_module._CLI_READINESS_SCRIPT
    assert credential_secret not in " ".join(command)
    assert credential_secret not in json.dumps(context.environment)
    assert credential_secret not in config.model_dump_json()
    assert credential_secret not in json.dumps(payload)
    compile(gateway_module._CLI_READINESS_SCRIPT, "<cli-readiness>", "exec")


def _readiness_payload(check_id: str, status: str) -> dict[str, object]:
    return {
        "checks": [
            {
                "id": check_id,
                "category": "core",
                "title": check_id.replace("_", " ").title(),
                "status": status,
                "detail": f"{check_id} is {status}.",
                "remediation": "Retry." if status == "fail" else "No action required.",
            }
        ]
    }


def test_real_installer_composition_bounds_initial_and_remaining_context_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.entrypoints.cli import installer
    from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

    gateway, process, config, *_ = _configure_runtime_contract(tmp_path, monkeypatch)
    context = gateway._cli_runtime_context(config, log=None)
    command = (
        *context.compose_prefix,
        "exec",
        "-T",
        "fs-cli",
        "python",
        "-c",
        gateway_module._CLI_READINESS_SCRIPT,
    )
    process.outcomes[command] = ProcessOutcome(
        command=command,
        returncode=0,
        stdout=json.dumps(_readiness_payload("database", "pass")) + "\n",
    )
    LifecycleConfigurationStore(tmp_path, state_root=gateway.state_store.root).write_active(config)
    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(gateway.state_store.root))
    monkeypatch.setattr(installer, "DockerInstallerGateway", lambda **kwargs: gateway)
    now = [0.0]
    monkeypatch.setattr(gateway_health, "monotonic", lambda: now[0], raising=False)
    run = process.run

    def bounded_run(command, **kwargs):
        if kwargs.get("timeout_seconds") is None:
            raise TimeoutError("readiness subprocess was unbounded")
        now[0] += 1.0
        return run(command, **kwargs)

    process.run = bounded_run
    process.invocations.clear()
    service = installer._create_service()
    assert service.readiness_service.check().core_failed == 0
    assert [kwargs["timeout_seconds"] for _, kwargs in process.invocations] == [60.0, 59.0, 58.0]
    process.invocations.clear()
    assert service.readiness_reprobe(7.0).core_failed == 0
    assert [kwargs["timeout_seconds"] for _, kwargs in process.invocations] == [7.0, 6.0, 5.0]


@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_bounded_readiness_clips_retry_sleep_and_refuses_late_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    gateway, process, config, *_ = _configure_runtime_contract(
        tmp_path, monkeypatch, environment="production", file_storage="local_fs"
    )
    config = config.model_copy(update={"deployment_profile": profile})
    now = [0.0]
    sleeps = []
    monkeypatch.setattr(gateway_health, "monotonic", lambda: now[0])

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    gateway._sleep = sleep
    run = process.run
    executions = []

    def timed_run(command, **kwargs):
        assert kwargs["timeout_seconds"] > 0
        now[0] += 1.0
        if "exec" not in command:
            return run(command, **kwargs)
        executions.append(kwargs["timeout_seconds"])
        payload = _readiness_payload(
            "queue_worker_health", "fail" if len(executions) == 1 else "pass"
        )
        return ProcessOutcome(
            command=tuple(command), returncode=0, stdout=json.dumps(payload) + "\n"
        )

    process.run = timed_run
    assert (
        gateway.probe_cli_readiness(config, timeout_seconds=3.5) == _CLI_READINESS_FAILURE_PAYLOAD
    )
    assert now[0] <= 4.0
    if profile == "compact":
        assert executions == [3.5, 0.5]
        assert sleeps == [2.0]
    else:
        assert executions == [1.5]
        assert sleeps == [0.5]


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_bounded_readiness_refuses_invalid_budget_before_processes(
    tmp_path: Path, timeout: float
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    assert (
        gateway.probe_cli_readiness(InstallConfig(), timeout_seconds=timeout)
        == _CLI_READINESS_FAILURE_PAYLOAD
    )
    assert gateway.process.commands == []


@pytest.mark.parametrize("check_id", ["security_posture", "workflow_knowledge_sync", "database"])
def test_bounded_compact_preserves_nontransient_blocker_for_installer_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check_id: str
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    now = [0.0]
    monkeypatch.setattr(gateway_health, "monotonic", lambda: now[0])
    payload = _readiness_payload(check_id, "fail")
    calls = []
    sleeps = []

    def run(command, **kwargs):
        calls.append(kwargs["timeout_seconds"])
        now[0] += 1.0
        return ProcessOutcome(
            command=tuple(command), returncode=0, stdout=json.dumps(payload) + "\n"
        )

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    process.run = run
    gateway._sleep = sleep
    config = InstallConfig(
        deployment_profile="compact", environment="production", file_storage="local_fs"
    )
    assert gateway.probe_cli_readiness(config, timeout_seconds=2.0) == payload
    assert calls == [2.0]
    assert sleeps == []


def _queued_readiness_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[dict[str, object]],
) -> tuple[
    DockerInstallerGateway,
    FakeProcessExecutor,
    DeploymentConfig,
    tuple[str, ...],
    list[float],
]:
    gateway, process, config, _helper_command, _ps_command, _inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch)
    )
    sleeps: list[float] = []
    gateway._sleep = sleeps.append
    context = gateway._cli_runtime_context(config, log=None)
    command = (
        *context.compose_prefix,
        "exec",
        "-T",
        "fs-cli",
        "python",
        "-c",
        gateway_module._CLI_READINESS_SCRIPT,
    )
    process.queued_outcomes[command] = [
        ProcessOutcome(command=command, returncode=0, stdout=json.dumps(payload) + "\n")
        for payload in payloads
    ]
    process.invocations.clear()
    return gateway, process, config, command, sleeps


def test_cli_readiness_probe_retries_only_queue_worker_failure_and_returns_fresh_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_failure = _readiness_payload("queue_worker_health", "fail")
    ready = _readiness_payload("queue_worker_health", "pass")
    gateway, process, config, command, sleeps = _queued_readiness_probe(
        tmp_path, monkeypatch, [queue_failure, ready]
    )

    assert gateway.probe_cli_readiness(config) == ready
    assert [invocation[0] for invocation in process.invocations].count(command) == 2
    assert sleeps == [gateway_module._READINESS_DELAY_SECONDS]


def test_cli_readiness_probe_retries_candidate_startup_failures_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup_ids = (
        "api_ready",
        "job_dispatch",
        "mcp_auth",
        "queue_worker_health",
        "rabbitmq_socket",
    )
    starting = {
        "checks": [_readiness_payload(check_id, "fail")["checks"][0] for check_id in startup_ids]
    }
    ready = {
        "checks": [_readiness_payload(check_id, "pass")["checks"][0] for check_id in startup_ids]
    }
    gateway, process, config, command, sleeps = _queued_readiness_probe(
        tmp_path,
        monkeypatch,
        [starting, ready],
    )

    assert gateway.probe_cli_readiness(config) == ready
    assert [invocation[0] for invocation in process.invocations].count(command) == 2
    assert sleeps == [gateway_module._READINESS_DELAY_SECONDS]


def test_cli_readiness_probe_does_not_retry_startup_failure_with_env_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = {
        "checks": [
            _readiness_payload("api_ready", "fail")["checks"][0],
            _readiness_payload("env_duplicate_keys", "fail")["checks"][0],
        ]
    }
    gateway, process, config, command, sleeps = _queued_readiness_probe(
        tmp_path,
        monkeypatch,
        [blocked, _readiness_payload("api_ready", "pass")],
    )

    assert gateway.probe_cli_readiness(config) == blocked
    assert [invocation[0] for invocation in process.invocations].count(command) == 1
    assert sleeps == []


def test_cli_readiness_probe_retries_queue_only_core_failure_with_optional_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_failure = _readiness_payload("queue_worker_health", "fail")
    optional_failure = _readiness_payload("provider_cli_auth", "fail")
    optional_failure["checks"][0]["category"] = "runtime"
    combined = {"checks": [*queue_failure["checks"], *optional_failure["checks"]]}
    ready = {
        "checks": [
            *_readiness_payload("queue_worker_health", "pass")["checks"],
            *optional_failure["checks"],
        ]
    }
    gateway, process, config, command, sleeps = _queued_readiness_probe(
        tmp_path, monkeypatch, [combined, ready]
    )

    assert gateway.probe_cli_readiness(config) == ready
    assert [invocation[0] for invocation in process.invocations].count(command) == 2
    assert sleeps == [gateway_module._READINESS_DELAY_SECONDS]


def test_cli_readiness_probe_returns_unrelated_valid_blocker_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = _readiness_payload("database", "fail")
    gateway, process, config, command, sleeps = _queued_readiness_probe(
        tmp_path, monkeypatch, [blocker, _readiness_payload("database", "pass")]
    )

    assert gateway.probe_cli_readiness(config) == blocker
    assert [invocation[0] for invocation in process.invocations].count(command) == 1
    assert sleeps == []


def test_cli_readiness_probe_does_not_retry_mixed_queue_and_core_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mixed = {
        "checks": [
            *_readiness_payload("queue_worker_health", "fail")["checks"],
            *_readiness_payload("database", "fail")["checks"],
        ]
    }
    gateway, process, config, command, sleeps = _queued_readiness_probe(
        tmp_path, monkeypatch, [mixed, _readiness_payload("database", "pass")]
    )

    assert gateway.probe_cli_readiness(config) == mixed
    assert [invocation[0] for invocation in process.invocations].count(command) == 1
    assert sleeps == []


def test_cli_readiness_probe_returns_final_queue_failure_after_bounded_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_health, "_READINESS_ATTEMPTS", 3)
    failures = [_readiness_payload("queue_worker_health", "fail") for _ in range(3)]
    gateway, process, config, command, sleeps = _queued_readiness_probe(
        tmp_path, monkeypatch, failures
    )

    assert gateway.probe_cli_readiness(config) == failures[-1]
    assert [invocation[0] for invocation in process.invocations].count(command) == 3
    assert sleeps == [gateway_module._READINESS_DELAY_SECONDS] * 2


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (7, "top-secret-output\n", "top-secret-error\n"),
        (0, "", ""),
        (0, "\n", ""),
        (0, '{"checks":[]}\nextra\n', ""),
        (0, "not-json\n", ""),
        (0, "[]\n", ""),
        (0, "{}\n", ""),
        (0, '{"checks":[]}\n', ""),
        (0, '{"checks":["bad"]}\n', ""),
        (
            0,
            '{"checks":[{"id":"database","category":"core","title":"Database",'
            '"status":"unknown","detail":"ready","remediation":"none"}]}\n',
            "",
        ),
        (
            0,
            '{"checks":[{"id":"","category":"core","title":"Database",'
            '"status":"pass","detail":"ready","remediation":"none"}]}\n',
            "",
        ),
        (
            0,
            '{"checks":[{"id":"database","category":"core","title":"Database",'
            '"status":"pass","detail":"ready","remediation":"none","extra":"bad"}]}\n',
            "",
        ),
        (
            0,
            json.dumps(
                {
                    "checks": [
                        {
                            "id": "database",
                            "category": "core",
                            "title": "Database",
                            "status": "pass",
                            "detail": "x" * (gateway_module._MAX_CLI_READINESS_FIELD_LENGTH + 1),
                            "remediation": "none",
                        }
                    ]
                },
                separators=(",", ":"),
            )
            + "\n",
            "",
        ),
        (0, '{"checks":[{"id":"database","status":"pass"}]}\n', " \t\n"),
    ],
    ids=(
        "nonzero",
        "blank",
        "blank-line",
        "multiline",
        "malformed-json",
        "not-object",
        "missing-checks",
        "empty-checks",
        "non-object-check",
        "invalid-status",
        "missing-id",
        "extra-check-field",
        "oversized-check-field",
        "stderr",
    ),
)
def test_cli_readiness_probe_fails_closed_without_forwarding_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    stderr: str,
) -> None:
    gateway, process, config, _helper_command, _ps_command, _inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch)
    )
    forwarded: list[str] = []
    gateway._technical_output_sink = forwarded.append
    context = gateway._cli_runtime_context(config, log=None)
    command = (
        *context.compose_prefix,
        "exec",
        "-T",
        "fs-cli",
        "python",
        "-c",
        gateway_module._CLI_READINESS_SCRIPT,
    )
    process.outcomes[command] = ProcessOutcome(
        command=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )

    assert gateway.probe_cli_readiness(config) == _CLI_READINESS_FAILURE_PAYLOAD
    assert forwarded == []


def test_cli_readiness_probe_sanitizes_context_and_process_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, process, config, _helper_command, _ps_command, _inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch)
    )
    (tmp_path / "VERSION").unlink()

    assert gateway.probe_cli_readiness(config) == _CLI_READINESS_FAILURE_PAYLOAD

    (tmp_path / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    original_run = process.run

    def process_failure(command, **kwargs):
        if "exec" in command:
            raise OSError("top-secret process failure")
        return original_run(command, **kwargs)

    monkeypatch.setattr(process, "run", process_failure)

    assert gateway.probe_cli_readiness(config) == _CLI_READINESS_FAILURE_PAYLOAD


def test_custom_compose_identity_is_used_for_inspection_and_mutation(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "FS_COMPOSE_PROJECT_NAME=flow-steward-customer\n"
        "FS_SECRET_KEY=x\nENCRYPTION_KEY=y\nFS_BACKUP_ENCRYPTION_KEY=z\n",
        encoding="utf-8",
    )
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)

    evidence = gateway.inspect()
    log = gateway.state_store.open_log("custom-project")
    gateway.start_core(InstallConfig(), log=log)
    log.close()

    assert evidence.project_identity == "flow-steward-customer"
    assert any(
        command[:4] == ("docker", "compose", "-p", "flow-steward-customer")
        for command in process.commands
    )


def test_nested_encrypted_backup_is_detected(tmp_path: Path) -> None:
    backup = tmp_path / "backups/2026/07/snapshot.enc"
    backup.parent.mkdir(parents=True)
    backup.write_text("encrypted", encoding="utf-8")
    gateway = _gateway(tmp_path, FakeProcessExecutor())

    evidence = gateway.inspect()

    assert evidence.protected_backups_exist is True


def test_production_compose_declares_opt_in_bundled_storage() -> None:
    compose = (
        Path(__file__).resolve().parents[3] / "infra/docker/docker-compose.prod.yaml"
    ).read_text(encoding="utf-8")

    assert "  seaweedfs:" in compose
    assert 'profiles: ["storage-seaweedfs"]' in compose
    assert "  seaweedfs-data:" in compose


@pytest.mark.parametrize("installer_state", [None, "exited", "running"])
def test_health_requires_the_extension_installer(
    tmp_path: Path, installer_state: str | None
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig()
    rows = [
        {"Service": service, "State": "running", "Health": "healthy"}
        for service in (
            "traefik",
            "rabbitmq-fs",
            "pg-fs",
            "fs-mcp",
            "fs-app",
            "fs-worker",
            "control-plane",
            "seaweedfs",
        )
    ]
    if installer_state is not None:
        rows.append({"Service": "fs-extension-installer", "State": installer_state, "Health": ""})
    command = (*gateway._compose_prefix(config), "ps", "--format", "json")
    gateway.process.outcomes[command] = ProcessOutcome(
        command=command, returncode=0, stdout=json.dumps(rows)
    )
    log = gateway.state_store.open_log("installer-health")
    try:
        if installer_state == "running":
            gateway.verify_health(config, log=log)
        else:
            with pytest.raises(InstallerFailure) as raised:
                gateway.verify_health(config, log=log)
            assert raised.value.code == "container_unhealthy"
            assert "fs-extension-installer" in raised.value.cause
    finally:
        log.close()


def test_health_rejects_non_running_or_unhealthy_core_service(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig()
    command = (*gateway._compose_prefix(config), "ps", "--format", "json")
    gateway.process.outcomes[command] = ProcessOutcome(
        command=command,
        returncode=0,
        stdout=json.dumps(
            [
                {"Service": "pg-fs", "State": "running", "Health": "healthy"},
                {"Service": "fs-app", "State": "exited", "Health": ""},
            ]
        ),
    )
    log = gateway.state_store.open_log("health")

    with pytest.raises(InstallerFailure) as raised:
        gateway.verify_health(config, log=log)

    log.close()
    assert raised.value.code == "container_unhealthy"
    assert "fs-app" in raised.value.cause


def test_bootstrap_probe_requires_structured_application_state(tmp_path: Path, monkeypatch) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "ok", "auth": {"bootstrap_required": True}}).encode()

    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    log = gateway.state_store.open_log("bootstrap-probe")

    result = gateway.verify_bootstrap(config, log=log)

    assert result.bootstrap_required is True
    assert result.token_path is None

    log.close()


def test_bootstrap_handoff_is_issued_on_a_loopback_host(tmp_path: Path, monkeypatch) -> None:
    """AC17-R93: a localhost installation was left with an unusable mandatory token.

    The handoff used to be skipped on a loopback host, on the assumption that a
    loopback request may bootstrap without one. It may not: the installer always
    writes FS_AUTH_BOOTSTRAP_STATE_FILE, and bootstrap_request_allowed() returns on
    that state file before reaching the localhost allowance. The token was minted,
    required, and handed to nobody.
    """
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig(public_host="localhost")
    expiry = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_env_values(tmp_path / ".env", {"FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT": expiry})
    token_file = gateway.state_store.root / "secret-output/bootstrap-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("setup-token-value", encoding="utf-8")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"auth": {"bootstrap_required": True}}).encode()

    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", lambda *a, **k: Response())
    log = gateway.state_store.open_log("loopback-bootstrap")

    result = gateway.verify_bootstrap(config, log=log)
    log.close()

    assert result.bootstrap_required is True
    assert result.token == "setup-token-value"
    assert result.token_path is not None
    assert result.token_expired is False


def test_bootstrap_probe_retries_until_application_state_is_ready(
    tmp_path: Path, monkeypatch
) -> None:
    waits: list[float] = []
    gateway = _gateway(tmp_path, FakeProcessExecutor(), sleep=waits.append)
    config = InstallConfig()
    attempts = 0

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"auth": {"bootstrap_required": True}}).encode()

    def eventually_ready(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("connection refused")
        return Response()

    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", eventually_ready)
    log = gateway.state_store.open_log("bootstrap-retry")

    result = gateway.verify_bootstrap(config, log=log)
    log.close()

    assert result.bootstrap_required is True
    assert waits == [2.0]


def test_fresh_bootstrap_probe_rejects_false_state(tmp_path: Path, monkeypatch) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "ok", "auth": {"bootstrap_required": False}}).encode()

    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", lambda *_a, **_kw: Response())
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    log = gateway.state_store.open_log("bootstrap-false")

    with pytest.raises(InstallerFailure) as raised:
        gateway.verify_bootstrap(config, log=log)

    log.close()
    assert raised.value.code == "bootstrap_state_unavailable"


def test_installed_repeat_accepts_completed_bootstrap_state(tmp_path: Path, monkeypatch) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "ok", "auth": {"bootstrap_required": False}}).encode()

    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", lambda *_a, **_kw: Response())
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    log = gateway.state_store.open_log("bootstrap-complete")

    result = gateway.verify_bootstrap(config, log=log, require_first_admin=False)
    assert result.bootstrap_required is False
    assert result.token_path is None
    log.close()


def test_expired_non_loopback_token_returns_reissue_state(tmp_path: Path, monkeypatch) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig(public_host="flow.example.test")
    (tmp_path / ".env").write_text(
        "FS_AUTH_BOOTSTRAP_TOKEN=expired\n"
        "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT=2020-01-01T00:00:00+00:00\n",
        encoding="utf-8",
    )
    token_path = tmp_path / ".flow-steward/secret-output/bootstrap-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("expired\n", encoding="utf-8")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"auth": {"bootstrap_required": True}}).encode()

    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", lambda *_a, **_kw: Response())
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    log = gateway.state_store.open_log("expired-bootstrap")

    result = gateway.verify_bootstrap(config, log=log, require_first_admin=False)
    log.close()

    assert result.bootstrap_required is True
    assert result.token_expired is True
    assert result.token_path is None


def test_ensure_secrets_replaces_a_setup_token_whose_hour_is_over(tmp_path: Path) -> None:
    # AC17-R49: .env.prod sits at the repo root, outside every volume, so `down -v` plus a
    # deleted state directory still leaves the old token and the old expiry behind. Reuse used
    # to ask only whether an expiry string existed, never whether it had passed, so a fresh
    # install handed back a token that had already died and printed "within 60 minutes".
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    config = InstallConfig(public_host="flow.example.test")
    (tmp_path / ".env").write_text(
        "FS_AUTH_BOOTSTRAP_TOKEN=token-from-the-previous-installation\n"
        "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT=2020-01-01T00:00:00+00:00\n",
        encoding="utf-8",
    )
    log = gateway.state_store.open_log("stale-bootstrap-token")

    gateway.ensure_secrets(config, log=log)
    log.close()

    values = gateway_module._read_env(tmp_path / ".env")
    assert values["FS_AUTH_BOOTSTRAP_TOKEN"] != "token-from-the-previous-installation"
    assert gateway_module._future_timestamp(values["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"])


def test_a_live_token_from_another_installation_is_still_replaced(tmp_path: Path) -> None:
    # AC17-R49, strict half, inside the original hour. The expiry is 42 minutes away, so the
    # time check alone would reuse this value. Nothing in the state directory says this
    # installation minted it, which is what an inherited `.env.prod` looks like, so it goes.
    # A genuine resume is covered by test_a_resume_keeps_the_token_its_own_installation_minted.
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    config = InstallConfig(public_host="flow.example.test")
    alive = (datetime.now(UTC) + timedelta(minutes=42)).isoformat()
    (tmp_path / ".env").write_text(
        f"FS_AUTH_BOOTSTRAP_TOKEN=inherited-token\nFS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT={alive}\n",
        encoding="utf-8",
    )
    log = gateway.state_store.open_log("live-bootstrap-token")

    gateway.ensure_secrets(config, log=log)
    log.close()

    values = gateway_module._read_env(tmp_path / ".env")
    assert values["FS_AUTH_BOOTSTRAP_TOKEN"] != "inherited-token"
    assert values["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"] != alive
    assert gateway_module._future_timestamp(values["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"])


def test_secrets_postcondition_fails_for_an_expired_setup_token(tmp_path: Path) -> None:
    # Otherwise a resumed install skips the secrets step entirely and the dead token stays.
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    config = InstallConfig(public_host="flow.example.test")
    log = gateway.state_store.open_log("postcondition-bootstrap-token")
    gateway.ensure_secrets(config, log=log)
    log.close()
    assert gateway.postcondition_holds("secrets", config) is True

    gateway.state_store.update_env_values(
        tmp_path / ".env",
        {"FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT": "2020-01-01T00:00:00+00:00"},
    )

    assert gateway.postcondition_holds("secrets", config) is False


@pytest.mark.parametrize(
    ("profile", "service"),
    (("compact", "compact-runtime"), ("scalable", "fs-app")),
)
def test_reissue_bootstrap_token_mints_and_restarts_the_profile_application_service(
    tmp_path: Path, profile: str, service: str
) -> None:
    # AC17-R50. The Compact runtime service is `compact-runtime`; `fs-app` does not exist in
    # that compose set, which is the second reason the CLI command could not have served here:
    # its activation step recreates `fs-app` unconditionally.
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, secret_factory=lambda: "generated-secret")
    config = InstallConfig(
        public_host="flow.example.test",
        deployment_profile=profile,
        environment="production",
    )
    env_path = tmp_path / ".env.prod"
    log = gateway.state_store.open_log("reissue-bootstrap-token")
    gateway.ensure_secrets(config, log=log)
    before = gateway_module._read_env(env_path)["FS_AUTH_BOOTSTRAP_TOKEN"]
    gateway.state_store.update_env_values(env_path, {"FS_AUTH_BOOTSTRAP_ENABLED": "0"})

    gateway.reissue_bootstrap_token(config, log=log)
    log.close()

    values = gateway_module._read_env(env_path)
    assert values["FS_AUTH_BOOTSTRAP_TOKEN"] != before
    assert values["FS_AUTH_BOOTSTRAP_ENABLED"] == "1"
    assert gateway_module._future_timestamp(values["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"])
    secret_output = tmp_path / ".flow-steward/secret-output/bootstrap-token"
    assert secret_output.read_text(encoding="utf-8").strip() == values["FS_AUTH_BOOTSTRAP_TOKEN"]
    restart = [
        command for command in process.commands if "up" in command and "--force-recreate" in command
    ]
    assert restart, "the replacement token only takes effect after the runtime restarts"
    assert restart[-1][-1] == service
    assert values["FS_AUTH_BOOTSTRAP_TOKEN"] not in log.path.read_text(encoding="utf-8")


def test_failed_token_activation_leaves_the_expired_values_on_disk(tmp_path: Path) -> None:
    # Otherwise the retry is poisoned: the env would carry a future expiry the running
    # container never received, so the next run reads "not expired", skips re-issuing, and
    # hands the operator a link that can only 403.
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, secret_factory=lambda: "generated-secret")
    config = InstallConfig(
        public_host="flow.example.test",
        deployment_profile="compact",
        environment="production",
    )
    env_path = tmp_path / ".env.prod"
    log = gateway.state_store.open_log("failed-activation")
    gateway.ensure_secrets(config, log=log)
    gateway.state_store.update_env_values(
        env_path,
        {
            "FS_AUTH_BOOTSTRAP_TOKEN": "token-that-the-runtime-holds",
            "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT": "2020-01-01T00:00:00+00:00",
        },
    )
    restart = (
        *gateway._compose_prefix(config),
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "compact-runtime",
    )
    process.outcomes[restart] = ProcessOutcome(
        command=restart, returncode=1, stderr="no such service"
    )

    touched = (
        env_path,
        gateway.state_store.root / "runtime/bootstrap-token-state.json",
        gateway.state_store.root / "secret-output/bootstrap-token",
    )
    before_artifacts = {path: (path.read_bytes(), path.stat().st_mode) for path in touched}
    with pytest.raises(InstallerFailure) as raised:
        gateway.reissue_bootstrap_token(config, log=log)
    log.close()

    assert raised.value.code == "bootstrap_recovery_needed"
    assert {path: (path.read_bytes(), path.stat().st_mode) for path in touched} == before_artifacts
    values = gateway_module._read_env(env_path)
    assert values["FS_AUTH_BOOTSTRAP_TOKEN"] == "token-that-the-runtime-holds"
    assert gateway_module._future_timestamp(values["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"]) is False


def test_a_token_this_installation_did_not_mint_is_replaced(tmp_path: Path) -> None:
    # AC17-R49, the strict half. `.env.prod` sits at the repository root, outside every volume
    # and outside the state directory, so `down -v` plus `rm -rf .flow-steward` leaves the old
    # one-time token behind and a fresh install inherits it. Within the original hour the
    # expiry check alone cannot tell that apart from a resume; the state file can, because it
    # died with the installation that wrote it.
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    config = InstallConfig(public_host="flow.example.test")
    log = gateway.state_store.open_log("inherited-bootstrap-token")
    gateway.ensure_secrets(config, log=log)
    minted = gateway_module._read_env(tmp_path / ".env")["FS_AUTH_BOOTSTRAP_TOKEN"]
    assert gateway.postcondition_holds("secrets", config) is True

    # the teardown: volumes and the state directory go, the env file does not
    shutil.rmtree(tmp_path / ".flow-steward")

    assert gateway.postcondition_holds("secrets", config) is False
    gateway.state_store.ensure_runtime_directory()
    gateway.ensure_secrets(config, log=gateway.state_store.open_log("after-teardown"))

    values = gateway_module._read_env(tmp_path / ".env")
    assert values["FS_AUTH_BOOTSTRAP_TOKEN"] != minted
    assert gateway_module._future_timestamp(values["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"])


def test_a_resume_keeps_the_token_its_own_installation_minted(tmp_path: Path) -> None:
    # The whole point of reuse: a rerun on a half-finished install must not invalidate a link
    # the operator is already holding.
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    config = InstallConfig(public_host="flow.example.test")
    log = gateway.state_store.open_log("resumed-bootstrap-token")
    gateway.ensure_secrets(config, log=log)
    minted = gateway_module._read_env(tmp_path / ".env")["FS_AUTH_BOOTSTRAP_TOKEN"]

    gateway.ensure_secrets(config, log=log)
    log.close()

    assert gateway_module._read_env(tmp_path / ".env")["FS_AUTH_BOOTSTRAP_TOKEN"] == minted
    assert gateway.postcondition_holds("secrets", config) is True


def _route_failure(error: OSError, tmp_path: Path, monkeypatch) -> InstallerFailure:
    monkeypatch.setattr(gateway_module.socket, "socket", lambda *_args: HostRouteSocket(error))
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(),
        port_available=None,
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
    )
    log = gateway.state_store.open_log("host-route-diagnosis")
    config = InstallConfig(public_host="127.0.0.1", public_port=8099)
    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(config, log=log)
    log.close()
    return raised.value


def test_a_filtered_host_route_is_not_treated_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R78: on a hardened host a timeout is what a FREE port looks like.

    Measured on the owner's server: `ufw` with `Chain INPUT (policy DROP)`. Port 22, which is
    busy, answers; every free port the installer wanted times out, because a SYN to an
    unlistened port is dropped rather than refused. The installer read that as fatal and
    abandoned the install at preflight — three of four ingress modes reach this check, and
    only external_traefik escaped it by publishing nothing.

    The bind probe below binds on the host and needs no container-to-host traffic, so it is
    unaffected by the firewall and remains the authoritative answer.
    """
    monkeypatch.setattr(gateway_module.socket, "gethostname", lambda: "bootstrap-container")
    monkeypatch.setattr(
        gateway_module.socket, "socket", lambda *_args: HostRouteSocket(TimeoutError("timed out"))
    )
    inspect_command = (
        "docker",
        "container",
        "inspect",
        "bootstrap-container",
        "--format",
        "{{.Image}}",
    )
    process = FakeProcessExecutor(
        {
            inspect_command: ProcessOutcome(
                command=inspect_command, returncode=0, stdout=f"{_APP_IMAGE_ID}\n"
            )
        }
    )
    gateway = _gateway(
        tmp_path,
        process,
        port_available=None,
        environment={"FS_INSTALLER_USE_HOST_PYTHON": "0"},
    )
    log = gateway.state_store.open_log("filtered-host-route")

    gateway.preflight(InstallConfig(public_host="127.0.0.1", public_port=8099), log=log)
    log.close()

    # it went on to the authoritative check instead of refusing
    assert any("--publish" in " ".join(command) for command in process.commands)
    recorded = log.path.read_text(encoding="utf-8")
    assert "did not answer" in recorded
    assert "the bind probe decides" in recorded


def test_an_unresolvable_host_route_points_at_how_the_installer_was_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other cause. install.sh maps the name with --add-host, so a resolution failure
    means the installer was launched some other way — a different answer entirely."""
    failure = _route_failure(
        socket.gaierror(-2, "Name or service not known"), tmp_path, monkeypatch
    )

    assert failure.code == "host_port_route_probe_failed"
    assert "host.docker.internal:8099" in failure.cause
    assert "could not resolve" in failure.cause
    assert "--add-host" in failure.cause
    assert "./install.sh" in failure.remediation


def test_initial_preservation_manifest_records_owned_resource_identities(tmp_path: Path) -> None:
    volume_command = (
        "docker",
        "volume",
        "ls",
        "--filter",
        "label=com.docker.compose.project=flow-steward",
        "--format",
        "{{.Name}}",
    )
    container_command = (
        "docker",
        "ps",
        "-a",
        "--filter",
        "label=com.docker.compose.project=flow-steward",
        "--format",
        "{{.Names}}",
    )
    gateway = _gateway(
        tmp_path,
        FakeProcessExecutor(
            {
                volume_command: ProcessOutcome(
                    command=volume_command, returncode=0, stdout="flow-steward_pg-fs\n"
                ),
                container_command: ProcessOutcome(
                    command=container_command, returncode=0, stdout="flow-steward-fs-app-1\n"
                ),
            }
        ),
    )
    log = gateway.state_store.open_log("manifest")

    gateway.capture_initial_manifest(InstallConfig(), log=log)

    log.close()
    payload = json.loads(
        (tmp_path / ".flow-steward/initial-preservation-manifest.json").read_text()
    )
    assert payload["owned_volumes"] == ["flow-steward_pg-fs"]
    assert payload["owned_containers"] == ["flow-steward-fs-app-1"]
    assert payload["captured_at"]


def test_preservation_manifest_fails_closed_when_inventory_is_unavailable(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    volume_command = (
        "docker",
        "volume",
        "ls",
        "--filter",
        "label=com.docker.compose.project=flow-steward",
        "--format",
        "{{.Name}}",
    )
    process.outcomes[volume_command] = ProcessOutcome(command=volume_command, returncode=1)
    log = gateway.state_store.open_log("manifest-invalid-inventory")

    _raises_input_3915_1 = InstallConfig()
    with pytest.raises(InstallerFailure) as raised:
        gateway.capture_initial_manifest(_raises_input_3915_1, log=log)
    log.close()

    assert raised.value.code == "preservation_inventory_unavailable"
    assert not (tmp_path / ".flow-steward/initial-preservation-manifest.json").exists()


def test_inspect_rejects_placeholder_secrets(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    (tmp_path / ".env").write_text(
        "FS_SECRET_KEY=CHANGE_ME_STRONG_PASSWORD\n"
        "ENCRYPTION_KEY=real-encryption-key\n"
        "FS_BACKUP_ENCRYPTION_KEY=replace_me\n"
        "FS_METRICS_TOKEN=real-metrics-token\n",
        encoding="utf-8",
    )

    evidence = gateway.inspect()

    assert evidence.durable_secrets_present is False
    assert evidence.backup_encryption_key_present is False


def test_secret_step_does_not_resume_past_placeholder_values(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig()
    (tmp_path / ".env").write_text(
        "FS_SECRET_KEY=CHANGE_ME_STRONG_PASSWORD\n"
        "ENCRYPTION_KEY=real-encryption-key\n"
        "FS_BACKUP_ENCRYPTION_KEY=real-backup-key\n"
        "FS_METRICS_TOKEN=real-metrics-token\n"
        "FS_COMPOSE_PROJECT_NAME=flow-steward\n"
        "FS_PUBLIC_BASE_URL=https://localhost:8099\n"
        "FS_AUTH_TRUSTED_ORIGINS=https://localhost:8099\n",
        encoding="utf-8",
    )

    assert gateway.postcondition_holds("secrets", config) is False


def test_development_secret_step_does_not_resume_across_source_revision_change(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, secret_factory=lambda: "generated-secret")
    config = InstallConfig(environment="development")
    log = gateway.state_store.open_log("development-source-revision")
    gateway.ensure_secrets(config, log=log)
    log.close()
    git_revision = ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD^{commit}")

    assert gateway.postcondition_holds("secrets", config) is True

    process.outcomes[git_revision] = ProcessOutcome(
        command=git_revision,
        returncode=0,
        stdout="f" * 40 + "\n",
    )
    assert gateway.postcondition_holds("secrets", config) is False


@pytest.mark.parametrize(
    ("name", "placeholder"),
    (
        ("POSTGRES_PASSWORD", "CHANGE_ME_STRONG_PASSWORD"),
        ("RABBITMQ_PASS", "CHANGE_ME_STRONG_PASSWORD"),
        ("SECURITY_PII_SALT", "default-pii-salt-change-me"),
    ),
)
def test_secret_step_checks_every_selected_generated_secret(
    tmp_path: Path, name: str, placeholder: str
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor(), secret_factory=lambda: "generated-secret")
    config = InstallConfig(development_tools=("audit_tools",))
    log = gateway.state_store.open_log("selected-secrets")
    gateway.ensure_secrets(config, log=log)
    log.close()
    env_path = tmp_path / ".env"
    values = gateway_module._read_env(env_path)
    values[name] = placeholder
    gateway.state_store.update_env_values(env_path, values)

    assert gateway.postcondition_holds("secrets", config) is False


def _runtime_inspection(
    gateway: DockerInstallerGateway,
    *,
    image_id: str,
    revision: str,
    state_runtime_root: Path,
    host_self_bind_source: Path | None = None,
    candidate_state_dir: Path | None = None,
    candidate_prod_env: Path | None = None,
    installer_state_root: Path | None = None,
    group_add: list[str] | None = None,
    extra_mounts: list[dict[str, object]] | None = None,
    host_directory_mounts: tuple[tuple[Path, str], ...] | None = None,
) -> str:
    repo_root = gateway.repo_root
    mounts = [
        {
            "Type": "bind",
            "Source": str((host_self_bind_source or repo_root).resolve()),
            "Destination": str(repo_root),
            "RW": True,
        },
        {
            "Type": "bind",
            "Source": str(state_runtime_root.resolve()),
            "Destination": "/run/flow-steward-runtime",
            "RW": True,
        },
        {
            "Type": "bind",
            "Source": "/var/run/docker.sock",
            "Destination": "/var/run/docker.sock",
            "RW": True,
        },
    ]
    if candidate_state_dir is not None:
        mounts.append(
            {
                "Type": "bind",
                "Source": str(candidate_state_dir),
                "Destination": "/run/flow-steward-candidate-state",
                "RW": True,
            }
        )
    if candidate_prod_env is not None:
        mounts.append(
            {
                "Type": "bind",
                "Source": str(candidate_prod_env),
                "Destination": "/app/.env.prod",
                "RW": True,
            }
        )
    if candidate_state_dir is None and candidate_prod_env is None:
        selected_state_root = (installer_state_root or gateway.state_store.root).resolve()
        mounts.extend(
            (
                {
                    "Type": "bind",
                    "Source": str(selected_state_root),
                    "Destination": "/run/flow-steward-install-state",
                    "RW": True,
                },
                {
                    "Type": "bind",
                    "Source": str(selected_state_root),
                    "Destination": str(selected_state_root),
                    "RW": True,
                },
            )
        )
    selected_roots = (
        host_directory_mounts
        if host_directory_mounts is not None
        else (
            (repo_root / "extensions", "/app/extensions"),
            (repo_root / "backups", "/app/backups"),
        )
    )
    mounts.extend(
        {"Type": "bind", "Source": str(source), "Destination": destination, "RW": True}
        for source, destination in selected_roots
    )
    mounts.extend(extra_mounts or [])
    return json.dumps(
        [
            {
                "State": {"Running": True},
                "Image": image_id,
                "Config": {
                    "User": "1001:1002",
                    "Labels": {
                        "com.docker.compose.project": "flow-steward",
                        "com.docker.compose.service": "fs-cli",
                        "com.flow-steward.cli-runtime": "true",
                        "com.flow-steward.cli-runtime-contract": "1",
                        "com.flow-steward.cli-runtime-revision": revision,
                    },
                },
                "HostConfig": {"GroupAdd": group_add or ["1003"], "Privileged": False},
                "Mounts": mounts,
            }
        ]
    )


def test_cli_exact_mount_contract_includes_selected_host_clone_roots(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    runtime_root = gateway.state_store.root / "runtime"
    host_roots = (
        (Path("/srv/instance-extensions"), "/app/extensions"),
        (Path("/srv/clone-transfer"), "/app/backups"),
        (Path("/srv/instance-logs/artifacts"), "/app/logs/artifacts"),
    )
    context = SimpleNamespace(
        environment={
            "FS_INSTALLER_STATE_DIR": str(gateway.state_store.root.resolve()),
            "FS_STACK_ENVIRONMENT": "local_development",
        },
        state_runtime_root=runtime_root,
        candidate_state_dir=None,
        candidate_prod_env=None,
        host_directory_mounts=host_roots,
    )
    record = json.loads(
        _runtime_inspection(
            gateway,
            image_id="sha256:cli-image",
            revision="a" * 64,
            state_runtime_root=runtime_root,
            host_directory_mounts=(),
            extra_mounts=[
                {"Type": "bind", "Source": str(source), "Destination": target, "RW": True}
                for source, target in host_roots
            ],
        )
    )[0]
    matches, _detail = gateway._cli_mounts_match(record["Mounts"], context)
    assert matches is True

    for invalid in ("missing", "wrong", "readonly", "extra"):
        mounts = [dict(mount) for mount in record["Mounts"]]
        if invalid == "missing":
            mounts.pop()
        elif invalid == "wrong":
            mounts[-1]["Source"] = "/srv/another-instance/artifacts"
        elif invalid == "readonly":
            mounts[-1]["RW"] = False
        else:
            mounts.append(
                {
                    "Type": "bind",
                    "Source": "/srv/unrelated",
                    "Destination": "/unrelated",
                    "RW": True,
                }
            )
        matches, detail = gateway._cli_mounts_match(mounts, context)
        assert matches is False, invalid
        assert "mount contract" in detail

    normalized = [dict(mount) for mount in record["Mounts"]]
    for mount in normalized:
        if mount["Source"] != "/var/run/docker.sock":
            mount["Source"] = "/host_mnt" + mount["Source"]
    assert gateway._cli_mounts_match(normalized, context)[0] is True


@pytest.mark.parametrize(
    ("mounts", "expected"),
    (
        ("selected", True),
        ("missing-alias", False),
        ("wrong-alias-source", False),
        ("wrong-host-identity-source", False),
        ("extra", False),
    ),
)
def test_development_cli_mount_contract_uses_selected_external_state_root(
    tmp_path: Path,
    mounts: str,
    expected: bool,
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    selected_state_root = (tmp_path.parent / f"{tmp_path.name}-selected-installer-state").resolve()
    selected_state_root.mkdir()
    runtime_root = gateway.state_store.root / "runtime"
    context = SimpleNamespace(
        environment={
            "FS_INSTALLER_STATE_DIR": str(selected_state_root),
            "FS_STACK_ENVIRONMENT": "local_development",
        },
        state_runtime_root=runtime_root,
        candidate_state_dir=None,
        candidate_prod_env=None,
        host_directory_mounts=(),
    )
    record = json.loads(
        _runtime_inspection(
            gateway,
            image_id="sha256:cli-image",
            revision="a" * 64,
            state_runtime_root=runtime_root,
            host_directory_mounts=(),
            installer_state_root=selected_state_root,
        )
    )[0]
    state_mounts = [
        mount
        for mount in record["Mounts"]
        if mount["Destination"] in {"/run/flow-steward-install-state", str(selected_state_root)}
    ]
    if mounts == "missing-alias":
        state_mounts = [
            mount
            for mount in state_mounts
            if mount["Destination"] != "/run/flow-steward-install-state"
        ]
        record["Mounts"] = [
            mount
            for mount in record["Mounts"]
            if mount["Destination"] != "/run/flow-steward-install-state"
        ]
    elif mounts == "wrong-alias-source":
        next(
            mount
            for mount in state_mounts
            if mount["Destination"] == "/run/flow-steward-install-state"
        )["Source"] = str(tmp_path / "wrong-state")
    elif mounts == "wrong-host-identity-source":
        next(mount for mount in state_mounts if mount["Destination"] == str(selected_state_root))[
            "Source"
        ] = str(tmp_path / "wrong-state")
    elif mounts == "extra":
        record["Mounts"].append(
            {
                "Type": "bind",
                "Source": str(tmp_path / "unrelated"),
                "Destination": "/unrelated",
                "RW": True,
            }
        )

    matches, _detail = gateway._cli_mounts_match(record["Mounts"], context)
    assert matches is expected


def _configure_runtime_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    container_id: str = "cli-container-id",
    environment: str = "development",
    cli_image: str | None = None,
    bootstrap_docker_gid: str | None = None,
    data_placement: str = "host_directories",
    file_storage: str = "bundled",
) -> tuple[
    DockerInstallerGateway,
    FakeProcessExecutor,
    InstallConfig,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    monkeypatch.setattr(gateway_module.os, "getuid", lambda: 1001)
    monkeypatch.setattr(gateway_module.os, "getgid", lambda: 1002)
    monkeypatch.setattr(gateway_cli_runtime, "_docker_socket_group_id", lambda: 1003)
    process = FakeProcessExecutor()
    gateway_environment = (
        {"FS_DOCKER_GID": bootstrap_docker_gid} if bootstrap_docker_gid is not None else {}
    )
    gateway = _gateway(tmp_path, process, environment=gateway_environment)
    config = InstallConfig(
        environment=environment, data_placement=data_placement, file_storage=file_storage
    )
    env_path = tmp_path / (".env" if environment == "development" else ".env.prod")
    env_lines = ["FS_COMPOSE_PROJECT_NAME=flow-steward"]
    candidate_state_dir: Path | None = None
    candidate_prod_env: Path | None = None
    if environment == "production":
        candidate_state_dir = gateway.state_store.root / "candidate-state"
        candidate_state_dir.mkdir(parents=True)
        candidate_prod_env = env_path.resolve()
        env_lines.extend(
            (
                f"FS_CANDIDATE_STATE_DIR={candidate_state_dir}",
                f"FS_CANDIDATE_PROD_ENV={candidate_prod_env}",
            )
        )
    if cli_image is not None:
        env_lines.append(f"FS_CLI_IMAGE={cli_image}")
    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    gateway.state_store.ensure_runtime_directory()
    build_revision = "0123456789abcdef0123456789abcdef01234567"
    runtime_revision = "a" * 64
    image = cli_image.strip() if cli_image and cli_image.strip() else "flow-steward-cli:0.1.0"
    image_id = "sha256:cli-image"
    git_command = ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD")
    image_revision_command = (
        "docker",
        "image",
        "inspect",
        image,
        "--format",
        '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
    )
    expected_compose_files = [
        "infra/docker/docker-compose.yaml"
        if environment == "development"
        else "infra/docker/docker-compose.prod.yaml"
    ]
    if data_placement == "compact_volume":
        expected_compose_files.append("infra/docker/scalable/data-compact_volume.yaml")
    else:
        expected_compose_files.append("infra/docker/scalable/cli-host-directories.yaml")
        if file_storage == "local_fs":
            expected_compose_files.append(
                "infra/docker/scalable/cli-host-directories-artifacts.yaml"
            )
    helper_command = (
        str(tmp_path / "core/infrastructure/cli_runtime/runtime_identity.sh"),
        "--compose-project",
        "flow-steward",
        "--stack-environment",
        "local_development" if environment == "development" else "production",
        "--active-revision",
        "initial",
        "--compose-files",
        ":".join(str(tmp_path / relative) for relative in expected_compose_files),
        "--env-file",
        str(env_path),
        "--host-project-root",
        str(tmp_path.resolve()),
        "--host-uid",
        "1001",
        "--host-gid",
        "1002",
        "--docker-gid",
        bootstrap_docker_gid or "1003",
        "--env-file-digest-from-environment",
    )
    image_id_command = (
        "docker",
        "image",
        "inspect",
        image,
        "--format",
        "{{.Id}}",
    )
    compose_prefix = (*gateway._compose_prefix(config), "--profile", "cli")
    ps_command = (*compose_prefix, "ps", "-q", "--all", "fs-cli")
    inspect_command = ("docker", "container", "inspect", container_id)
    process.outcomes.update(
        {
            git_command: ProcessOutcome(
                command=git_command, returncode=0, stdout=build_revision + "\n"
            ),
            image_revision_command: ProcessOutcome(
                command=image_revision_command, returncode=0, stdout=build_revision + "\n"
            ),
            helper_command: ProcessOutcome(
                command=helper_command, returncode=0, stdout=runtime_revision + "\n"
            ),
            image_id_command: ProcessOutcome(
                command=image_id_command, returncode=0, stdout=image_id + "\n"
            ),
            ps_command: ProcessOutcome(
                command=ps_command, returncode=0, stdout=container_id + "\n"
            ),
            inspect_command: ProcessOutcome(
                command=inspect_command,
                returncode=0,
                stdout=_runtime_inspection(
                    gateway,
                    image_id=image_id,
                    revision=runtime_revision,
                    state_runtime_root=gateway.state_store.root / "runtime",
                    candidate_state_dir=candidate_state_dir,
                    candidate_prod_env=candidate_prod_env,
                    group_add=[bootstrap_docker_gid or "1003"],
                    host_directory_mounts=()
                    if data_placement == "compact_volume"
                    else (
                        (tmp_path / "extensions", "/app/extensions"),
                        (tmp_path / "backups", "/app/backups"),
                        *(
                            ((tmp_path / "logs/artifacts", "/app/logs/artifacts"),)
                            if file_storage == "local_fs"
                            else ()
                        ),
                    ),
                ),
            ),
        }
    )
    return gateway, process, config, helper_command, ps_command, inspect_command


@pytest.mark.parametrize("cli_image", [None, "   "], ids=("absent", "blank"))
def test_cli_runtime_falls_back_to_version_image_only_when_selection_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_image: str | None,
) -> None:
    gateway, _process, config, _helper_command, _ps_command, _inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch, cli_image=cli_image)
    )

    context = gateway._cli_runtime_context(config, log=None)

    assert context.image == "flow-steward-cli:0.1.0"
    assert context.environment["FS_CLI_IMAGE"] == "flow-steward-cli:0.1.0"


def test_cli_context_normalizes_selected_clone_paths_and_ignores_ambient_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, _process, config, _helper, _ps, _inspect = _configure_runtime_contract(
        tmp_path,
        monkeypatch,
        file_storage="local_fs",
    )
    gateway.state_store.update_env_values(
        tmp_path / ".env",
        {
            "FS_EXTENSIONS_DIR": "../../selected-extensions",
            "FS_BACKUPS_DIR": "/srv/selected-transfers",
            "FS_LOGS_DIR": "../../selected-logs",
        },
    )
    monkeypatch.setenv("FS_EXTENSIONS_DIR", "/ambient/wrong-extensions")
    monkeypatch.setenv("FS_BACKUPS_DIR", "/ambient/wrong-transfers")
    monkeypatch.setenv("FS_LOGS_DIR", "/ambient/wrong-logs")
    context = gateway._cli_runtime_context(config, log=None)
    assert context.host_directory_mounts == (
        (tmp_path / "selected-extensions", "/app/extensions"),
        (Path("/srv/selected-transfers"), "/app/backups"),
        (tmp_path / "selected-logs/artifacts", "/app/logs/artifacts"),
    )
    assert context.environment["FS_EXTENSIONS_DIR"] == str(tmp_path / "selected-extensions")
    assert context.environment["FS_BACKUPS_DIR"] == "/srv/selected-transfers"
    assert context.environment["FS_LOGS_DIR"] == str(tmp_path / "selected-logs")


def test_cli_volume_placement_keeps_exact_candidate_mounts_without_host_clone_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, process, config, _helper, _ps, inspect_command = _configure_runtime_contract(
        tmp_path,
        monkeypatch,
        environment="production",
        data_placement="compact_volume",
        file_storage="local_fs",
    )
    context = gateway._cli_runtime_context(config, log=None)
    assert context.host_directory_mounts == ()
    assert "FS_EXTENSIONS_DIR" not in context.environment
    assert "FS_BACKUPS_DIR" not in context.environment
    assert "FS_LOGS_DIR" not in context.environment
    log = gateway.state_store.open_log("volume-placement-cli")
    try:
        gateway.ensure_cli_runtime(config, log=log)
    finally:
        log.close()
    record = json.loads(process.outcomes[inspect_command].stdout)[0]
    assert len(record["Mounts"]) == 5
    assert not any("up" in command or "build" in command for command in process.commands)


def test_cli_runtime_uses_bootstrap_host_docker_gid_for_stable_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, _process, config, helper_command, _ps_command, _inspect_command = (
        _configure_runtime_contract(
            tmp_path,
            monkeypatch,
            bootstrap_docker_gid="20",
        )
    )

    context = gateway._cli_runtime_context(config, log=None)

    assert helper_command[helper_command.index("--docker-gid") + 1] == "20"
    assert context.docker_gid == "20"
    assert context.environment["FS_DOCKER_GID"] == "20"


def test_cli_repair_prepares_missing_default_clone_transfer_root_as_private_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, process, config, _helper, ps_command, _inspect = _configure_runtime_contract(
        tmp_path,
        monkeypatch,
    )
    transfers = tmp_path / "backups"
    assert not transfers.exists()
    process.queued_outcomes[ps_command] = [
        ProcessOutcome(command=ps_command, returncode=0, stdout=""),
        process.outcomes[ps_command],
    ]
    log = gateway.state_store.open_log("default-clone-transfer-root")
    try:
        gateway.ensure_cli_runtime(config, log=log)
    finally:
        log.close()
    assert transfers.is_dir()
    assert transfers.stat().st_mode & 0o777 == 0o700
    assert any("up" in command for command in process.commands)


def test_core_start_prepares_default_artifacts_before_docker_can_create_root_owned_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production", file_storage="local_fs")
    original_run = process.run

    def require_artifact_source(command, **kwargs):
        if "up" in command:
            assert (tmp_path / "logs/artifacts").is_dir()
        return original_run(command, **kwargs)

    monkeypatch.setattr(process, "run", require_artifact_source)
    log = gateway.state_store.open_log("early-artifact-root")
    try:
        gateway.start_core(config, log=log)
    finally:
        log.close()


def test_cli_repair_prepares_default_artifact_directory_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, process, config, _helper, ps_command, _inspect = _configure_runtime_contract(
        tmp_path, monkeypatch, environment="production", file_storage="local_fs"
    )
    artifacts = tmp_path / "logs/artifacts"
    assert not artifacts.exists()
    process.queued_outcomes[ps_command] = [
        ProcessOutcome(command=ps_command, returncode=0, stdout=""),
        process.outcomes[ps_command],
    ]
    original_run = process.run

    def require_artifact_source(command, **kwargs):
        if "up" in command:
            assert artifacts.is_dir(), "CLI artifact bind source must exist before Compose up"
        return original_run(command, **kwargs)

    monkeypatch.setattr(process, "run", require_artifact_source)
    log = gateway.state_store.open_log("default-artifact-root")
    try:
        gateway.ensure_cli_runtime(config, log=log)
    finally:
        log.close()
    assert artifacts.is_dir()
    assert artifacts.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("external_exists", [False, True])
def test_core_start_does_not_create_or_change_external_artifact_roots(
    tmp_path: Path, external_exists: bool
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    external = tmp_path / "operator-logs"
    if external_exists:
        external.mkdir(mode=0o750)
    _write_env_values(tmp_path / ".env.prod", {"FS_LOGS_DIR": str(external)})
    log = gateway.state_store.open_log("external-artifacts")
    try:
        gateway.start_core(
            InstallConfig(environment="production", file_storage="local_fs"), log=log
        )
    finally:
        log.close()
    assert external.exists() is external_exists
    assert not (external / "artifacts").exists()
    assert not (tmp_path / "logs").exists()
    if external_exists:
        assert external.stat().st_mode & 0o777 == 0o750


@pytest.mark.parametrize("blocked_path", ["logs", "logs/artifacts"])
def test_cli_repair_rejects_symlinked_default_artifact_root_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_path: str
) -> None:
    gateway, process, config, _helper, ps_command, _inspect = _configure_runtime_contract(
        tmp_path, monkeypatch, environment="production", file_storage="local_fs"
    )
    outside = tmp_path / "external-artifacts"
    outside.mkdir(mode=0o750)
    link = tmp_path / blocked_path
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    process.queued_outcomes[ps_command] = [
        ProcessOutcome(command=ps_command, returncode=0, stdout=""),
        process.outcomes[ps_command],
    ]
    log = gateway.state_store.open_log("symlink-artifacts")
    try:
        with pytest.raises(InstallerFailure):
            gateway.ensure_cli_runtime(config, log=log)
    finally:
        log.close()
    assert not any("up" in command for command in process.commands)
    assert not list(outside.iterdir())
    assert outside.stat().st_mode & 0o777 == 0o750


def test_cli_repair_rejects_symlinked_default_clone_transfer_root_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, process, config, _helper, ps_command, _inspect = _configure_runtime_contract(
        tmp_path,
        monkeypatch,
    )
    outside = tmp_path / "external-transfer-storage"
    outside.mkdir(mode=0o750)
    marker = outside / "retained.fsclone"
    marker.write_bytes(b"existing encrypted archive")
    (tmp_path / "backups").symlink_to(outside, target_is_directory=True)
    process.queued_outcomes[ps_command] = [
        ProcessOutcome(command=ps_command, returncode=0, stdout=""),
        process.outcomes[ps_command],
    ]
    log = gateway.state_store.open_log("symlink-clone-transfer-root")
    try:
        with pytest.raises(InstallerFailure) as raised:
            gateway.ensure_cli_runtime(config, log=log)
    finally:
        log.close()

    assert not any("up" in command for command in process.commands)
    assert "FS_BACKUPS_DIR" in str(raised.value)
    assert marker.read_bytes() == b"existing encrypted archive"
    assert outside.stat().st_mode & 0o777 == 0o750
    assert (tmp_path / "backups").is_symlink()


def test_cli_runtime_reuses_exact_container_and_invokes_shared_identity_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, process, config, helper_command, _ps_command, _inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch)
    )
    log = gateway.state_store.open_log("cli-runtime-reuse")

    gateway.ensure_cli_runtime(config, log=log)

    log.close()
    assert helper_command in process.commands
    helper_kwargs = next(
        kwargs for command, kwargs in process.invocations if command == helper_command
    )
    helper_environment = helper_kwargs["environment"]
    assert helper_environment["FS_INSTALLER_STATE_DIR"] == str(gateway.state_store.root)
    assert "FS_PROJECTS_ROOT" not in helper_environment
    assert "FS_TEST_PROJECT_ROOT" not in helper_environment
    assert not any("build" in command or "up" in command for command in process.commands)


def test_exact_prebuilt_cli_repair_uses_pinned_revision_without_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, process, config, _helper_command, ps_command, inspect_command = (
        _configure_runtime_contract(
            tmp_path,
            monkeypatch,
            environment="production",
            cli_image=_CLI_IMAGE_ID,
        )
    )
    gateway.state_store.update_env_values(
        tmp_path / ".env.prod",
        {"FS_CLI_IMAGE_REVISION": _CLI_REVISION},
    )
    image_revision_command = (
        "docker",
        "image",
        "inspect",
        _CLI_IMAGE_ID,
        "--format",
        '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
    )
    image_id_command = (
        "docker",
        "image",
        "inspect",
        _CLI_IMAGE_ID,
        "--format",
        "{{.Id}}",
    )
    process.outcomes[image_revision_command] = ProcessOutcome(
        command=image_revision_command,
        returncode=0,
        stdout=_CLI_REVISION + "\n",
    )
    process.outcomes[image_id_command] = ProcessOutcome(
        command=image_id_command,
        returncode=0,
        stdout=_CLI_IMAGE_ID + "\n",
    )
    process.queued_outcomes[ps_command] = [
        ProcessOutcome(command=ps_command, returncode=0, stdout=""),
        process.outcomes[ps_command],
    ]
    process.outcomes[inspect_command] = ProcessOutcome(
        command=inspect_command,
        returncode=0,
        stdout=_runtime_inspection(
            gateway,
            image_id=_CLI_IMAGE_ID,
            revision="a" * 64,
            state_runtime_root=gateway.state_store.root / "runtime",
            candidate_state_dir=gateway.state_store.root / "candidate-state",
            candidate_prod_env=tmp_path / ".env.prod",
        ),
    )
    compose_prefix = (*gateway._compose_prefix(config), "--profile", "cli")
    start_command = (
        *compose_prefix,
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "--force-recreate",
        "fs-cli",
    )
    log = gateway.state_store.open_log("exact-cli-runtime-repair")

    gateway.ensure_cli_runtime(config, log=log)

    log.close()
    assert start_command in process.commands
    assert not any("build" in command for command in process.commands)
    assert not any(
        command[:3] == ("git", "-C", str(tmp_path.resolve())) for command in process.commands
    )


def test_production_cli_runtime_reuses_exact_host_directory_mount_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, process, config, _helper_command, _ps_command, inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch, environment="production")
    )
    log = gateway.state_store.open_log("production-cli-runtime-reuse")

    gateway.ensure_cli_runtime(config, log=log)

    log.close()
    inspection = json.loads(process.outcomes[inspect_command].stdout)
    assert len(inspection[0]["Mounts"]) == 7
    assert not any(mount["Destination"] == "/app" for mount in inspection[0]["Mounts"])
    assert (
        next(
            mount
            for mount in inspection[0]["Mounts"]
            if mount["Destination"] == str(tmp_path.resolve())
        )["RW"]
        is True
    )
    assert (
        next(
            mount
            for mount in inspection[0]["Mounts"]
            if mount["Destination"] == "/run/flow-steward-runtime"
        )["RW"]
        is True
    )
    assert not any("build" in command or "up" in command for command in process.commands)


@pytest.mark.parametrize(
    "candidate_mounts",
    (
        "missing",
        "wrong",
        "extra",
    ),
)
def test_production_cli_runtime_rejects_non_exact_candidate_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_mounts: str,
) -> None:
    gateway, process, config, _helper_command, _ps_command, inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch, environment="production")
    )
    candidate_state_dir = gateway.state_store.root / "candidate-state"
    candidate_prod_env = tmp_path / ".env.prod"
    inspection_options: dict[str, object] = {
        "candidate_state_dir": candidate_state_dir,
        "candidate_prod_env": candidate_prod_env,
    }
    if candidate_mounts == "missing":
        inspection_options["candidate_prod_env"] = None
    elif candidate_mounts == "wrong":
        inspection_options["candidate_state_dir"] = tmp_path / "wrong-candidate-state"
    else:
        inspection_options["extra_mounts"] = [
            {
                "Type": "bind",
                "Source": str(tmp_path / "extra-candidate-source"),
                "Destination": "/tmp/extra-candidate-source",
                "RW": False,
            }
        ]
    process.outcomes[inspect_command] = ProcessOutcome(
        command=inspect_command,
        returncode=0,
        stdout=_runtime_inspection(
            gateway,
            image_id="sha256:cli-image",
            revision="a" * 64,
            state_runtime_root=gateway.state_store.root / "runtime",
            **inspection_options,
        ),
    )
    log = gateway.state_store.open_log(f"production-cli-runtime-{candidate_mounts}")

    with pytest.raises(InstallerFailure) as raised:
        gateway.ensure_cli_runtime(config, log=log)

    log.close()
    assert raised.value.code == "cli_runtime_unavailable"
    assert "mount contract" in raised.value.cause


def test_development_cli_runtime_keeps_exact_host_directory_mount_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, process, config, _helper_command, _ps_command, inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch, environment="development")
    )
    log = gateway.state_store.open_log("development-cli-runtime-mounts")

    gateway.ensure_cli_runtime(config, log=log)

    log.close()
    inspection = json.loads(process.outcomes[inspect_command].stdout)
    assert len(inspection[0]["Mounts"]) == 7
    assert (
        next(
            mount
            for mount in inspection[0]["Mounts"]
            if mount["Destination"] == "/run/flow-steward-runtime"
        )["RW"]
        is True
    )
    assert not any("build" in command or "up" in command for command in process.commands)


@pytest.mark.parametrize("environment", ["development", "production"])
def test_cli_runtime_preserves_selected_image_while_building_and_starting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    selected_image = f"fs-inst-{environment}-cli:acceptance"
    gateway, process, config, helper_command, ps_command, _inspect_command = (
        _configure_runtime_contract(
            tmp_path,
            monkeypatch,
            environment=environment,
            cli_image=selected_image,
        )
    )
    image_revision_command = next(
        command
        for command in process.commands or process.outcomes
        if command[:3] == ("docker", "image", "inspect") and command[-1] != "{{.Id}}"
    )
    expected_revision = process.outcomes[image_revision_command].stdout
    process.queued_outcomes[image_revision_command] = [
        ProcessOutcome(command=image_revision_command, returncode=0, stdout="stale\n"),
        ProcessOutcome(command=image_revision_command, returncode=0, stdout=expected_revision),
    ]
    process.queued_outcomes[ps_command] = [
        ProcessOutcome(command=ps_command, returncode=0, stdout=""),
        process.outcomes[ps_command],
    ]
    compose_prefix = (*gateway._compose_prefix(config), "--profile", "cli")
    build_command = (*compose_prefix, "build", "fs-cli")
    start_command = (
        *compose_prefix,
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--force-recreate",
        "fs-cli",
    )
    log = gateway.state_store.open_log(f"cli-runtime-repair-{environment}")

    gateway.ensure_cli_runtime(config, log=log)

    log.close()
    assert build_command in process.commands
    assert start_command in process.commands
    assert not any("fs-app" in command or "fs-worker" in command for command in process.commands)
    relevant_invocations = [
        kwargs
        for command, kwargs in process.invocations
        if command == helper_command
        or command[:3] == ("docker", "image", "inspect")
        or command in (build_command, start_command)
    ]
    assert all(
        invocation["environment"]["FS_CLI_IMAGE"] == selected_image
        for invocation in relevant_invocations
    )
    start_kwargs = next(
        kwargs for command, kwargs in process.invocations if command == start_command
    )
    assert start_kwargs["environment"]["FS_CLI_RUNTIME_REVISION"] == "a" * 64
    assert not any(
        "flow-steward-cli:0.1.0" in part for command in process.commands for part in command
    )


def test_cli_runtime_preserves_explicit_buildx_config_for_exact_runtime_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, process, config, helper_command, ps_command, _inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch)
    )
    buildx_config = "/tmp/buildx"
    gateway._environment["BUILDX_CONFIG"] = buildx_config
    image_revision_command = next(
        command
        for command in process.commands or process.outcomes
        if command[:3] == ("docker", "image", "inspect") and command[-1] != "{{.Id}}"
    )
    expected_revision = process.outcomes[image_revision_command].stdout
    process.queued_outcomes[image_revision_command] = [
        ProcessOutcome(command=image_revision_command, returncode=0, stdout="stale\n"),
        ProcessOutcome(command=image_revision_command, returncode=0, stdout=expected_revision),
    ]
    process.queued_outcomes[ps_command] = [
        ProcessOutcome(command=ps_command, returncode=0, stdout=""),
        process.outcomes[ps_command],
    ]
    compose_prefix = (*gateway._compose_prefix(config), "--profile", "cli")
    build_command = (*compose_prefix, "build", "fs-cli")
    start_command = (
        *compose_prefix,
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--force-recreate",
        "fs-cli",
    )
    log = gateway.state_store.open_log("cli-runtime-buildx-config")

    gateway.ensure_cli_runtime(config, log=log)

    log.close()
    assert build_command in process.commands
    assert start_command in process.commands
    runtime_invocations = [
        kwargs
        for command, kwargs in process.invocations
        if command == helper_command
        or command[:3]
        in {
            ("docker", "image", "inspect"),
            ("docker", "container", "inspect"),
        }
        or command in {build_command, ps_command, start_command}
    ]
    assert runtime_invocations
    assert all(
        invocation["environment"]["BUILDX_CONFIG"] == buildx_config
        for invocation in runtime_invocations
    )


@pytest.mark.parametrize("buildx_config", [None, "", "  \t "], ids=("absent", "empty", "blank"))
def test_cli_runtime_does_not_inject_absent_or_blank_buildx_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    buildx_config: str | None,
) -> None:
    gateway, process, config, _helper_command, _ps_command, _inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch)
    )
    if buildx_config is not None:
        gateway._environment["BUILDX_CONFIG"] = buildx_config
    log = gateway.state_store.open_log("cli-runtime-no-buildx-config")

    gateway.ensure_cli_runtime(config, log=log)

    log.close()
    explicit_environments = [
        kwargs["environment"]
        for _command, kwargs in process.invocations
        if kwargs.get("environment")
    ]
    assert explicit_environments
    assert all("BUILDX_CONFIG" not in environment for environment in explicit_environments)


def test_cli_runtime_rejects_wrong_required_mount_with_stable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, process, config, _helper_command, _ps_command, inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch)
    )
    process.outcomes[inspect_command] = ProcessOutcome(
        command=inspect_command,
        returncode=0,
        stdout=_runtime_inspection(
            gateway,
            image_id="sha256:cli-image",
            revision="a" * 64,
            state_runtime_root=gateway.state_store.root / "runtime",
            host_self_bind_source=tmp_path / "wrong-source",
        ),
    )
    log = gateway.state_store.open_log("cli-runtime-wrong-mount")

    with pytest.raises(InstallerFailure) as raised:
        gateway.ensure_cli_runtime(config, log=log)

    log.close()
    assert raised.value.code == "cli_runtime_unavailable"
    assert raised.value.exit_code == 8
    assert raised.value.retryable is True
    assert raised.value.step == "cli_runtime"
    assert raised.value.log_path == str(log.path)
    assert "mount" in raised.value.cause.lower()


@pytest.mark.parametrize(
    ("inspection_overrides", "expected_cause"),
    [
        ({"host_self_bind_source": Path("/wrong/host-self-bind")}, "mount"),
        ({"group_add": ["1003", "9999"]}, "group"),
        (
            {
                "extra_mounts": [
                    {
                        "Type": "bind",
                        "Source": "/var/run/docker.sock",
                        "Destination": "/tmp/extra-docker.sock",
                        "RW": True,
                    }
                ]
            },
            "mount",
        ),
    ],
    ids=("wrong-host-self-bind", "extra-group", "extra-docker-socket-mount"),
)
def test_cli_runtime_rejects_non_exact_privileged_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inspection_overrides: dict[str, object],
    expected_cause: str,
) -> None:
    gateway, process, config, _helper_command, _ps_command, inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch)
    )
    process.outcomes[inspect_command] = ProcessOutcome(
        command=inspect_command,
        returncode=0,
        stdout=_runtime_inspection(
            gateway,
            image_id="sha256:cli-image",
            revision="a" * 64,
            state_runtime_root=gateway.state_store.root / "runtime",
            **inspection_overrides,
        ),
    )
    log = gateway.state_store.open_log(f"cli-runtime-non-exact-{expected_cause}")

    with pytest.raises(InstallerFailure) as raised:
        gateway.ensure_cli_runtime(config, log=log)

    log.close()
    assert raised.value.code == "cli_runtime_unavailable"
    assert expected_cause in raised.value.cause.lower()


def test_cli_runtime_accepts_docker_desktop_host_mount_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, process, config, _helper_command, _ps_command, inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch)
    )
    monkeypatch.setattr(gateway_module.platform, "system", lambda: "Darwin")
    host_prefix = Path("/host_mnt")
    process.outcomes[inspect_command] = ProcessOutcome(
        command=inspect_command,
        returncode=0,
        stdout=_runtime_inspection(
            gateway,
            image_id="sha256:cli-image",
            revision="a" * 64,
            state_runtime_root=host_prefix
            / str((gateway.state_store.root / "runtime").resolve()).lstrip("/"),
            host_self_bind_source=host_prefix / str(tmp_path.resolve()).lstrip("/"),
        ),
    )
    log = gateway.state_store.open_log("cli-runtime-docker-desktop-mounts")

    gateway.ensure_cli_runtime(config, log=log)

    log.close()
    assert not any(" up " in f" {' '.join(command)} " for command in process.commands)


def test_production_cli_runtime_accepts_docker_desktop_candidate_mount_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, process, config, _helper_command, _ps_command, inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch, environment="production")
    )
    host_prefix = Path("/host_mnt")
    candidate_state_dir = gateway.state_store.root / "candidate-state"
    candidate_prod_env = tmp_path / ".env.prod"
    process.outcomes[inspect_command] = ProcessOutcome(
        command=inspect_command,
        returncode=0,
        stdout=_runtime_inspection(
            gateway,
            image_id="sha256:cli-image",
            revision="a" * 64,
            state_runtime_root=host_prefix
            / str((gateway.state_store.root / "runtime").resolve()).lstrip("/"),
            host_self_bind_source=host_prefix / str(tmp_path.resolve()).lstrip("/"),
            candidate_state_dir=host_prefix / str(candidate_state_dir.resolve()).lstrip("/"),
            candidate_prod_env=host_prefix / str(candidate_prod_env.resolve()).lstrip("/"),
        ),
    )
    log = gateway.state_store.open_log("production-cli-runtime-docker-desktop-mounts")

    gateway.ensure_cli_runtime(config, log=log)

    log.close()
    assert not any(" up " in f" {' '.join(command)} " for command in process.commands)


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("FS_CANDIDATE_STATE_DIR", ""),
        ("FS_CANDIDATE_STATE_DIR", "relative/candidate-state"),
        ("FS_CANDIDATE_PROD_ENV", ""),
        ("FS_CANDIDATE_PROD_ENV", "relative/.env.prod"),
    ),
    ids=("missing-state", "relative-state", "missing-env", "relative-env"),
)
def test_production_cli_runtime_fails_closed_for_invalid_candidate_mount_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    gateway, process, config, helper_command, _ps_command, _inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch, environment="production")
    )
    env_path = tmp_path / ".env.prod"
    values = gateway_module._read_env(env_path)
    values[name] = value
    gateway.state_store.update_env_values(env_path, values)
    log = gateway.state_store.open_log("production-cli-runtime-invalid-candidate-source")

    with pytest.raises(InstallerFailure) as raised:
        gateway.ensure_cli_runtime(config, log=log)

    log.close()
    assert raised.value.code == "cli_runtime_unavailable"
    assert raised.value.cause == "The Production CLI candidate mount paths are invalid."
    assert helper_command not in process.commands
    if value:
        assert value not in log.path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "invalid_kind"),
    (
        ("FS_CANDIDATE_STATE_DIR", "missing"),
        ("FS_CANDIDATE_STATE_DIR", "file"),
        ("FS_CANDIDATE_PROD_ENV", "missing"),
        ("FS_CANDIDATE_PROD_ENV", "directory"),
    ),
    ids=("missing-state", "state-is-file", "missing-env", "env-is-directory"),
)
def test_production_cli_runtime_fails_closed_for_unusable_candidate_mount_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    invalid_kind: str,
) -> None:
    gateway, process, config, helper_command, _ps_command, _inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch, environment="production")
    )
    invalid_path = tmp_path / f"invalid-{invalid_kind}"
    if invalid_kind == "file":
        invalid_path.write_text("not a state directory\n", encoding="utf-8")
    elif invalid_kind == "directory":
        invalid_path.mkdir()
    values = gateway_module._read_env(tmp_path / ".env.prod")
    values[name] = str(invalid_path)
    gateway.state_store.update_env_values(tmp_path / ".env.prod", values)
    log = gateway.state_store.open_log("production-cli-runtime-unusable-candidate-source")

    with pytest.raises(InstallerFailure) as raised:
        gateway.ensure_cli_runtime(config, log=log)

    log.close()
    assert raised.value.cause == "The Production CLI candidate mount paths are invalid."
    assert helper_command not in process.commands
    assert str(invalid_path) not in log.path.read_text(encoding="utf-8")


def test_cli_runtime_postcondition_requires_exact_runtime_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, process, config, _helper_command, _ps_command, inspect_command = (
        _configure_runtime_contract(tmp_path, monkeypatch)
    )

    assert gateway.postcondition_holds("cli_runtime", config) is True

    process.outcomes[inspect_command] = ProcessOutcome(
        command=inspect_command,
        returncode=0,
        stdout="[]",
    )
    assert gateway.postcondition_holds("cli_runtime", config) is False


@pytest.mark.parametrize("deployment_profile", ("scalable", "compact"))
@pytest.mark.parametrize(
    ("file_storage", "expected_selection", "expected_backend"),
    (
        ("local_fs", "local_fs", "local_fs"),
        ("bundled", "bundled_seaweedfs", "s3"),
        ("external_s3", "external_s3", "s3"),
    ),
)
def test_installer_env_selection_keeps_every_storage_backend(
    deployment_profile: str,
    file_storage: str,
    expected_selection: str,
    expected_backend: str,
) -> None:
    """A fresh install must write the backend the operator chose, not a substitute."""
    from core.infrastructure.config.env_registry import build_installer_env

    extra: dict[str, object] = (
        {"s3_endpoint": "https://s3.example", "s3_credentials_path": "/run/s3.json"}
        if file_storage == "external_s3"
        else {}
    )
    config = InstallConfig(
        environment="production",
        deployment_profile=deployment_profile,
        file_storage=file_storage,
        **extra,
    )

    selection = gateway_module._env_selection(config)

    assert selection.file_storage == expected_selection
    rendered = build_installer_env(
        selection=selection,
        existing={},
        secret_factory=lambda: "generated-secret",
    )
    assert rendered["ARTIFACT_STORAGE_BACKEND"] == expected_backend


def test_installer_env_selection_refuses_an_untranslated_storage_backend() -> None:
    """An unknown backend must stop the install, never fall through to S3."""
    config = InstallConfig(environment="production", file_storage="local_fs")
    object.__setattr__(config, "file_storage", "object_store_v9")

    with pytest.raises(InstallerFailure) as raised:
        gateway_module._env_selection(config)

    assert raised.value.code == "storage_backend_unsupported"
    assert "object_store_v9" in raised.value.cause
    assert "--file-storage" in raised.value.remediation


def _external_traefik_config(**overrides) -> InstallConfig:
    values = {
        "environment": "production",
        "deployment_profile": "compact",
        "file_storage": "local_fs",
        "ingress_mode": "external_traefik",
        "tls_mode": "external_resolver",
        "traefik_network": "traefik",
        "traefik_cert_resolver": "myresolver",
        "public_host": "fs.example.eu",
        "public_port": 443,
    }
    values.update(overrides)
    return InstallConfig(**values)


def test_external_traefik_install_ignores_a_public_port_the_external_proxy_owns(
    tmp_path: Path,
) -> None:
    """AC17-R43: the external_traefik overlay declares no host ports:.

    A bound 443 is the required state on such a host, not a conflict, so refusing the
    install made the mode impossible exactly where it is meant to be used.
    """
    process = FakeProcessExecutor(
        {
            (
                "docker",
                "network",
                "inspect",
                "traefik",
                "--format",
                "{{len .Containers}}",
            ): ProcessOutcome(
                command=("docker", "network", "inspect", "traefik"),
                returncode=0,
                stdout="5\n",
            ),
        }
    )
    gateway = _gateway(
        tmp_path,
        process,
        port_available=lambda _host, _port: pytest.fail(
            "the public port must not be probed when the overlay publishes nothing"
        ),
        environment=_COMPACT_TEST_IMAGE,
    )
    _stage_official_release(gateway, "compact")
    log = gateway.state_store.open_log("external-traefik-preflight")

    gateway.preflight(_external_traefik_config(), log=log)

    log.close()
    assert (
        "docker",
        "network",
        "inspect",
        "traefik",
        "--format",
        "{{len .Containers}}",
    ) in process.commands


def test_external_traefik_install_fails_closed_when_the_named_network_is_absent(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor(
        {
            (
                "docker",
                "network",
                "inspect",
                "traefik",
                "--format",
                "{{len .Containers}}",
            ): ProcessOutcome(
                command=("docker", "network", "inspect", "traefik"),
                returncode=1,
                stdout="",
            ),
        }
    )
    gateway = _gateway(
        tmp_path,
        process,
        port_available=lambda _host, _port: True,
        environment=_COMPACT_TEST_IMAGE,
    )
    _stage_official_release(gateway, "compact")
    log = gateway.state_store.open_log("external-traefik-missing-network")

    config = _external_traefik_config()

    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(config, log=log)

    log.close()
    assert raised.value.code == "traefik_network_missing"
    assert "traefik" in raised.value.cause
    assert "--traefik-network" in raised.value.remediation


def test_external_traefik_install_fails_closed_when_no_proxy_is_on_the_network(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor(
        {
            (
                "docker",
                "network",
                "inspect",
                "traefik",
                "--format",
                "{{len .Containers}}",
            ): ProcessOutcome(
                command=("docker", "network", "inspect", "traefik"),
                returncode=0,
                stdout="0\n",
            ),
        }
    )
    gateway = _gateway(
        tmp_path,
        process,
        port_available=lambda _host, _port: True,
        environment=_COMPACT_TEST_IMAGE,
    )
    _stage_official_release(gateway, "compact")
    log = gateway.state_store.open_log("external-traefik-empty-network")

    config = _external_traefik_config()

    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(config, log=log)

    log.close()
    assert raised.value.code == "traefik_network_unattached"
    assert "nothing would route" in raised.value.cause


@pytest.mark.parametrize(
    ("ingress_mode", "extra"),
    (
        ("bundled_traefik", {"tls_mode": "self_signed"}),
        ("external_proxy", {"tls_mode": "reverse_proxy", "proxy_placement": "host"}),
    ),
)
def test_publishing_ingress_modes_keep_the_port_precondition(
    tmp_path: Path,
    ingress_mode: str,
    extra: dict[str, object],
) -> None:
    """Only the overlays that publish nothing may skip the port check."""
    probed: list[int] = []
    process = FakeProcessExecutor()
    gateway = _gateway(
        tmp_path,
        process,
        port_available=lambda _host, port: probed.append(port) or False,
        environment=_COMPACT_TEST_IMAGE,
    )
    _stage_official_release(gateway, "compact")
    log = gateway.state_store.open_log(f"port-precondition-{ingress_mode}")
    config = InstallConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
        ingress_mode=ingress_mode,
        public_host="127.0.0.1",
        public_port=8443,
        **extra,
    )

    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(config, log=log)

    log.close()
    assert raised.value.code == "port_conflict"
    assert probed


def _compact_local_install(tmp_path: Path, *, descriptor_digest: str | None, image_id: str):
    """A local (non-registry) Compact install pinned to an exact image id."""
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production", deployment_profile="compact")
    env_path = tmp_path / ".env.prod"
    _write_env_values(env_path, {"FS_COMPACT_IMAGE": image_id})
    inspect_command = ("docker", "image", "inspect", image_id)
    process.outcomes[inspect_command] = ProcessOutcome(
        command=inspect_command,
        returncode=0,
        stdout=_image_inspection(
            image_id,
            service="compact-runtime",
            entrypoint=("python", "-m", "core.entrypoints.cli.compact_provider_runtime"),
            descriptor_digest=descriptor_digest,
        ),
    )
    return gateway, config, env_path


@pytest.mark.parametrize("store", ("overlay2", "containerd"))
def test_compact_provenance_works_on_both_docker_image_stores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: str,
) -> None:
    """AC17-R44: only containerd reports a local OCI descriptor.

    A packaged Docker Engine on overlay2 returns Descriptor null for every image and
    reports the config digest as the image ID, which made Compact uninstallable there.
    """
    index_digest = f"sha256:{'1' * 64}"
    manifest_digest = f"sha256:{'2' * 64}"
    config_digest = f"sha256:{'3' * 64}"
    containerd = store == "containerd"
    image_id = index_digest if containerd else config_digest
    gateway, config, env_path = _compact_local_install(
        tmp_path,
        descriptor_digest=index_digest if containerd else None,
        image_id=image_id,
    )
    monkeypatch.setattr(
        gateway_image_provenance,
        "resolve_local_image_archive",
        lambda **_kwargs: gateway_module.LocalImageContentIdentity(
            index_digest=index_digest,
            manifest_digest=manifest_digest,
            config_digest=config_digest,
        ),
    )
    log = gateway.state_store.open_log(f"compact-{store}")

    gateway.prepare_core(config, log=log)

    log.close()
    values = gateway.state_store.read_env_values(env_path)
    assert values["FS_COMPACT_IMAGE"] == image_id
    assert values["FS_COMPACT_IMAGE_MANIFEST_DIGEST"] == manifest_digest


@pytest.mark.parametrize("archive_config_matches", (True, False))
def test_compact_overlay2_local_config_pin_accepts_incidental_published_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_config_matches: bool
) -> None:
    config_digest = f"sha256:{'3' * 64}"
    saved_manifest = f"sha256:{'2' * 64}"
    published_parent = f"sha256:{'9' * 64}"
    gateway, config, env_path = _compact_local_install(
        tmp_path, descriptor_digest=None, image_id=config_digest
    )
    command = ("docker", "image", "inspect", config_digest)
    details = json.loads(gateway.process.outcomes[command].stdout)
    details[0]["Descriptor"] = None
    details[0]["RepoDigests"] = [f"registry.example/compact@{published_parent}"]
    gateway.process.outcomes[command] = ProcessOutcome(
        command=command, returncode=0, stdout=json.dumps(details)
    )
    monkeypatch.setattr(
        gateway_image_provenance,
        "resolve_local_image_archive",
        lambda **_kwargs: gateway_module.LocalImageContentIdentity(
            index_digest=saved_manifest,
            manifest_digest=saved_manifest,
            config_digest=config_digest if archive_config_matches else f"sha256:{'6' * 64}",
        ),
    )
    log = gateway.state_store.open_log("compact-overlay2-parent")
    try:
        if archive_config_matches:
            gateway.prepare_core(config, log=log)
        else:
            with pytest.raises(InstallerFailure, match="could not be bound to its own content"):
                gateway.prepare_core(config, log=log)
    finally:
        log.close()

    if not archive_config_matches:
        assert gateway.state_store.read_env_values(env_path)["FS_COMPACT_IMAGE"] == config_digest
        return
    values = gateway.state_store.read_env_values(env_path)
    assert values["FS_COMPACT_IMAGE"] == config_digest
    assert values["FS_COMPACT_IMAGE_CONFIG_DIGEST"] == config_digest
    assert values["FS_COMPACT_IMAGE_MANIFEST_DIGEST"] == saved_manifest


@pytest.mark.parametrize("mismatch", (None, "config", "descriptor", "platform", "revision", "role"))
def test_compact_registry_config_binding_does_not_equate_regenerated_save_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str | None
) -> None:
    config_digest = f"sha256:{'3' * 64}"
    saved_manifest = f"sha256:{'2' * 64}"
    published_parent = f"sha256:{'9' * 64}"
    published_manifest = f"sha256:{'8' * 64}"
    gateway, config, env_path = _compact_local_install(
        tmp_path,
        descriptor_digest=f"sha256:{'7' * 64}" if mismatch == "descriptor" else None,
        image_id=config_digest,
    )
    monkeypatch.setattr(
        gateway_image_provenance,
        "resolve_local_image_archive",
        lambda **_kwargs: gateway_module.LocalImageContentIdentity(
            index_digest=saved_manifest,
            manifest_digest=saved_manifest,
            config_digest=config_digest,
        ),
    )
    verified_registry = RegistryImageIdentity(
        registry_ref=f"registry.example/compact@{published_manifest}",
        index_digest=published_parent,
        manifest_digest=published_manifest,
        config_digest=f"sha256:{'6' * 64}" if mismatch == "config" else config_digest,
        revision="f" * 40 if mismatch == "revision" else _FULL_REVISION,
        role="fs-cli" if mismatch == "role" else "compact-runtime",
        platform="linux/amd64" if mismatch == "platform" else _HOST_PLATFORM,
        local_image_id=config_digest,
    )
    log = gateway.state_store.open_log(f"compact-registry-save-{mismatch}")
    try:
        if mismatch is not None:
            with pytest.raises(InstallerFailure):
                gateway._prepare_exact_prebuilt_provenance(
                    config, log=log, registry_identity=verified_registry
                )
        else:
            gateway._prepare_exact_prebuilt_provenance(
                config, log=log, registry_identity=verified_registry
            )
    finally:
        log.close()
    if mismatch is not None:
        assert gateway.state_store.read_env_values(env_path) == {"FS_COMPACT_IMAGE": config_digest}
        return
    values = gateway.state_store.read_env_values(env_path)
    assert values["FS_COMPACT_IMAGE"] == f"registry.example/compact@{published_manifest}"
    assert values["FS_COMPACT_IMAGE_INDEX_DIGEST"] == published_parent
    assert values["FS_COMPACT_IMAGE_MANIFEST_DIGEST"] == published_manifest
    assert values["FS_COMPACT_IMAGE_CONFIG_DIGEST"] == config_digest
    # Resume rechecks archive contents without replacing published identity with
    # the regenerated save manifest, and must keep the same durable tuple.
    resume_log = gateway.state_store.open_log("compact-registry-save-resume")
    try:
        gateway._prepare_exact_prebuilt_provenance(
            config, log=resume_log, registry_identity=verified_registry
        )
    finally:
        resume_log.close()
    assert gateway.state_store.read_env_values(env_path) == values


def _compact_overlay2_registry_prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production", deployment_profile="compact")
    repository = "registry.example/compact"
    config_digest = f"sha256:{'3' * 64}"
    manifest_raw = json.dumps(
        {"schemaVersion": 2, "config": {"digest": config_digest}, "layers": []},
        separators=(",", ":"),
    )
    manifest_digest = "sha256:" + hashlib.sha256(manifest_raw.encode()).hexdigest()
    index_raw = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {"digest": manifest_digest, "platform": {"os": "linux", "architecture": "arm64"}}
            ],
        },
        separators=(",", ":"),
    )
    index_digest = "sha256:" + hashlib.sha256(index_raw.encode()).hexdigest()
    parent_ref = f"{repository}@{index_digest}"
    manifest_ref = f"{repository}@{manifest_digest}"
    details = json.loads(
        _image_inspection(
            config_digest,
            service="compact-runtime",
            entrypoint=("python", "-m", "core.entrypoints.cli.compact_provider_runtime"),
            embedded_revision=_FULL_REVISION,
        )
    )
    details[0].update({"Descriptor": None, "RepoDigests": [parent_ref], "Os": "linux"})
    details[0]["Config"]["Labels"]["org.opencontainers.image.role"] = "compact-runtime"
    outputs = {
        ("docker", "buildx", "imagetools", "inspect", "--raw", parent_ref): index_raw,
        ("docker", "buildx", "imagetools", "inspect", "--raw", manifest_ref): manifest_raw,
        ("docker", "image", "inspect", manifest_ref): json.dumps(details),
        ("docker", "image", "inspect", config_digest): json.dumps(details),
        ("docker", "image", "inspect", _CLI_IMAGE_ID): _image_inspection(
            _CLI_IMAGE_ID,
            service="fs-cli",
            entrypoint=("python", "-m", "core.entrypoints.cli.lifecycle_click_app"),
        ),
    }
    for command, stdout in outputs.items():
        process.outcomes[command] = ProcessOutcome(command=command, returncode=0, stdout=stdout)
    env_path = tmp_path / ".env.prod"
    _write_env_values(
        env_path,
        {
            "FS_INSTALL_MODE": "source",
            "FS_COMPACT_IMAGE_REGISTRY_REF": parent_ref,
            "FS_COMPACT_IMAGE_REVISION": _FULL_REVISION,
            "FS_COMPACT_IMAGE_PLATFORM": _HOST_PLATFORM,
            "FS_CLI_IMAGE": _CLI_IMAGE_ID,
        },
    )
    monkeypatch.setattr(
        gateway_image_provenance,
        "resolve_local_image_archive",
        lambda **_kwargs: gateway_module.LocalImageContentIdentity(
            index_digest=f"sha256:{'2' * 64}",
            manifest_digest=f"sha256:{'2' * 64}",
            config_digest=config_digest,
        ),
    )
    return gateway, config, env_path, index_digest, manifest_digest, config_digest


def test_compact_real_registry_prepare_resume_reverifies_and_preserves_published_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, config, env_path, parent, manifest, image_config = _compact_overlay2_registry_prepare(
        tmp_path, monkeypatch
    )
    for attempt in ("initial", "resume"):
        log = gateway.state_store.open_log(f"compact-real-registry-{attempt}")
        try:
            gateway.prepare_core(config, log=log)
        finally:
            log.close()
        values = gateway.state_store.read_env_values(env_path)
        assert values["FS_COMPACT_IMAGE_INDEX_DIGEST"] == parent
        assert values["FS_COMPACT_IMAGE_MANIFEST_DIGEST"] == manifest
        assert values["FS_COMPACT_IMAGE_CONFIG_DIGEST"] == image_config
    # The real resolver must read the published parent bytes on both attempts,
    # not just copy a stale persisted digest after resolving a platform manifest.
    parent_reads = [
        command
        for command in gateway.process.commands
        if command[:5] == ("docker", "buildx", "imagetools", "inspect", "--raw")
        and command[-1] == f"registry.example/compact@{parent}"
    ]
    assert len(parent_reads) == 2


@pytest.mark.parametrize("field", ("MANIFEST_DIGEST", "CONFIG_DIGEST"))
def test_compact_registry_resume_refuses_contradictory_persisted_identity_before_env_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    gateway, config, env_path, _parent, _manifest, _image_config = (
        _compact_overlay2_registry_prepare(tmp_path, monkeypatch)
    )
    log = gateway.state_store.open_log("compact-registry-first")
    try:
        gateway.prepare_core(config, log=log)
    finally:
        log.close()
    values = gateway.state_store.read_env_values(env_path)
    values[f"FS_COMPACT_IMAGE_{field}"] = f"sha256:{'f' * 64}"
    _write_env_values(env_path, values)
    log = gateway.state_store.open_log(f"compact-registry-contradictory-{field}")
    try:
        with pytest.raises(InstallerFailure):
            gateway.prepare_core(config, log=log)
    finally:
        log.close()
    assert gateway.state_store.read_env_values(env_path) == values


def test_compact_provenance_refuses_an_image_that_is_not_its_own_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Substitution is still rejected when no descriptor exists to compare."""
    image_id = f"sha256:{'4' * 64}"
    gateway, config, _env_path = _compact_local_install(
        tmp_path, descriptor_digest=None, image_id=image_id
    )
    monkeypatch.setattr(
        gateway_image_provenance,
        "resolve_local_image_archive",
        lambda **_kwargs: gateway_module.LocalImageContentIdentity(
            index_digest=f"sha256:{'5' * 64}",
            manifest_digest=f"sha256:{'6' * 64}",
            config_digest=f"sha256:{'7' * 64}",
        ),
    )
    log = gateway.state_store.open_log("compact-substituted")

    with pytest.raises(InstallerFailure) as raised:
        gateway.prepare_core(config, log=log)

    log.close()
    message = f"{raised.value.cause} {raised.value.remediation}"
    assert "could not be bound to its own content" in message
    # the operator is told what was observed, not just that something failed
    assert image_id in message
    assert f"sha256:{'5' * 64}" in message


def test_compact_build_stamps_both_provenance_arguments(tmp_path: Path) -> None:
    """AC17-R92 must not reintroduce AC17-R91.

    The compact-runtime stage bakes FS_EMBEDDED_IMAGE_REVISION and
    FS_EMBEDDED_IMAGE_PLATFORM from BUILD_SHA and FS_IMAGE_PLATFORM, both defaulting
    to "unknown", and FS_IMAGE_PLATFORM is not derived from BuildKit's
    TARGETPLATFORM. A build that omits either produces an image the installer
    accepts and the runtime rejects with EX_CONFIG. That is why the installer runs
    the build itself instead of adding a Compose `build:` stanza.
    """
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment={})
    config = InstallConfig(deployment_profile="compact", environment="production")
    built_image = f"sha256:{'e' * 64}"
    tag = f"flow-steward-compact:flow-steward-{_FULL_REVISION[:12]}"
    inspect_command = ("docker", "image", "inspect", "--format", "{{.Id}}", tag)
    process.outcomes[inspect_command] = ProcessOutcome(
        command=inspect_command, returncode=0, stdout=built_image + "\n"
    )
    log = gateway.state_store.open_log("compact-build")

    gateway._build_compact_runtime_image(config, log=log)
    log.close()

    build = next(c for c in process.commands if c[:3] == ("docker", "buildx", "build"))
    assert f"BUILD_SHA={_FULL_REVISION}" in build
    assert f"FS_IMAGE_PLATFORM={_HOST_PLATFORM}" in build
    assert "--target" in build
    assert "compact-runtime" in build
    # the built image is handed to the existing exact-prebuilt provenance path
    assert gateway_module._read_env(tmp_path / ".env.prod")["FS_COMPACT_IMAGE"] == built_image


def test_compact_build_is_skipped_when_an_image_was_supplied(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment=dict(_COMPACT_TEST_IMAGE))
    config = InstallConfig(deployment_profile="compact", environment="production")
    log = gateway.state_store.open_log("compact-build-skipped")

    gateway._build_compact_runtime_image(config, log=log)
    log.close()

    assert not any(c[:3] == ("docker", "buildx", "build") for c in process.commands)


def test_preflight_requires_an_official_release_when_no_compact_image_is_supplied(
    tmp_path: Path,
) -> None:
    """AC17-R92: the owner withdrew the "prebuilt image only" premise.

    AC17-R81 made preflight refuse a Compact install with no image, because
    `compact-runtime` declared `image:` and no `build:`. Most operators clone the
    repository and will never hold a prebuilt image, so that refusal failed the
    product at its first step. The installer now builds the image itself, and
    preflight must let the install proceed. Do not reinstate the refusal.
    """
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment={})
    log = gateway.state_store.open_log("compact-builds-its-own-image")
    config = InstallConfig(deployment_profile="compact", environment="production")

    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(config, log=log)
    log.close()
    assert raised.value.code == "official_release_missing"


def test_preflight_refuses_compact_when_the_tree_cannot_build_an_image(tmp_path: Path) -> None:
    """The precondition that survives R92: an unbuildable tree still fails early.

    It must fail before configuration, secrets and a certificate are written, and it
    must name the missing Dockerfile rather than a build or pull that never ran.
    """
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment={})
    (tmp_path / "infra/docker/Dockerfile").unlink()
    log = gateway.state_store.open_log("compact-unbuildable")
    config = InstallConfig(deployment_profile="compact", environment="production")

    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(config, log=log)

    log.close()
    failure = raised.value
    assert failure.code == "compact_image_unbuildable"
    assert failure.step == "preflight"
    assert "infra/docker/Dockerfile" in failure.cause
    assert not (tmp_path / ".env.prod").exists()
    assert not (tmp_path / ".ssl").exists()
    assert process.commands == []
    assert not (gateway.repo_root / ".env.prod").exists()


@pytest.mark.parametrize(
    "supplied",
    (
        "fs-compact:47b080d61",  # the measured case: a locally built, tagged image
        "flow-steward-compact",  # no tag at all, which Docker reads as :latest
        "registry.example/flow-steward@sha256:abc",  # a digest too short to be one
        "@sha256:" + "b" * 64,  # a digest with no repository to pull it from
    ),
)
def test_preflight_refuses_a_compact_image_that_is_not_pinned(
    tmp_path: Path, supplied: str
) -> None:
    """AC17-R139: a tag passed silently and failed minutes later inside Compose.

    It skipped the resolution that writes FS_COMPACT_IMAGE_REVISION and the rest of
    the image identity, so the install died after configuration, secrets and a
    certificate were already written, with a raw interpolation error that sent the
    operator to add a variable by hand. It must fail here, and name the image.
    """
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment={"FS_COMPACT_IMAGE": supplied})
    log = gateway.state_store.open_log("compact-image-not-pinned")
    config = InstallConfig(deployment_profile="compact", environment="production")

    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(config, log=log)

    log.close()
    failure = raised.value
    assert failure.code == "compact_image_not_pinned"
    assert failure.step == "preflight"
    assert supplied in failure.cause
    assert "@sha256:" in failure.remediation
    assert "FS_COMPACT_IMAGE_REVISION" not in failure.cause + failure.remediation
    assert not (tmp_path / ".env.prod").exists()
    assert not (tmp_path / ".ssl").exists()
    assert process.commands == []


@pytest.mark.parametrize(
    "recorded",
    (
        # What a self-build records: its image ID.
        {"FS_COMPACT_IMAGE": "sha256:" + "e" * 64},
        # What a registry resolution records: the ID and the pinned reference.
        {
            "FS_COMPACT_IMAGE": "sha256:" + "e" * 64,
            "FS_COMPACT_IMAGE_REGISTRY_REF": "registry.example/flow-steward@sha256:" + "b" * 64,
        },
    ),
)
def test_preflight_never_refuses_what_the_installer_recorded_itself(
    tmp_path: Path, recorded: dict[str, str]
) -> None:
    """The green direction, and the one that would break every resume if it went red."""
    process = FakeProcessExecutor()
    _write_env_values(tmp_path / ".env.prod", recorded)
    gateway = _gateway(tmp_path, process, environment={})
    log = gateway.state_store.open_log("compact-image-resume")

    gateway._require_compact_image_input(
        InstallConfig(deployment_profile="compact", environment="production")
    )
    log.close()


def test_preflight_accepts_the_documented_compact_registry_reference_from_the_environment(
    tmp_path: Path,
) -> None:
    # The runbook's documented input is an exported FS_COMPACT_IMAGE_REGISTRY_REF. It was
    # unreachable: install.sh forwarded no FS_COMPACT_* name into the installer container,
    # and the only other route -- writing it into .env.prod -- needs a file a clean tree
    # does not have yet.
    process = FakeProcessExecutor()
    gateway = _gateway(
        tmp_path,
        process,
        environment={
            "FS_COMPACT_IMAGE_REGISTRY_REF": "registry.example/flow-steward@sha256:" + "b" * 64,
            "FS_COMPACT_IMAGE_REVISION": _FULL_REVISION,
            "FS_COMPACT_IMAGE_PLATFORM": "linux/arm64",
        },
    )
    _stage_official_release(gateway, "compact")
    log = gateway.state_store.open_log("compact-registry-ref")

    gateway.preflight(
        InstallConfig(deployment_profile="compact", environment="production"), log=log
    )
    log.close()


def test_preflight_accepts_a_compact_image_already_recorded_by_an_earlier_run(
    tmp_path: Path,
) -> None:
    # A resume re-runs preflight with the operator's shell long gone. The value it supplied
    # the first time lives in .env.prod, and refusing it here would break every resume.
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment={})
    _stage_official_release(gateway, "compact")
    _write_env_values(gateway.repo_root / ".env.prod", {"FS_COMPACT_IMAGE": "sha256:" + "c" * 64})
    log = gateway.state_store.open_log("compact-image-resumed")

    gateway.preflight(
        InstallConfig(deployment_profile="compact", environment="production"), log=log
    )
    log.close()


def test_installer_forwards_every_isolation_override_it_reads(tmp_path: Path) -> None:
    # The two lists are a contract split across a shell script and a Python module: a name
    # the gateway reads from the environment is unreachable unless install.sh forwards it
    # into the container. FS_COMPACT_* was the drift that made the documented Compact input
    # impossible to supply; this pins the pair together so the next one cannot repeat it.
    from core.infrastructure.installer.gateway import _BOOTSTRAP_ISOLATION_ENV_NAMES

    script = (Path(__file__).resolve().parents[3] / "install.sh").read_text(encoding="utf-8")
    block = script.split("for env_name in \\", 1)[1].split("; do", 1)[0]
    forwarded = {line.strip().rstrip("\\").strip() for line in block.splitlines()}

    missing = sorted(name for name in _BOOTSTRAP_ISOLATION_ENV_NAMES if name not in forwarded)
    assert missing == [], f"install.sh does not forward: {missing}"


def test_public_route_certificate_mismatch_names_the_certificate_not_healthy_components(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # AC17-R83: bundled Traefik published `tls.certificates` with no default store, so a
    # client whose SNI matched none of them was served `CN=TRAEFIK DEFAULT CERT`. Every
    # route answered 200 and both Traefik and fs-app were healthy, yet the install failed
    # for 90 attempts telling the operator to go and "Inspect Traefik and fs-app health":
    # the two things that were working.
    gateway = _gateway(tmp_path, FakeProcessExecutor(), environment=_COMPACT_TEST_IMAGE)
    config = InstallConfig(tls_mode="self_signed", public_host="192.168.0.168")
    certificate = tmp_path / config.certificate_path
    certificate.parent.mkdir(parents=True)
    certificate.write_text("not a pem", encoding="utf-8")
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())

    def refuse(_url, *, timeout, context=None):
        raise ssl.SSLCertVerificationError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"
        )

    monkeypatch.setattr(gateway_module.urlrequest, "urlopen", refuse)
    monkeypatch.setattr(gateway, "_readiness_wait", lambda *_a, **_k: None)
    log = gateway.state_store.open_log("public-route-certificate")

    with pytest.raises(InstallerFailure) as raised:
        gateway.verify_public_route(config, log=log)

    log.close()
    failure = raised.value
    assert failure.code == "public_certificate_mismatch"
    assert failure.step == "public_route"
    # it points at the certificate, and no longer sends the operator to healthy components
    assert ".ssl/cert.pem" in failure.remediation
    assert "default certificate" in failure.remediation
    assert "fs-app health" not in failure.remediation


def test_a_rerun_after_a_source_update_rebuilds_instead_of_reusing_the_old_image(
    tmp_path: Path,
) -> None:
    """AC17-R102: the update mechanism reported success without applying the update.

    On an existing installation `./install.sh` finished with `"status": "installed"`
    and no build step in the journal, because a reference the previous run persisted
    is read back the same way as one the operator supplied -- so the branch meaning
    "the operator brought their own image" was taken by the installer's own
    bookkeeping. A release that closes a dependency vulnerability would be reported as
    applied and would not be.
    """
    process = FakeProcessExecutor()
    built_image = "sha256:" + "f" * 64
    inspect_built = (
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        f"flow-steward-compact:flow-steward-{_FULL_REVISION[:12]}",
    )
    process.outcomes[inspect_built] = ProcessOutcome(
        command=inspect_built, returncode=0, stdout=built_image + "\n"
    )
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )
    env_path = tmp_path / ".env.prod"
    _write_env_values(
        env_path,
        {
            "FS_COMPACT_IMAGE": "sha256:" + "b" * 64,
            "FS_COMPACT_IMAGE_REGISTRY_REF": "example.invalid/compact@sha256:" + "c" * 64,
            "FS_COMPACT_IMAGE_REVISION": "1111111111111111111111111111111111111111",
        },
    )
    log = gateway.state_store.open_log("stale-image-rerun")

    gateway._build_compact_runtime_image(config, log=log)

    log.close()
    build = [c for c in process.commands if c[:3] == ("docker", "buildx", "build")]
    assert build, "the installer reused an image built from different sources"
    assert f"BUILD_SHA={_FULL_REVISION}" in build[0]
    values = gateway_module._read_env(env_path)
    assert values["FS_COMPACT_IMAGE"] == built_image
    # the previous run's digest pin must not survive, or it wins again downstream
    assert values["FS_COMPACT_IMAGE_REGISTRY_REF"] == ""
    # and the new image is dated, or every later run would rebuild it again
    assert values["FS_COMPACT_IMAGE_REVISION"] == _FULL_REVISION


def test_a_rerun_at_the_same_revision_does_not_rebuild(tmp_path: Path) -> None:
    """An unchanged tree must stay cheap: this runs on every resume."""
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )
    _write_env_values(
        tmp_path / ".env.prod",
        {
            "FS_COMPACT_IMAGE": "sha256:" + "b" * 64,
            "FS_COMPACT_IMAGE_REVISION": _FULL_REVISION,
        },
    )
    log = gateway.state_store.open_log("same-revision-rerun")

    gateway._build_compact_runtime_image(config, log=log)

    log.close()
    assert [c for c in process.commands if c[:3] == ("docker", "buildx", "build")] == []


def test_an_operator_supplied_image_is_never_replaced_by_a_rebuild(tmp_path: Path) -> None:
    """A reference named in the environment is the operator's choice, not bookkeeping."""
    process = FakeProcessExecutor()
    gateway = _gateway(
        tmp_path,
        process,
        environment={"FS_COMPACT_IMAGE": "sha256:" + "d" * 64},
    )
    config = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )
    _write_env_values(
        tmp_path / ".env.prod",
        {"FS_COMPACT_IMAGE_REVISION": "2222222222222222222222222222222222222222"},
    )
    log = gateway.state_store.open_log("operator-supplied-image")

    gateway._build_compact_runtime_image(config, log=log)

    log.close()
    assert [c for c in process.commands if c[:3] == ("docker", "buildx", "build")] == []


def test_an_image_recorded_without_a_revision_is_left_alone_for_now(tmp_path: Path) -> None:
    """The locally pinned exact-prebuilt path supplies digests and no revision.

    Treating that as stale is the safer rule and is not taken here: it would rebuild
    over a supported way to install, which is a decision about the product rather than
    part of AC17-R102. Pinned so the choice is visible if someone changes it.
    """
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )
    _write_env_values(tmp_path / ".env.prod", {"FS_COMPACT_IMAGE": "sha256:" + "e" * 64})
    log = gateway.state_store.open_log("undated-image")

    gateway._build_compact_runtime_image(config, log=log)

    log.close()
    assert [c for c in process.commands if c[:3] == ("docker", "buildx", "build")] == []


def _candidate_env(root: Path, revision: str, **overrides: str) -> Path:
    values = {
        "FS_APP_IMAGE": "sha256:" + "1" * 64,
        "FS_APP_IMAGE_DIGEST": "sha256:" + "1" * 64,
        "FS_WORKER_IMAGE": "sha256:" + "2" * 64,
        "FS_WORKER_IMAGE_DIGEST": "sha256:" + "2" * 64,
        "FS_CONTROL_IMAGE": "sha256:" + "3" * 64,
        "FS_CONTROL_IMAGE_DIGEST": "sha256:" + "3" * 64,
        "FS_CLI_IMAGE": "sha256:" + "4" * 64,
        "MCP_IMAGE": "sha256:" + "5" * 64,
        "MCP_IMAGE_DIGEST": "sha256:" + "5" * 64,
        "POSTGRES_IMAGE": "sha256:" + "6" * 64,
    }
    values.update(overrides)
    path = root / ".flow-steward/release" / revision / "candidate.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_env_values(path, values)
    return path


def test_a_scalable_install_builds_its_images_with_the_release_path(tmp_path: Path) -> None:
    """AC17-R104: the installer refused an install it had the means to complete.

    A Scalable install died at "Prepare core images" with `required variable
    FS_CLI_IMAGE is missing a value`, under advice to resolve a Docker build failure
    that had not happened -- no build was attempted. docker-compose.prod.yaml declares
    five images and no build stanzas, the installer could build one of them, and
    scripts/release/build_candidate.sh has built all five for about a year. Nothing was
    missing but the wiring.
    """
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    _candidate_env(gateway.repo_root, _FULL_REVISION)
    config = InstallConfig(
        environment="production", deployment_profile="scalable", file_storage="local_fs"
    )
    log = gateway.state_store.open_log("scalable-candidate-build")

    built = gateway._build_scalable_candidate_images(config, log=log)

    log.close()
    assert built
    assert _candidate_builds(process.commands), "the release build path was never invoked"
    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert values["FS_CLI_IMAGE"] == "sha256:" + "4" * 64
    # candidate.env does not write this and docker-compose.prod.yaml requires it, so
    # wiring the build alone would have moved the failure rather than removed it
    assert values["MCP_IMAGE_REVISION"] == _FULL_REVISION


def test_supplied_scalable_images_are_never_rebuilt_underneath_the_operator(
    tmp_path: Path,
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(
        tmp_path,
        process,
        # the whole contract, because that is what "already managed" means now
        environment={
            "FS_APP_IMAGE": "registry.invalid/app@sha256:" + "a" * 64,
            "FS_APP_IMAGE_DIGEST": "sha256:" + "a" * 64,
            "FS_WORKER_IMAGE": "registry.invalid/worker@sha256:" + "b" * 64,
            "FS_WORKER_IMAGE_DIGEST": "sha256:" + "b" * 64,
            "FS_CONTROL_IMAGE": "registry.invalid/control@sha256:" + "c" * 64,
            "FS_CONTROL_IMAGE_DIGEST": "sha256:" + "c" * 64,
            "FS_CLI_IMAGE": "registry.invalid/cli@sha256:" + "d" * 64,
            "MCP_IMAGE": "registry.invalid/mcp@sha256:" + "e" * 64,
            "MCP_IMAGE_DIGEST": "sha256:" + "e" * 64,
            "POSTGRES_IMAGE": "registry.invalid/pg@sha256:" + "f" * 64,
        },
    )
    config = InstallConfig(
        environment="production", deployment_profile="scalable", file_storage="local_fs"
    )
    log = gateway.state_store.open_log("scalable-supplied-images")

    built = gateway._build_scalable_candidate_images(config, log=log)

    log.close()
    assert built is False
    assert not [c for c in process.commands if c[0] == "bash"]


def test_a_candidate_build_that_names_no_images_fails_loudly(tmp_path: Path) -> None:
    """A build that reports success and produces nothing must not reach "installed"."""
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    _candidate_env(gateway.repo_root, _FULL_REVISION, FS_CLI_IMAGE="")
    config = InstallConfig(
        environment="production", deployment_profile="scalable", file_storage="local_fs"
    )
    log = gateway.state_store.open_log("scalable-empty-candidate")

    with pytest.raises(InstallerFailure) as raised:
        gateway._build_scalable_candidate_images(config, log=log)

    log.close()
    assert raised.value.code == "scalable_image_build_failed"
    assert "FS_CLI_IMAGE" in str(raised.value.cause)


def test_a_compact_install_does_not_take_the_scalable_build_path(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )
    log = gateway.state_store.open_log("compact-not-scalable")

    assert gateway._build_scalable_candidate_images(config, log=log) is False
    log.close()


def test_an_image_recorded_without_a_revision_is_dated_from_its_own_label(
    tmp_path: Path,
) -> None:
    """AC17-R102 with AC17-R109's realisation: the artifact already knows.

    The env file can record an image without a revision -- the locally pinned
    exact-prebuilt path does exactly that -- and treating it as undatable meant either
    rebuilding over a supported way to install or accepting an image nobody could
    date. The image carries org.opencontainers.image.revision from build time, so it
    is asked instead of the environment.
    """
    process = FakeProcessExecutor()
    label_command = (
        "docker",
        "image",
        "inspect",
        "--format",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
        "sha256:" + "e" * 64,
    )
    process.outcomes[label_command] = ProcessOutcome(
        command=label_command, returncode=0, stdout="7" * 40 + "\n"
    )
    built_image = "sha256:" + "9" * 64
    inspect_built = (
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        f"flow-steward-compact:flow-steward-{_FULL_REVISION[:12]}",
    )
    process.outcomes[inspect_built] = ProcessOutcome(
        command=inspect_built, returncode=0, stdout=built_image + "\n"
    )
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )
    _write_env_values(tmp_path / ".env.prod", {"FS_COMPACT_IMAGE": "sha256:" + "e" * 64})
    log = gateway.state_store.open_log("label-dated-image")

    gateway._build_compact_runtime_image(config, log=log)

    log.close()
    assert [c for c in process.commands if c[:3] == ("docker", "buildx", "build")], (
        "the image's own label said it predates these sources and it was reused anyway"
    )


def test_an_image_that_answers_nothing_about_itself_is_left_alone(tmp_path: Path) -> None:
    """A foreign image carrying no revision label is not this method's business."""
    process = FakeProcessExecutor()
    label_command = (
        "docker",
        "image",
        "inspect",
        "--format",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
        "sha256:" + "e" * 64,
    )
    process.outcomes[label_command] = ProcessOutcome(
        command=label_command, returncode=0, stdout="<no value>\n"
    )
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )
    _write_env_values(tmp_path / ".env.prod", {"FS_COMPACT_IMAGE": "sha256:" + "e" * 64})
    log = gateway.state_store.open_log("foreign-image")

    gateway._build_compact_runtime_image(config, log=log)

    log.close()
    assert [c for c in process.commands if c[:3] == ("docker", "buildx", "build")] == []


def test_the_installers_own_default_tags_do_not_pass_for_supplied_images(
    tmp_path: Path,
) -> None:
    """AC17-R116: the guard was never false, so the build never ran.

    "Save installation configuration" runs before this step and writes the environment
    registry's defaults into .env.prod, so FS_APP_IMAGE arrives as
    the current app image tag before anyone asks whether images were supplied. Reading
    that back as the operator's choice is the same mistake AC17-R102 fixed in the
    sibling method, and it meant a clean Scalable install could not build -- exactly
    the case the change existed for. The defaults are tags besides, and the Compose
    contract is digests, so they could not have satisfied it either way.
    """
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(
        environment="production", deployment_profile="scalable", file_storage="local_fs"
    )
    _candidate_env(gateway.repo_root, _FULL_REVISION)
    _write_env_values(
        tmp_path / ".env.prod",
        {
            name: registry_default_value(name)
            for name in gateway_module.DockerInstallerGateway._SCALABLE_IMAGE_NAMES
        },
    )
    log = gateway.state_store.open_log("scalable-default-tags")

    built = gateway._build_scalable_candidate_images(config, log=log)

    log.close()
    assert built, "the installer read its own defaults as images the operator supplied"
    assert _candidate_builds(process.commands)


def test_the_guard_ignores_the_installers_own_registry_defaults(tmp_path: Path) -> None:
    """The discriminator is "did anyone choose this", and the registry defines the default.

    Requiring the digests instead looked tempting -- the production Compose demands
    them -- and would have been wrong: they are produced by the provenance path inside
    prepare_core itself, so the guard would have rebuilt over an installation that was
    about to derive them. A pre-existing provenance test caught that.
    """
    from core.infrastructure.config.env_registry import registry_default_value

    known = {
        name: registry_default_value(name)
        for name in gateway_module.DockerInstallerGateway._SCALABLE_IMAGE_NAMES
    }
    chosen = {
        name for name in known if gateway_module.DockerInstallerGateway._is_chosen(name, known)
    }
    assert chosen == set(), f"these registry defaults were read as an operator's choice: {chosen}"

    picked = dict(known, FS_APP_IMAGE="registry.invalid/app@sha256:" + "a" * 64)
    assert gateway_module.DockerInstallerGateway._is_chosen("FS_APP_IMAGE", picked)


def test_a_successful_command_that_warns_does_not_corrupt_the_value_it_printed() -> None:
    """AC17-R138: an install stopped on a revision git had resolved correctly.

    The executor merges stderr into stdout by default, which is what the operation
    log wants -- one transcript in the order it happened. But a caller reading the
    output as a *value* then gets the warnings too, and `rev-parse` is allowed to
    warn while succeeding: dubious ownership, unreadable alternates, a missing hook.
    The install failed with "could not resolve a full 40-character source revision"
    while holding the correct forty characters on the line below a warning.

    Measured against the real executor rather than a stub, because the thing under
    test is which stream the bytes arrive on.
    """
    from core.infrastructure.installer.process import SubprocessExecutor

    script = "import sys; sys.stderr.write('warning: something\\n'); print('deadbeef' * 5)"

    merged = SubprocessExecutor().run([sys.executable, "-c", script])
    separated = SubprocessExecutor().run([sys.executable, "-c", script], separate_stderr=True)

    assert merged.returncode == 0
    assert separated.returncode == 0
    assert "warning" in merged.stdout
    assert merged.stdout.strip() != "deadbeef" * 5
    # Apart, the value is the value and the warning is still kept for the log.
    assert separated.stdout.strip() == "deadbeef" * 5
    assert "warning" in separated.stderr


def test_every_revision_lookup_keeps_the_streams_apart() -> None:
    """Derived from the call, not from a list of the ones that were wrong.

    Three of these read the revision only to compare it, so a warning made the
    installer quietly decide an image could not be reused and rebuild it -- no error,
    just work nobody asked for. Those are worth more than the one that failed loudly.
    """
    import re

    source = Path(gateway_module.__file__).read_text(encoding="utf-8")
    offenders = []
    for match in re.finditer(r'"rev-parse"', source):
        # Both directions: the flag follows the command, and a helper that already
        # asks for it precedes the command. Looking only forward reported the one
        # site that was correct.
        window = source[max(0, match.start() - 240) : match.start() + 320]
        if "separate_stderr=True" in window or "checked_source_revision" in window:
            continue
        offenders.append(source[: match.start()].count("\n") + 1)
    assert not offenders, (
        f"lines {offenders} read a git revision from a stream the executor merged "
        "with stderr; a successful command is allowed to warn"
    )


# --- AC17-R141: comparing the sources, what was recorded, and what is running ---

_R141_OLD = "1" * 40
_R141_RUNNING = "sha256:" + "e" * 64
_R141_OTHER = "sha256:" + "f" * 64


class _RuntimeExecutor(FakeProcessExecutor):
    """Answers the questions image_currency asks Docker, by shape rather than by full line.

    The Compose prefix those commands carry is the gateway's business and long; what
    these tests control is which container runs, from which image, dated how.
    """

    def __init__(
        self,
        *,
        running: dict[str, str],
        labels: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.running = running
        self.labels = labels or {}
        self.tags = tags or {}

    def run(self, command, **kwargs):
        key = tuple(command)
        if key[:3] == ("docker", "ps", "-q"):
            self.commands.append(key)
            prefix = "label=com.docker.compose.service="
            service = next(part[len(prefix) :] for part in key if part.startswith(prefix))
            stdout = f"cid-{service}\n" if service in self.running else ""
            return ProcessOutcome(command=key, returncode=0, stdout=stdout)
        if key[:5] == ("docker", "container", "inspect", "--format", "{{.Image}}"):
            self.commands.append(key)
            service = key[5].removeprefix("cid-")
            return ProcessOutcome(command=key, returncode=0, stdout=self.running[service] + "\n")
        if key[:5] == ("docker", "image", "inspect", "--format", "{{.Id}}"):
            self.commands.append(key)
            found = self.tags.get(key[5], "")
            return ProcessOutcome(command=key, returncode=0 if found else 1, stdout=found + "\n")
        if key[:4] == ("docker", "image", "inspect", "--format") and "image.revision" in key[4]:
            self.commands.append(key)
            return ProcessOutcome(
                command=key, returncode=0, stdout=self.labels.get(key[5], "") + "\n"
            )
        return super().run(command, **kwargs)


def _compact() -> InstallConfig:
    return InstallConfig(deployment_profile="compact", environment="production")


def _scalable() -> InstallConfig:
    return InstallConfig(deployment_profile="scalable", environment="production")


def _currency(tmp_path: Path, process: FakeProcessExecutor, config: InstallConfig, **kwargs):
    gateway = _gateway(tmp_path, process, **kwargs)
    log = gateway.state_store.open_log("image-currency")
    try:
        return gateway.image_currency(config, log=log)
    finally:
        log.close()


def test_a_compact_image_built_from_older_sources_is_due_for_update(tmp_path: Path) -> None:
    """The measured case on fs-r123: recorded 47b080d6, sources c80fd8b1."""
    _write_env_values(
        tmp_path / ".env.prod",
        {"FS_COMPACT_IMAGE": _R141_RUNNING, "FS_COMPACT_IMAGE_REVISION": _R141_OLD},
    )
    process = _RuntimeExecutor(
        running={"compact-runtime": _R141_RUNNING}, labels={_R141_RUNNING: _R141_OLD}
    )

    currency = _currency(tmp_path, process, _compact(), environment={})

    assert currency.update_required is True
    assert currency.resumes_prepared_update is False
    assert currency.source_revision == _FULL_REVISION
    assert currency.running_revision == _R141_OLD
    assert _R141_OLD in currency.reason


def test_a_current_compact_installation_is_not_due_for_anything(tmp_path: Path) -> None:
    """The green direction: without it every rerun would rebuild."""
    _write_env_values(
        tmp_path / ".env.prod",
        {"FS_COMPACT_IMAGE": _R141_RUNNING, "FS_COMPACT_IMAGE_REVISION": _FULL_REVISION},
    )
    process = _RuntimeExecutor(
        running={"compact-runtime": _R141_RUNNING}, labels={_R141_RUNNING: _FULL_REVISION}
    )

    currency = _currency(tmp_path, process, _compact(), environment={})

    assert currency.update_required is False
    assert currency.running_revision == _FULL_REVISION


def test_a_recorded_image_that_never_started_is_finished_not_restarted_around(
    tmp_path: Path,
) -> None:
    """What a schema refusal leaves: the new image recorded, the old one running.

    Taken for "already installed", the next run would recreate the runtime from the
    recorded image -- new code on a database whose migration has not been applied.
    """
    _write_env_values(
        tmp_path / ".env.prod",
        {"FS_COMPACT_IMAGE": _R141_OTHER, "FS_COMPACT_IMAGE_REVISION": _FULL_REVISION},
    )
    process = _RuntimeExecutor(
        running={"compact-runtime": _R141_RUNNING}, labels={_R141_RUNNING: _R141_OLD}
    )

    currency = _currency(tmp_path, process, _compact(), environment={})

    assert currency.update_required is True
    assert currency.resumes_prepared_update is True
    assert "compact-runtime" in currency.reason


def test_a_compact_image_the_operator_named_is_never_updated_underneath_them(
    tmp_path: Path,
) -> None:
    """AC17-R102's rule, kept: a name in the process environment is a choice."""
    _write_env_values(
        tmp_path / ".env.prod",
        {"FS_COMPACT_IMAGE": _R141_RUNNING, "FS_COMPACT_IMAGE_REVISION": _R141_OLD},
    )
    process = _RuntimeExecutor(
        running={"compact-runtime": _R141_RUNNING}, labels={_R141_RUNNING: _R141_OLD}
    )

    currency = _currency(
        tmp_path, process, _compact(), environment={"FS_COMPACT_IMAGE": _R141_RUNNING}
    )

    assert currency.update_required is False
    # Still dated, so the service can say the pin is behind rather than call it current.
    assert currency.running_revision == _R141_OLD


def test_a_runtime_that_is_not_running_is_not_mistaken_for_an_update(tmp_path: Path) -> None:
    _write_env_values(
        tmp_path / ".env.prod",
        {"FS_COMPACT_IMAGE": _R141_RUNNING, "FS_COMPACT_IMAGE_REVISION": _FULL_REVISION},
    )
    process = _RuntimeExecutor(running={})

    currency = _currency(tmp_path, process, _compact(), environment={})

    assert currency.update_required is False


def test_image_currency_makes_no_claim_about_a_development_stack(tmp_path: Path) -> None:
    """Development builds through Compose; nothing here is asked about it."""
    process = _RuntimeExecutor(running={})

    currency = _currency(
        tmp_path,
        process,
        InstallConfig(deployment_profile="scalable", environment="development"),
        environment={},
    )

    assert currency.update_required is False
    assert process.commands == []


_R141_SCALABLE = {
    "FS_APP_IMAGE": "flow-steward-app:111111111111",
    "FS_WORKER_IMAGE": "flow-steward-worker:111111111111",
    "FS_CONTROL_IMAGE": "flow-steward-control:111111111111",
    "FS_CLI_IMAGE": "flow-steward-cli:111111111111",
    "MCP_IMAGE": "flow-steward-mcp:111111111111",
    "POSTGRES_IMAGE": "flow-steward-postgres:111111111111",
}


def _record_scalable_build(tmp_path: Path, revision: str, names: dict[str, str]) -> None:
    release = tmp_path / ".flow-steward/release"
    (release / "endpoint-state").mkdir(parents=True, exist_ok=True)  # not a build
    build = release / revision
    build.mkdir(parents=True, exist_ok=True)
    _write_env_values(build / "candidate.env", names)


def _scalable_process() -> _RuntimeExecutor:
    return _RuntimeExecutor(
        running={"fs-app": _R141_RUNNING},
        labels={_R141_RUNNING: _R141_OLD},
        tags={"flow-steward-app:111111111111": _R141_RUNNING},
    )


def test_scalable_images_this_installer_built_from_older_sources_are_due_for_update(
    tmp_path: Path,
) -> None:
    """The measured case on fs-scal12, checked against the build's own record."""
    _record_scalable_build(tmp_path, _R141_OLD, _R141_SCALABLE)
    _write_env_values(tmp_path / ".env.prod", {**_R141_SCALABLE, "MCP_IMAGE_REVISION": _R141_OLD})

    currency = _currency(tmp_path, _scalable_process(), _scalable(), environment={})

    assert currency.update_required is True
    assert currency.resumes_prepared_update is False


def test_scalable_images_pinned_by_hand_are_never_rebuilt_underneath_the_operator(
    tmp_path: Path,
) -> None:
    """The promise R102's rule would have broken: pins written into .env.prod by hand."""
    _record_scalable_build(tmp_path, _R141_OLD, _R141_SCALABLE)
    pinned = {**_R141_SCALABLE, "FS_APP_IMAGE": "registry.example/app:2026.09"}
    _write_env_values(tmp_path / ".env.prod", {**pinned, "MCP_IMAGE_REVISION": _R141_OLD})
    process = _scalable_process()
    process.tags["registry.example/app:2026.09"] = _R141_RUNNING

    currency = _currency(tmp_path, process, _scalable(), environment={})

    assert currency.update_required is False
    assert currency.running_revision == _R141_OLD


def test_a_release_directory_without_a_build_record_is_not_read_as_one(tmp_path: Path) -> None:
    """release/ also holds endpoint-state; only the recorded revision's own file counts."""
    _record_scalable_build(tmp_path, "3" * 40, _R141_SCALABLE)  # some other build
    _write_env_values(tmp_path / ".env.prod", {**_R141_SCALABLE, "MCP_IMAGE_REVISION": _R141_OLD})

    currency = _currency(tmp_path, _scalable_process(), _scalable(), environment={})

    assert currency.update_required is False


def test_stale_scalable_images_are_rebuilt(tmp_path: Path) -> None:
    """The image build is where the decision is acted on, so it is checked there too."""
    _record_scalable_build(tmp_path, _R141_OLD, _R141_SCALABLE)
    _record_scalable_build(tmp_path, _FULL_REVISION, _R141_SCALABLE)  # what the build writes
    _write_env_values(tmp_path / ".env.prod", {**_R141_SCALABLE, "MCP_IMAGE_REVISION": _R141_OLD})
    process = _RuntimeExecutor(running={})
    gateway = _gateway(tmp_path, process, environment={})
    log = gateway.state_store.open_log("scalable-rebuild")

    assert gateway._build_scalable_candidate_images(_scalable(), log=log) is True
    log.close()

    assert _candidate_builds(process.commands)


def test_scalable_images_pinned_by_hand_are_not_rebuilt(tmp_path: Path) -> None:
    _record_scalable_build(tmp_path, _R141_OLD, _R141_SCALABLE)
    _write_env_values(
        tmp_path / ".env.prod",
        {
            **_R141_SCALABLE,
            "FS_APP_IMAGE": "registry.example/app:2026.09",
            "MCP_IMAGE_REVISION": _R141_OLD,
        },
    )
    process = _RuntimeExecutor(running={})
    gateway = _gateway(tmp_path, process, environment={})
    log = gateway.state_store.open_log("scalable-pinned")

    assert gateway._build_scalable_candidate_images(_scalable(), log=log) is False
    log.close()

    assert not _candidate_builds(process.commands)


# --- AC17-R141 acceptance: an update refused by the installation's own ports ---


@pytest.mark.parametrize(
    ("listing", "expected"),
    (
        # Exactly what fs-r123's Traefik printed: two adjacent ports, one range.
        ("0.0.0.0:8595-8596->8595-8596/tcp", {8595, 8596}),
        ("0.0.0.0:8595-8596->8595-8596/tcp, [::]:8595-8596->8595-8596/tcp", {8595, 8596}),
        ("0.0.0.0:8592->8592/tcp, :::8592->8592/tcp", {8592}),
        ("127.0.0.1:8591->8591/tcp", {8591}),
        # Exposed, not published: no host port is held.
        ("5432/tcp", set()),
        ("", set()),
        ("0.0.0.0:8080->8080/tcp\n0.0.0.0:8099->8099/tcp", {8080, 8099}),
    ),
)
def test_published_ports_reads_every_shape_docker_prints(listing: str, expected: set) -> None:
    """The ownership check looked for `:8595->`, which a collapsed range never contains."""
    assert gateway_module._published_ports(listing) == expected


def _owned_ports(
    gateway: DockerInstallerGateway, process: FakeProcessExecutor, listing: str
) -> None:
    command = (
        "docker",
        "ps",
        "--filter",
        f"label=com.docker.compose.project={gateway._project_identity()}",
        "--format",
        "{{.Ports}}",
    )
    process.outcomes[command] = ProcessOutcome(command=command, returncode=0, stdout=listing)


def _port_preflight(
    tmp_path: Path, *, busy: set[int], owned: str, env: dict[str, str], config: InstallConfig
) -> tuple[DockerInstallerGateway, FakeProcessExecutor]:
    process = FakeProcessExecutor()
    _write_env_values(tmp_path / ".env.prod", env)
    gateway = _gateway(
        tmp_path,
        process,
        environment={},
        port_available=lambda _host, port: port not in busy,
    )
    _stage_official_release(gateway, config.deployment_profile)
    _owned_ports(gateway, process, owned)
    return gateway, process


def test_a_compact_update_is_not_refused_by_its_own_traefik(tmp_path: Path) -> None:
    """fs-r123: the ports were held by the installation being updated."""
    config = InstallConfig(
        deployment_profile="compact",
        environment="production",
        public_port=8595,
        redirect_http_port=8596,
    )
    gateway, _process = _port_preflight(
        tmp_path,
        busy={8595, 8596},
        owned="0.0.0.0:8595-8596->8595-8596/tcp, [::]:8595-8596->8595-8596/tcp",
        env={
            "FS_COMPACT_IMAGE": "sha256:" + "e" * 64,
            "APP_PORT": "8595",
            "HTTP_PORT": "8596",
        },
        config=config,
    )
    log = gateway.state_store.open_log("compact-update-ports")

    gateway.update_preflight(config, log=log)
    log.close()


def test_a_scalable_update_probes_the_ports_it_runs_on_and_names_the_drift(
    tmp_path: Path,
) -> None:
    """fs-scal12: the record said 8080, the installation has always served on 8592."""
    config = InstallConfig(
        deployment_profile="scalable", environment="production", public_port=8591
    )
    gateway, _process = _port_preflight(
        tmp_path,
        busy={8080, 8591, 8592},  # 8080 is someone else's airflow
        owned="0.0.0.0:8591-8592->8591-8592/tcp",
        env={"APP_PORT": "8591", "HTTP_PORT": "8592"},
        config=config,
    )
    log = gateway.state_store.open_log("scalable-update-ports")

    gateway.update_preflight(config, log=log)
    log.close()

    assert "8080" in log.path.read_text(encoding="utf-8")
    assert "keeps the ports the installation runs on" in log.path.read_text(encoding="utf-8")


def test_an_update_is_still_refused_by_a_port_that_is_not_its_own(tmp_path: Path) -> None:
    """The red direction: owning some ports does not make every busy port ours."""
    config = InstallConfig(
        deployment_profile="scalable", environment="production", public_port=8591
    )
    gateway, _process = _port_preflight(
        tmp_path,
        busy={8591, 8592},
        owned="0.0.0.0:8591->8591/tcp",  # 8592 is held by something else
        env={"APP_PORT": "8591", "HTTP_PORT": "8592"},
        config=config,
    )
    log = gateway.state_store.open_log("scalable-update-foreign-port")

    with pytest.raises(InstallerFailure) as raised:
        gateway.update_preflight(config, log=log)
    log.close()

    assert raised.value.code == "port_conflict"
    assert "8592" in raised.value.cause


def test_the_fresh_preflight_still_probes_the_configured_ports(tmp_path: Path) -> None:
    """What the update path now avoids, and what a first install must keep doing."""
    config = InstallConfig(
        deployment_profile="scalable", environment="production", public_port=8591
    )
    gateway, _process = _port_preflight(
        tmp_path,
        busy={8080},
        owned="",
        env={"APP_PORT": "8591", "HTTP_PORT": "8592"},
        config=config,
    )
    log = gateway.state_store.open_log("fresh-ports")

    with pytest.raises(InstallerFailure) as raised:
        gateway.preflight(config, log=log)
    log.close()

    assert raised.value.code == "port_conflict"
    assert "8080" in raised.value.cause


# --- AC17-R141 acceptance, second layer: a rebuild that left the old identity behind ---


@pytest.mark.parametrize("previous_build_completed", [True, False])
def test_a_compact_rebuild_supersedes_the_provenance_of_the_image_it_replaced(
    tmp_path: Path,
    previous_build_completed: bool,
) -> None:
    """fs-r123: "Persisted source provenance does not match the installer checkout".

    FS_BUILD_SHA is what that guard compares with the checkout, and the rebuild left
    it naming the previous install's revision -- so after an update it differed by
    definition, and the rebuilt image was refused after it had been built.
    """
    process = FakeProcessExecutor()
    built_image = "sha256:" + "f" * 64
    inspect_built = (
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        f"flow-steward-compact:flow-steward-{_FULL_REVISION[:12]}",
    )
    process.outcomes[inspect_built] = ProcessOutcome(
        command=inspect_built, returncode=0, stdout=built_image + "\n"
    )
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(
        environment="production", deployment_profile="compact", file_storage="local_fs"
    )
    env_path = tmp_path / ".env.prod"
    previous = "1" * 40
    previous_values = {"FS_BUILD_SHA": previous}
    if previous_build_completed:
        previous_values.update(
            {
                "FS_COMPACT_IMAGE": "sha256:" + "b" * 64,
                "FS_COMPACT_IMAGE_REVISION": previous,
                "FS_COMPACT_IMAGE_CONFIG_DIGEST": "sha256:" + "d" * 64,
            }
        )
    _write_env_values(env_path, previous_values)
    log = gateway.state_store.open_log("compact-rebuild-provenance")

    gateway._build_compact_runtime_image(config, log=log)
    log.close()

    values = gateway_module._read_env(env_path)
    assert values["FS_COMPACT_IMAGE"] == built_image
    # The guard's own input: persisted FS_BUILD_SHA against the checkout.
    assert values["FS_BUILD_SHA"] == _FULL_REVISION
    # A digest of the replaced image must not stand next to the new one.
    if previous_build_completed:
        assert values["FS_COMPACT_IMAGE_CONFIG_DIGEST"] == ""
    assert values["FS_COMPACT_IMAGE_REVISION"] == _FULL_REVISION


_R141_NEW_IDS = {
    "FS_APP_IMAGE": "sha256:" + "6" * 64,
    "FS_WORKER_IMAGE": "sha256:" + "7" * 64,
    "FS_CONTROL_IMAGE": "sha256:" + "8" * 64,
    "FS_CLI_IMAGE": "sha256:" + "9" * 64,
    "MCP_IMAGE": "sha256:" + "a" * 64,
    "POSTGRES_IMAGE": "sha256:" + "5" * 64,
}


def test_a_scalable_rebuild_does_not_leave_a_torn_image_identity(tmp_path: Path) -> None:
    """fs-scal12: "FS_APP_IMAGE_DIGEST does not match the selected immutable image ID".

    The rebuild wrote the new image IDs and kept the previous build's digests and
    revisions, for every role, not only fs-app. The exact-image selection then refused
    a record that contradicted itself. Asserted against that same selection.
    """
    old_ids = dict.fromkeys(_R141_NEW_IDS, "sha256:" + "1" * 64)
    _record_scalable_build(tmp_path, _R141_OLD, old_ids)
    _record_scalable_build(tmp_path, _FULL_REVISION, _R141_NEW_IDS)  # what the build writes
    env_path = tmp_path / ".env.prod"
    _write_env_values(
        env_path,
        {
            **old_ids,
            "MCP_IMAGE_REVISION": _R141_OLD,
            "FS_APP_IMAGE_DIGEST": old_ids["FS_APP_IMAGE"],
            "FS_WORKER_IMAGE_DIGEST": old_ids["FS_WORKER_IMAGE"],
            "FS_CONTROL_IMAGE_DIGEST": old_ids["FS_CONTROL_IMAGE"],
            "MCP_IMAGE_DIGEST": old_ids["MCP_IMAGE"],
            "FS_APP_IMAGE_REVISION": _R141_OLD,
            "FS_WORKER_IMAGE_REVISION": _R141_OLD,
            "FS_CONTROL_IMAGE_REVISION": _R141_OLD,
            "FS_CLI_IMAGE_REVISION": _R141_OLD,
            "FS_BUILD_SHA": _R141_OLD,
        },
    )
    process = _RuntimeExecutor(running={})
    gateway = _gateway(tmp_path, process, environment={})
    log = gateway.state_store.open_log("scalable-torn-identity")

    assert gateway._build_scalable_candidate_images(_scalable(), log=log) is True
    log.close()

    values = gateway_module._read_env(env_path)
    for name, image in _R141_NEW_IDS.items():
        assert values[name] == image, name
    for derived in (
        "FS_APP_IMAGE_DIGEST",
        "FS_WORKER_IMAGE_DIGEST",
        "FS_CONTROL_IMAGE_DIGEST",
        "MCP_IMAGE_DIGEST",
        "FS_APP_IMAGE_REVISION",
        "FS_WORKER_IMAGE_REVISION",
        "FS_CONTROL_IMAGE_REVISION",
        "FS_CLI_IMAGE_REVISION",
    ):
        assert values[derived] == "", derived
    assert values["MCP_IMAGE_REVISION"] == _FULL_REVISION
    assert values["FS_BUILD_SHA"] == _FULL_REVISION
    # The selection that refused the torn record accepts the superseded one.
    gateway._exact_prebuilt_image_refs(_scalable(), values)


def test_a_rerun_from_the_torn_state_the_previous_build_left_completes_the_update(
    tmp_path: Path,
) -> None:
    """Where acceptance restarts, and where a real operator would be standing.

    fc43e1537 left fs-scal12 with its new image IDs recorded beside the previous
    build's digests and revisions, and the runtime still on the old images. The fix
    arrives as a newer checkout, so those recorded images are behind the sources
    again: the update is due, the rebuild runs, and its now-complete write leaves a
    record the exact-image selection accepts.
    """
    torn_build = "4" * 40  # the fc43e1537 build
    torn_ids = dict.fromkeys(_R141_NEW_IDS, "sha256:" + "c" * 64)
    _record_scalable_build(tmp_path, torn_build, torn_ids)
    _record_scalable_build(tmp_path, _FULL_REVISION, _R141_NEW_IDS)
    env_path = tmp_path / ".env.prod"
    stale_digest = "sha256:" + "1" * 64
    _write_env_values(
        env_path,
        {
            **torn_ids,
            "MCP_IMAGE_REVISION": torn_build,
            "FS_APP_IMAGE_DIGEST": stale_digest,
            "FS_WORKER_IMAGE_DIGEST": stale_digest,
            "FS_CONTROL_IMAGE_DIGEST": stale_digest,
            "MCP_IMAGE_DIGEST": stale_digest,
            "FS_APP_IMAGE_REVISION": _R141_OLD,
            "FS_BUILD_SHA": _R141_OLD,
        },
    )
    process = _RuntimeExecutor(
        running={"fs-app": "sha256:" + "1" * 64}, labels={"sha256:" + "1" * 64: _R141_OLD}
    )

    gateway = _gateway(tmp_path, process, environment={})
    log = gateway.state_store.open_log("scalable-torn-rerun")

    currency = gateway.image_currency(_scalable(), log=log)
    assert currency.update_required is True
    assert currency.resumes_prepared_update is False
    assert gateway._build_scalable_candidate_images(_scalable(), log=log) is True
    log.close()

    values = gateway_module._read_env(env_path)
    assert values["FS_APP_IMAGE"] == _R141_NEW_IDS["FS_APP_IMAGE"]
    assert values["FS_APP_IMAGE_DIGEST"] == ""
    gateway._exact_prebuilt_image_refs(_scalable(), values)


def test_the_revision_updated_from_is_what_ran_even_when_the_env_file_is_incomplete(
    tmp_path: Path,
) -> None:
    """fs-r123 reported "Updated from fc43e153" while 47b080d6 had been running.

    The interrupted update had left .env.prod without a digest the Compact compose
    file declares with `:?`, so `docker compose ps` could not parse the file, the
    running image was never found, and the recorded revision stood in for it. Docker
    is asked directly now, by the labels Compose sets, and nothing stands in.
    """
    _write_env_values(
        tmp_path / ".env.prod",
        {
            "FS_COMPACT_IMAGE": _R141_OTHER,
            "FS_COMPACT_IMAGE_REVISION": "4" * 40,  # the interrupted build
            "FS_COMPACT_IMAGE_INDEX_DIGEST": "",  # what made the compose file unparsable
        },
    )
    process = _RuntimeExecutor(
        running={"compact-runtime": _R141_RUNNING}, labels={_R141_RUNNING: _R141_OLD}
    )

    currency = _currency(tmp_path, process, _compact(), environment={})

    assert currency.update_required is True
    assert currency.running_revision == _R141_OLD
    listed = next(c for c in process.commands if c[:3] == ("docker", "ps", "-q"))
    # A `compose run` container carries the same service label; it is not the runtime.
    assert "label=com.docker.compose.oneoff=False" in listed
    assert not any(c[:2] == ("docker", "compose") and "ps" in c for c in process.commands)


def test_an_unreadable_running_revision_is_not_filled_in_from_the_record(tmp_path: Path) -> None:
    _write_env_values(
        tmp_path / ".env.prod",
        {"FS_COMPACT_IMAGE": _R141_RUNNING, "FS_COMPACT_IMAGE_REVISION": _R141_OLD},
    )
    process = _RuntimeExecutor(running={"compact-runtime": _R141_RUNNING}, labels={})

    currency = _currency(tmp_path, process, _compact(), environment={})

    assert currency.update_required is True
    assert currency.running_revision == ""


@pytest.mark.parametrize(
    ("tls_mode", "ingress_mode", "expected"),
    (
        ("local", "bundled_traefik", "local"),
        ("self_signed", "bundled_traefik", "local"),
        ("provided", "bundled_traefik", "local"),
        # AC17-R143: behind an existing Traefik the certificate is the ingress's.
        ("external_resolver", "external_traefik", "ingress"),
        ("external_certificate", "external_traefik", "ingress"),
        # Never tried on Scalable, and it would have failed the same way.
        ("acme", "bundled_traefik", "ingress"),
    ),
)
def test_the_candidate_build_is_told_whether_the_certificate_is_a_local_file(
    tmp_path: Path, tls_mode: str, ingress_mode: str, expected: str
) -> None:
    """The build demanded .ssl/cert.pem for every Scalable install.

    Behind an existing Traefik that file does not exist by definition, so the install
    the owner approved -- Scalable behind the shared Traefik on flow.example.com -- built
    all five images and then refused. The mode is the installer's to know.
    """
    process = FakeProcessExecutor()
    candidate = tmp_path / ".flow-steward/release" / _FULL_REVISION / "candidate.env"
    candidate.parent.mkdir(parents=True)
    _write_env_values(candidate, _R141_NEW_IDS)
    gateway = _gateway(tmp_path, process, environment={})
    config = InstallConfig(
        deployment_profile="scalable",
        environment="production",
        ingress_mode=ingress_mode,
        tls_mode=tls_mode,
        public_host="fs.example.test",
        traefik_network="traefik",
        traefik_cert_resolver="myresolver",
        **(
            {
                "acme_contact": "ops@example.test",
                "acme_directory_url": "https://acme.example.test/dir",
            }
            if tls_mode == "acme"
            else {}
        ),
    )
    log = gateway.state_store.open_log("scalable-tls-inputs")

    assert gateway._build_scalable_candidate_images(config, log=log) is True
    log.close()

    (build,) = _candidate_builds(process.commands)
    assert build[:2] == ("env", f"FS_RELEASE_TLS_INPUTS={expected}")


def test_a_failed_candidate_build_names_the_scripts_reason_not_dockers_first_line(
    tmp_path: Path,
) -> None:
    """The operator read "Error: #0 building with "default" instance using docker driver".

    That was the first line of several thousand; the reason was the last one.
    """
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment={})
    script = str(tmp_path / gateway._SCALABLE_CANDIDATE_SCRIPT)
    command = ("env", "FS_RELEASE_TLS_INPUTS=local", "bash", script)
    reason = "deployment input TLS_CERT source is not readable: /home/admin/fs-ext/.ssl/cert.pem"
    process.outcomes[command] = ProcessOutcome(
        command=command,
        returncode=1,
        stdout=(
            '#0 building with "default" instance using docker driver\n'
            "#1 [internal] load build definition from Dockerfile\n"
            "#1 DONE 0.0s\n"
            f"{reason}\n"
        ),
    )
    log = gateway.state_store.open_log("scalable-build-reason")

    config = _scalable()
    with pytest.raises(InstallerFailure) as raised:
        gateway._build_scalable_candidate_images(config, log=log)
    log.close()

    assert raised.value.code == "scalable_image_build_failed"
    assert raised.value.cause == reason
    # The rest is still where it belongs.
    assert "#0 building" in log.path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("profile", "before", "after"),
    (
        ("compact", "revoked-installer-copy", ""),
        ("scalable", "the-scalable-workers-token", "the-scalable-workers-token"),
    ),
)
def test_only_compact_drops_its_superseded_worker_credential(
    tmp_path: Path, profile: str, before: str, after: str
) -> None:
    """AC17-R142: on Scalable the installer owns the identity and the value is live."""
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    env_path = tmp_path / ".env.prod"
    env_path.write_text(f"FS_WORKER_MCP_BEARER_TOKEN={before}\nAPP_PORT=8595\n", encoding="utf-8")
    config = InstallConfig(environment="production", deployment_profile=profile)
    log = gateway.state_store.open_log("r142-cleanup")

    gateway.drop_superseded_worker_credential(config, log=log)
    log.close()

    values = gateway_module._read_env(env_path)
    assert values["FS_WORKER_MCP_BEARER_TOKEN"] == after
    assert values["APP_PORT"] == "8595"


def test_an_installed_configuration_is_read_from_the_applied_stack_not_the_journal(
    tmp_path: Path,
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    recorded = InstallConfig()

    assert gateway.installed_config(recorded) is recorded  # nothing applied yet

    applied = InstallConfig(public_host="flow.example.test", public_port=8443)
    log = gateway.state_store.open_log("applied-configuration")
    gateway.ensure_configuration(applied, log=log)
    log.close()

    assert gateway.installed_config(recorded) == applied


@pytest.mark.parametrize(
    ("reason", "hint_expected"),
    [
        (
            'candidate snapshot failed: {"failure_class":"harness","reason":"candidate '
            'snapshot is stale: candidate source changed","status":"failed"}',
            False,
        ),
        ("tracked working tree changes must be committed before candidate build", True),
    ],
    ids=["stale-snapshot", "dirty-tree"],
)
def test_the_commit_or_stash_hint_appears_only_when_that_is_the_cause(
    tmp_path: Path, reason: str, hint_expected: bool
) -> None:
    """AC17-R159: a stale snapshot was reported under "commit or stash them first"."""
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment={})
    script = str(tmp_path / gateway._SCALABLE_CANDIDATE_SCRIPT)
    command = ("env", "FS_RELEASE_TLS_INPUTS=local", "bash", script)
    process.outcomes[command] = ProcessOutcome(
        command=command, returncode=2, stdout=f"building\n{reason}\n"
    )
    log = gateway.state_store.open_log("scalable-build-hint")

    config = _scalable()
    with pytest.raises(InstallerFailure) as raised:
        gateway._build_scalable_candidate_images(config, log=log)
    log.close()

    assert raised.value.cause == reason
    assert ("commit or stash" in raised.value.remediation) is hint_expected


def _rolled_back(tmp_path: Path, *, provenance: bool = True) -> Path:
    """Running B (_R141_OLD); these sources are A (_FULL_REVISION), whose release the
    retention kept: the rollback of AC17-R159."""
    current_ids = dict.fromkeys(_R141_NEW_IDS, "sha256:" + "1" * 64)
    _record_scalable_build(tmp_path, _R141_OLD, current_ids)
    _record_scalable_build(tmp_path, _FULL_REVISION, _R141_NEW_IDS)
    release = tmp_path / ".flow-steward/release" / _FULL_REVISION
    if provenance:
        digests = {
            "fs-app": _R141_NEW_IDS["FS_APP_IMAGE"],
            "fs-worker": _R141_NEW_IDS["FS_WORKER_IMAGE"],
            "control-plane": _R141_NEW_IDS["FS_CONTROL_IMAGE"],
            "fs-cli": _R141_NEW_IDS["FS_CLI_IMAGE"],
            "fs-mcp": _R141_NEW_IDS["MCP_IMAGE"],
        }
        (release / "candidate-provenance.json").write_text(
            json.dumps({"image_digests": digests}), encoding="utf-8"
        )
    _write_env_values(
        tmp_path / ".env.prod",
        {**current_ids, "MCP_IMAGE_REVISION": _R141_OLD, "FS_BUILD_SHA": _R141_OLD},
    )
    return release


def test_a_rollback_reuses_the_kept_images_without_rebuilding(tmp_path: Path) -> None:
    """AC17-R159: rolling back must take minutes, not a rebuild the cache may not serve."""
    _rolled_back(tmp_path)
    process = _RuntimeExecutor(
        running={}, labels=dict.fromkeys(_R141_NEW_IDS.values(), _FULL_REVISION)
    )
    gateway = _gateway(tmp_path, process, environment={})
    log = gateway.state_store.open_log("rollback-reuse")

    assert gateway._build_scalable_candidate_images(_scalable(), log=log) is True
    log.close()

    assert _candidate_builds(process.commands) == []  # nothing rebuilt
    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert {name: values[name] for name in _R141_NEW_IDS} == _R141_NEW_IDS
    written = log.path.read_text(encoding="utf-8")
    assert "nothing is rebuilt" in written
    assert "Building the Scalable images" not in written  # R159: no build was announced


@pytest.mark.parametrize(
    "missing",
    ["an-image-is-gone", "no-provenance"],
)
def test_a_rollback_without_its_images_rebuilds_and_says_so(tmp_path: Path, missing: str) -> None:
    _rolled_back(tmp_path, provenance=missing != "no-provenance")
    labels = dict.fromkeys(_R141_NEW_IDS.values(), _FULL_REVISION)
    if missing == "an-image-is-gone":
        labels.pop(_R141_NEW_IDS["FS_WORKER_IMAGE"])
    process = _RuntimeExecutor(running={}, labels=labels)
    gateway = _gateway(tmp_path, process, environment={})
    log = gateway.state_store.open_log("rollback-rebuild")

    assert gateway._build_scalable_candidate_images(_scalable(), log=log) is True
    log.close()

    assert len(_candidate_builds(process.commands)) == 1


def _stale_then(process: FakeProcessExecutor, gateway, *outcomes: tuple[int, str]) -> None:
    script = str(gateway.repo_root / gateway._SCALABLE_CANDIDATE_SCRIPT)
    command = ("env", "FS_RELEASE_TLS_INPUTS=local", "bash", script)
    process.queued_outcomes[command] = [
        ProcessOutcome(command=command, returncode=code, stdout=f"building\n{text}\n")
        for code, text in outcomes
    ]


_STALE = (
    'candidate snapshot failed: {"failure_class":"harness","reason":"candidate snapshot '
    'is stale: candidate source changed","status":"failed"}'
)


def test_a_release_record_the_build_refuses_is_removed_once_and_rebuilt(tmp_path: Path) -> None:
    """The measured rollback: exit 2 on a stale snapshot, with the images gone."""
    release = _rolled_back(tmp_path)
    process = _RuntimeExecutor(running={})
    gateway = _gateway(tmp_path, process, environment={})
    _stale_then(process, gateway, (2, _STALE), (0, "candidate_commit=ok"))

    def rebuilt(command, **kwargs):  # the second build writes its record again
        is_build = bool(command) and str(command[-1]).endswith("build_candidate.sh")
        if is_build and not release.exists():
            _record_scalable_build(tmp_path, _FULL_REVISION, _R141_NEW_IDS)
        return _RuntimeExecutor.run(process, command, **kwargs)

    process.run = rebuilt  # type: ignore[method-assign]
    log = gateway.state_store.open_log("rollback-stale")

    assert gateway._build_scalable_candidate_images(_scalable(), log=log) is True
    log.close()

    assert len(_candidate_builds(process.commands)) == 2
    (notice,) = gateway.take_notices()
    assert "could not be reused (candidate snapshot failed" in notice
    assert str(release) in notice


def test_only_the_named_refusals_remove_a_release_and_only_once(tmp_path: Path) -> None:
    release = _rolled_back(tmp_path)
    process = _RuntimeExecutor(running={})
    gateway = _gateway(tmp_path, process, environment={})
    _stale_then(process, gateway, (2, "docker daemon unavailable"))
    log = gateway.state_store.open_log("rollback-other")

    config = _scalable()
    with pytest.raises(InstallerFailure):
        gateway._build_scalable_candidate_images(config, log=log)
    assert release.is_dir()  # an unrelated failure deletes nothing

    _stale_then(process, gateway, (2, _STALE), (2, _STALE))
    config = _scalable()
    with pytest.raises(InstallerFailure) as raised:
        gateway._build_scalable_candidate_images(config, log=log)
    log.close()

    assert "candidate snapshot is stale" in raised.value.cause
    assert len(_candidate_builds(process.commands)) == 3  # one, then one retry, no more


# -- AC17-R177b: a recorded image the host no longer has --------------------------------------


def _built_here_and_pruned(tmp_path: Path, *gone: str) -> _RuntimeExecutor:
    """fs-ac19-bench (2026-09-24): this installer built the images at these sources,
    then `docker image prune -a` removed POSTGRES_IMAGE, and the update failed with
    "pull access denied for sha256"."""
    _rolled_back(tmp_path)  # the record of the build at _FULL_REVISION, with provenance
    _write_env_values(
        tmp_path / ".env.prod",
        {**_R141_NEW_IDS, "MCP_IMAGE_REVISION": _FULL_REVISION, "FS_BUILD_SHA": _FULL_REVISION},
    )
    present = {name: image for name, image in _R141_NEW_IDS.items() if name not in gone}
    return _RuntimeExecutor(
        running={},
        labels=dict.fromkeys(present.values(), _FULL_REVISION),
        tags={image: image for image in present.values()},
    )


def test_an_image_this_installer_built_and_the_host_lost_is_built_again(tmp_path: Path) -> None:
    process = _built_here_and_pruned(tmp_path, "POSTGRES_IMAGE")
    gateway = _gateway(tmp_path, process, environment={})
    log = gateway.state_store.open_log("r177b-rebuild")

    assert gateway._build_scalable_candidate_images(_scalable(), log=log) is True
    log.close()

    assert len(_candidate_builds(process.commands)) == 1
    written = log.path.read_text(encoding="utf-8")
    assert f"POSTGRES_IMAGE={_R141_NEW_IDS['POSTGRES_IMAGE']}" in written
    assert "no longer in Docker" in written
    assert "sources have moved on" not in written  # the same sources: not an update


def test_the_images_this_installer_built_are_reused_while_the_host_has_them(
    tmp_path: Path,
) -> None:
    process = _built_here_and_pruned(tmp_path)
    gateway = _gateway(tmp_path, process, environment={})
    log = gateway.state_store.open_log("r177b-reuse")

    assert gateway._build_scalable_candidate_images(_scalable(), log=log) is False
    log.close()

    assert _candidate_builds(process.commands) == []


def test_its_own_record_is_rebuilt_when_the_build_refuses_it_for_the_lost_image(
    tmp_path: Path,
) -> None:
    """The running installation's own record is never removed -- but once its images
    are gone from Docker it describes nothing that can run."""
    process = _built_here_and_pruned(tmp_path, "POSTGRES_IMAGE")
    gateway = _gateway(tmp_path, process, environment={})
    release = tmp_path / ".flow-steward/release" / _FULL_REVISION
    _stale_then(process, gateway, (2, _STALE), (0, "candidate_commit=ok"))
    run = process.run

    def rebuilt(command, **kwargs):
        is_build = bool(command) and str(command[-1]).endswith("build_candidate.sh")
        if is_build and not release.exists():
            _record_scalable_build(tmp_path, _FULL_REVISION, _R141_NEW_IDS)
        return run(command, **kwargs)

    process.run = rebuilt  # type: ignore[method-assign]
    log = gateway.state_store.open_log("r177b-own-record")

    assert gateway._build_scalable_candidate_images(_scalable(), log=log) is True
    log.close()

    assert len(_candidate_builds(process.commands)) == 2


def test_a_compact_image_this_installer_built_and_the_host_lost_is_built_again(
    tmp_path: Path,
) -> None:
    lost = "sha256:" + "4" * 64
    _write_env_values(
        tmp_path / ".env.prod",
        {"FS_COMPACT_IMAGE": lost, "FS_COMPACT_IMAGE_REVISION": _FULL_REVISION},
    )
    rebuilt = "sha256:" + "3" * 64
    process = _RuntimeExecutor(
        running={}, tags={f"flow-steward-compact:flow-steward-{_FULL_REVISION[:12]}": rebuilt}
    )
    gateway = _gateway(tmp_path, process, environment={})
    log = gateway.state_store.open_log("r177b-compact")

    gateway._build_compact_runtime_image(_compact(), log=log)
    log.close()

    assert any(command[:3] == ("docker", "buildx", "build") for command in process.commands)
    values = gateway_module._read_env(tmp_path / ".env.prod")
    assert values["FS_COMPACT_IMAGE"] == rebuilt
    assert f"FS_COMPACT_IMAGE={lost}" in log.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("profile", ["scalable", "compact"])
def test_an_image_id_the_operator_chose_and_the_host_lost_is_refused_by_name(
    tmp_path: Path, profile: str
) -> None:
    """Not built here, so not the installer's to rebuild; and an image ID cannot be
    pulled back, so Compose would only fail with "pull access denied for sha256"."""
    chosen = "sha256:" + "9" * 64
    runtime = "sha256:" + "4" * 64  # Compact's own image is there: nothing to build
    _write_env_values(
        tmp_path / ".env.prod",
        {
            "POSTGRES_IMAGE": chosen,
            **(
                {"FS_COMPACT_IMAGE": runtime, "FS_COMPACT_IMAGE_REVISION": _FULL_REVISION}
                if profile == "compact"
                else {}
            ),
        },
    )
    process = _RuntimeExecutor(running={}, tags={runtime: runtime})
    gateway = _gateway(tmp_path, process, environment={})
    log = gateway.state_store.open_log("r177b-chosen")

    config = InstallConfig(deployment_profile=profile, environment="production")
    with pytest.raises(InstallerFailure) as raised:
        gateway.prepare_core(config, log=log)
    log.close()

    assert raised.value.code == "recorded_image_missing"
    assert raised.value.cause == (
        f"POSTGRES_IMAGE={chosen} is not present locally, and an image ID cannot be pulled."
    )
    assert "restore it" in raised.value.remediation
    assert "clear the value" in raised.value.remediation
    assert not any(command[:3] == ("docker", "buildx", "build") for command in process.commands)


def test_a_rollback_across_a_migration_is_refused_by_name(tmp_path: Path) -> None:
    """AC17-R159 condition 3: older code on a newer schema is never started."""
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment={})
    refusal = (
        "RuntimeError: The database carries migrations this release does not know: "
        "20260201000000_newer.sql. A newer release upgraded it, and this older one is not "
        "started on a schema it has never seen."
    )

    def run(command, **kwargs):
        if "--baseline-migration-history" in command:
            return ProcessOutcome(command=tuple(command), returncode=1, stdout=f"x\n{refusal}\n")
        return FakeProcessExecutor.run(process, command, **kwargs)

    process.run = run  # type: ignore[method-assign]
    log = gateway.state_store.open_log("rollback-migration")

    config = _scalable()
    with pytest.raises(InstallerFailure) as raised:
        gateway.verify_schema(config, log=log)
    log.close()

    assert raised.value.code == "database_newer_than_release"
    assert "20260201000000_newer.sql" in raised.value.cause
    assert "database upgrade" not in raised.value.remediation  # cannot take a schema back
    assert "backup" in raised.value.remediation


def test_an_unapplied_forward_migration_has_a_dedicated_failure_code(tmp_path: Path) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment={})

    def run(command, **kwargs):
        if "--baseline-migration-history" in command:
            return ProcessOutcome(
                command=tuple(command),
                returncode=2,
                stderr="Migration history already exists but does not match this release\n",
            )
        return FakeProcessExecutor.run(process, command, **kwargs)

    process.run = run  # type: ignore[method-assign]
    log = gateway.state_store.open_log("forward-migration")
    config = _scalable()

    with pytest.raises(InstallerFailure) as raised:
        gateway.precheck_migrations(config, log=log)
    log.close()

    assert raised.value.code == "release_migration_required"
    assert "database upgrade" in raised.value.remediation


_COMPACT_INDEX = f"sha256:{'a' * 64}"
_COMPACT_MANIFEST = f"sha256:{'b' * 64}"
_COMPACT_CONFIG = f"sha256:{'c' * 64}"


def _compact_pins(image_id: str, **extra: str) -> dict[str, str]:
    return {
        "FS_COMPACT_IMAGE": image_id,
        "FS_COMPACT_IMAGE_INDEX_DIGEST": _COMPACT_INDEX,
        "FS_COMPACT_IMAGE_MANIFEST_DIGEST": _COMPACT_MANIFEST,
        "FS_COMPACT_IMAGE_CONFIG_DIGEST": _COMPACT_CONFIG,
        **extra,
    }


@pytest.mark.parametrize(
    "image_id",
    [
        pytest.param(_COMPACT_INDEX, id="containerd-store-index"),
        pytest.param(_COMPACT_MANIFEST, id="containerd-store-manifest"),
        pytest.param(_COMPACT_CONFIG, id="classic-store-config"),
    ],
)
def test_compact_selection_accepts_the_image_id_of_either_docker_image_store(
    tmp_path: Path, image_id: str
) -> None:
    """Docker Desktop's containerd store reports the index digest as the image ID.

    The installer records index, manifest and config digests beside the ID; the
    selection compared the ID with the config digest only, so every in-place update
    of a Compact stand on Docker Desktop failed with invalid_exact_image_selection.
    """
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig(environment="production", deployment_profile="compact")

    # A tagged MCP_IMAGE left by an earlier Scalable install sends the selection
    # down the strict path, where the comparison used to refuse it.
    refs = gateway._exact_prebuilt_image_refs(
        config, _compact_pins(image_id, MCP_IMAGE="flow-steward-mcp:0.4.0")
    )
    assert refs == {"compact": image_id}
    assert gateway._exact_prebuilt_image_refs(config, _compact_pins(image_id)) == {
        "compact": image_id
    }


def test_compact_selection_still_refuses_an_image_id_that_matches_no_recorded_digest(
    tmp_path: Path,
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig(environment="production", deployment_profile="compact")

    pins = _compact_pins(f"sha256:{'d' * 64}")
    with pytest.raises(InstallerFailure) as refused:
        gateway._exact_prebuilt_image_refs(config, pins)

    assert refused.value.code == "invalid_exact_image_selection"
    assert "matches none of the config, manifest or index digests" in refused.value.cause


def test_compact_selection_still_refuses_a_tagged_cli_image_compose_would_run(
    tmp_path: Path,
) -> None:
    gateway = _gateway(tmp_path, FakeProcessExecutor())
    config = InstallConfig(environment="production", deployment_profile="compact")

    pins = _compact_pins(_COMPACT_INDEX, FS_CLI_IMAGE="flow-steward-cli:0.4.0")
    with pytest.raises(InstallerFailure) as refused:
        gateway._exact_prebuilt_image_refs(config, pins)

    assert refused.value.code == "invalid_exact_image_selection"


def test_installer_reissue_retains_snapshot_when_compensation_only_exits_zero(tmp_path):
    class PartlyFailedProcess(FakeProcessExecutor):
        attempts = 0

        def run(self, command, **kwargs):
            if "--force-recreate" in command:
                self.attempts += 1
                if self.attempts == 1:
                    return ProcessOutcome(tuple(command), 1)
            return super().run(command, **kwargs)

    process = PartlyFailedProcess()
    gateway = _gateway(tmp_path, process, secret_factory=lambda: "generated-secret")
    config = InstallConfig(
        public_host="flow.example.test", deployment_profile="compact", environment="production"
    )
    log = gateway.state_store.open_log("partial-reissue")
    gateway.ensure_secrets(config, log=log)
    with pytest.raises(InstallerFailure) as raised:
        gateway.reissue_bootstrap_token(config, log=log)
    log.close()
    assert raised.value.code == "bootstrap_recovery_needed"
    assert process.attempts == 2
    assert (gateway.state_store.root / "operations/bootstrap-token-reissue/manifest.json").is_file()
