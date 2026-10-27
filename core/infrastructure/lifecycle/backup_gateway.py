"""Filesystem, codec, and PostgreSQL process adapter for lifecycle backups."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from core.application.lifecycle.backup_service import (
    BackupArtifactSnapshot,
    BackupGatewayError,
    BackupLog,
    BackupVerificationStatus,
    BackupWorkspace,
)
from core.application.lifecycle.contracts import ComposeTarget
from core.application.tooling.db.protected_export import (
    KeySource,
    backup_encryption_key_from_environment,
    decrypt_file,
    encrypt_file,
    validate_authenticated_file,
)
from core.infrastructure.lifecycle.backup_postgres import (
    _libpq_environment,
)
from core.infrastructure.lifecycle.backup_postgres import (
    dump_database as dump_postgres_database,
)
from core.infrastructure.lifecycle.backup_postgres import (
    inspect_dump as inspect_postgres_dump,
)
from core.infrastructure.lifecycle.backup_postgres import (
    restore_database as restore_postgres_database,
)
from core.infrastructure.lifecycle.state_store import SecureLifecycleLog

_PATH_DUMP_ENC = ".dump.enc"
_PATH_DUMP = ".dump"
_BACKUP_SUPPORT_ESCAPE_MESSAGE = "backup support directory escapes the project root"

ProcessRunner = Callable[..., Any]

_PROCESS_TIMEOUT_SECONDS = 300.0
_METADATA_SCHEMA_VERSION = 1
_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class FilesystemBackupGateway:
    """Keep backup files and transient plaintext confined to ``backups/``."""

    def __init__(
        self,
        *,
        project_root: Path,
        key_source: KeySource = backup_encryption_key_from_environment,
        runner: ProcessRunner | None = None,
        now: Callable[[], datetime] | None = None,
        compose_command: Sequence[str] = ("docker", "compose"),
        compose_project: str = "flow-steward",
        recovery_target: str | None = None,
        database_user: str = "flow_steward",
        database_name: str = "flow_steward",
        database_password: str | None = None,
        database_dsn: str | None = None,
        database_host: str = "127.0.0.1",
        database_port: int = 5432,
        native_database: bool = False,
        timeout_seconds: float = _PROCESS_TIMEOUT_SECONDS,
        staging_root: Path | None = None,
        logs_root: Path | None = None,
        database_parameters: Mapping[str, str] | None = None,
    ) -> None:
        self._project_root = Path(project_root).resolve()
        self._backups_root = self._project_root / "backups"
        self._logs_root = Path(
            os.path.abspath(logs_root or self._project_root / "logs" / "runtime")
        )
        self._external_logs_root = logs_root is not None
        self._staging_root = Path(
            os.path.abspath(
                staging_root or Path(tempfile.gettempdir()) / f"flow-steward-backups-{os.getuid()}"
            )
        )
        self._staging_directories: set[Path] = set()
        self._allocated_staging_paths: set[Path] = set()
        self._key_source = key_source
        self._runner = runner or subprocess.run
        self._now = now or (lambda: datetime.now(UTC))
        self._compose_command = tuple(str(item) for item in compose_command)
        self._compose_project = str(compose_project)
        self._recovery_target = str(recovery_target or compose_project)
        self._database_user = str(database_user)
        self._database_name = str(database_name)
        self._database_password = str(database_password or "")
        self._database_host = str(database_host)
        self._database_port = int(database_port)
        self._native_database = bool(native_database)
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        # A DSN may contain credentials, so this adapter never forwards it in argv.
        # Native callers must provide the split connection fields above.
        del database_dsn
        self._libpq_environment = _libpq_environment(database_parameters or {})

    @classmethod
    def for_compose_target(
        cls,
        *,
        project_root: Path,
        docker_host_project_root: Path | None = None,
        target: ComposeTarget,
        key_source: KeySource = backup_encryption_key_from_environment,
        runner: ProcessRunner | None = None,
    ) -> Self:
        """Build the process adapter from an application-derived Compose target."""
        root = Path(project_root).resolve()
        compose_root = Path(docker_host_project_root or project_root).resolve()
        compose_command = [
            "docker",
            "compose",
            "--env-file",
            str(compose_root / target.env_file),
        ]
        for compose_file in target.compose_files:
            compose_command.extend(("-f", str(compose_root / compose_file)))
        for profile in target.compose_profiles:
            compose_command.extend(("--profile", profile))
        return cls(
            project_root=root,
            key_source=key_source,
            compose_command=compose_command,
            compose_project=target.project_name,
            recovery_target=target.project_name,
            runner=runner,
        )

    @classmethod
    def for_native_database(
        cls,
        *,
        project_root: Path,
        database_host: str,
        database_port: int,
        database_user: str,
        database_name: str,
        database_password: str,
        **kwargs: Any,
    ) -> Self:
        """Build a Dockerless PostgreSQL client adapter for platform runtimes."""
        return cls(
            project_root=project_root,
            database_host=database_host,
            database_port=database_port,
            database_user=database_user,
            database_name=database_name,
            database_password=database_password,
            native_database=True,
            **kwargs,
        )

    def prepare_create(self, operation_id: str | None = None) -> BackupWorkspace:
        allocated: list[Path] = []
        staging_directory: Path | None = None
        try:
            self._ensure_directory(self._backups_root)
            now = self._utc_now()
            if operation_id is not None and not _SAFE_OPERATION_ID.fullmatch(operation_id):
                raise ValueError("Backup operation id is invalid")
            staging_directory = self._new_staging_directory()
            backup_operation_id = operation_id or f"backup-{uuid4().hex}"
            final_name = (
                f"flow_steward_environment_{backup_operation_id}.dump.enc"
                if operation_id is not None
                else f"flow_steward_{now:%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}.dump.enc"
            )
            final_path = self._backups_root / final_name
            for prefix, suffix in (
                (".backup-", _PATH_DUMP),
                (".backup-", _PATH_DUMP_ENC),
                (".backup-validate-", _PATH_DUMP),
            ):
                allocated.append(self._secure_temp(prefix, suffix, directory=staging_directory))
            return BackupWorkspace(
                operation_id=backup_operation_id,
                dump_path=allocated[0],
                encrypted_path=allocated[1],
                validation_path=allocated[2],
                final_path=final_path,
            )
        except (OSError, ValueError) as exc:
            cleanup_failure: BackupGatewayError | None = None
            for path in allocated:
                try:
                    self.remove_file(path)
                except BackupGatewayError as cleanup_exc:
                    cleanup_failure = cleanup_failure or cleanup_exc
            if staging_directory is not None:
                self._remove_empty_staging_directory(staging_directory)
            if cleanup_failure is not None:
                raise cleanup_failure from exc
            raise BackupGatewayError(
                code="backup_storage_unavailable",
                cause="Owner-only backup workspace files could not be created under backups/.",
                remediation="Check backups/ ownership, permissions, symlinks, and available disk space.",
                step="prepare",
            ) from exc

    def prepare_clone_dump(self, operation_id: str) -> Path:
        """Allocate one owner-only plaintext path for a composite Clone operation."""
        if not _SAFE_OPERATION_ID.fullmatch(str(operation_id)):
            raise ValueError("Clone operation id is invalid")
        staging_directory = self._new_staging_directory()
        try:
            return self._secure_temp(
                ".clone-database-",
                _PATH_DUMP,
                directory=staging_directory,
            )
        except OSError:
            self._remove_empty_staging_directory(staging_directory)
            raise

    def find_verified(self, operation_id: str) -> BackupArtifactSnapshot | None:
        if not _SAFE_OPERATION_ID.fullmatch(str(operation_id)):
            raise ValueError("Backup operation id is invalid")
        expected_name = f"flow_steward_environment_{operation_id}.dump.enc"
        return next(
            (
                snapshot
                for snapshot in self.list_backups()
                if Path(snapshot.path).name == expected_name
                and snapshot.verification_status is BackupVerificationStatus.VERIFIED
            ),
            None,
        )

    def prepare_validation(self, source: Path) -> BackupWorkspace:
        resolved = self.resolve_backup(str(source))
        staging_directory = self._new_staging_directory()
        try:
            return BackupWorkspace(
                operation_id=f"backup-verify-{uuid4().hex}",
                dump_path=resolved,
                encrypted_path=resolved,
                validation_path=self._secure_temp(
                    ".backup-validate-",
                    _PATH_DUMP,
                    directory=staging_directory,
                ),
                final_path=resolved,
            )
        except OSError:
            self._remove_empty_staging_directory(staging_directory)
            raise

    def open_log(self, operation_id: str) -> SecureLifecycleLog:
        if self._external_logs_root:
            self._ensure_external_root(self._logs_root, label="backup logs")
        else:
            self._ensure_directory(self._logs_root)
        safe_id = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in str(operation_id)
        )
        return SecureLifecycleLog(self._logs_root / f"{safe_id}-{uuid4().hex}.log")

    def validate_key(self) -> None:
        """Fail before creating plaintext when the dedicated key is unavailable."""
        self._dedicated_key()

    def dump_database(self, destination: Path, *, log: BackupLog) -> None:
        dump_postgres_database(
            self, destination, log=log, _nonempty_regular_file=_nonempty_regular_file
        )

    def encrypt_dump(self, source: Path, destination: Path) -> None:
        self._require_confined_temp(source)
        self._require_confined_temp(destination)
        try:
            key = self._dedicated_key()
            encrypt_file(source, destination, key_source=lambda: key)
            destination.chmod(0o600)
            self._fsync_file(destination)
        except BackupGatewayError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise BackupGatewayError(
                code="backup_encrypt_failed",
                cause="The PostgreSQL backup could not be encrypted.",
                remediation="Check backup storage permissions and retry.",
                step="encrypt",
            ) from exc
        if not _nonempty_regular_file(destination):
            raise BackupGatewayError(
                code="backup_encrypt_failed",
                cause="Backup encryption did not produce a protected artifact.",
                remediation="Check backup storage permissions and retry.",
                step="encrypt",
            )

    def verify_envelope(self, source: Path) -> None:
        self._require_confined_file(source)
        try:
            validate_authenticated_file(source)
        except (OSError, RuntimeError, ValueError) as exc:
            raise BackupGatewayError(
                code="backup_envelope_invalid",
                cause="The backup protected-export envelope is empty, malformed, or truncated.",
                remediation="Create a new backup; do not use the incomplete artifact.",
                step="envelope",
                retryable=False,
            ) from exc

    def decrypt_dump(self, source: Path, destination: Path) -> None:
        self._require_confined_file(source)
        self._require_confined_temp(destination)
        try:
            key = self._dedicated_key()
            decrypt_file(source, destination, key_source=lambda: key)
            destination.chmod(0o600)
            self._fsync_file(destination)
            if not _nonempty_regular_file(destination):
                raise RuntimeError("decrypted backup is empty")
        except BackupGatewayError:
            raise
        except OSError as exc:
            self.remove_file(destination)
            raise BackupGatewayError(
                code="backup_storage_unavailable",
                cause="Backup staging storage failed while decrypting the protected artifact.",
                remediation="Check container-local temporary storage permissions and available space.",
                step="decrypt",
            ) from exc
        except (RuntimeError, ValueError) as exc:
            self.remove_file(destination)
            raise BackupGatewayError(
                code="backup_decrypt_failed",
                cause="The backup could not be authenticated and decrypted.",
                remediation=(
                    "Provide the matching FS_BACKUP_ENCRYPTION_KEY or create a new backup "
                    "if the artifact is damaged."
                ),
                step="decrypt",
                retryable=False,
            ) from exc

    def inspect_dump(self, source: Path, *, log: BackupLog) -> None:
        inspect_postgres_dump(self, source, log=log)

    def restore_database(
        self,
        source: Path,
        *,
        log: BackupLog,
        maintenance_confirmed: bool,
        verified: bool,
        recovery_target: str,
        expected_recovery_target: str,
    ) -> None:
        """Restore one already decrypted and inspected dump through the configured transport."""
        restore_postgres_database(
            self,
            source,
            log=log,
            maintenance_confirmed=maintenance_confirmed,
            verified=verified,
            recovery_target=recovery_target,
            expected_recovery_target=expected_recovery_target,
        )

    def restore_clone_dump(self, source: Path, *, recovery_target: str) -> None:
        """Confine, inspect, and restore a plaintext dump authenticated by Clone."""
        if not recovery_target or recovery_target != self._recovery_target:
            raise ValueError("recovery target does not match this installation")
        selected = Path(source)
        if selected.is_symlink() or not selected.is_file():
            raise ValueError("Clone database dump must be a real file")
        staging_directory = self._new_staging_directory()
        confined = self._secure_temp(
            ".clone-restore-database-",
            _PATH_DUMP,
            directory=staging_directory,
        )
        log = self.open_log("clone-database-restore")
        try:
            shutil.copyfile(selected, confined)
            os.chmod(confined, 0o600)
            self.inspect_dump(confined, log=log)
            self.restore_database(
                confined,
                log=log,
                maintenance_confirmed=True,
                verified=True,
                recovery_target=recovery_target,
                expected_recovery_target=self._recovery_target,
            )
        finally:
            self.remove_plaintext(confined)
            log.close()

    def publish_backup(self, workspace: BackupWorkspace) -> BackupArtifactSnapshot:
        self._require_confined_temp(workspace.encrypted_path)
        self._require_confined_destination(workspace.final_path)
        if workspace.final_path.exists() or workspace.final_path.is_symlink():
            raise BackupGatewayError(
                code="backup_publish_conflict",
                cause="The generated backup output path already exists.",
                remediation="Retry to generate a new backup name.",
                step="publish",
            )
        publish_temp: Path | None = None
        try:
            publish_temp = self._secure_temp(".backup-publish-", _PATH_DUMP_ENC)
            with workspace.encrypted_path.open("rb") as source, publish_temp.open("wb") as target:
                os.fchmod(target.fileno(), 0o600)
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(publish_temp, workspace.final_path)
            workspace.final_path.chmod(0o600)
            self._fsync_directory(self._backups_root)
            return self._record_verification(
                workspace.final_path,
                created_at=self._utc_now(),
            )
        except BackupGatewayError:
            raise
        except OSError as exc:
            raise BackupGatewayError(
                code="backup_publish_failed",
                cause="The verified backup could not be published atomically.",
                remediation="Check backup storage permissions and available disk space.",
                step="publish",
            ) from exc
        finally:
            if publish_temp is not None:
                self.remove_file(publish_temp)

    def list_backups(self, *, read_only: bool = False) -> tuple[BackupArtifactSnapshot, ...]:
        if read_only:
            if self._backups_root.is_symlink():
                raise OSError("backup support directory must not use symbolic links")
            try:
                self._backups_root.stat()
            except FileNotFoundError:
                return ()
            if not self._backups_root.is_dir():
                raise NotADirectoryError("backup support path is not a directory")
            if self._backups_root.resolve(strict=True) != self._backups_root:
                raise OSError(_BACKUP_SUPPORT_ESCAPE_MESSAGE)
            # iterdir propagates permission errors instead of reporting an empty store.
            paths = sorted(
                (path for path in self._backups_root.iterdir() if path.match("*.dump.enc")),
                reverse=True,
            )
        else:
            self._ensure_directory(self._backups_root)
            paths = sorted(self._backups_root.glob("*.dump.enc"), reverse=True)
        snapshots: list[BackupArtifactSnapshot] = []
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size <= 0:
                continue
            metadata = self._read_matching_metadata(path, stat)
            created_at = (
                _parse_datetime(metadata.get("created_at"))
                if metadata is not None
                else datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            )
            verified_at = (
                _parse_datetime(metadata.get("verified_at")) if metadata is not None else None
            )
            snapshots.append(
                BackupArtifactSnapshot(
                    path=self._display_path(path),
                    size_bytes=stat.st_size,
                    created_at=created_at,
                    verification_status=(
                        BackupVerificationStatus.VERIFIED
                        if metadata is not None and verified_at is not None
                        else BackupVerificationStatus.UNVERIFIED
                    ),
                    verified_at=verified_at,
                )
            )
        return tuple(snapshots)

    def resolve_backup(self, raw_path: str) -> Path:
        self._ensure_directory(self._backups_root)
        raw = str(raw_path).strip()
        if not raw or "\x00" in raw:
            raise self._invalid_path()
        requested = Path(raw)
        if requested.is_absolute():
            candidate = requested
        elif requested.parts and requested.parts[0] == "backups":
            candidate = self._project_root / requested
        else:
            candidate = self._backups_root / requested
        lexical = Path(os.path.abspath(candidate))
        try:
            relative = lexical.relative_to(self._backups_root)
        except ValueError as exc:
            raise self._invalid_path() from exc
        if not relative.parts or lexical.name.endswith(".metadata.json"):
            raise self._invalid_path()
        current = self._backups_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise self._invalid_path()
        try:
            resolved = lexical.resolve(strict=True)
            resolved.relative_to(self._backups_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise self._invalid_path() from exc
        if (
            resolved.is_symlink()
            or not resolved.is_file()
            or not resolved.name.endswith(_PATH_DUMP_ENC)
        ):
            raise self._invalid_path()
        return resolved

    def record_verification(self, source: Path) -> BackupArtifactSnapshot:
        return self._record_verification(source)

    def _record_verification(
        self,
        source: Path,
        *,
        created_at: datetime | None = None,
    ) -> BackupArtifactSnapshot:
        resolved = self.resolve_backup(str(source))
        stat = resolved.stat()
        if stat.st_size <= 0:
            raise BackupGatewayError(
                code="backup_envelope_invalid",
                cause="The backup artifact is empty.",
                remediation="Create a new backup.",
                step="metadata",
                retryable=False,
            )
        now = self._utc_now()
        existing_metadata = self._read_matching_metadata(resolved, stat)
        artifact_created_at = (
            _parse_datetime(existing_metadata["created_at"])
            if existing_metadata is not None
            else created_at or datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        )
        metadata = {
            "schema_version": _METADATA_SCHEMA_VERSION,
            "path": resolved.name,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "created_at": artifact_created_at.isoformat(),
            "verified_at": now.isoformat(),
            "verification_status": BackupVerificationStatus.VERIFIED.value,
        }
        self._write_metadata(resolved, metadata)
        return BackupArtifactSnapshot(
            path=self._display_path(resolved),
            size_bytes=stat.st_size,
            created_at=artifact_created_at,
            verification_status=BackupVerificationStatus.VERIFIED,
            verified_at=now,
        )

    def remove_file(self, path: Path) -> None:
        candidate = Path(os.path.abspath(path))
        if not self._is_backup_path(candidate) and not self._is_staging_path(candidate):
            raise self._artifact_cleanup_failure()
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            raise self._artifact_cleanup_failure() from exc
        if candidate.exists() or candidate.is_symlink():
            raise self._artifact_cleanup_failure()
        self._remove_empty_staging_directory(candidate.parent)

    def remove_plaintext(self, path: Path) -> None:
        candidate = Path(os.path.abspath(path))
        if not self._is_staging_path(candidate):
            raise self._plaintext_cleanup_failure()
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            raise self._plaintext_cleanup_failure() from exc
        if candidate.exists() or candidate.is_symlink():
            raise self._plaintext_cleanup_failure()
        self._remove_empty_staging_directory(candidate.parent)

    def remove_incomplete_output(self, workspace: BackupWorkspace) -> None:
        first_failure: BackupGatewayError | None = None
        for path in (
            workspace.dump_path,
            workspace.validation_path,
            workspace.encrypted_path,
            workspace.final_path,
            self._metadata_path(workspace.final_path),
        ):
            try:
                self.remove_file(path)
            except BackupGatewayError as exc:
                first_failure = first_failure or exc
        if first_failure is not None:
            raise first_failure

    def _dedicated_key(self) -> str | bytes:
        try:
            value = self._key_source()
        except Exception as exc:
            raise BackupGatewayError(
                code="backup_key_unavailable",
                cause="The dedicated backup encryption key is missing or unreadable.",
                remediation="Set FS_BACKUP_ENCRYPTION_KEY to the durable key for this installation.",
                step="key",
                retryable=False,
            ) from exc
        present = bool(value.strip()) if isinstance(value, bytes) else bool(str(value).strip())
        if not present:
            raise BackupGatewayError(
                code="backup_key_unavailable",
                cause="The dedicated backup encryption key is missing or unreadable.",
                remediation="Set FS_BACKUP_ENCRYPTION_KEY to the durable key for this installation.",
                step="key",
                retryable=False,
            )
        return value

    def _compose_prefix(self) -> tuple[str, ...]:
        return (*self._compose_command, "-p", self._compose_project)

    def _database_connection_args(self) -> tuple[str, ...]:
        return (*self._database_server_args(), "-d", self._database_name)

    def _database_server_args(self) -> tuple[str, ...]:
        if not 1 <= self._database_port <= 65535:
            raise ValueError("database port must be between 1 and 65535")
        if not all((self._database_host, self._database_user, self._database_name)):
            raise ValueError("native database connection fields must be non-empty")
        return (
            "-h",
            self._database_host,
            "-p",
            str(self._database_port),
            "-U",
            self._database_user,
        )

    def _native_process_environment(self) -> dict[str, object]:
        if not self._native_database:
            return {}
        environment = dict(os.environ)
        environment.update(self._libpq_environment)
        if self._database_password:
            environment["PGPASSWORD"] = self._database_password
        return {"env": environment}

    def _secure_temp(
        self,
        prefix: str,
        suffix: str,
        *,
        directory: Path | None = None,
    ) -> Path:
        target_directory = directory or self._backups_root
        if directory is None:
            self._ensure_directory(target_directory)
        elif directory not in self._staging_directories:
            raise OSError("backup staging directory is not owned by this gateway")
        fd, raw_path = tempfile.mkstemp(
            dir=target_directory,
            prefix=prefix,
            suffix=suffix,
        )
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        path = Path(raw_path)
        if directory is not None:
            self._allocated_staging_paths.add(path)
        return path

    def _require_confined_temp(self, path: Path) -> None:
        self._require_confined_file(path)
        if not self._is_staging_path(Path(os.path.abspath(path))):
            raise self._invalid_path()

    def _require_confined_file(self, path: Path) -> None:
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.is_file():
            raise self._invalid_path()
        resolved = candidate.resolve(strict=True)
        if not self._is_backup_path(resolved) and not self._is_staging_path(resolved):
            raise self._invalid_path()

    def _require_confined_destination(self, path: Path) -> None:
        lexical = Path(os.path.abspath(path))
        try:
            relative = lexical.relative_to(self._backups_root)
        except ValueError as exc:
            raise self._invalid_path() from exc
        if len(relative.parts) != 1:
            raise self._invalid_path()

    def _read_matching_metadata(
        self,
        backup: Path,
        stat: os.stat_result,
    ) -> dict[str, Any] | None:
        metadata_path = self._metadata_path(backup)
        if metadata_path.is_symlink() or not metadata_path.is_file():
            return None
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        if (
            value.get("schema_version") != _METADATA_SCHEMA_VERSION
            or value.get("path") != backup.name
            or value.get("size_bytes") != stat.st_size
            or value.get("mtime_ns") != stat.st_mtime_ns
            or value.get("verification_status") != BackupVerificationStatus.VERIFIED.value
        ):
            return None
        try:
            _parse_datetime(value.get("created_at"))
            _parse_datetime(value.get("verified_at"))
        except (TypeError, ValueError):
            return None
        return value

    def _write_metadata(self, backup: Path, metadata: dict[str, object]) -> None:
        metadata_path = self._metadata_path(backup)
        temp_path = self._secure_temp(".backup-metadata-", ".json")
        try:
            payload = json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            self._write_owner_only(temp_path, payload.encode())
            os.replace(temp_path, metadata_path)
            metadata_path.chmod(0o600)
            self._fsync_directory(self._backups_root)
        finally:
            self.remove_file(temp_path)

    def _write_owner_only(self, path: Path, payload: bytes) -> None:
        flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def _display_path(self, path: Path) -> Path:
        return path.relative_to(self._project_root)

    def _metadata_path(self, backup: Path) -> Path:
        return backup.with_name(f"{backup.name}.metadata.json")

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _ensure_directory(self, path: Path) -> None:
        try:
            relative = path.relative_to(self._project_root)
        except ValueError as exc:
            raise OSError(_BACKUP_SUPPORT_ESCAPE_MESSAGE) from exc
        current = self._project_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise OSError("backup support directory must not use symbolic links")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.resolve(strict=True).relative_to(self._project_root)
        except (FileNotFoundError, ValueError) as exc:
            raise OSError(_BACKUP_SUPPORT_ESCAPE_MESSAGE) from exc
        path.chmod(0o700)

    def _new_staging_directory(self) -> Path:
        if self._staging_root.is_symlink():
            raise OSError("backup staging root must not be a symbolic link")
        self._staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._staging_root.chmod(0o700)
        if self._staging_root.stat().st_uid != os.getuid():
            raise OSError("backup staging root must be owned by the current user")
        raw_path = tempfile.mkdtemp(prefix="workspace-", dir=self._staging_root)
        directory = Path(raw_path).resolve(strict=True)
        directory.chmod(0o700)
        self._staging_directories.add(directory)
        return directory

    @staticmethod
    def _ensure_external_root(path: Path, *, label: str) -> None:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            if current.is_symlink():
                raise OSError(f"{label} root must not use symbolic links")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.resolve(strict=True) != path:
            raise OSError(f"{label} root resolves outside its configured path")
        path.chmod(0o700)
        if path.stat().st_uid != os.getuid():
            raise OSError(f"{label} root must be owned by the current user")

    def _is_backup_path(self, path: Path) -> bool:
        try:
            path.relative_to(self._backups_root)
        except ValueError:
            return False
        return True

    def _is_staging_path(self, path: Path) -> bool:
        return path in self._allocated_staging_paths or any(
            path == directory or directory in path.parents
            for directory in self._staging_directories
        )

    def _remove_empty_staging_directory(self, directory: Path) -> None:
        if directory not in self._staging_directories:
            return
        try:
            directory.rmdir()
        except OSError:
            return
        self._staging_directories.discard(directory)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _invalid_path() -> BackupGatewayError:
        return BackupGatewayError(
            code="backup_path_invalid",
            cause="The backup path is missing, outside backups/, or uses a symbolic link.",
            remediation="Select a regular protected backup file under backups/.",
            step="resolve",
            retryable=False,
        )

    @staticmethod
    def _plaintext_cleanup_failure() -> BackupGatewayError:
        return BackupGatewayError(
            code="backup_plaintext_cleanup_failed",
            cause="Temporary plaintext backup material could not be removed.",
            remediation="Remove owner-only backup temp files under backups/ before retrying.",
            step="cleanup",
            retryable=False,
        )

    @staticmethod
    def _artifact_cleanup_failure() -> BackupGatewayError:
        return BackupGatewayError(
            code="backup_cleanup_failed",
            cause="Temporary or incomplete backup artifacts could not be removed.",
            remediation="Remove the reported owner-only backup artifact before retrying.",
            step="cleanup",
            retryable=False,
        )


def _nonempty_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink() and path.stat().st_size > 0


def _parse_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = ["FilesystemBackupGateway"]
