from __future__ import annotations

import base64
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.application.lifecycle.backup_service import (
    BackupArtifactSnapshot,
    BackupCreateRequest,
    BackupGatewayError,
    BackupListRequest,
    BackupService,
    BackupVerificationStatus,
    BackupVerifyRequest,
    BackupWorkspace,
)
from core.application.lifecycle.contracts import CancellationToken
from core.application.lifecycle.errors import LifecycleError
from core.application.management.command_specs.contracts import (
    OperationProgressEvent,
    OperationStepStatus,
)
from core.infrastructure.lifecycle.backup_gateway import FilesystemBackupGateway


class FakeLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.messages: list[str] = []
        self.closed = False

    def write(self, message: str) -> None:
        self.messages.append(message)

    def close(self) -> None:
        self.closed = True


class FakeBackupGateway:
    def __init__(self, tmp_path: Path, *, fail_step: str = "") -> None:
        backups = tmp_path / "backups"
        self.workspace = BackupWorkspace(
            operation_id="backup-1",
            dump_path=backups / ".backup-1.dump",
            encrypted_path=backups / ".backup-1.dump.enc",
            validation_path=backups / ".backup-1.validate.dump",
            final_path=backups / "flow_steward_20260724T120000Z.dump.enc",
        )
        self.snapshot = BackupArtifactSnapshot(
            path=self.workspace.final_path,
            size_bytes=321,
            created_at=datetime(2026, 7, 24, 12, tzinfo=UTC),
            verification_status=BackupVerificationStatus.VERIFIED,
            verified_at=datetime(2026, 7, 24, 12, 0, 1, tzinfo=UTC),
        )
        self.fail_step = fail_step
        self.calls: list[str] = []
        self.log = FakeLog(tmp_path / "logs" / "backup.log")
        self.published_operation_id: str | None = None

    def prepare_create(self, operation_id: str | None = None) -> BackupWorkspace:
        self.calls.append(
            f"prepare_create:{operation_id}" if operation_id is not None else "prepare_create"
        )
        if self.fail_step == "prepare":
            raise BackupGatewayError(
                code="backup_storage_unavailable",
                cause="The backup workspace is unavailable.",
                remediation="Repair backups/ permissions and retry.",
                step="prepare",
            )
        if operation_id is not None:
            self.workspace = replace(self.workspace, operation_id=operation_id)
        return self.workspace

    def open_log(self, operation_id: str) -> FakeLog:
        self.calls.append(f"open_log:{operation_id}")
        return self.log

    def dump_database(self, destination: Path, *, log: FakeLog) -> None:
        assert destination == self.workspace.dump_path
        assert log is self.log
        self._step("pg_dump")

    def validate_key(self) -> None:
        self._step("key")

    def encrypt_dump(self, source: Path, destination: Path) -> None:
        assert source == self.workspace.dump_path
        assert destination == self.workspace.encrypted_path
        self._step("encrypt")

    def verify_envelope(self, source: Path) -> None:
        assert source in {self.workspace.encrypted_path, self.workspace.final_path}
        self._step("envelope")

    def decrypt_dump(self, source: Path, destination: Path) -> None:
        assert source in {self.workspace.encrypted_path, self.workspace.final_path}
        assert destination == self.workspace.validation_path
        self._step("decrypt")

    def inspect_dump(self, source: Path, *, log: FakeLog) -> None:
        assert source == self.workspace.validation_path
        assert log is self.log
        self._step("pg_restore_list")

    def publish_backup(self, workspace: BackupWorkspace) -> BackupArtifactSnapshot:
        assert workspace is self.workspace
        self._step("publish")
        self.published_operation_id = workspace.operation_id
        return self.snapshot

    def find_verified(self, operation_id: str) -> BackupArtifactSnapshot | None:
        self.calls.append(f"find_verified:{operation_id}")
        return self.snapshot if self.published_operation_id == operation_id else None

    def list_backups(self) -> tuple[BackupArtifactSnapshot, ...]:
        self.calls.append("list")
        return (self.snapshot,)

    def resolve_backup(self, raw_path: str) -> Path:
        self.calls.append(f"resolve:{raw_path}")
        if self.fail_step == "resolve":
            raise BackupGatewayError(
                code="backup_path_invalid",
                cause="The backup path is outside the configured backup directory.",
                remediation="Select a regular backup file under backups/.",
                step="resolve",
            )
        return self.workspace.final_path

    def prepare_validation(self, source: Path) -> BackupWorkspace:
        assert source == self.workspace.final_path
        self.calls.append("prepare_validation")
        return self.workspace

    def record_verification(self, source: Path) -> BackupArtifactSnapshot:
        assert source == self.workspace.final_path
        self._step("record_verification")
        return self.snapshot

    def remove_file(self, path: Path) -> None:
        self.calls.append(f"remove:{path.name}")
        if self.fail_step == "artifact_cleanup" and path == self.workspace.encrypted_path:
            raise BackupGatewayError(
                code="backup_cleanup_failed",
                cause="Temporary backup artifacts could not be removed.",
                remediation="Remove the reported owner-only temp file before retrying.",
                step="cleanup",
                retryable=False,
            )

    def remove_plaintext(self, path: Path) -> None:
        self.calls.append(f"remove:{path.name}")
        if self.fail_step == "cleanup" and path == self.workspace.validation_path:
            raise BackupGatewayError(
                code="backup_plaintext_cleanup_failed",
                cause="Temporary plaintext backup material could not be removed.",
                remediation="Remove the reported owner-only temp file before retrying.",
                step="cleanup",
                retryable=False,
            )

    def remove_incomplete_output(self, workspace: BackupWorkspace) -> None:
        assert workspace is self.workspace
        self.calls.append("remove_incomplete_output")

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_step == name:
            raise BackupGatewayError(
                code=f"backup_{name}_failed",
                cause=f"The {name} step failed.",
                remediation="Inspect the retained backup log and retry.",
                step=name,
            )


