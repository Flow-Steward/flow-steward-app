from __future__ import annotations

from pathlib import Path

import pytest


def test_platform_runtime_accepts_only_fixed_operations() -> None:
    from core.entrypoints.cli.platform_runtime import _canonical_argv, _parser

    assert _parser().parse_args(["bootstrap"]).operation == "bootstrap"
    assert _parser().parse_args(["candidate-preflight"]).operation == "candidate-preflight"
    args = _parser().parse_args(
        ["database-upgrade", "--author", "operator", "--maintenance-confirmed", "--confirm"]
    )
    assert _canonical_argv(args) == [
        "database",
        "upgrade",
        "--author",
        "operator",
        "--maintenance-confirmed",
        "--confirm",
    ]
    verify = _parser().parse_args(["backup-verify", "--backup", "backups/candidate.dump.enc"])
    assert _canonical_argv(verify) == [
        "backup",
        "verify",
        "backups/candidate.dump.enc",
    ]
    parity = _parser().parse_args(
        [
            "database-restore-parity-verify",
            "--baseline",
            "backups/restore/baseline.json",
            "--output",
            "backups/restore/compact.json",
            "--label",
            "compact",
            "--confirm",
        ]
    )
    assert _canonical_argv(parity) == [
        "database",
        "restore-parity-verify",
        "--label",
        "compact",
        "--baseline",
        "backups/restore/baseline.json",
        "--output",
        "backups/restore/compact.json",
        "--confirm",
    ]
    release = _parser().parse_args(
        [
            "release-upgrade",
            "--target-version",
            "1.2.3",
            "--timeout-seconds",
            "120",
            "--dry-run",
            "--confirm",
            "--json",
            "--plain",
            "--tui",
            "--no-color",
            "--non-interactive",
            "--verbose",
        ]
    )
    assert _canonical_argv(release) == [
        "release",
        "upgrade",
        "--timeout-seconds",
        "120",
        "--target-version",
        "1.2.3",
        "--dry-run",
        "--confirm",
        "--json",
        "--plain",
        "--tui",
        "--no-color",
        "--non-interactive",
        "--verbose",
    ]
    parser = _parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["shell"])


@pytest.mark.parametrize(
    "argv",
    (
        ["database-upgrade"],
        ["maintenance-enable"],
        ["backup-verify"],
        ["database-restore-parity-capture"],
        ["database-restore-parity-verify"],
    ),
)
def test_platform_runtime_rejects_missing_safety_inputs(argv: list[str]) -> None:
    from core.entrypoints.cli.platform_runtime import _canonical_argv, _parser

    parsed = _parser().parse_args(argv)

    with pytest.raises(ValueError):
        _canonical_argv(parsed)


@pytest.mark.parametrize(
    "argv",
    (
        ["release-info", "--target-version", "9.9.9"],
        ["release-check", "--dry-run"],
        ["release-upgrade", "--baseline", "ignored.json", "--confirm"],
        ["maintenance-enable", "--dry-run", "--confirm"],
    ),
)
def test_platform_runtime_rejects_options_irrelevant_to_the_selected_operation(
    argv: list[str],
) -> None:
    from core.entrypoints.cli.platform_runtime import _canonical_argv, _parser

    parsed = _parser().parse_args(argv)

    with pytest.raises(ValueError, match="does not accept"):
        _canonical_argv(parsed)


def test_platform_runtime_preserves_click_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.exceptions import Exit

    from core.entrypoints.cli import lifecycle_click_app as click_module
    from core.entrypoints.cli import platform_runtime

    def fail(**_kwargs: object) -> None:
        raise Exit(7)

    monkeypatch.setattr(click_module.click_app, "main", fail)

    assert platform_runtime.main(["status", "--json"]) == 7


def test_platform_runtime_preserves_returned_click_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from core.entrypoints.cli import platform_runtime

    click_module = importlib.import_module("core.entrypoints.cli.lifecycle_click_app")
    monkeypatch.setattr(click_module.click_app, "main", lambda **_kwargs: 8)

    assert platform_runtime.main(["status", "--json"]) == 8


