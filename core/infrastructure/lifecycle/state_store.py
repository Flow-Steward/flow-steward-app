"""Owner-only lifecycle journals, snapshots, logs, and mutation lock."""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from core.application.installer.redaction import redact_installer_text
from core.application.lifecycle.contracts import (
    LifecycleLockDiagnosis,
    LifecycleLockStatus,
    LifecycleOperationJournal,
    LifecycleOperationStatus,
)
from core.application.lifecycle.errors import LifecycleError
from core.infrastructure.installer.state_store import (
    INSTALLER_LOCK_KIND,
    _assert_secret_free,
    _atomic_write_text,
)

_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RELEASE_PREPARED_MARKER_PREFIX = "release-prepared-"


class SecureLifecycleLog:
    """Append-only redacted log with an owner-only file mode."""

    def __init__(self, path: Path) -> None:
        self.path = path
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self._fd = os.open(path, flags, 0o600)
        os.fchmod(self._fd, 0o600)

    def write(self, message: str) -> None:
        line = f"{datetime.now(UTC).isoformat()} {redact_installer_text(message)}\n"
        os.write(self._fd, line.encode("utf-8", errors="replace"))

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


class LifecycleStateStore:
    """Persist lifecycle operation state without trusting process umask."""

    def __init__(self, project_root: Path, *, installation_lock_path: Path | None = None) -> None:
        root = Path(project_root)
        self.state_root = root if root.name == ".flow-steward" else root / ".flow-steward"
        self.operations_dir = self.state_root / "operations"
        self.logs_dir = self.operations_dir / "logs"
        self.lock_path = self.operations_dir / "active.lock"
        # AC17-R162: where ./install.sh holds the lock, when that is not lock_path. On
        # Compact the operations live in the data volume, and the installer cannot see
        # them; this is the host's copy, mounted read-only, so it is only locked.
        self.installation_lock_path = installation_lock_path
        self._journal_lock = RLock()
        self._held_lock_descriptors: dict[str, int] = {}

    def journal_path(self, operation_id: str) -> Path:
        return self.operations_dir / f"{_validated_operation_id(operation_id)}.json"

    def previous_config_path(self, operation_id: str) -> Path:
        safe_id = _validated_operation_id(operation_id)
        return self.operations_dir / f"{safe_id}.previous-config.json"

    def load_journal(self, operation_id: str) -> LifecycleOperationJournal | None:
        with self._journal_lock:
            path = self.journal_path(operation_id)
            if not path.exists():
                return None
            return LifecycleOperationJournal.model_validate_json(path.read_text(encoding="utf-8"))

    def save_journal(self, journal: LifecycleOperationJournal) -> None:
        with self._journal_lock:
            self._write_json(
                self.journal_path(journal.operation_id), journal.model_dump(mode="json")
            )

    def save_previous_config(self, operation_id: str, snapshot: Mapping[str, object]) -> Path:
        path = self.previous_config_path(operation_id)
        self._write_json(path, dict(snapshot))
        return path

    def load_previous_config(self, operation_id: str) -> dict[str, Any] | None:
        with self._journal_lock:
            path = self.previous_config_path(operation_id)
            if not path.exists():
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Lifecycle previous state must be a JSON object")
            _assert_secret_free(value)
            return value

    def unfinished_journals(self) -> tuple[LifecycleOperationJournal, ...]:
        """Return durable mutations that must be resumed or reconciled before new work."""
        with self._journal_lock:
            if not self.operations_dir.exists():
                return ()
            journals: list[LifecycleOperationJournal] = []
            for path in self.operations_dir.glob("*.json"):
                if not _SAFE_OPERATION_ID.fullmatch(path.stem):
                    continue
                try:
                    raw = path.read_text(encoding="utf-8")
                    if _is_release_prepared_marker(path, raw):
                        continue
                    journal = LifecycleOperationJournal.model_validate_json(raw)
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        f"Lifecycle operation journal {path.name!r} is unreadable"
                    ) from exc
                if journal.status in {
                    LifecycleOperationStatus.RUNNING,
                    LifecycleOperationStatus.CANCELLED,
                    LifecycleOperationStatus.RECOVERY_REQUIRED,
                } or (journal.status is LifecycleOperationStatus.FAILED and journal.retryable):
                    journals.append(journal)
            return tuple(sorted(journals, key=lambda item: item.updated_at, reverse=True))

    def open_log(self, operation_id: str) -> SecureLifecycleLog:
        safe_id = _validated_operation_id(operation_id)
        self._ensure_directory(self.operations_dir)
        self._ensure_directory(self.logs_dir)
        return SecureLifecycleLog(self.logs_dir / f"{safe_id}-{uuid4().hex}.log")

    def mark_recovery_required(
        self,
        operation_id: str,
        *,
        step: str,
        error_code: str,
        initial_journal: LifecycleOperationJournal | None = None,
    ) -> None:
        with self._journal_lock:
            journal = self.load_journal(operation_id) or initial_journal
            if journal is None or journal.status is not LifecycleOperationStatus.RUNNING:
                return
            self.save_journal(
                journal.model_copy(
                    update={
                        "status": LifecycleOperationStatus.RECOVERY_REQUIRED,
                        "failed_step": step or journal.current_step,
                        "last_error_code": error_code,
                        "retryable": False,
                        "updated_at": datetime.now(UTC),
                    }
                )
            )

    def diagnose_active_lock(self) -> LifecycleLockDiagnosis:
        if not self.lock_path.exists():
            return LifecycleLockDiagnosis(
                status=LifecycleLockStatus.CLEAR,
                detail="No lifecycle mutation lock exists.",
            )
        fd = self._open_lock_file()
        try:
            metadata = _read_lock_metadata(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return _lock_diagnosis(LifecycleLockStatus.ACTIVE, metadata)
            try:
                status = LifecycleLockStatus.STALE if metadata else LifecycleLockStatus.CLEAR
                return _lock_diagnosis(status, metadata)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @contextmanager
    def operation_lock(self, operation_id: str, *, mutating: bool) -> Iterator[None]:
        """Serialize mutations while allowing read-only calls to continue."""
        if not mutating:
            yield
            return
        safe_id = _validated_operation_id(operation_id)
        self._ensure_directory(self.operations_dir)
        fd = self._open_lock_file()
        installation_fd = -1
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise self._conflict(_read_lock_metadata(fd)) from exc
            _write_lock_metadata(
                fd,
                {
                    "operation_id": safe_id,
                    "pid": os.getpid(),
                    "acquired_at": datetime.now(UTC).isoformat(),
                },
            )
            installation_fd = self._lock_installation()
            self._held_lock_descriptors = {
                os.path.realpath(self.lock_path): fd,
                **(
                    {os.path.realpath(self.installation_lock_path): installation_fd}
                    if installation_fd >= 0 and self.installation_lock_path is not None
                    else {}
                ),
            }
            yield
        finally:
            try:
                self._held_lock_descriptors = {}
                if installation_fd >= 0:
                    fcntl.flock(installation_fd, fcntl.LOCK_UN)
                    os.close(installation_fd)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def held_lock_descriptors(self) -> dict[str, int]:
        """Return verified descriptors owned by the active operation in this process."""
        verified: dict[str, int] = {}
        for path, descriptor in self._held_lock_descriptors.items():
            path_stat = os.stat(path)
            descriptor_stat = os.fstat(descriptor)
            if (path_stat.st_dev, path_stat.st_ino) != (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            ):
                raise RuntimeError("Lifecycle lock descriptor no longer matches its path")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            verified[path] = descriptor
        return verified

    def _lock_installation(self) -> int:
        """Also hold the host lock the installer takes; -1 when there is none to hold."""
        path = self.installation_lock_path
        if path is None:
            return -1
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            installation_fd = os.open(path, flags)
        except FileNotFoundError:
            return -1  # no installer of this release has run here
        try:
            fcntl.flock(installation_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            metadata = _read_lock_metadata(installation_fd)
            os.close(installation_fd)
            raise self._conflict(metadata) from exc
        return installation_fd

    def _conflict(self, metadata: Mapping[str, object]) -> LifecycleError:
        """Name what holds the lock: an operation with its step, or the installer."""
        active_id = _metadata_str(metadata, "operation_id")
        if metadata.get("kind") == INSTALLER_LOCK_KIND:
            return LifecycleError.conflict(
                operation_id=active_id,
                cause=(
                    f"./install.sh is running (operation {active_id or 'unknown'}, pid "
                    f"{metadata.get('pid') or 'unknown'}); rerun this once it has finished."
                ),
            )
        if not active_id:
            return LifecycleError.conflict(
                operation_id=None, cause="Another lifecycle operation owns the mutation lock."
            )
        try:
            journal = self.load_journal(active_id)
        except (OSError, ValueError):
            journal = None
        if journal is None:
            detail = f"Lifecycle operation {active_id} owns the mutation lock."
        else:
            kind = str(getattr(journal.kind, "value", journal.kind)).replace("_", " ")
            step = journal.current_step or "unknown"
            detail = f"Operation {active_id} is running: {kind}, step {step}."
        return LifecycleError.conflict(operation_id=active_id, cause=detail)

    def _write_json(self, path: Path, value: Mapping[str, object]) -> None:
        _assert_secret_free(value)
        self._ensure_directory(self.operations_dir)
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_write_text(path, text, mode=0o600)

    def _open_lock_file(self) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.lock_path, flags, 0o600)
        os.fchmod(fd, 0o600)
        return fd

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)