class CancelAfterDumpGateway(FakeBackupGateway):
    def __init__(self, tmp_path: Path, cancellation: CancellationToken) -> None:
        super().__init__(tmp_path)
        self.cancellation = cancellation

    def dump_database(self, destination: Path, *, log: FakeLog) -> None:
        super().dump_database(destination, log=log)
        self.cancellation.request()


def test_backup_requests_and_results_are_closed_contracts(tmp_path: Path) -> None:
    service = BackupService(gateway=FakeBackupGateway(tmp_path))

    assert service.create(BackupCreateRequest()).verification_status == "verified"
    assert service.list(BackupListRequest()).backups[0].size_bytes == 321
    assert (
        service.verify(
            BackupVerifyRequest(backup="flow_steward_20260724T120000Z.dump.enc")
        ).verification_status
        == "verified"
    )
    with pytest.raises(ValidationError):
        BackupCreateRequest(output="/tmp/operator-selected.dump.enc")
    with pytest.raises(ValidationError):
        BackupListRequest(decrypt=True)
    with pytest.raises(ValidationError):
        BackupVerifyRequest(
            backup="flow_steward_20260724T120000Z.dump.enc",
            restore_target="postgresql://example.invalid/restore",
        )


def test_create_validates_before_atomic_publish_and_cleans_all_temps(tmp_path: Path) -> None:
    gateway = FakeBackupGateway(tmp_path)

    result = BackupService(gateway=gateway).create(BackupCreateRequest())

    assert result.path.endswith("flow_steward_20260724T120000Z.dump.enc")
    assert result.size_bytes == 321
    assert gateway.calls[:9] == [
        "open_log:backup-create",
        "prepare_create",
        "key",
        "pg_dump",
        "encrypt",
        "envelope",
        "decrypt",
        "pg_restore_list",
        "publish",
    ]
    assert gateway.calls[9:] == [
        "remove:.backup-1.dump",
        "remove:.backup-1.validate.dump",
        "remove:.backup-1.dump.enc",
    ]
    assert gateway.log.closed is True