def test_platform_runtime_does_not_leak_compact_project_root_to_later_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import os

    from core.entrypoints.cli import platform_runtime

    click_module = importlib.import_module("core.entrypoints.cli.lifecycle_click_app")
    monkeypatch.delenv("FS_DOCKER_HOST_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(click_module.click_app, "main", lambda **_kwargs: 0)

    assert platform_runtime.main(["status", "--json"]) == 0
    assert "FS_DOCKER_HOST_PROJECT_ROOT" not in os.environ


@pytest.mark.parametrize("host_state", ("", "   "))
def test_compact_backup_gateway_uses_native_database_and_data_volume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host_state: str,
) -> None:
    from core.entrypoints.cli.compact_platform_runtime import (
        CompactPlatformRuntime,
    )

    monkeypatch.setenv(
        "FS_DB_DSN",
        "postgresql://flow:secret@pg-fs:5432/flow_steward",
    )
    installer_state = tmp_path / "installer-state"
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(installer_state))
    monkeypatch.setenv("FS_CONFIGURATION_STATE_ROOT", host_state)

    gateway = CompactPlatformRuntime()._build_backup_service()._gateway

    assert gateway._native_database is True
    assert gateway._project_root.as_posix() == "/data"
    assert gateway._backups_root.as_posix() == "/data/backups"
    assert gateway._staging_root == installer_state / "clone/database-staging"
    assert gateway._logs_root == installer_state / "clone/database-logs"
    assert gateway._database_host == "pg-fs"
    assert CompactPlatformRuntime()._clone_workspace_root() == (installer_state / "clone/workspace")


def test_compact_host_release_reads_runtime_maintenance_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from core.application.lifecycle.contracts import ComposeTarget
    from core.entrypoints.cli.compact_platform_runtime import CompactPlatformRuntime
    from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway
    from core.infrastructure.lifecycle.maintenance_gateway import FilesystemMaintenanceGateway

    monkeypatch.setenv("FS_CONFIGURATION_STATE_ROOT", str(tmp_path / "host-state"))
    monkeypatch.setenv("FS_DB_DSN", "postgresql://flow:secret@pg-fs:5432/flow_steward")

    def private_volume_read(_self: object) -> None:
        raise PermissionError("Host UID cannot read the private runtime maintenance marker")

    monkeypatch.setattr(FilesystemMaintenanceGateway, "read", private_volume_read)
    commands: list[tuple[str, ...]] = []

    class Process:
        def run(self, command: list[str], **_kwargs: object):
            commands.append(tuple(command))
            return SimpleNamespace(
                returncode=0,
                stdout='{"schema":"flow-steward.cli.v1","ok":true,'
                '"command":"maintenance.status","result":{"status":"enabled",'
                '"activated_at":"2026-10-08T11:00:00Z","owner_kind":"manual"}}',
                stderr="",
            )

    target = ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        project_name="owned-compact",
        deployment_profile="compact",
        compose_files=("infra/docker/docker-compose.compact.yaml",),
        env_file=".env.prod",
    )
    runtime = CompactPlatformRuntime()
    runtime.__dict__["configuration_service"] = SimpleNamespace(
        read_active=lambda: SimpleNamespace(deployment_profile="compact"),
        derive_target=lambda _config: target,
    )
    runtime.__dict__["docker_lifecycle_gateway"] = DockerLifecycleGateway(
        project_root=tmp_path, process=Process()
    )

    status = runtime.maintenance_service.status()

    assert status.status == "enabled"
    assert status.owner_kind == "manual"
    assert commands[0][-10:] == (
        "exec",
        "-T",
        "--user",
        "10007:10007",
        "compact-runtime",
        "python",
        "-m",
        "core.entrypoints.cli.platform_runtime",
        "maintenance-status",
        "--json",
    )


def _host_compact_maintenance_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import json
    from types import SimpleNamespace

    from core.application.lifecycle.contracts import ComposeTarget
    from core.entrypoints.cli import compact_platform_runtime
    from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway

    root = tmp_path / "checkout"
    root.mkdir()
    installer_root = tmp_path / "installed-state"
    installer_root.mkdir()
    monkeypatch.setattr(compact_platform_runtime, "_LIFECYCLE_ROOT", root)
    monkeypatch.setenv("FS_CONFIGURATION_STATE_ROOT", str(installer_root))
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(installer_root))
    commands: list[tuple[str, ...]] = []

    class Process:
        def run(self, command: list[str], **_kwargs: object):
            commands.append(tuple(command))
            operation = next(part for part in command if part.startswith("maintenance-"))
            result = {"status": "disabled", "changed": True}
            if operation == "maintenance-enable":
                result["status"] = "enabled"
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "schema": "flow-steward.cli.v1",
                        "ok": True,
                        "command": operation.replace("-", "."),
                        "result": result,
                    }
                ),
                stderr="",
            )

    target = ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        project_name="owned-compact",
        deployment_profile="compact",
        compose_files=("infra/docker/docker-compose.compact.yaml",),
        env_file=".env.prod",
    )
    runtime = compact_platform_runtime.CompactPlatformRuntime()
    runtime.__dict__["configuration_service"] = SimpleNamespace(
        read_active=lambda: SimpleNamespace(deployment_profile="compact"),
        derive_target=lambda _config: target,
    )
    runtime.__dict__["docker_lifecycle_gateway"] = DockerLifecycleGateway(
        project_root=root, process=Process()
    )
    return runtime, root, installer_root, commands


