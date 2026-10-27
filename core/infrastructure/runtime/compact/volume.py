"""Descriptor-relative preparation of the mounted Compact data volume."""

from __future__ import annotations

import fcntl
import os
import socket
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.extension_runtime.tree_modes import normalize_previous_tree

# Item 5: after a switch from Scalable these trees are Scalable's host directories, bound
# over the volume (infra/docker/compact/data-host_directories.yaml, which says so here).
# Their owner is the Scalable host user; the group and mode are still the model's, which
# the switch gave them (step 3), and they are still checked.
_HOST_BOUND_TREES = frozenset({"artifacts", "extensions"})


def _host_bound_trees() -> frozenset[str]:
    placement = str(_runtime_env.get("FS_COMPACT_DATA_PLACEMENT") or "").strip()
    if placement != "host_directories":
        return frozenset()
    # AC17-R169: in object storage the artifacts are not bound; /data/artifacts is the
    # volume's own, and is checked as such.
    backend = str(_runtime_env.get("ARTIFACT_STORAGE_BACKEND") or "local_fs").strip()
    return _HOST_BOUND_TREES if backend == "local_fs" else _HOST_BOUND_TREES - {"artifacts"}


@dataclass(frozen=True, slots=True)
class CompactVolumeLayout:
    root_uid: int = 0
    root_gid: int = 0
    operator_uid: int = 10007
    runtime_gid: int = 10102
    extension_gid: int = 10103
    artifact_gid: int = 10100
    app_uid: int = 10001
    worker_uid: int = 10002
    control_uid: int = 10003
    mcp_uid: int = 10004
    installer_uid: int = 10005
    router_uid: int = 10006

    @classmethod
    def for_current_user(cls) -> CompactVolumeLayout:
        uid = os.getuid()
        gid = os.getgid()
        return cls(
            root_uid=uid,
            root_gid=gid,
            operator_uid=uid,
            runtime_gid=gid,
            extension_gid=gid,
            artifact_gid=gid,
            app_uid=uid,
            worker_uid=uid,
            control_uid=uid,
            mcp_uid=uid,
            installer_uid=uid,
            router_uid=uid,
        )


@dataclass(frozen=True, slots=True)
class _DirectorySpec:
    relative_path: str
    uid: int
    gid: int
    mode: int
    # AC17-R163: the mode an earlier release created this directory with. Such a tree
    # is normalized to `mode` before anything runs on it, and the directory's own mode
    # is set last, so it is the record that the normalization finished.
    previous_modes: tuple[int, ...] = ()


def _directory_specs(layout: CompactVolumeLayout) -> tuple[_DirectorySpec, ...]:
    return (
        # ADR 0029, decision B: 10103 writes (either profile's installer), everyone
        # else reads through the "other" bits; setgid keeps the group on new entries.
        _DirectorySpec(
            "extensions",
            layout.installer_uid,
            layout.extension_gid,
            0o2775,
            previous_modes=(0o750,),
        ),
        _DirectorySpec("artifacts", layout.operator_uid, layout.artifact_gid, 0o2770),
        _DirectorySpec("logs", layout.root_uid, layout.root_gid, 0o755),
        _DirectorySpec("logs/app", layout.app_uid, layout.root_gid, 0o700),
        _DirectorySpec("logs/worker", layout.worker_uid, layout.root_gid, 0o700),
        _DirectorySpec("logs/control", layout.control_uid, layout.root_gid, 0o700),
        _DirectorySpec("logs/installer", layout.installer_uid, layout.root_gid, 0o700),
        _DirectorySpec("logs/mcp", layout.mcp_uid, layout.root_gid, 0o700),
        _DirectorySpec("logs/router", layout.router_uid, layout.root_gid, 0o700),
        _DirectorySpec("state", layout.operator_uid, layout.runtime_gid, 0o750),
        _DirectorySpec("state/.flow-steward", layout.operator_uid, layout.runtime_gid, 0o750),
        _DirectorySpec(
            "state/.flow-steward/runtime", layout.operator_uid, layout.runtime_gid, 0o2750
        ),
        _DirectorySpec("backups", layout.operator_uid, layout.root_gid, 0o700),
    )