def _validated_operation_id(operation_id: str) -> str:
    value = str(operation_id)
    if not _SAFE_OPERATION_ID.fullmatch(value):
        raise ValueError("Lifecycle operation id must contain only safe identifier characters")
    return value


def _is_release_prepared_marker(path: Path, raw: str) -> bool:
    if not path.stem.startswith(_RELEASE_PREPARED_MARKER_PREFIX):
        return False
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "project_name",
        "environment",
        "release",
        "host_port_overrides",
    }:
        return False
    project_name = value.get("project_name")
    return (
        value.get("schema_version") == 2
        and isinstance(project_name, str)
        and bool(project_name)
        and path.stem == f"{_RELEASE_PREPARED_MARKER_PREFIX}{project_name}"
        and value.get("environment") in {"development", "production"}
        and isinstance(value.get("release"), dict)
        and isinstance(value.get("host_port_overrides"), dict)
    )


def _read_lock_metadata(fd: int) -> dict[str, Any]:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 16_384).decode("utf-8", errors="replace").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"invalid": True}
    return value if isinstance(value, dict) else {"invalid": True}


def _write_lock_metadata(fd: int, value: Mapping[str, object]) -> None:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)
    os.fsync(fd)


def _metadata_str(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    return str(value) if value not in (None, "") else None


def _lock_diagnosis(
    status: LifecycleLockStatus, metadata: Mapping[str, object]
) -> LifecycleLockDiagnosis:
    operation_id = _metadata_str(metadata, "operation_id")
    raw_pid = metadata.get("pid")
    pid = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
    acquired_at = _metadata_str(metadata, "acquired_at")
    if status is LifecycleLockStatus.ACTIVE:
        detail = "A lifecycle mutation currently owns the advisory lock."
    elif status is LifecycleLockStatus.STALE:
        detail = "Stale lifecycle lock metadata was found without an active advisory lock."
    else:
        detail = "The lifecycle mutation lock is available."
    return LifecycleLockDiagnosis(
        status=status,
        operation_id=operation_id,
        pid=pid,
        acquired_at=acquired_at,
        detail=detail,
    )