@pytest.mark.parametrize("status", ("running", "cancelled", "recovery_required", "failed"))
def test_compact_host_disable_refuses_unfinished_host_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str
) -> None:
    from core.application.lifecycle.contracts import LifecycleOperationJournal
    from core.application.lifecycle.errors import LifecycleError
    from core.infrastructure.lifecycle.state_store import LifecycleStateStore

    runtime, root, _installer_root, commands = _host_compact_maintenance_runtime(
        monkeypatch, tmp_path
    )
    LifecycleStateStore(root).save_journal(
        LifecycleOperationJournal(
            operation_id="interrupted-clone",
            kind="clone_restore",
            status=status,
            retryable=True,
            recovery_command="flow-steward clone restore",
        )
    )

    with pytest.raises(LifecycleError) as caught:
        runtime.maintenance_service.disable()

    assert caught.value.code == "maintenance_unfinished_operations"
    assert not commands


@pytest.mark.parametrize("method", ("enable", "disable"))
def test_compact_host_mutation_refuses_real_held_installer_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str
) -> None:
    import fcntl

    from core.application.lifecycle.errors import LifecycleError

    runtime, _root, installer_root, commands = _host_compact_maintenance_runtime(
        monkeypatch, tmp_path
    )
    with (installer_root / "install.lock").open("w+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        operation = getattr(runtime.maintenance_service, method)
        with pytest.raises(LifecycleError):
            operation()

    assert not commands


def test_compact_backup_gateway_accepts_installer_libpq_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.compact_platform_runtime import _native_backup_gateway

    monkeypatch.setenv(
        "FS_DB_DSN",
        "dbname='flow steward' user=flow password='secret value' host=pg-fs port=6432",
    )

    gateway = _native_backup_gateway()

    assert gateway._database_name == "flow steward"
    assert gateway._database_user == "flow"
    assert gateway._database_password == "secret value"
    assert gateway._database_host == "pg-fs"
    assert gateway._database_port == 6432


def test_compact_host_public_backup_uses_installed_compose_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from core.application.lifecycle.backup_service import (
        BackupCreateRequest,
        BackupListRequest,
        BackupVerificationStatus,
        BackupVerifyRequest,
    )
    from core.application.lifecycle.contracts import ComposeTarget
    from core.entrypoints.cli import compact_platform_runtime, lifecycle_runtime
    from core.infrastructure.installer.state_store import InstallerStateStore
    from core.infrastructure.lifecycle import backup_gateway

    root, volume = tmp_path / "host", tmp_path / "private-volume"
    root.mkdir()
    volume.mkdir(mode=0o700)
    state_root = root / ".flow-steward"
    monkeypatch.setattr(lifecycle_runtime, "_REPO_ROOT", root)
    monkeypatch.setattr(compact_platform_runtime, "_LIFECYCLE_ROOT", root)
    monkeypatch.setattr(compact_platform_runtime, "_DATA_ROOT", volume)
    monkeypatch.setattr(
        compact_platform_runtime, "_INSTALLATION_LOCK", state_root / "operations/active.lock"
    )
    monkeypatch.setenv("FS_CONFIGURATION_STATE_ROOT", str(state_root))
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(state_root))
    monkeypatch.setenv("FS_DOCKER_HOST_PROJECT_ROOT", str(root))
    # Fake pg_process below handles this test-only database transport.
    monkeypatch.setenv(
        "FS_DB_DSN",
        "postgresql://flow:fixture@pg-fs:5432/flow_steward",  # pragma: allowlist secret
    )
    monkeypatch.setenv("FS_BACKUP_ENCRYPTION_KEY", "not-the-installed-key")
    monkeypatch.setattr(
        lifecycle_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_BACKUP_ENCRYPTION_KEY": "installed-fixture-key"},
    )
    commands: list[tuple[str, ...]] = []

    def pg_process(command, **kwargs):
        argv = tuple(str(part) for part in command)
        commands.append(argv)
        if "pg_dump" in argv:
            kwargs["stdout"].write(b"PGDMP fixture-only")
        elif "pg_restore" in argv and "-l" in argv:
            kwargs["stdin"].read()
        else:
            raise AssertionError(f"unexpected database process: {argv}")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(backup_gateway.subprocess, "run", pg_process)
    target = ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        project_name="owned-compact",
        deployment_profile="compact",
        compose_files=("infra/docker/docker-compose.compact.yaml",),
        env_file=".env.prod",
    )
    runtime = compact_platform_runtime.CompactPlatformRuntime()
    runtime.__dict__["configuration_service"] = SimpleNamespace(
        read_active=lambda: SimpleNamespace(deployment_profile="compact"),
        derive_target=lambda _config: target,
    )
    with InstallerStateStore(state_root).operation_lock():
        pass

    created = runtime.backup_command_service.create(BackupCreateRequest())
    listed = runtime.backup_command_service.list(BackupListRequest())
    verified = runtime.backup_command_service.verify(BackupVerifyRequest(backup=created.path))

    assert (root / created.path).is_file()
    assert listed.backups[0].path == created.path
    assert verified.verification_status is BackupVerificationStatus.VERIFIED
    assert list(volume.iterdir()) == []
    assert volume.stat().st_mode & 0o777 == 0o700
    assert runtime._build_backup_service()._gateway._key_source() == "installed-fixture-key"
    assert commands
    assert all(command[:2] == ("docker", "compose") for command in commands)
    assert all("owned-compact" in command and "pg-fs" in command for command in commands)
    assert all(str(root / "infra/docker/docker-compose.compact.yaml") in c for c in commands)
    assert all("compact-runtime" not in command for command in commands)
    assert not list((root / "backups").glob(".*.dump"))