def test_create_and_verify_emit_structured_progress(tmp_path: Path) -> None:
    gateway = FakeBackupGateway(tmp_path)
    service = BackupService(gateway=gateway)
    events: list[OperationProgressEvent] = []

    created = service.create(BackupCreateRequest(), progress_sink=events.append)
    create_steps = [(event.step_id, event.status) for event in events]
    events.clear()
    verified = service.verify(
        BackupVerifyRequest(backup=created.path),
        progress_sink=events.append,
    )

    assert create_steps == [
        ("dump", OperationStepStatus.RUNNING),
        ("dump", OperationStepStatus.SUCCEEDED),
        ("encrypt", OperationStepStatus.RUNNING),
        ("encrypt", OperationStepStatus.SUCCEEDED),
        ("inspect", OperationStepStatus.RUNNING),
        ("inspect", OperationStepStatus.SUCCEEDED),
        ("publish", OperationStepStatus.RUNNING),
        ("publish", OperationStepStatus.SUCCEEDED),
    ]
    assert [(event.step_id, event.status) for event in events] == [
        ("inspect", OperationStepStatus.RUNNING),
        ("inspect", OperationStepStatus.SUCCEEDED),
    ]
    assert verified.verification_status is BackupVerificationStatus.VERIFIED


def test_create_cancels_at_safe_boundary_after_dump_and_cleans_workspace(
    tmp_path: Path,
) -> None:
    cancellation = CancellationToken()
    gateway = CancelAfterDumpGateway(tmp_path, cancellation)

    _raises_callable_279_1 = BackupService(gateway=gateway).create
    _raises_input_279_2 = BackupCreateRequest(operation_id="public-backup-op")
    with pytest.raises(LifecycleError) as raised:
        _raises_callable_279_1(_raises_input_279_2, cancellation=cancellation)

    assert raised.value.code == "lifecycle_cancelled"
    assert raised.value.step == "dump"
    assert "encrypt" not in gateway.calls
    assert "publish" not in gateway.calls
    assert "remove_incomplete_output" in gateway.calls
    assert "remove:.backup-1.dump" in gateway.calls
    assert "remove:.backup-1.validate.dump" in gateway.calls
    assert gateway.log.closed is True


def test_operation_scoped_create_is_idempotent_after_publish_before_outer_journal(
    tmp_path: Path,
) -> None:
    gateway = FakeBackupGateway(tmp_path)
    service = BackupService(gateway=gateway)
    request = BackupCreateRequest(operation_id="environment-op-1")

    first = service.create(request)
    second = service.create(request)

    assert first.path == second.path
    assert first.size_bytes == second.size_bytes
    assert first.verification_status is second.verification_status
    assert gateway.calls.count("pg_dump") == 1
    assert gateway.calls.count("publish") == 1
    assert gateway.calls.count("find_verified:environment-op-1") == 2


@pytest.mark.parametrize(
    "failure_step",
    ["pg_dump", "encrypt", "envelope", "decrypt", "pg_restore_list", "publish"],
)
def test_create_failure_never_publishes_success_and_removes_plaintext(
    tmp_path: Path,
    failure_step: str,
) -> None:
    gateway = FakeBackupGateway(tmp_path, fail_step=failure_step)

    _raises_callable_323_1 = BackupService(gateway=gateway).create
    _raises_input_323_2 = BackupCreateRequest()
    with pytest.raises(LifecycleError) as raised:
        _raises_callable_323_1(_raises_input_323_2)

    if failure_step != "publish":
        assert "publish" not in gateway.calls
    assert "remove_incomplete_output" in gateway.calls
    assert "remove:.backup-1.dump" in gateway.calls
    assert "remove:.backup-1.validate.dump" in gateway.calls
    assert gateway.log.closed is True
    assert raised.value.log_path == str(gateway.log.path)
    assert raised.value.remediation == "Inspect the retained backup log and retry."


def test_plaintext_cleanup_failure_blocks_create_success(tmp_path: Path) -> None:
    gateway = FakeBackupGateway(tmp_path, fail_step="cleanup")

    _raises_callable_339_1 = BackupService(gateway=gateway).create
    _raises_input_339_2 = BackupCreateRequest()
    with pytest.raises(LifecycleError) as raised:
        _raises_callable_339_1(_raises_input_339_2)

    assert raised.value.code == "backup_plaintext_cleanup_failed"
    assert gateway.log.closed is True


