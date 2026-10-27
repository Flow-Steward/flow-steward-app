"""Publish Clone filesystems with retained-shell and target-ownership checks."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path

from core.application.clone.state_policy import CLONE_TARGET_RETAINED_STATE_NAMES

_FLOW_STEWARD_STATE_NAME = ".flow-steward"


def _replace_directory(
    source: Path,
    target: Path,
    *,
    plan_digest: str,
    publication_modes: Mapping[str, int] | None = None,
    owner: tuple[int, int] | None = None,
) -> None:
    if source.is_symlink() or not source.is_dir() or target.is_symlink():
        raise ValueError("Clone filesystem replacement requires real directories")
    if len(plan_digest) != 64 or any(
        character not in "0123456789abcdef" for character in plan_digest
    ):
        raise ValueError("Clone filesystem replacement plan digest is invalid")
    expected_owner = owner or (os.geteuid(), os.getegid())
    _require_runtime_owner(expected_owner)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary, previous = _replacement_paths(target, plan_digest)
    expected = {temporary.name, previous.name}
    foreign = {
        path.name
        for pattern in (f".{target.name}.clone-new-*", f".{target.name}.clone-previous-*")
        for path in target.parent.glob(pattern)
        if path.name not in expected
    }
    if foreign:
        raise RuntimeError("Clone filesystem replacement has foreign plan residue")
    if temporary.is_symlink() or previous.is_symlink():
        raise RuntimeError("Clone filesystem replacement residue must be a real directory")
    target_exists = target.exists()
    temporary_exists = temporary.exists()
    previous_exists = previous.exists()
    if target_exists and temporary_exists and not previous_exists:
        shutil.rmtree(temporary)
    elif not target_exists and temporary_exists and previous_exists:
        if _tree_matches_publication(
            source,
            temporary,
            publication_modes=publication_modes,
            owner=expected_owner,
        ):
            temporary.rename(target)
            shutil.rmtree(previous)
            return
        shutil.rmtree(temporary)
        previous.rename(target)
    elif target_exists and not temporary_exists and previous_exists:
        if not _tree_matches_publication(
            source,
            target,
            publication_modes=publication_modes,
            owner=expected_owner,
        ):
            raise RuntimeError("Clone filesystem replacement residue is ambiguous")
        shutil.rmtree(previous)
        return
    elif not target_exists and temporary_exists and not previous_exists:
        if _tree_matches_publication(
            source,
            temporary,
            publication_modes=publication_modes,
            owner=expected_owner,
        ):
            temporary.rename(target)
            return
        shutil.rmtree(temporary)
    elif not target_exists and not temporary_exists and previous_exists:
        previous.rename(target)
    elif temporary_exists or previous_exists:
        raise RuntimeError("Clone filesystem replacement residue is inconsistent")
    _copy_tree_for_target(
        source,
        temporary,
        publication_modes=publication_modes,
        owner=expected_owner,
    )
    try:
        if target.exists():
            target.rename(previous)
        temporary.rename(target)
        if previous.exists():
            shutil.rmtree(previous)
        if previous.exists() or previous.is_symlink():
            raise RuntimeError("Clone previous sensitive tree cleanup did not complete")
    except BaseException:
        if not target.exists() and previous.exists():
            previous.rename(target)
        raise


def _replacement_paths(target: Path, plan_digest: str) -> tuple[Path, Path]:
    suffix = plan_digest[:16]
    return (
        target.with_name(f".{target.name}.clone-new-{suffix}"),
        target.with_name(f".{target.name}.clone-previous-{suffix}"),
    )


def _replacement_residue_exists(target: Path, plan_digest: str) -> bool:
    temporary, previous = _replacement_paths(target, plan_digest)
    if any(path.exists() or path.is_symlink() for path in (temporary, previous)):
        return True
    return any(target.parent.glob(f".{target.name}.clone-new-*")) or any(
        target.parent.glob(f".{target.name}.clone-previous-*")
    )


def _replace_durable_children(
    source: Path,
    target: Path,
    *,
    publication_modes: Mapping[str, int] | None = None,
    owner: tuple[int, int] | None = None,
) -> None:
    """Replace source-owned children while retaining the target lifecycle shell."""
    if source.is_symlink() or not source.is_dir() or target.is_symlink():
        raise ValueError("Clone durable-state replacement requires real directories")
    _require_real_nested_shell(source, target)
    expected_owner = owner or (os.geteuid(), os.getegid())
    _require_runtime_owner(expected_owner)
    retained = CLONE_TARGET_RETAINED_STATE_NAMES
    target.mkdir(parents=True, exist_ok=True)
    for child in tuple(target.iterdir()):
        source_child = source / child.name
        nested_shell = (
            child.name == _FLOW_STEWARD_STATE_NAME
            and source_child.is_dir()
            and not source_child.is_symlink()
            and child.is_dir()
            and not child.is_symlink()
        )
        if child.name not in retained and not nested_shell:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    for child in source.iterdir():
        if child.name in retained:
            continue
        destination = target / child.name
        if (
            child.name == _FLOW_STEWARD_STATE_NAME
            and child.is_dir()
            and not child.is_symlink()
            and destination.is_dir()
            and not destination.is_symlink()
        ):
            _replace_durable_children(
                child,
                destination,
                publication_modes=_child_modes(publication_modes, child.name),
                owner=expected_owner,
            )
        else:
            _copy_entry_for_target(
                child,
                destination,
                relative=child.name,
                publication_modes=publication_modes,
                owner=expected_owner,
            )


def _require_real_nested_shell(source: Path, target: Path) -> None:
    for shell in (
        source / _FLOW_STEWARD_STATE_NAME,
        target / _FLOW_STEWARD_STATE_NAME,
    ):
        if (shell.exists() or shell.is_symlink()) and (shell.is_symlink() or not shell.is_dir()):
            raise ValueError("Clone durable-state nested shell must be a real directory")


def _durable_children_match(
    source: Path,
    target: Path,
    *,
    publication_modes: Mapping[str, int] | None = None,
    owner: tuple[int, int] | None = None,
) -> bool:
    if source.is_symlink() or target.is_symlink() or not source.is_dir() or not target.is_dir():
        return False
    retained = CLONE_TARGET_RETAINED_STATE_NAMES
    source_names = {child.name for child in source.iterdir() if child.name not in retained}
    target_names = {child.name for child in target.iterdir() if child.name not in retained}
    if source_names != target_names:
        return False
    for name in source_names:
        source_child = source / name
        target_child = target / name
        if (
            name == _FLOW_STEWARD_STATE_NAME
            and source_child.is_dir()
            and not source_child.is_symlink()
        ):
            if not _durable_children_match(
                source_child,
                target_child,
                publication_modes=_child_modes(publication_modes, name),
                owner=owner,
            ):
                return False
        elif not _tree_matches_publication(
            source_child,
            target_child,
            publication_modes=_child_modes(publication_modes, name),
            owner=owner,
        ):
            return False
    return True


def _copy_tree_for_target(
    source: Path,
    destination: Path,
    *,
    publication_modes: Mapping[str, int] | None,
    owner: tuple[int, int],
) -> None:
    metadata = source.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("Clone filesystem source must be a real directory")
    _copy_directory_for_target(
        source,
        destination,
        relative=".",
        publication_modes=publication_modes,
        owner=owner,
    )


def _copy_entry_for_target(
    source: Path,
    destination: Path,
    *,
    relative: str,
    publication_modes: Mapping[str, int] | None,
    owner: tuple[int, int],
) -> None:
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        os.symlink(os.readlink(source), destination)
    elif stat.S_ISDIR(metadata.st_mode):
        _copy_directory_for_target(
            source,
            destination,
            relative=relative,
            publication_modes=publication_modes,
            owner=owner,
        )
    elif stat.S_ISREG(metadata.st_mode):
        _copy_file_for_target(
            source,
            destination,
            mode=_publication_mode(publication_modes, relative, metadata.st_mode),
        )
    else:
        raise ValueError("Clone filesystem source contains an unsupported entry")
    _require_path_owner(destination, owner)


def _copy_directory_for_target(
    source: Path,
    destination: Path,
    *,
    relative: str,
    publication_modes: Mapping[str, int] | None,
    owner: tuple[int, int],
) -> None:
    destination.mkdir(mode=0o700)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        child_relative = child.name if relative == "." else f"{relative}/{child.name}"
        _copy_entry_for_target(
            child,
            destination / child.name,
            relative=child_relative,
            publication_modes=publication_modes,
            owner=owner,
        )
    destination.chmod(
        _publication_mode(publication_modes, relative, source.lstat().st_mode),
        follow_symlinks=False,
    )
    _require_path_owner(destination, owner)


def _copy_file_for_target(source: Path, destination: Path, *, mode: int) -> None:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(source, os.O_RDONLY | no_follow)
    try:
        metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Clone filesystem source file changed during copy")
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow,
            0o600,
        )
        try:
            while chunk := os.read(source_descriptor, 1024 * 1024):
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    view = view[written:]
            os.fchmod(destination_descriptor, mode)
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)


def _publication_mode(
    publication_modes: Mapping[str, int] | None,
    relative: str,
    source_mode: int,
) -> int:
    if publication_modes is None:
        return stat.S_IMODE(source_mode)
    try:
        return publication_modes[relative]
    except KeyError as exc:
        raise ValueError("Clone publication metadata is incomplete") from exc


def _require_runtime_owner(owner: tuple[int, int]) -> None:
    if (os.geteuid(), os.getegid()) != owner:
        raise RuntimeError("Clone filesystem publication runtime identity does not match target")


def _require_path_owner(path: Path, owner: tuple[int, int]) -> None:
    metadata = path.lstat()
    if (metadata.st_uid, metadata.st_gid) != owner:
        raise RuntimeError("Clone filesystem publication ownership does not match target")


def _child_modes(
    publication_modes: Mapping[str, int] | None,
    name: str,
) -> dict[str, int] | None:
    if publication_modes is None:
        return None
    prefix = f"{name}/"
    selected = {
        path.removeprefix(prefix): mode
        for path, mode in publication_modes.items()
        if path.startswith(prefix)
    }
    if name in publication_modes:
        selected["."] = publication_modes[name]
    return selected


def _tree_matches_publication(
    source: Path,
    target: Path,
    *,
    publication_modes: Mapping[str, int] | None,
    owner: tuple[int, int] | None,
) -> bool:
    if _tree_content_snapshot(source) != _tree_content_snapshot(target):
        return False
    expected_owner = owner or (os.geteuid(), os.getegid())
    paths = [target] if not target.is_dir() or target.is_symlink() else [target, *target.rglob("*")]
    for path in paths:
        relative = "." if path == target else path.relative_to(target).as_posix()
        metadata = path.lstat()
        if (metadata.st_uid, metadata.st_gid) != expected_owner:
            return False
        if not stat.S_ISLNK(metadata.st_mode):
            expected_mode = _publication_mode(publication_modes, relative, metadata.st_mode)
            if stat.S_IMODE(metadata.st_mode) != expected_mode:
                return False
    return True


def _tree_content_snapshot(root: Path) -> tuple[tuple[str, str, str], ...]:
    return tuple((path, kind, value) for path, kind, _mode, value in _tree_snapshot(root))


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    values: list[tuple[str, str, int, str]] = []
    if root.is_symlink():
        values.append((".", "symlink", stat.S_IMODE(root.lstat().st_mode), os.readlink(root)))
    elif root.is_file():
        values.append((".", "file", stat.S_IMODE(root.stat().st_mode), _sha256_file(root)))
    elif root.is_dir():
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if path.is_symlink():
                values.append((relative, "symlink", mode, os.readlink(path)))
            elif path.is_dir():
                values.append((relative, "directory", mode, ""))
            elif path.is_file():
                values.append((relative, "file", mode, _sha256_file(path)))
            else:
                values.append((relative, "unsupported", mode, ""))
    return tuple(values)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