def test_compact_host_mutation_uses_distinct_installer_lock_without_self_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core.application.installer.errors import InstallerFailure
    from core.application.lifecycle.errors import LifecycleError
    from core.entrypoints.cli import compact_platform_runtime, lifecycle_runtime
    from core.infrastructure.installer.state_store import InstallerStateStore

    root = tmp_path / "checkout"
    root.mkdir()
    state_root = root / ".flow-steward"
    monkeypatch.setattr(compact_platform_runtime, "_LIFECYCLE_ROOT", root)
    monkeypatch.setattr(lifecycle_runtime, "_REPO_ROOT", root)
    monkeypatch.setattr(
        compact_platform_runtime, "_INSTALLATION_LOCK", state_root / "operations/active.lock"
    )
    monkeypatch.setenv("FS_CONFIGURATION_STATE_ROOT", str(state_root))
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(state_root))
    runtime = compact_platform_runtime.CompactPlatformRuntime()
    installer = InstallerStateStore(state_root)
    with installer.operation_lock():
        pass

    state = runtime._lifecycle_state()
    with (
        state.operation_lock("host-backup", mutating=True),
        pytest.raises(InstallerFailure) as refused,
        installer.operation_lock(),
    ):
        pass
    assert refused.value.code == "installer_operation_active"

    with (
        installer.operation_lock(),
        pytest.raises(LifecycleError) as refused,
        state.operation_lock("host-backup", mutating=True),
    ):
        pass
    assert refused.value.code == "lifecycle_operation_active"


def test_compact_health_uses_configured_internal_service_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli import compact_platform_runtime

    requests: list[tuple[str, int, int, str, str]] = []

    class Response:
        status = 200

        @staticmethod
        def read(_limit: int) -> bytes:
            return b'{"ready": true}'

    class Connection:
        def __init__(self, host: str, port: int, *, timeout: int) -> None:
            requests.append((host, port, timeout, "", ""))

        def request(self, method: str, path: str) -> None:
            host, port, timeout, _, _ = requests[-1]
            requests[-1] = (host, port, timeout, method, path)

        @staticmethod
        def getresponse() -> Response:
            return Response()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setenv(
        "FS_COMPACT_HEALTH_URL",
        "http://compact-runtime:8086/health/ready",
    )
    monkeypatch.setenv("FS_RUNTIME_HTTP_PORT", "8086")
    monkeypatch.setattr(compact_platform_runtime, "HTTPConnection", Connection)

    assert compact_platform_runtime._compact_health_payload() == {"ready": True}
    assert requests == [("compact-runtime", 8086, 5, "GET", "/health/ready")]


@pytest.mark.parametrize(
    "configured",
    (
        "https://compact-runtime:8080/health/ready",
        "http://untrusted:8080/health/ready",
        "http://compact-runtime:99999/health/ready",
        "http://compact-runtime:8080/private",
    ),
)
def test_compact_health_rejects_untrusted_url(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
) -> None:
    from core.entrypoints.cli.compact_platform_runtime import _compact_health_payload

    monkeypatch.setenv("FS_COMPACT_HEALTH_URL", configured)

    with pytest.raises(RuntimeError, match="Compact health URL is invalid"):
        _compact_health_payload()


def test_compact_health_rejects_port_different_from_selected_runtime_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.compact_platform_runtime import _compact_health_payload

    monkeypatch.setenv("FS_RUNTIME_HTTP_PORT", "8086")
    monkeypatch.setenv(
        "FS_COMPACT_HEALTH_URL",
        "http://compact-runtime:8080/health/ready",
    )

    with pytest.raises(RuntimeError, match="Compact health URL is invalid"):
        _compact_health_payload()


def test_compact_cli_health_url_uses_selected_runtime_port() -> None:
    from pathlib import Path

    compose = Path("infra/docker/docker-compose.compact.yaml").read_text(encoding="utf-8")

    assert (
        "FS_COMPACT_HEALTH_URL: http://compact-runtime:${FS_RUNTIME_HTTP_PORT:-8080}/health/ready"
    ) in compose