def test_encrypted_temp_cleanup_failure_blocks_create_success(tmp_path: Path) -> None:
    gateway = FakeBackupGateway(tmp_path, fail_step="artifact_cleanup")

    _raises_callable_349_1 = BackupService(gateway=gateway).create
    _raises_input_349_2 = BackupCreateRequest()
    with pytest.raises(LifecycleError) as raised:
        _raises_callable_349_1(_raises_input_349_2)

    assert raised.value.code == "backup_cleanup_failed"
    assert gateway.log.closed is True


def test_list_uses_metadata_only_without_decrypting_backups(tmp_path: Path) -> None:
    gateway = FakeBackupGateway(tmp_path)

    result = BackupService(gateway=gateway).list(BackupListRequest())

    assert result.backups[0].verification_status is BackupVerificationStatus.VERIFIED
    assert gateway.calls == ["list"]
    assert "decrypt" not in gateway.calls
    assert "pg_restore_list" not in gateway.calls


def test_verify_is_nonmutating_and_cleans_validation_plaintext(tmp_path: Path) -> None:
    gateway = FakeBackupGateway(tmp_path)

    result = BackupService(gateway=gateway).verify(
        BackupVerifyRequest(backup="backups/flow_steward_20260724T120000Z.dump.enc")
    )

    assert result.verification_status is BackupVerificationStatus.VERIFIED
    assert gateway.calls == [
        "open_log:backup-verify",
        "resolve:backups/flow_steward_20260724T120000Z.dump.enc",
        "prepare_validation",
        "envelope",
        "decrypt",
        "pg_restore_list",
        "record_verification",
        "remove:.backup-1.validate.dump",
    ]
    assert "publish" not in gateway.calls
    assert "pg_dump" not in gateway.calls


def test_verify_path_failure_does_not_open_or_decrypt_backup(tmp_path: Path) -> None:
    gateway = FakeBackupGateway(tmp_path, fail_step="resolve")

    _raises_callable_392_1 = BackupService(gateway=gateway).verify
    _raises_input_392_2 = BackupVerifyRequest(backup="../outside.dump.enc")
    with pytest.raises(LifecycleError) as raised:
        _raises_callable_392_1(_raises_input_392_2)

    assert raised.value.code == "backup_path_invalid"
    assert raised.value.log_path == str(gateway.log.path)
    assert gateway.calls == ["open_log:backup-verify", "resolve:../outside.dump.enc"]


class FakeProcessRunner:
    def __init__(
        self,
        *,
        dump_payload: bytes = b"PGDMP custom-format",
        fail_dump: bool = False,
        fail_restore_list: bool = False,
    ) -> None:
        self.dump_payload = dump_payload
        self.fail_dump = fail_dump
        self.fail_restore_list = fail_restore_list
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, **kwargs):
        normalized = tuple(str(item) for item in command)
        self.commands.append(normalized)
        if "pg_dump" in normalized:
            if self.fail_dump:
                return SimpleNamespace(returncode=1, stderr=b"pg_dump failed for password=unsafe")
            kwargs["stdout"].write(self.dump_payload)
            return SimpleNamespace(returncode=0, stderr=b"")
        if "pg_restore" in normalized and "-l" in normalized:
            kwargs["stdin"].read()
            return SimpleNamespace(
                returncode=1 if self.fail_restore_list else 0,
                stderr=b"invalid dump for postgresql://operator:unsafe@example/db",
            )
        raise AssertionError(f"unexpected command: {normalized}")


def _real_service(
    tmp_path: Path,
    *,
    key: str | None,
    runner: FakeProcessRunner | None = None,
) -> tuple[BackupService, FilesystemBackupGateway, FakeProcessRunner]:
    process = runner or FakeProcessRunner()

    def key_source() -> str:
        if key is None:
            raise RuntimeError("FS_BACKUP_ENCRYPTION_KEY is required for protected backups")
        return key

    gateway = FilesystemBackupGateway(
        project_root=tmp_path,
        key_source=key_source,
        runner=process,
        now=lambda: datetime(2026, 7, 24, 12, tzinfo=UTC),
    )
    return BackupService(gateway=gateway), gateway, process


def _temp_plaintext(backups: Path) -> list[Path]:
    return [
        path for path in backups.glob(".*") if path.suffix == ".dump" or ".validate." in path.name
    ]


