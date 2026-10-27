"""Crash-safe filesystem reconciliation for committed extension uninstalls."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_JOURNAL_DIR = ".fs-uninstall-cleanup"
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


@dataclass(frozen=True)
class CleanupReconciliation:
    completed: int = 0
    discarded: int = 0
    pending: int = 0
    errors: tuple[str, ...] = ()


def _contained(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    absolute = Path(os.path.abspath(candidate))
    if absolute == resolved_root or resolved_root not in absolute.parents:
        raise ValueError("Extension cleanup path escapes the managed extensions root")
    current = resolved_root
    for component in absolute.relative_to(resolved_root).parts[:-1]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("Extension cleanup path has a symlinked ancestor")
    return absolute


@contextlib.contextmanager
def _opened_directory(
    root: Path,
    components: tuple[str, ...],
    *,
    create: bool = False,
) -> Iterator[int]:
    descriptor = os.open(root, _DIRECTORY_OPEN_FLAGS)
    try:
        for component in components:
            try:
                child = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                child = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        try:
            yield descriptor
        finally:
            os.close(descriptor)
            descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@contextlib.contextmanager
def _opened_parent(root: Path, candidate: Path) -> Iterator[tuple[int, str]]:
    absolute = _contained(root, candidate)
    relative = absolute.relative_to(root.resolve())
    with _opened_directory(root.resolve(), tuple(relative.parts[:-1])) as descriptor:
        yield descriptor, relative.name


def _entry_exists(root: Path, path: Path) -> bool:
    try:
        with _opened_parent(root, path) as (parent_descriptor, name):
            try:
                os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return False
            return True
    except FileNotFoundError:
        return False


def _remove_entry(parent_descriptor: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode):
        os.unlink(name, dir_fd=parent_descriptor)
        return
    child_descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_descriptor)
    try:
        for child_name in os.listdir(child_descriptor):
            _remove_entry(child_descriptor, child_name)
    finally:
        os.close(child_descriptor)
    os.rmdir(name, dir_fd=parent_descriptor)


def _remove_path(root: Path, path: Path) -> None:
    with _opened_parent(root, path) as (parent_descriptor, name):
        _remove_entry(parent_descriptor, name)


def _replace_and_remove(*, root: Path, source: Path, quarantine: Path) -> None:
    with (
        _opened_parent(root, source) as (source_parent, source_name),
        _opened_parent(root, quarantine) as (quarantine_parent, quarantine_name),
    ):
        os.replace(
            source_name,
            quarantine_name,
            src_dir_fd=source_parent,
            dst_dir_fd=quarantine_parent,
        )
        _remove_entry(quarantine_parent, quarantine_name)


def _read_json(root: Path, path: Path) -> dict[str, Any]:
    with _opened_parent(root, path) as (parent_descriptor, name):
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise ValueError("Extension cleanup journal entry must be a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            return json.load(stream)


def _unlink_path(root: Path, path: Path) -> None:
    with _opened_parent(root, path) as (parent_descriptor, name):
        os.unlink(name, dir_fd=parent_descriptor)


def _journal_paths(root: Path) -> tuple[Path, ...]:
    journal_root = _contained(root, root / _JOURNAL_DIR)
    try:
        relative = journal_root.relative_to(root.resolve())
        with _opened_directory(root.resolve(), tuple(relative.parts)) as descriptor:
            return tuple(
                sorted(
                    journal_root / name for name in os.listdir(descriptor) if name.endswith(".json")
                )
            )
    except FileNotFoundError:
        return ()


def _atomic_json(root: Path, path: Path, payload: dict[str, Any]) -> None:
    absolute = _contained(root, path)
    relative = absolute.relative_to(root.resolve())
    temporary_name = f"{relative.name}.tmp"
    with _opened_directory(
        root.resolve(),
        tuple(relative.parts[:-1]),
        create=True,
    ) as directory:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(payload, stream, ensure_ascii=True, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary_name,
                relative.name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
            os.fsync(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory)


def prepare_cleanup_intent(
    *,
    extensions_root: Path,
    extension_id: str,
    account_id: str,
    plan_revision: str,
    bundle_roots: Iterable[Path],
) -> Path | None:
    root = extensions_root.resolve()
    sources = tuple(dict.fromkeys(_contained(root, Path(item)) for item in bundle_roots))
    if not sources:
        return None
    digest = hashlib.sha256(
        "\0".join(
            (extension_id, account_id, plan_revision, *(str(item) for item in sources))
        ).encode()
    ).hexdigest()[:32]
    entries = [
        {
            "source": str(source),
            "quarantine": str(source.parent / f".{source.name}.uninstall-{digest[:12]}"),
        }
        for source in sources
    ]
    path = root / _JOURNAL_DIR / f"{digest}.json"
    path = _contained(root, path)
    _atomic_json(
        root,
        path,
        {
            "version": 1,
            "extension_id": extension_id,
            "account_id": account_id,
            "plan_revision": plan_revision,
            "roots": entries,
        },
    )
    return path


def reconcile_cleanup_journal(
    *,
    extensions_root: Path,
    is_installed: Callable[[str, str], bool],
    intent_path: Path | None = None,
    discard_installed_intents: bool = False,
    discovered_extension_ids: Iterable[str] = (),
) -> CleanupReconciliation:
    """Reconcile intents; discard installed rows only under the global lifecycle lock."""
    root = extensions_root.resolve()
    discovered_ids = {
        str(extension_id or "").strip()
        for extension_id in discovered_extension_ids
        if str(extension_id or "").strip()
    }
    completed = discarded = pending = 0
    errors: list[str] = []
    try:
        paths = (intent_path,) if intent_path is not None else _journal_paths(root)
    except PermissionError as exc:
        # A process that may not write the managed root cannot finish cleanups;
        # say so as pending work instead of failing the caller (a scan).
        return CleanupReconciliation(
            pending=1, errors=(f"{_JOURNAL_DIR}: {exc.strerror or 'permission denied'}",)
        )
    for path in paths:
        if path is None:
            continue
        try:
            path = _contained(root, path)
            if not _entry_exists(root, path):
                continue
            payload = _read_json(root, path)
            extension_id = str(payload["extension_id"])
            account_id = str(payload["account_id"])
            if is_installed(extension_id, account_id):
                if discard_installed_intents:
                    _unlink_path(root, path)
                    discarded += 1
                else:
                    pending += 1
                    errors.append(
                        f"{path.name}: extension is still installed or lifecycle in progress"
                    )
                continue
            entries = tuple(payload.get("roots", ()))
            if extension_id in discovered_ids:
                _unlink_path(root, path)
                discarded += 1
                continue
            for entry in entries:
                source = _contained(root, Path(str(entry["source"])))
                quarantine = _contained(root, Path(str(entry["quarantine"])))
                if _entry_exists(root, quarantine):
                    _remove_path(root, quarantine)
                if _entry_exists(root, source):
                    _replace_and_remove(root=root, source=source, quarantine=quarantine)
            _unlink_path(root, path)
            completed += 1
        except (OSError, ValueError, KeyError, TypeError) as exc:
            pending += 1
            errors.append(f"{path.name}: {exc}")
    return CleanupReconciliation(
        completed=completed,
        discarded=discarded,
        pending=pending,
        errors=tuple(errors),
    )
