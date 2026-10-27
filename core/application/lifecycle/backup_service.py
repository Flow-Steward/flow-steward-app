"""Application policy and closed contracts for protected PostgreSQL backups."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from core.application.lifecycle.contracts import CancellationToken
from core.application.lifecycle.errors import ForcedLifecycleInterrupt, LifecycleError
from core.application.management.command_specs.contracts import (
    ExitCode,
    OperationProgressEvent,
    OperationStepStatus,
)

_MESSAGE_INSPECT_PROTECTED_POSTGRESQL_BACKUP = "Inspect protected PostgreSQL backup"

TechnicalSink = Callable[[str], None]
ProgressSink = Callable[[OperationProgressEvent], None]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BackupVerificationStatus(StrEnum):
    """Persisted verification state exposed by backup list."""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"


class BackupCreateRequest(_ClosedModel):
    """Closed no-input contract for generated backup creation."""

    operation_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
    )


class BackupListRequest(_ClosedModel):
    """Closed no-input contract for backup metadata listing."""


class BackupVerifyRequest(_ClosedModel):
    """One operator-selected artifact that must resolve below ``backups/``."""

    backup: str = Field(min_length=1)


class BackupResult(_ClosedModel):
    """Safe metadata returned after create or full verification."""

    path: str
    size_bytes: int = Field(ge=1)
    created_at: datetime
    verification_status: BackupVerificationStatus
    verified_at: datetime | None = None
    log_path: str | None = None


class BackupListResult(_ClosedModel):
    """Metadata-only backup inventory."""

    backups: tuple[BackupResult, ...] = ()


@dataclass(frozen=True)
class BackupArtifactSnapshot:
    """Infrastructure-neutral metadata for one encrypted backup."""

    path: Path
    size_bytes: int
    created_at: datetime
    verification_status: BackupVerificationStatus
    verified_at: datetime | None = None


@dataclass(frozen=True)
class BackupWorkspace:
    """Owner-only paths allocated by the filesystem adapter."""

    operation_id: str
    dump_path: Path
    encrypted_path: Path
    validation_path: Path
    final_path: Path


class BackupLog(Protocol):
    path: Path

    def write(self, message: str) -> None: ...

    def close(self) -> None: ...


class BackupGatewayError(RuntimeError):
    """Safe technical failure mapped to the lifecycle error contract."""

    def __init__(
        self,
        *,
        code: str,
        cause: str,
        remediation: str,
        step: str,
        retryable: bool = True,
    ) -> None:
        super().__init__(cause)
        self.code = code
        self.cause = cause
        self.remediation = remediation
        self.step = step
        self.retryable = retryable


class BackupGateway(Protocol):
    """Technical filesystem, process, and codec operations used by the service."""

    def prepare_create(self, operation_id: str | None = None) -> BackupWorkspace: ...

    def prepare_clone_dump(self, operation_id: str) -> Path: ...

    def find_verified(self, operation_id: str) -> BackupArtifactSnapshot | None: ...

    def open_log(self, operation_id: str) -> BackupLog: ...

    def validate_key(self) -> None: ...

    def dump_database(self, destination: Path, *, log: BackupLog) -> None: ...

    def encrypt_dump(self, source: Path, destination: Path) -> None: ...

    def verify_envelope(self, source: Path) -> None: ...

    def decrypt_dump(self, source: Path, destination: Path) -> None: ...

    def inspect_dump(self, source: Path, *, log: BackupLog) -> None: ...

    def publish_backup(self, workspace: BackupWorkspace) -> BackupArtifactSnapshot: ...

    def list_backups(self) -> tuple[BackupArtifactSnapshot, ...]: ...

    def resolve_backup(self, raw_path: str) -> Path: ...

    def prepare_validation(self, source: Path) -> BackupWorkspace: ...

    def record_verification(self, source: Path) -> BackupArtifactSnapshot: ...

    def remove_file(self, path: Path) -> None: ...

    def remove_plaintext(self, path: Path) -> None: ...

    def remove_incomplete_output(self, workspace: BackupWorkspace) -> None: ...

    def restore_clone_dump(self, source: Path, *, recovery_target: str) -> None: ...


class BackupService:
    """Orchestrate backup policy while delegating technical effects to a gateway."""

    def __init__(self, *, gateway: BackupGateway) -> None:
        self._gateway = gateway

    @contextmanager
    def clone_dump(self, operation_id: str) -> Iterator[Path]:
        """Yield one inspected plaintext dump without acquiring a lifecycle lock."""
        try:
            log = self._gateway.open_log("clone-database")
        except (OSError, ValueError, RuntimeError) as exc:
            raise _unexpected_error(step="clone_database_log") from exc
        dump: Path | None = None
        try:
            dump = self._gateway.prepare_clone_dump(operation_id)
            self._gateway.dump_database(dump, log=log)
            self._gateway.inspect_dump(dump, log=log)
            yield dump
        except BackupGatewayError as exc:
            raise _lifecycle_error(exc, log_path=log.path) from exc
        except LifecycleError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise _unexpected_error(step="clone_database", log_path=log.path) from exc
        finally:
            cleanup_failure: BackupGatewayError | None = None
            if dump is not None:
                try:
                    self._gateway.remove_plaintext(dump)
                except BackupGatewayError as exc:
                    cleanup_failure = exc
            log.close()
            if cleanup_failure is not None:
                raise _lifecycle_error(cleanup_failure, log_path=log.path)

    def restore_clone_dump(self, source: Path, *, recovery_target: str) -> None:
        """Restore an authenticated Clone dump without acquiring another lifecycle lock."""
        try:
            self._gateway.restore_clone_dump(source, recovery_target=recovery_target)
        except BackupGatewayError as exc:
            raise _lifecycle_error(exc) from exc
        except LifecycleError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise _unexpected_error(step="clone_database_restore") from exc

    def create(self, request: BackupCreateRequest, **operation_sinks: object) -> BackupResult:
        """Create, fully validate, and only then atomically publish a backup."""
        operation_id = request.operation_id
        if operation_id is not None:
            try:
                existing = self._gateway.find_verified(operation_id)
            except (OSError, ValueError, RuntimeError) as exc:
                raise _unexpected_error(step="metadata") from exc
            if existing is not None:
                return _result(existing)
        technical_sink = _technical_sink(operation_sinks)
        progress_sink = _progress_sink(operation_sinks)
        cancellation = _cancellation_token(operation_sinks)
        try:
            log = self._gateway.open_log("backup-create")
        except (OSError, ValueError, RuntimeError) as exc:
            raise _unexpected_error(step="log") from exc
        workspace: BackupWorkspace | None = None
        try:
            workspace = self._gateway.prepare_create(operation_id)
            self._gateway.validate_key()
            _progress(progress_sink, "dump", "Create PostgreSQL custom-format dump")
            _emit(technical_sink, "Creating PostgreSQL custom-format dump.")
            self._gateway.dump_database(workspace.dump_path, log=log)
            _progress(
                progress_sink,
                "dump",
                "Create PostgreSQL custom-format dump",
                succeeded=True,
            )
            _raise_for_cancellation(cancellation, step="dump")
            _progress(progress_sink, "encrypt", "Encrypt protected backup")
            _emit(technical_sink, "Encrypting backup with the dedicated backup key.")
            self._gateway.encrypt_dump(workspace.dump_path, workspace.encrypted_path)
            self._gateway.verify_envelope(workspace.encrypted_path)
            _progress(progress_sink, "encrypt", "Encrypt protected backup", succeeded=True)
            _raise_for_cancellation(cancellation, step="encrypt")
            _progress(progress_sink, "inspect", _MESSAGE_INSPECT_PROTECTED_POSTGRESQL_BACKUP)
            _emit(technical_sink, "Validating encrypted PostgreSQL backup.")
            self._gateway.decrypt_dump(workspace.encrypted_path, workspace.validation_path)
            _raise_for_cancellation(cancellation, step="decrypt")
            self._gateway.inspect_dump(workspace.validation_path, log=log)
            _progress(
                progress_sink,
                "inspect",
                _MESSAGE_INSPECT_PROTECTED_POSTGRESQL_BACKUP,
                succeeded=True,
            )
            _raise_for_cancellation(cancellation, step="inspect")
            _progress(progress_sink, "publish", "Publish protected backup")
            snapshot = self._gateway.publish_backup(workspace)
            _progress(progress_sink, "publish", "Publish protected backup", succeeded=True)
            _emit(technical_sink, "Protected PostgreSQL backup published.")
            return _result(snapshot, log_path=log.path)
        except BackupGatewayError as exc:
            failure = _remove_incomplete(self._gateway, workspace) or exc
            log.write(f"{failure.code}: {failure.cause}")
            raise _lifecycle_error(failure, log_path=log.path) from exc
        except LifecycleError as exc:
            cleanup_failure = _remove_incomplete(self._gateway, workspace)
            if cleanup_failure is not None:
                log.write(f"{cleanup_failure.code}: {cleanup_failure.cause}")
                raise _lifecycle_error(cleanup_failure, log_path=log.path) from exc
            log.write(f"{exc.code}: {exc.cause}")
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            cleanup_failure = _remove_incomplete(self._gateway, workspace)
            if cleanup_failure is not None:
                log.write(f"{cleanup_failure.code}: {cleanup_failure.cause}")
                raise _lifecycle_error(cleanup_failure, log_path=log.path) from exc
            log.write("backup_internal_error: protected backup creation failed")
            raise _unexpected_error(step="backup", log_path=log.path) from exc
        finally:
            cleanup_failure = _remove_workspace_temps(self._gateway, workspace)
            if cleanup_failure is not None:
                log.write(f"{cleanup_failure.code}: {cleanup_failure.cause}")
            log.close()
            if cleanup_failure is not None:
                raise _lifecycle_error(cleanup_failure, log_path=log.path)

    def find_verified(self, operation_id: str) -> BackupResult | None:
        """Return an operation-scoped verified artifact without creating another backup."""
        try:
            snapshot = self._gateway.find_verified(operation_id)
        except (OSError, ValueError, RuntimeError) as exc:
            raise _unexpected_error(step="metadata") from exc
        return _result(snapshot) if snapshot is not None else None

    def list(self, request: BackupListRequest) -> BackupListResult:
        """Return persisted/stat metadata without decrypting backup contents."""
        del request
        try:
            return BackupListResult(
                backups=tuple(_result(snapshot) for snapshot in self._gateway.list_backups())
            )
        except BackupGatewayError as exc:
            raise _lifecycle_error(exc) from exc
        except (OSError, ValueError, RuntimeError) as exc:
            raise _unexpected_error(step="list") from exc

    def verify(self, request: BackupVerifyRequest, **operation_sinks: object) -> BackupResult:
        """Fully validate one confined artifact without connecting to a restore target."""
        technical_sink = _technical_sink(operation_sinks)
        progress_sink = _progress_sink(operation_sinks)
        try:
            log = self._gateway.open_log("backup-verify")
        except (OSError, ValueError, RuntimeError) as exc:
            raise _unexpected_error(step="log") from exc
        workspace: BackupWorkspace | None = None
        try:
            source = self._gateway.resolve_backup(request.backup)
            workspace = self._gateway.prepare_validation(source)
            _progress(progress_sink, "inspect", _MESSAGE_INSPECT_PROTECTED_POSTGRESQL_BACKUP)
            _emit(technical_sink, "Validating protected PostgreSQL backup.")
            self._gateway.verify_envelope(source)
            self._gateway.decrypt_dump(source, workspace.validation_path)
            self._gateway.inspect_dump(workspace.validation_path, log=log)
            snapshot = self._gateway.record_verification(source)
            _progress(
                progress_sink,
                "inspect",
                _MESSAGE_INSPECT_PROTECTED_POSTGRESQL_BACKUP,
                succeeded=True,
            )
            return _result(snapshot, log_path=log.path)
        except BackupGatewayError as exc:
            log.write(f"{exc.code}: {exc.cause}")
            raise _lifecycle_error(exc, log_path=log.path) from exc
        except (OSError, ValueError, RuntimeError) as exc:
            log.write("backup_internal_error: protected backup verification failed")
            raise _unexpected_error(step="verify", log_path=log.path) from exc
        finally:
            cleanup_failure = _remove_validation_temp(self._gateway, workspace)
            if cleanup_failure is not None:
                log.write(f"{cleanup_failure.code}: {cleanup_failure.cause}")
            log.close()
            if cleanup_failure is not None:
                raise _lifecycle_error(cleanup_failure, log_path=log.path)


def _technical_sink(operation_sinks: dict[str, object]) -> TechnicalSink | None:
    value = operation_sinks.get("technical_sink")
    return value if callable(value) else None


def _progress_sink(operation_sinks: dict[str, object]) -> ProgressSink | None:
    value = operation_sinks.get("progress_sink")
    return value if callable(value) else None


def _cancellation_token(operation_sinks: dict[str, object]) -> CancellationToken | None:
    value = operation_sinks.get("cancellation")
    return value if isinstance(value, CancellationToken) else None


def _raise_for_cancellation(
    cancellation: CancellationToken | None,
    *,
    step: str,
) -> None:
    if cancellation is None:
        return
    if cancellation.forced:
        raise ForcedLifecycleInterrupt
    if cancellation.requested:
        raise LifecycleError.interrupted(
            step=step,
            recovery_command=cancellation.recovery_command,
            forced=False,
        )


def _progress(
    sink: ProgressSink | None,
    step_id: str,
    label: str,
    *,
    succeeded: bool = False,
) -> None:
    if sink is not None:
        sink(
            OperationProgressEvent(
                step_id=step_id,
                status=(
                    OperationStepStatus.SUCCEEDED if succeeded else OperationStepStatus.RUNNING
                ),
                label=label,
            )
        )


def _emit(sink: TechnicalSink | None, message: str) -> None:
    if sink is not None:
        sink(message)


def _result(
    snapshot: BackupArtifactSnapshot,
    *,
    log_path: Path | None = None,
) -> BackupResult:
    return BackupResult(
        path=str(snapshot.path),
        size_bytes=snapshot.size_bytes,
        created_at=snapshot.created_at,
        verification_status=snapshot.verification_status,
        verified_at=snapshot.verified_at,
        log_path=str(log_path) if log_path is not None else None,
    )


def _lifecycle_error(
    failure: BackupGatewayError,
    *,
    log_path: Path | None = None,
) -> LifecycleError:
    return LifecycleError(
        code=failure.code,
        summary="The protected PostgreSQL backup operation failed.",
        cause=failure.cause,
        remediation=failure.remediation,
        retryable=failure.retryable,
        step=failure.step,
        exit_code=ExitCode.DEPENDENCY if failure.retryable else ExitCode.STABLE_FAILURE,
        log_path=str(log_path) if log_path is not None else None,
    )


def _unexpected_error(*, step: str, log_path: Path | None = None) -> LifecycleError:
    return LifecycleError(
        code="backup_internal_error",
        summary="The protected PostgreSQL backup operation failed.",
        cause="An unexpected backup error occurred.",
        remediation="Inspect the retained backup log before retrying.",
        retryable=False,
        step=step,
        exit_code=ExitCode.INTERNAL,
        log_path=str(log_path) if log_path is not None else None,
    )


def _remove_plaintext(
    gateway: BackupGateway,
    *paths: Path,
) -> BackupGatewayError | None:
    first_failure: BackupGatewayError | None = None
    for path in paths:
        try:
            gateway.remove_plaintext(path)
        except BackupGatewayError as exc:
            first_failure = first_failure or exc
    return first_failure


def _remove_workspace_temps(
    gateway: BackupGateway,
    workspace: BackupWorkspace | None,
) -> BackupGatewayError | None:
    if workspace is None:
        return None
    first_failure = _remove_plaintext(
        gateway,
        workspace.dump_path,
        workspace.validation_path,
    )
    try:
        gateway.remove_file(workspace.encrypted_path)
    except BackupGatewayError as exc:
        first_failure = first_failure or exc
    return first_failure


def _remove_validation_temp(
    gateway: BackupGateway,
    workspace: BackupWorkspace | None,
) -> BackupGatewayError | None:
    if workspace is None:
        return None
    return _remove_plaintext(gateway, workspace.validation_path)


def _remove_incomplete(
    gateway: BackupGateway,
    workspace: BackupWorkspace | None,
) -> BackupGatewayError | None:
    if workspace is None:
        return None
    try:
        gateway.remove_incomplete_output(workspace)
    except BackupGatewayError as exc:
        return exc
    return None


__all__ = [
    "BackupArtifactSnapshot",
    "BackupCreateRequest",
    "BackupGateway",
    "BackupGatewayError",
    "BackupListRequest",
    "BackupListResult",
    "BackupResult",
    "BackupService",
    "BackupVerificationStatus",
    "BackupVerifyRequest",
    "BackupWorkspace",
]