def test_real_create_uses_owner_only_backup_temps_and_nonmutating_restore_list(
    tmp_path: Path,
) -> None:
    service, _gateway, process = _real_service(tmp_path, key="dedicated-key")

    result = service.create(BackupCreateRequest())

    backup = tmp_path / result.path
    assert backup.is_file()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "backups").stat().st_mode & 0o777 == 0o700
    assert _temp_plaintext(tmp_path / "backups") == []
    assert any("pg_dump" in command and "-Fc" in command for command in process.commands)
    restore_commands = [command for command in process.commands if "pg_restore" in command]
    assert len(restore_commands) == 1
    assert "-l" in restore_commands[0]
    assert "-d" not in restore_commands[0]
    assert "--dbname" not in restore_commands[0]


def test_real_create_stages_off_bind_mount_and_publishes_with_destination_local_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUDIT-076: Docker bind mounts must not host active crypto staging files."""
    project_root = tmp_path / "bind-mounted-project"
    project_root.mkdir()
    staging_root = tmp_path / "container-local"
    process = FakeProcessRunner()
    gateway = FilesystemBackupGateway(
        project_root=project_root,
        staging_root=staging_root,
        key_source=lambda: "dedicated-key",
        runner=process,
        now=lambda: datetime(2026, 7, 24, 12, tzinfo=UTC),
    )
    original_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def same_filesystem_replace(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        replacements.append((source_path, destination_path))
        if source_path.parent != destination_path.parent:
            raise OSError("simulated cross-filesystem rename")
        original_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", same_filesystem_replace)

    result = BackupService(gateway=gateway).create(BackupCreateRequest())

    assert (project_root / result.path).is_file()
    assert replacements
    assert all(source.parent == destination.parent for source, destination in replacements)
    assert list(staging_root.rglob("*")) == []
    assert not _temp_plaintext(project_root / "backups")


def test_decrypt_filesystem_error_is_not_reported_as_wrong_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUDIT-076: storage failures retain a storage error category."""
    from core.infrastructure.lifecycle import backup_gateway as backup_gateway_module

    staging_root = tmp_path / "container-local"
    gateway = FilesystemBackupGateway(
        project_root=tmp_path,
        staging_root=staging_root,
        key_source=lambda: "dedicated-key",
    )
    workspace = gateway.prepare_create()
    workspace.encrypted_path.write_bytes(b"protected payload")
    monkeypatch.setattr(
        backup_gateway_module,
        "decrypt_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("read-only filesystem")),
    )

    with pytest.raises(BackupGatewayError) as raised:
        gateway.decrypt_dump(workspace.encrypted_path, workspace.validation_path)

    assert raised.value.code == "backup_storage_unavailable"
    assert raised.value.step == "decrypt"
    assert "key" not in raised.value.remediation.lower()
    gateway.remove_incomplete_output(workspace)


@pytest.mark.parametrize(
    ("key", "runner", "expected_code"),
    [
        (None, FakeProcessRunner(), "backup_key_unavailable"),
        ("dedicated-key", FakeProcessRunner(fail_dump=True), "backup_pg_dump_failed"),
        (
            "dedicated-key",
            FakeProcessRunner(fail_restore_list=True),
            "backup_pg_restore_list_failed",
        ),
    ],
)
def test_real_create_failures_remove_incomplete_output_and_plaintext(
    tmp_path: Path,
    key: str | None,
    runner: FakeProcessRunner,
    expected_code: str,
) -> None:
    service, _gateway, _process = _real_service(tmp_path, key=key, runner=runner)

    _raises_input_566_1 = BackupCreateRequest()
    with pytest.raises(LifecycleError) as raised:
        service.create(_raises_input_566_1)

    backups = tmp_path / "backups"
    assert raised.value.code == expected_code
    assert list(backups.glob("*.dump.enc")) == []
    assert _temp_plaintext(backups) == []
    assert raised.value.log_path is not None
    log_text = Path(raised.value.log_path).read_text(encoding="utf-8")
    assert "dedicated-key" not in log_text
    assert "operator:unsafe" not in log_text
    assert "password=unsafe" not in log_text
    if expected_code == "backup_key_unavailable":
        assert runner.commands == []