def _open_directory(name: str, *, directory_fd: int) -> int:
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )


@contextmanager
def _effective_directory_group(gid: int, mode: int) -> Iterator[None]:
    """Retain setgid when the process lacks CAP_FSETID but may change group."""
    previous = os.getegid()
    changed = bool(mode & stat.S_ISGID) and previous != gid
    if changed:
        os.setegid(gid)
    try:
        yield
    finally:
        if changed:
            os.setegid(previous)


def _fchmod_for_group(fd: int, mode: int, gid: int) -> None:
    with _effective_directory_group(gid, mode):
        os.fchmod(fd, mode)


def _chmod_for_group(name: str, mode: int, gid: int, *, directory_fd: int) -> None:
    with _effective_directory_group(gid, mode):
        os.chmod(name, mode, dir_fd=directory_fd, follow_symlinks=False)


def _ensure_directory(
    root_fd: int,
    spec: _DirectorySpec,
    *,
    host_bound: bool = False,
) -> None:
    parts = spec.relative_path.split("/")
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = _open_directory(part, directory_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        leaf = parts[-1]
        created = False
        try:
            leaf_fd = _open_directory(leaf, directory_fd=parent_fd)
        except FileNotFoundError:
            os.mkdir(leaf, mode=spec.mode, dir_fd=parent_fd)
            os.chown(
                leaf,
                spec.uid,
                spec.gid,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            _chmod_for_group(leaf, spec.mode, spec.gid, directory_fd=parent_fd)
            leaf_fd = _open_directory(leaf, directory_fd=parent_fd)
            created = True
        except OSError as exc:
            raise ValueError(
                f"Compact volume path is not a safe directory: {spec.relative_path}"
            ) from exc
        try:
            metadata = os.fstat(leaf_fd)
            actual_mode = stat.S_IMODE(metadata.st_mode)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"Compact volume path is not a directory: {spec.relative_path}")
            owner_ok = host_bound or metadata.st_uid == spec.uid
            if not owner_ok or metadata.st_gid != spec.gid:
                raise ValueError(f"Compact volume ownership is unsafe: {spec.relative_path}")
            if actual_mode in spec.previous_modes:
                # Every existing Compact volume, on its first start on this release:
                # refusing it stopped the update of every installation (AC17-R163).
                with _effective_directory_group(spec.gid, spec.mode):
                    normalize_previous_tree(leaf_fd)
                    os.fchmod(leaf_fd, spec.mode)
                os.fsync(leaf_fd)
            elif actual_mode != spec.mode:
                raise ValueError(f"Compact volume mode is unsafe: {spec.relative_path}")
            if created:
                os.fsync(leaf_fd)
        finally:
            os.close(leaf_fd)
    finally:
        os.close(parent_fd)


def prepare_compact_volume(
    root: Path,
    *,
    layout: CompactVolumeLayout | None = None,
) -> tuple[str, ...]:
    """Create only known absent directories and reject unsafe restored paths."""
    selected = layout or CompactVolumeLayout()
    root_path = Path(root)
    root_metadata = root_path.lstat()
    if root_path.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("Compact volume root must be a real directory")

    root_fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    specs = _directory_specs(selected)
    try:
        root_metadata = os.fstat(root_fd)
        owner_ok = (root_metadata.st_uid, root_metadata.st_gid) == (
            selected.root_uid,
            selected.root_gid,
        )
        mode_ok = stat.S_IMODE(root_metadata.st_mode) == 0o755
        if not (owner_ok and mode_ok):
            _adopt_empty_provider_volume(root_fd, root_metadata, selected)
        host_bound = _host_bound_trees()
        for spec in specs:
            _ensure_directory(root_fd, spec, host_bound=spec.relative_path in host_bound)
    finally:
        os.close(root_fd)
    return tuple(spec.relative_path for spec in specs)


def _adopt_empty_provider_volume(
    root_fd: int,
    metadata: os.stat_result,
    layout: CompactVolumeLayout,
) -> None:
    """Normalize only an explicitly accepted, fresh provider-mounted root."""
    expected_owner = (layout.root_uid, layout.root_gid)
    owner_ok = (metadata.st_uid, metadata.st_gid) == expected_owner
    failure = "ownership" if not owner_ok else "mode"
    if str(_runtime_env.get("FS_COMPACT_ADOPT_EMPTY_VOLUME") or "") != "1":
        raise ValueError(f"Compact volume root {failure} is unsafe")
    if stat.S_IMODE(metadata.st_mode) not in {0o755, 0o2755, 0o2775}:
        raise ValueError(f"Compact volume root {failure} is unsafe")

    allowed = {_VOLUME_LOCK_NAME, "lost+found"}
    entries = set(os.listdir(root_fd))
    if entries - allowed:
        raise ValueError(f"Compact volume root {failure} is unsafe")
    for name in entries:
        if name == _VOLUME_LOCK_NAME:
            child = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if stat.S_ISREG(child.st_mode):
                continue
            raise ValueError(f"Compact volume root {failure} is unsafe")
        try:
            child_fd = _open_directory(name, directory_fd=root_fd)
        except OSError as exc:
            raise ValueError(f"Compact volume root {failure} is unsafe") from exc
        try:
            if os.listdir(child_fd):
                raise ValueError(f"Compact volume root {failure} is unsafe")
        finally:
            os.close(child_fd)

    if not owner_ok:
        os.fchown(root_fd, layout.root_uid, layout.root_gid)
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.fchmod(root_fd, 0o755)  # nosec B103 -- root is traversal-only; child modes protect data.
    os.fsync(root_fd)
    prepared = os.fstat(root_fd)
    if (prepared.st_uid, prepared.st_gid) != expected_owner:
        raise ValueError("Compact volume root ownership is unsafe")
    if stat.S_IMODE(prepared.st_mode) != 0o755:
        raise ValueError("Compact volume root mode is unsafe")


_VOLUME_LOCK_NAME = ".flow-steward-runtime.lock"


class CompactVolumeInUse(RuntimeError):
    """Another Compact runtime holds this data volume."""

    def __init__(self, path: Path, holder: str) -> None:
        self.path = path
        self.holder = holder
        super().__init__(
            "Another Compact runtime is using this data volume "
            f"({path.parent}){f': {holder}' if holder else ''}. Compact runs one runtime "
            "per volume; stop the other one, or let it finish stopping, and this one will "
            "start."
        )


@dataclass(slots=True)
class CompactVolumeLock:
    """Held for the life of the runtime process; released by the kernel when it dies."""

    path: Path
    fd: int

    def release(self) -> None:
        with suppress(OSError):
            os.close(self.fd)


def acquire_compact_volume_lock(data_root: Path) -> CompactVolumeLock:
    """Take the one-runtime-per-volume lock ADR 0029 requires, or refuse.

    P1-2. An exclusive flock on a file on the volume itself. It follows the holder's
    life by construction -- the kernel drops it when the process exits, however it
    exits -- so a crashed container never wedges the installation, and no lease or
    timeout has to guess whether the holder is still alive. The descriptor is
    close-on-exec, so the roles the supervisor starts do not inherit it and cannot
    keep it held after the supervisor is gone.

    It lives at the volume root rather than under state/, which bootstrap creates:
    the lock has to be taken before bootstrap touches anything.

    As the ADR says, a volume lock cannot see a second deployment on a different
    volume against the same database; stop-before-start on the platform covers that.
    """
    path = Path(data_root) / _VOLUME_LOCK_NAME
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ValueError("Compact volume lock must be a safe regular file") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("Compact volume lock must be a safe regular file")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            holder = os.pread(fd, 512, 0).decode("utf-8", errors="replace").strip()
        except OSError:
            holder = ""
        os.close(fd)
        raise CompactVolumeInUse(path, holder) from None
    except BaseException:
        os.close(fd)
        raise
    holder = f"held by pid {os.getpid()} on {socket.gethostname()} since {int(time.time())}"
    with suppress(OSError):
        os.ftruncate(fd, 0)
        os.pwrite(fd, holder.encode("utf-8"), 0)
    return CompactVolumeLock(path=path, fd=fd)
