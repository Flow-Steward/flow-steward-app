"""Infrastructure composition tests for lifecycle backups."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.lifecycle.backup_service import BackupGatewayError
from core.application.lifecycle.contracts import ComposeTarget
from core.infrastructure.lifecycle.backup_gateway import FilesystemBackupGateway


class _NativeRunner:
    def __init__(self, *, fail_command: str = "") -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.fail_command = fail_command

    def __call__(self, command, **kwargs):
        normalized = tuple(str(item) for item in command)
        self.calls.append((normalized, kwargs))
        if normalized[0] == self.fail_command:
            return SimpleNamespace(returncode=1, stderr=b"expected failure")
        if normalized[0] == "pg_dump":
            kwargs["stdout"].write(b"PGDMP native")
        elif normalized[0] == "pg_restore":
            kwargs["stdin"].read()
        return SimpleNamespace(returncode=0, stderr=b"")


class _ComposeRestoreRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command, **kwargs):
        normalized = tuple(str(item) for item in command)
        self.calls.append(normalized)
        if "pg_restore" in normalized and kwargs.get("stdin") is not None:
            kwargs["stdin"].read()
        return SimpleNamespace(returncode=0, stderr=b"")


def test_backup_gateway_builds_compose_prefix_from_canonical_target(tmp_path: Path) -> None:
    target = ComposeTarget(
        stack_environment="production",
        file_storage="bundled",
        env_file=".env.prod",
        compose_files=(
            "infra/docker/docker-compose.yaml",
            "infra/docker/docker-compose.prod.yaml",
        ),
        compose_profiles=("production",),
        project_name="installed-project",
    )

    gateway = FilesystemBackupGateway.for_compose_target(
        project_root=tmp_path,
        target=target,
    )

    assert gateway._compose_command == (
        "docker",
        "compose",
        "--env-file",
        str(tmp_path / ".env.prod"),
        "-f",
        str(tmp_path / "infra/docker/docker-compose.yaml"),
        "-f",
        str(tmp_path / "infra/docker/docker-compose.prod.yaml"),
        "--profile",
        "production",
    )
    assert gateway._compose_project == "installed-project"


def test_backup_gateway_separates_local_artifacts_from_host_compose_paths(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "host-repository"
    target = ComposeTarget(
        stack_environment="development",
        file_storage="bundled",
        env_file=".env",
        compose_files=("infra/docker/docker-compose.yaml",),
        project_name="phase4-backup-path-test",
    )

    gateway = FilesystemBackupGateway.for_compose_target(
        project_root=tmp_path,
        docker_host_project_root=host_root,
        target=target,
    )

    assert str(host_root / ".env") in gateway._compose_command
    assert str(host_root / "infra/docker/docker-compose.yaml") in gateway._compose_command
    assert gateway._backups_root == tmp_path / "backups"


def test_compose_clone_restore_reuses_confined_backup_transport(tmp_path: Path) -> None:
    runner = _ComposeRestoreRunner()
    target = ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        env_file=".env.prod",
        compose_files=("infra/docker/docker-compose.prod.yaml",),
        project_name="installed-project",
    )
    gateway = FilesystemBackupGateway.for_compose_target(
        project_root=tmp_path,
        target=target,
        runner=runner,
    )
    source = tmp_path / "authenticated.dump"
    source.write_bytes(b"PGDMP clone")

    gateway.restore_clone_dump(source, recovery_target="installed-project")

    tools = [command[command.index("pg-fs") + 1] for command in runner.calls]
    assert tools == ["pg_restore", "dropdb", "createdb", "pg_restore", "psql"]
    assert all("installed-project" in command for command in runner.calls)


def test_compose_clone_restore_rejects_request_for_another_installation(tmp_path: Path) -> None:
    runner = _ComposeRestoreRunner()
    target = ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        env_file=".env.prod",
        compose_files=("infra/docker/docker-compose.prod.yaml",),
        project_name="installed-project",
    )
    gateway = FilesystemBackupGateway.for_compose_target(
        project_root=tmp_path,
        target=target,
        runner=runner,
    )
    source = tmp_path / "authenticated.dump"
    source.write_bytes(b"PGDMP clone")

    with pytest.raises(ValueError, match="does not match"):
        gateway.restore_clone_dump(source, recovery_target="other-project")

    assert runner.calls == []


def test_clone_restore_uses_external_logs_root_and_cleans_staging(tmp_path: Path) -> None:
    runner = _NativeRunner()
    project_root = tmp_path / "data"
    project_root.mkdir()
    support_root = tmp_path / "installer-state" / "clone"
    gateway = FilesystemBackupGateway.for_native_database(
        project_root=project_root,
        database_host="postgres.internal",
        database_port=5432,
        database_user="operator",
        database_name="database",
        database_password="secret",
        recovery_target="installed-project",
        staging_root=support_root / "database-staging",
        logs_root=support_root / "database-logs",
        runner=runner,
    )
    source = tmp_path / "authenticated.dump"
    source.write_bytes(b"PGDMP clone")

    gateway.restore_clone_dump(source, recovery_target="installed-project")

    logs = tuple((support_root / "database-logs").glob("clone-database-restore-*.log"))
    assert len(logs) == 1
    assert logs[0].stat().st_mode & 0o777 == 0o600
    assert list((support_root / "database-staging").rglob("*.dump")) == []
    assert not (project_root / "logs").exists()


def test_native_backup_transport_keeps_password_out_of_argv(tmp_path: Path) -> None:
    runner = _NativeRunner()
    gateway = FilesystemBackupGateway.for_native_database(
        project_root=tmp_path,
        database_host="postgres.internal",
        database_port=5544,
        database_user="flow_steward",
        database_name="flow_steward",
        database_password="not-in-argv",
        runner=runner,
        key_source=lambda: "backup-key",
    )
    workspace = gateway.prepare_create("native-transport")
    log = gateway.open_log("native-transport")

    gateway.dump_database(workspace.dump_path, log=log)
    gateway.inspect_dump(workspace.dump_path, log=log)

    assert runner.calls[0][0] == (
        "pg_dump",
        "-h",
        "postgres.internal",
        "-p",
        "5544",
        "-U",
        "flow_steward",
        "-d",
        "flow_steward",
        "-Fc",
    )
    assert runner.calls[1][0] == ("pg_restore", "-l")
    assert all("not-in-argv" not in item for call, _kwargs in runner.calls for item in call)
    assert all(kwargs["env"]["PGPASSWORD"] == "not-in-argv" for _call, kwargs in runner.calls)


def test_native_restore_requires_verified_matching_maintenance_target(tmp_path: Path) -> None:
    runner = _NativeRunner()
    gateway = FilesystemBackupGateway.for_native_database(
        project_root=tmp_path,
        database_host="postgres.internal",
        database_port=5432,
        database_user="operator",
        database_name="database",
        database_password="secret",
        runner=runner,
        key_source=lambda: "backup-key",
    )
    workspace = gateway.prepare_create("native-restore")
    workspace.dump_path.write_bytes(b"PGDMP verified")
    log = gateway.open_log("native-restore")

    for overrides, expected in (
        ({"maintenance_confirmed": False}, "maintenance"),
        ({"verified": False}, "verification"),
        ({"recovery_target": "other"}, "recovery target"),
    ):
        arguments = {
            "maintenance_confirmed": True,
            "verified": True,
            "recovery_target": "installation-a",
            "expected_recovery_target": "installation-a",
        }
        arguments.update(overrides)
        with pytest.raises(ValueError, match=expected):
            gateway.restore_database(workspace.dump_path, log=log, **arguments)
    assert runner.calls == []

    gateway.restore_database(
        workspace.dump_path,
        log=log,
        maintenance_confirmed=True,
        verified=True,
        recovery_target="installation-a",
        expected_recovery_target="installation-a",
    )

    drop_command, drop_kwargs = runner.calls[-4]
    create_command, create_kwargs = runner.calls[-3]
    command, kwargs = runner.calls[-2]
    finalize_command, finalize_kwargs = runner.calls[-1]
    assert drop_command == (
        "dropdb",
        "--if-exists",
        "--force",
        "--maintenance-db=postgres",
        "-h",
        "postgres.internal",
        "-p",
        "5432",
        "-U",
        "operator",
        "database",
    )
    assert create_command == (
        "createdb",
        "--template=template0",
        "--owner=operator",
        "--maintenance-db=postgres",
        "-h",
        "postgres.internal",
        "-p",
        "5432",
        "-U",
        "operator",
        "database",
    )
    assert command[:5] == (
        "pg_restore",
        "-v",
        "--no-owner",
        "--no-privileges",
        "-h",
    )
    assert finalize_command[0] == "psql"
    assert finalize_kwargs["input"].startswith(b"/* FLOW_STEWARD_AGE_RESTORE_REPAIR */")
    assert all(
        "secret" not in item
        for item in (*drop_command, *create_command, *command, *finalize_command)
    )
    for call_kwargs in (drop_kwargs, create_kwargs, kwargs, finalize_kwargs):
        assert call_kwargs["env"]["PGPASSWORD"] == "secret"


def test_native_restore_refuses_postgresql_system_database(tmp_path: Path) -> None:
    runner = _NativeRunner()
    gateway = FilesystemBackupGateway.for_native_database(
        project_root=tmp_path,
        database_host="postgres.internal",
        database_port=5432,
        database_user="operator",
        database_name="postgres",
        database_password="secret",
        runner=runner,
        key_source=lambda: "backup-key",
    )
    workspace = gateway.prepare_create("native-system-database")
    workspace.dump_path.write_bytes(b"PGDMP verified")

    log = gateway.open_log("native-system-database")

    with pytest.raises(ValueError, match="system databases"):
        gateway.restore_database(
            workspace.dump_path,
            log=log,
            maintenance_confirmed=True,
            verified=True,
            recovery_target="installation-a",
            expected_recovery_target="installation-a",
        )

    assert runner.calls == []


@pytest.mark.parametrize(
    ("failed_command", "expected_code", "expected_step"),
    (
        ("dropdb", "backup_database_reset_failed", "database_reset"),
        ("createdb", "backup_database_reset_failed", "database_reset"),
        ("pg_restore", "backup_pg_restore_failed", "pg_restore"),
        ("psql", "backup_database_finalize_failed", "database_finalize"),
    ),
)
def test_native_restore_keeps_reset_restore_and_finalize_failures_visible(
    tmp_path: Path,
    failed_command: str,
    expected_code: str,
    expected_step: str,
) -> None:
    runner = _NativeRunner(fail_command=failed_command)
    gateway = FilesystemBackupGateway.for_native_database(
        project_root=tmp_path,
        database_host="postgres.internal",
        database_port=5432,
        database_user="operator",
        database_name="flow_steward",
        database_password="secret",
        runner=runner,
        key_source=lambda: "backup-key",
    )
    workspace = gateway.prepare_create(f"native-{failed_command}-failure")
    workspace.dump_path.write_bytes(b"PGDMP verified")

    log = gateway.open_log(f"native-{failed_command}-failure")

    with pytest.raises(BackupGatewayError) as captured:
        gateway.restore_database(
            workspace.dump_path,
            log=log,
            maintenance_confirmed=True,
            verified=True,
            recovery_target="installation-a",
            expected_recovery_target="installation-a",
        )

    assert captured.value.code == expected_code
    assert captured.value.step == expected_step