def test_real_verify_rejects_escape_and_symlink_without_process_execution(tmp_path: Path) -> None:
    service, _gateway, process = _real_service(tmp_path, key="dedicated-key")
    outside = tmp_path / "outside.dump.enc"
    outside.write_bytes(b"protected-export:outside")
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    symlink = backups / "linked.dump.enc"
    symlink.symlink_to(outside)

    for raw in ("../outside.dump.enc", str(outside), str(symlink)):
        _raises_input_592_1 = BackupVerifyRequest(backup=raw)
        with pytest.raises(LifecycleError) as raised:
            service.verify(_raises_input_592_1)
        assert raised.value.code == "backup_path_invalid"
        assert raised.value.log_path is not None
        assert Path(raised.value.log_path).is_file()

    assert process.commands == []


def test_backup_root_symlink_is_rejected_without_writing_outside_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "backups").symlink_to(outside, target_is_directory=True)
    service, _gateway, process = _real_service(tmp_path, key="dedicated-key")

    _raises_input_607_1 = BackupCreateRequest()
    with pytest.raises(LifecycleError) as raised:
        service.create(_raises_input_607_1)

    assert raised.value.code == "backup_storage_unavailable"
    assert raised.value.log_path is not None
    assert Path(raised.value.log_path).is_file()
    assert list(outside.iterdir()) == []
    assert process.commands == []


def test_wrong_key_and_truncated_backup_fail_closed_without_plaintext(tmp_path: Path) -> None:
    creator, _gateway, _process = _real_service(tmp_path, key="correct-key")
    created = creator.create(BackupCreateRequest())
    backup_path = tmp_path / created.path

    wrong_key_service, _wrong_gateway, wrong_process = _real_service(
        tmp_path,
        key="wrong-key",
    )
    _raises_input_626_1 = BackupVerifyRequest(backup=created.path)
    with pytest.raises(LifecycleError) as wrong_key:
        wrong_key_service.verify(_raises_input_626_1)
    assert wrong_key.value.code == "backup_decrypt_failed"
    assert not any("pg_restore" in command for command in wrong_process.commands)
    assert _temp_plaintext(tmp_path / "backups") == []

    backup_path.write_bytes(backup_path.read_bytes()[:-12])
    _raises_input_633_1 = BackupVerifyRequest(backup=created.path)
    with pytest.raises(LifecycleError) as truncated:
        creator.verify(_raises_input_633_1)
    assert truncated.value.code in {"backup_envelope_invalid", "backup_decrypt_failed"}
    assert _temp_plaintext(tmp_path / "backups") == []


