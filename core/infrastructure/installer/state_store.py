"""Owner-only installer journals, logs, and durable env-secret updates."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import json
import os
import shlex
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from core.application.installer.contracts import InstallJournal
from core.application.installer.errors import InstallerFailure
from core.application.installer.redaction import redact_installer_text
from core.application.release.activation import ReleaseHandoffJournal
from core.application.release.contracts import ReleaseIdentity
from core.application.release.service import ReleaseTransitionBinding
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.installer.secret_values import is_missing_or_placeholder_secret

_SENSITIVE_KEY_PARTS = ("password", "secret", "token")


# The `kind` the installer writes into the lifecycle lock it holds; the lifecycle side
# reads it to say that ./install.sh, not an operation, is what holds the lock.
INSTALLER_LOCK_KIND = "install"


class SecureInstallerLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def write(self, message: str) -> None:
        line = f"{datetime.now(UTC).isoformat()} {redact_installer_text(message)}\n"
        os.write(self._fd, line.encode("utf-8", errors="replace"))

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


class InstallerStateStore:
    """Persist installer state without trusting process umask defaults."""

    def __init__(self, root: Path, *, lifecycle_state_root: Path | None = None) -> None:
        self.root = root
        self.logs_dir = root / "logs"
        self.journal_path = root / "install-state.json"
        self.installed_release_path = root / "installed-release.json"
        self.pending_release_path = root / "pending-release.json"
        self.release_handoff_path = root / "release-handoff.json"
        self.release_transition_binding_path = root / "release-transition-binding.json"
        self.release_handoff_secret_path = root / "release-handoff-secret.txt"
        self.lock_path = root / "install.lock"
        # AC17-R162: the lifecycle lock `config`, `stack`, `maintenance`, `backup` and
        # `database` take (core/infrastructure/lifecycle/state_store.py). The installer
        # holds it for its whole run, so neither side starts while the other works.
        self.lifecycle_lock_path = root / "operations" / "active.lock"
        # Scalable's fs-cli takes it under <checkout>/.flow-steward, Compact's through the
        # mount of this state directory; they are one file unless FS_INSTALLER_STATE_DIR
        # moved the state elsewhere, and then both are held.
        self.lifecycle_lock_paths = tuple(
            dict.fromkeys(
                (
                    self.lifecycle_lock_path,
                    *(
                        (Path(lifecycle_state_root) / "operations" / "active.lock",)
                        if lifecycle_state_root is not None
                        else ()
                    ),
                )
            )
        )

    def load_journal(self) -> InstallJournal | None:
        if not self.journal_path.exists():
            return None
        return InstallJournal.model_validate_json(self.journal_path.read_text(encoding="utf-8"))

    def save_journal(self, journal: InstallJournal) -> None:
        self._write_json(self.journal_path, journal.model_dump(mode="json"))

    def clear_journal(self) -> None:
        try:
            self.journal_path.unlink()
        except FileNotFoundError:
            return

    def load_installed_release(self) -> ReleaseIdentity | None:
        return self._load_release(self.installed_release_path)

    def load_pending_release(self) -> ReleaseIdentity | None:
        return self._load_release(self.pending_release_path)

    def stage_pending_release(self, release: ReleaseIdentity) -> None:
        self._write_json(self.pending_release_path, release.model_dump(mode="json"))

    def publish_installed_release(self, release: ReleaseIdentity) -> None:
        """Atomically publish the complete identity, then clear its pending marker."""
        self._write_json(self.installed_release_path, release.model_dump(mode="json"))
        self.clear_pending_release(release)

    def clear_pending_release(self, expected: ReleaseIdentity) -> bool:
        """Clear only the expected marker so recovery cannot discard another target."""
        if self.load_pending_release() != expected:
            return False
        with suppress(FileNotFoundError):
            self.pending_release_path.unlink()
        return True

    def load_release_handoff(self) -> ReleaseHandoffJournal | None:
        if not self.release_handoff_path.exists():
            return None
        return ReleaseHandoffJournal.model_validate_json(
            self.release_handoff_path.read_text(encoding="utf-8")
        )

    def save_release_handoff(self, journal: ReleaseHandoffJournal) -> None:
        self._write_json(self.release_handoff_path, journal.model_dump(mode="json"))

    def clear_release_handoff(self) -> None:
        with suppress(FileNotFoundError):
            self.release_handoff_path.unlink()

    def load_release_transition_binding(self) -> ReleaseTransitionBinding | None:
        if not self.release_transition_binding_path.exists():
            return None
        return ReleaseTransitionBinding.model_validate_json(
            self.release_transition_binding_path.read_text(encoding="utf-8")
        )

    def save_release_transition_binding(self, binding: ReleaseTransitionBinding) -> None:
        self._write_json(
            self.release_transition_binding_path,
            binding.model_dump(mode="json"),
        )

    def clear_release_transition_binding(self, expected: ReleaseTransitionBinding) -> bool:
        if self.load_release_transition_binding() != expected:
            return False
        with suppress(FileNotFoundError):
            self.release_transition_binding_path.unlink()
        return True

    def save_release_handoff_secret(self, value: str) -> None:
        self.write_owner_only_text(self.release_handoff_secret_path, value.rstrip("\n") + "\n")

    def take_release_handoff_secret(self) -> str:
        try:
            value = self.release_handoff_secret_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
        self.release_handoff_secret_path.unlink()
        return value

    def clear_release_handoff_secret(self) -> None:
        with suppress(FileNotFoundError):
            self.release_handoff_secret_path.unlink()

    @staticmethod
    def _load_release(path: Path) -> ReleaseIdentity | None:
        if not path.exists():
            return None
        return ReleaseIdentity.model_validate_json(path.read_text(encoding="utf-8"))

    def open_log(self, operation_id: str) -> SecureInstallerLog:
        self._ensure_directory(self.logs_dir)
        safe_id = "".join(char for char in operation_id if char.isalnum() or char in "-_")
        if not safe_id:
            safe_id = uuid4().hex
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return SecureInstallerLog(self.logs_dir / f"install-{stamp}-{safe_id}.log")

    @contextmanager
    def operation_lock(self, *, held_locks: Mapping[str, int] | None = None):
        if held_locks is not None:
            with self._operation_lock_reusing(held_locks) as combined:
                yield combined
            return
        if self._uses_verified_handoff_locks():
            yield self._verified_handoff_lock_descriptors()
            return
        self._ensure_directory(self.root)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.lock_path, flags, 0o600)
        os.fchmod(fd, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallerFailure(
                    code="installer_operation_active",
                    cause="Another Flow Steward installer operation is already active.",
                    remediation="Wait for the active installer to finish, then rerun ./install.sh.",
                    exit_code=6,
                    retryable=True,
                    step="lock",
                ) from exc
            with self._lifecycle_lock():
                held = {os.path.realpath(self.lock_path): fd}
                held.update(
                    {
                        os.path.realpath(path): lifecycle_fd
                        for path, lifecycle_fd in zip(
                            self.lifecycle_lock_paths,
                            self._lifecycle_path_fds,
                            strict=True,
                        )
                    }
                )
                yield held
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @contextmanager
    def _operation_lock_reusing(self, held_locks: Mapping[str, int]):
        """Verify caller-held locks and acquire only missing installer-owned locks."""
        expected_paths = {
            os.path.realpath(self.lock_path),
            *(os.path.realpath(path) for path in self.lifecycle_lock_paths),
        }
        combined: dict[str, int] = {}
        held_inodes: dict[tuple[int, int], int] = {}
        for raw_path, raw_descriptor in held_locks.items():
            path = os.path.realpath(raw_path)
            descriptor = int(raw_descriptor)
            if path not in expected_paths:
                raise ValueError("Installer received an unrelated held lock")
            path_stat = os.stat(path)
            descriptor_stat = os.fstat(descriptor)
            inode = descriptor_stat.st_dev, descriptor_stat.st_ino
            if inode != (path_stat.st_dev, path_stat.st_ino):
                raise ValueError("Installer held lock descriptor does not match its path")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            combined[path] = descriptor
            held_inodes[inode] = descriptor

        acquired: list[int] = []
        try:
            for path in (self.lock_path, *self.lifecycle_lock_paths):
                resolved = os.path.realpath(path)
                if resolved in combined:
                    continue
                try:
                    metadata = os.stat(path)
                    reused = held_inodes.get((metadata.st_dev, metadata.st_ino))
                except FileNotFoundError:
                    reused = None
                if reused is not None:
                    combined[resolved] = reused
                    continue
                if path == self.lock_path:
                    self._ensure_directory(self.root)
                    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    descriptor = os.open(path, flags, 0o600)
                    os.fchmod(descriptor, 0o600)
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        os.close(descriptor)
                        raise InstallerFailure(
                            code="installer_operation_active",
                            cause="Another Flow Steward installer operation is already active.",
                            remediation=(
                                "Wait for the active installer to finish, then rerun ./install.sh."
                            ),
                            exit_code=6,
                            retryable=True,
                            step="lock",
                        ) from exc
                else:
                    descriptor = self._hold_lifecycle_lock(path)
                acquired.append(descriptor)
                descriptor_stat = os.fstat(descriptor)
                held_inodes[(descriptor_stat.st_dev, descriptor_stat.st_ino)] = descriptor
                combined[resolved] = descriptor
            yield combined
        finally:
            for descriptor in acquired:
                _release_fd(descriptor)

    def has_active_operation(self) -> bool:
        """Return whether another process currently owns the installer lock."""
        if not self.lock_path.exists():
            return False
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)

    def _uses_verified_handoff_locks(self) -> bool:
        if _runtime_env.get("FS_RELEASE_HANDOFF_LOCK_PID") != str(os.getpid()):
            return False
        try:
            observed = json.loads(_runtime_env["FS_RELEASE_HANDOFF_LOCKS"])
        except (KeyError, TypeError, ValueError):
            return False
        if not isinstance(observed, dict):
            return False
        expected = {
            os.path.realpath(self.lock_path),
            *(os.path.realpath(path) for path in self.lifecycle_lock_paths),
        }
        if set(observed) != expected:
            return False
        try:
            for path, raw_fd in observed.items():
                fd = int(raw_fd)
                file_stat = os.stat(path)
                descriptor_stat = os.fstat(fd)
                if (file_stat.st_dev, file_stat.st_ino) != (
                    descriptor_stat.st_dev,
                    descriptor_stat.st_ino,
                ):
                    return False
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, TypeError, ValueError):
            return False
        return True

    def _verified_handoff_lock_descriptors(self) -> dict[str, int]:
        observed = json.loads(_runtime_env["FS_RELEASE_HANDOFF_LOCKS"])
        return {str(path): int(fd) for path, fd in observed.items()}

    @contextmanager
    def _lifecycle_lock(self):
        self._acquire_lifecycle_locks()
        try:
            yield
        finally:
            self._release_lifecycle_locks()

    @contextmanager
    def lifecycle_lock_lent(self):
        """Lend the lifecycle lock to an operation this run starts, and take it back.

        Item 5, step 4: a profile switch runs `config apply` in fs-cli, which takes the
        lifecycle lock and journals its own operation. install.lock and the host record
        stay held meanwhile, so ./flow-steward refuses everything else; and if this run
        dies, the operation keeps its lock until it finishes. Taken back afterwards, and
        refused by name if something else holds it by then.
        """
        self._release_lifecycle_locks()
        try:
            yield
        finally:
            self._acquire_lifecycle_locks()

    def _acquire_lifecycle_locks(self) -> None:
        held: list[int] = []
        path_fds: list[int] = []
        resolved_fds: dict[str, int] = {}
        try:
            for path in self.lifecycle_lock_paths:
                resolved = os.path.realpath(path)
                if resolved in resolved_fds:
                    path_fds.append(resolved_fds[resolved])
                    continue
                try:
                    path_stat = os.stat(path)
                    inode = path_stat.st_dev, path_stat.st_ino
                except FileNotFoundError:
                    inode = None
                reused = next(
                    (
                        fd
                        for fd in held
                        if inode is not None and inode == (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
                    ),
                    None,
                )
                if reused is None:
                    reused = self._hold_lifecycle_lock(path)
                    held.append(reused)
                resolved_fds[resolved] = reused
                path_fds.append(reused)
        except BaseException:
            for fd in held:
                _release_fd(fd)
            raise
        self._lifecycle_fds = held
        self._lifecycle_path_fds = path_fds

    def _release_lifecycle_locks(self) -> None:
        held, self._lifecycle_fds = getattr(self, "_lifecycle_fds", []), []
        self._lifecycle_path_fds = []
        for fd in held:
            _release_fd(fd)

    def _hold_lifecycle_lock(self, lock_path: Path) -> int:
        operations = lock_path.parent
        operations.mkdir(parents=True, exist_ok=True, mode=0o700)
        operations.chmod(0o700)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise self._lifecycle_operation_active(fd, operations) from exc
            payload = {
                "operation_id": f"install-{uuid4().hex[:12]}",
                "kind": INSTALLER_LOCK_KIND,
                "pid": os.getpid(),
                "acquired_at": datetime.now(UTC).isoformat(),
            }
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, (json.dumps(payload, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _lifecycle_operation_active(self, fd: int, operations: Path) -> InstallerFailure:
        """Name the operation that holds the lifecycle lock, with its step."""
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            metadata = json.loads(os.read(fd, 16_384).decode("utf-8", errors="replace") or "{}")
        except ValueError:
            metadata = {}
        if not isinstance(metadata, dict) or metadata.get("kind") == INSTALLER_LOCK_KIND:
            # What an earlier installer run left: install.lock rules out a second one, so
            # the holder is a Compact command, which locks this file read-only.
            metadata = {}
        active_id = str(metadata.get("operation_id") or "")
        journal: dict[str, object] = {}
        if active_id and "/" not in active_id and not active_id.startswith("."):
            try:
                loaded = json.loads((operations / f"{active_id}.json").read_text("utf-8"))
                journal = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                journal = {}
        if journal:
            kind = str(journal.get("kind") or "lifecycle operation").replace("_", " ")
            step = str(journal.get("current_step") or "unknown")
            cause = f"Operation {active_id} is running: {kind}, step {step}."
        elif active_id:
            cause = f"Lifecycle operation {active_id} is running."
        else:
            # Compact: its operations hold this lock read-only from inside the runtime
            # image, so they cannot say which they are here.
            cause = (
                "A lifecycle operation (config, stack, maintenance, backup or database) is running."
            )
        return InstallerFailure(
            code="lifecycle_operation_active",
            cause=cause,
            remediation=(
                "Let it finish -- ./flow-steward status names it -- then rerun ./install.sh. "
                "Nothing was changed."
            ),
            exit_code=6,
            retryable=True,
            step="lock",
        )

    def ensure_env_secrets(
        self,
        env_path: Path,
        names: Sequence[str],
        *,
        secret_factory: Callable[[], str],
    ) -> dict[str, str]:
        values = _read_env(env_path)
        updates: dict[str, str] = {}
        for name in names:
            if is_missing_or_placeholder_secret(values.get(name)):
                generated = secret_factory()
                values[name] = generated
                updates[name] = generated
        if updates or not env_path.exists():
            self.update_env_values(env_path, updates)
        else:
            env_path.chmod(0o600)
        return {name: values[name] for name in names}

    def write_owner_only_text(self, path: Path, text: str) -> None:
        _atomic_write_text(path, text, mode=0o600)

    def write_text(self, path: Path, text: str, *, mode: int) -> None:
        """Atomically write installer-managed non-secret or secret text."""
        if mode not in {0o600, 0o644}:
            raise ValueError("Installer text mode must be 0600 or 0644")
        _atomic_write_text(path, text, mode=mode)

    def write_executable_text(self, path: Path, text: str) -> None:
        _atomic_write_text(path, text, mode=0o700)

    def write_secret_output(self, name: str, value: str) -> Path:
        safe_name = "".join(char for char in name if char.isalnum() or char in "-_")
        if not safe_name:
            raise ValueError("Secret output name must contain a safe character")
        path = self.root / "secret-output" / safe_name
        _atomic_write_text(path, value.rstrip("\n") + "\n", mode=0o600)
        return path

    def write_bootstrap_token_state(self, *, token: str, expires_at: datetime) -> Path:
        path = self.root / "runtime/bootstrap-token-state.json"
        payload = {
            "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "expires_at": expires_at.astimezone(UTC).isoformat(),
        }
        _atomic_write_text(
            path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            mode=0o600,
        )
        return path

    def bootstrap_token_matches_state(self, token: str) -> bool:
        """True when this installation's own state records that exact token.

        AC17-R49: the production env file lives at the repository root, outside every volume
        and outside the state directory, so a token survives `down -v` and `rm -rf
        .flow-steward`. The state file does not. Comparing the two is what distinguishes
        "resuming the install that minted this link" from "a different installation inherited
        someone else's one-time secret".
        """
        if not token:
            return False
        try:
            payload = json.loads(
                (self.root / "runtime/bootstrap-token-state.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        if not isinstance(payload, Mapping):
            return False
        recorded = str(payload.get("token_sha256") or "")
        return bool(recorded) and hmac.compare_digest(
            recorded, hashlib.sha256(token.encode("utf-8")).hexdigest()
        )

    def ensure_runtime_directory(self) -> Path:
        path = self.root / "runtime"
        self._ensure_directory(path)
        return path

    def update_env_values(self, env_path: Path, updates: Mapping[str, str]) -> None:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        pending = dict(updates)
        rendered: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in pending:
                    rendered.append(f"{key}={shlex.quote(str(pending.pop(key)))}")
                    continue
            rendered.append(line)
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.extend(f"{key}={shlex.quote(str(value))}" for key, value in pending.items())
        _atomic_write_text(env_path, "\n".join(rendered).rstrip() + "\n", mode=0o600)

    def read_env_values(self, env_path: Path) -> dict[str, str]:
        return _read_env(env_path)

    def _write_json(self, path: Path, value: Mapping[str, object]) -> None:
        _assert_secret_free(value)
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_write_text(path, text, mode=0o600)

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)


def _assert_secret_free(value: object, *, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                raise ValueError(
                    f"Installer journal cannot contain secret-shaped field: {path}{key}"
                )
            _assert_secret_free(item, path=f"{path}{key}.")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_secret_free(item, path=f"{path}{index}.")


def _atomic_write_text(path: Path, text: str, *, mode: int) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def _atomic_write_bytes(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, mode)
        try:
            os.replace(temp_path, path)
        except OSError as exc:
            if exc.errno not in {errno.EBUSY, errno.EPERM} or not path.exists():
                raise
            # Docker file-level bind mounts cannot be replaced with rename(2):
            # Linux returns EBUSY and Docker Desktop returns EPERM even though
            # the mounted file itself is writable.  The fully rendered and
            # fsynced temp file still keeps validation/construction outside the
            # destination.  Copy it into the existing mount only for those
            # exact kernel failures, retaining owner-only permissions and
            # refusing symlink traversal.
            payload = temp_path.read_bytes()
            flags = os.O_WRONLY | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            target_fd = os.open(path, flags)
            try:
                os.fchmod(target_fd, mode)
                view = memoryview(payload)
                while view:
                    written = os.write(target_fd, view)
                    if written <= 0:
                        raise OSError("failed to persist bind-mounted installer file")
                    view = view[written:]
                os.fsync(target_fd)
            finally:
                os.close(target_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


def _release_fd(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