def test_lifecycle_verify_rejects_legacy_unauthenticated_cbc_before_decrypt(
    tmp_path: Path,
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    legacy = backups / "legacy.dump.enc"
    legacy.write_bytes(
        b"protected-export:openssl-aes256cbc-v1\n"
        + base64.urlsafe_b64encode(b"same-length-unauthenticated-payload")
        + b"\n"
    )
    service, _gateway, process = _real_service(tmp_path, key="dedicated-key")

    _raises_input_652_1 = BackupVerifyRequest(backup="backups/legacy.dump.enc")
    with pytest.raises(LifecycleError) as raised:
        service.verify(_raises_input_652_1)

    assert raised.value.code == "backup_envelope_invalid"
    assert not any("pg_restore" in command for command in process.commands)


def test_same_length_ciphertext_tamper_fails_authentication(tmp_path: Path) -> None:
    creator, _gateway, _process = _real_service(tmp_path, key="dedicated-key")
    created = creator.create(BackupCreateRequest())
    backup = tmp_path / created.path
    payload = bytearray(backup.read_bytes())
    if payload.startswith(b"protected-export:aesgcm-v1\n"):
        first_newline = payload.index(b"\n")
        second_newline = payload.index(b"\n", first_newline + 1)
        tamper_at = second_newline + 2
        payload[tamper_at] = ord("A") if payload[tamper_at] != ord("A") else ord("B")
    else:
        tamper_at = len(b"protected-export:aesgcm-stream-v2\n") + 12
        payload[tamper_at] ^= 1
    backup.write_bytes(payload)

    _raises_input_674_1 = BackupVerifyRequest(backup=created.path)
    with pytest.raises(LifecycleError) as raised:
        creator.verify(_raises_input_674_1)

    assert raised.value.code == "backup_decrypt_failed"
    assert _temp_plaintext(tmp_path / "backups") == []


def test_gateway_streams_backup_files_without_path_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _gateway, _process = _real_service(tmp_path, key="dedicated-key")

    def reject_whole_file_reads(_path: Path) -> bytes:
        raise AssertionError("whole-backup read_bytes is forbidden")

    monkeypatch.setattr(Path, "read_bytes", reject_whole_file_reads)

    created = service.create(BackupCreateRequest())
    verified = service.verify(BackupVerifyRequest(backup=created.path))

    assert verified.verification_status is BackupVerificationStatus.VERIFIED


def test_list_reads_persisted_verification_metadata_without_process_calls(tmp_path: Path) -> None:
    service, _gateway, process = _real_service(tmp_path, key="dedicated-key")
    created = service.create(BackupCreateRequest())
    process.commands.clear()

    listed = service.list(BackupListRequest())

    assert [item.path for item in listed.backups] == [created.path]
    assert listed.backups[0].size_bytes > 0
    assert listed.backups[0].created_at == datetime(2026, 7, 24, 12, tzinfo=UTC)
    assert listed.backups[0].verification_status is BackupVerificationStatus.VERIFIED
    assert listed.backups[0].verified_at is not None
    assert process.commands == []
    assert not _temp_plaintext(tmp_path / "backups")


def test_reverification_preserves_original_backup_timestamp(tmp_path: Path) -> None:
    created_at = datetime(2026, 7, 24, 12, tzinfo=UTC)
    verified_at = datetime(2026, 7, 25, 9, tzinfo=UTC)
    process = FakeProcessRunner()
    creator = BackupService(
        gateway=FilesystemBackupGateway(
            project_root=tmp_path,
            key_source=lambda: "dedicated-key",
            runner=process,
            now=lambda: created_at,
        )
    )
    created = creator.create(BackupCreateRequest())
    verifier = BackupService(
        gateway=FilesystemBackupGateway(
            project_root=tmp_path,
            key_source=lambda: "dedicated-key",
            runner=process,
            now=lambda: verified_at,
        )
    )

    verifier.verify(BackupVerifyRequest(backup=created.path))
    listed = verifier.list(BackupListRequest()).backups[0]

    assert listed.created_at == created_at
    assert listed.verified_at == verified_at


def test_first_verification_preserves_unverified_artifact_mtime(tmp_path: Path) -> None:
    creator, _gateway, _process = _real_service(tmp_path, key="dedicated-key")
    created = creator.create(BackupCreateRequest())
    backup = tmp_path / created.path
    backup.with_name(f"{backup.name}.metadata.json").unlink()
    expected_created_at = datetime.fromtimestamp(backup.stat().st_mtime, tz=UTC)
    verifier = BackupService(
        gateway=FilesystemBackupGateway(
            project_root=tmp_path,
            key_source=lambda: "dedicated-key",
            runner=FakeProcessRunner(),
            now=lambda: datetime(2026, 8, 1, tzinfo=UTC),
        )
    )

    verifier.verify(BackupVerifyRequest(backup=created.path))

    assert verifier.list(BackupListRequest()).backups[0].created_at == expected_created_at


def test_gateway_never_includes_database_credentials_or_dsn_in_commands(tmp_path: Path) -> None:
    runner = FakeProcessRunner()
    gateway = FilesystemBackupGateway(
        project_root=tmp_path,
        key_source=lambda: "dedicated-key",
        runner=runner,
        database_user="operator",
        database_name="flow_steward",
        database_password="must-not-appear",
        database_dsn="postgresql://operator:must-not-appear@example/db",
    )

    BackupService(gateway=gateway).create(BackupCreateRequest())

    rendered = repr(runner.commands)
    assert "must-not-appear" not in rendered
    assert "postgresql://" not in rendered
    assert os.environ.get("PGPASSWORD") != "must-not-appear"
