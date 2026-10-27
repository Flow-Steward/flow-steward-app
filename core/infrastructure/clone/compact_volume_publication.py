"""Publish authenticated Clone trees into the Compact data volume as root."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

from core.application.clone.contracts import CloneDataClass, CloneEntry
from core.application.clone.state_policy import CLONE_TARGET_RETAINED_STATE_NAMES
from core.infrastructure.clone.volume_metadata import apply_volume_metadata, volume_metadata_matches

_TARGET = Path("/target")
_REPLACE_ROOTS = ("artifacts", "extensions")
_FLOW_STEWARD_STATE_NAME = ".flow-steward"
_AMBIGUOUS_RESIDUE_ERROR = "Compact Clone replacement residue is ambiguous"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--plan-digest", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--validate-only", action="store_true")
    parser.add_argument("--durable-state-only", action="store_true")
    args = parser.parse_args()
    source = _source_root(args.source)
    if len(args.plan_digest) != 64 or any(
        character not in "0123456789abcdef" for character in args.plan_digest
    ):
        raise ValueError("Compact Clone plan digest is invalid")
    raw = json.load(sys.stdin)
    if not isinstance(raw, list):
        raise ValueError("Compact Clone metadata must be a list")
    entries = tuple(CloneEntry.model_validate(item) for item in raw)
    if args.validate_only:
        validate_publication(
            source,
            entries,
            plan_digest=args.plan_digest,
            durable_state_only=args.durable_state_only,
        )
        return 0
    if args.verify:
        return (
            0
            if publication_matches(source, entries, durable_state_only=args.durable_state_only)
            else 1
        )
    publish(
        source, entries, plan_digest=args.plan_digest, durable_state_only=args.durable_state_only
    )
    return 0


def publish(
    source: Path,
    entries: tuple[CloneEntry, ...],
    *,
    plan_digest: str,
    durable_state_only: bool = False,
) -> None:
    validate_publication(
        source, entries, plan_digest=plan_digest, durable_state_only=durable_state_only
    )
    if durable_state_only:
        source_state, target_state = _mixed_durable_roots(source, entries)
        _merge_durable(
            source_state, target_state, entries, root_path="state", plan_digest=plan_digest
        )
        return
    source_state = source / "state"
    for root_name in _REPLACE_ROOTS:
        source_tree = source / root_name
        selected = _entries_for(entries, root_name)
        if source_tree.is_dir() and selected:
            _replace_tree(source_tree, _TARGET / root_name, selected, plan_digest)
    state_entries = _entries_for(entries, "state")
    if source_state.is_dir() and state_entries:
        _merge_durable(
            source_state,
            _TARGET / "state",
            state_entries,
            root_path="state",
            plan_digest=plan_digest,
        )


def validate_publication(
    source: Path,
    entries: tuple[CloneEntry, ...],
    *,
    plan_digest: str,
    durable_state_only: bool = False,
) -> None:
    """Check all state publication prerequisites without changing the target."""
    if durable_state_only:
        source_state, target_state = _mixed_durable_roots(source, entries)
        _validate_durable_publication(
            source_state, target_state, entries, root_path="state", plan_digest=plan_digest
        )
    else:
        _validate_publication(source, entries, plan_digest=plan_digest)


def _validate_publication(
    source: Path,
    entries: tuple[CloneEntry, ...],
    *,
    plan_digest: str,
) -> None:
    source_state = source / "state"
    _require_real_directory(_TARGET, "Compact Clone target volume")
    _require_real_directory(source, "Compact Clone staging root")
    _require_real_directory(source_state, "Compact Clone staged state root")
    _require_real_directory(_TARGET / "state", "Compact Clone target state root")
    _require_real_nested_shell(source_state, _TARGET / "state")
    for root_name in _REPLACE_ROOTS:
        source_tree = source / root_name
        selected = _entries_for(entries, root_name)
        if source_tree.is_dir() and selected:
            _validate_tree_replacement(
                source_tree,
                _TARGET / root_name,
                selected,
                plan_digest,
            )
    state_entries = _entries_for(entries, "state")
    if state_entries:
        _validate_durable_publication(
            source_state,
            _TARGET / "state",
            state_entries,
            root_path="state",
            plan_digest=plan_digest,
        )


def publication_matches(
    source: Path, entries: tuple[CloneEntry, ...], *, durable_state_only: bool = False
) -> bool:
    if durable_state_only:
        try:
            source_state, target_state = _mixed_durable_roots(source, entries)
        except ValueError:
            return False
        return _durable_matches(source_state, target_state, entries, root_path="state")
    for root_name in _REPLACE_ROOTS:
        source_tree = source / root_name
        selected = _entries_for(entries, root_name)
        if source_tree.is_dir() and (
            _replacement_residue_exists(_TARGET / root_name)
            or _content_snapshot(source_tree) != _content_snapshot(_TARGET / root_name)
            or not volume_metadata_matches(_TARGET / root_name, selected)
        ):
            return False
    source_state = source / "state"
    state_entries = _entries_for(entries, "state")
    return not source_state.is_dir() or _durable_matches(
        source_state,
        _TARGET / "state",
        state_entries,
        root_path="state",
    )


def _mixed_durable_roots(source: Path, entries: tuple[CloneEntry, ...]) -> tuple[Path, Path]:
    if not entries or any(
        entry.data_class is not CloneDataClass.DURABLE_STATE
        or not (entry.path == "state" or entry.path.startswith("state/"))
        for entry in entries
    ):
        raise ValueError("Compact mixed-placement publication requires state-only metadata")
    for path in (source, source / "state", _TARGET, _TARGET / "state"):
        _require_real_directory(path, "Compact Clone state-only root")
    target = _TARGET / "state" / _FLOW_STEWARD_STATE_NAME
    _require_real_directory(target, "Compact Clone durable target")
    if {child.name for child in source.iterdir()} != {"state"}:
        raise ValueError("Compact mixed-placement publication requires state-only staging")
    return source / "state", target


def _source_root(raw: str) -> Path:
    if raw == "/capture/source":
        _require_real_directory(Path("/capture"), "Compact Clone capture root")
        source = Path(raw)
        _require_real_directory(source, "Compact Clone staging root")
        return source
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Compact Clone staging path is invalid")
    prefix = ("state", _FLOW_STEWARD_STATE_NAME, "clone", "restore-staging")
    if relative.parts[: len(prefix)] != prefix:
        raise ValueError("Compact Clone staging path is outside the retained operation tree")
    source = _TARGET / relative
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Compact Clone staging root is unavailable")
    return source


def _require_real_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be a real directory")


def _replace_tree(
    source: Path,
    target: Path,
    entries: tuple[CloneEntry, ...],
    plan_digest: str,
) -> None:
    _validate_tree_replacement(source, target, entries, plan_digest)
    _replace_entry(source, target, entries, plan_digest)


def _validate_tree_replacement(
    source: Path,
    target: Path,
    entries: tuple[CloneEntry, ...],
    plan_digest: str,
) -> None:
    if source.is_symlink() or not source.is_dir() or target.is_symlink():
        raise ValueError("Compact Clone tree replacement requires real directories")
    if not _source_content_matches(source, entries):
        raise ValueError("Compact Clone staged tree contents are incomplete")
    _validate_replacement_state(source, target, entries, plan_digest)


def _replace_entry(
    source: Path,
    target: Path,
    entries: tuple[CloneEntry, ...],
    plan_digest: str,
) -> None:
    _validate_replacement_state(source, target, entries, plan_digest)
    suffix = plan_digest[:16]
    temporary = target.with_name(f".{target.name}.clone-new-{suffix}")
    previous = target.with_name(f".{target.name}.clone-previous-{suffix}")
    target_exists = _path_exists(target)
    temporary_exists = _path_exists(temporary)
    previous_exists = _path_exists(previous)
    if target_exists and temporary_exists and not previous_exists:
        _remove(temporary)
    elif not target_exists and temporary_exists and not previous_exists:
        if _content_snapshot(source) == _content_snapshot(temporary) and volume_metadata_matches(
            temporary, entries
        ):
            temporary.rename(target)
            return
        _remove(temporary)
    elif not target_exists and temporary_exists and previous_exists:
        if _content_snapshot(source) == _content_snapshot(temporary) and volume_metadata_matches(
            temporary, entries
        ):
            temporary.rename(target)
            _remove(previous)
            return
        _remove(temporary)
        previous.rename(target)
    elif target_exists and not temporary_exists and previous_exists:
        if _content_snapshot(source) == _content_snapshot(target) and volume_metadata_matches(
            target, entries
        ):
            _remove(previous)
            return
        raise RuntimeError(_AMBIGUOUS_RESIDUE_ERROR)
    elif not target_exists and not temporary_exists and previous_exists:
        previous.rename(target)
    elif temporary_exists or previous_exists:
        raise RuntimeError(_AMBIGUOUS_RESIDUE_ERROR)
    _copy(source, temporary)
    apply_volume_metadata(temporary, entries)
    if _path_exists(target):
        target.rename(previous)
    temporary.rename(target)
    _remove(previous)
    if _path_exists(temporary) or _path_exists(previous):
        raise RuntimeError("Compact Clone replacement residue cleanup did not complete")


def _validate_replacement_state(
    source: Path,
    target: Path,
    entries: tuple[CloneEntry, ...],
    plan_digest: str,
) -> None:
    if not (source.is_symlink() or source.is_file() or source.is_dir()):
        raise ValueError("Compact Clone source contains an unsupported entry")
    suffix = plan_digest[:16]
    temporary = target.with_name(f".{target.name}.clone-new-{suffix}")
    previous = target.with_name(f".{target.name}.clone-previous-{suffix}")
    expected = {temporary.name, previous.name}
    foreign = {
        path.name
        for pattern in (f".{target.name}.clone-new-*", f".{target.name}.clone-previous-*")
        for path in target.parent.glob(pattern)
        if path.name not in expected
    }
    if foreign:
        raise RuntimeError("Compact Clone replacement has foreign plan residue")
    target_exists = _path_exists(target)
    temporary_exists = _path_exists(temporary)
    previous_exists = _path_exists(previous)
    if target_exists and temporary_exists and previous_exists:
        raise RuntimeError(_AMBIGUOUS_RESIDUE_ERROR)
    if (
        target_exists
        and not temporary_exists
        and previous_exists
        and not (
            _content_snapshot(source) == _content_snapshot(target)
            and volume_metadata_matches(target, entries)
        )
    ):
        raise RuntimeError(_AMBIGUOUS_RESIDUE_ERROR)


def _merge_durable(
    source: Path,
    target: Path,
    entries: tuple[CloneEntry, ...],
    *,
    root_path: str,
    plan_digest: str,
) -> None:
    if target.is_symlink() or not target.is_dir():
        raise ValueError("Compact Clone durable target is unavailable")
    _require_real_nested_shell(source, target)
    _validate_durable_residue_scope(source, target, plan_digest)
    source_names = {
        child.name
        for child in source.iterdir()
        if child.name not in CLONE_TARGET_RETAINED_STATE_NAMES
    }
    for child in tuple(target.iterdir()):
        residue_target = _residue_target_name(child.name, plan_digest)
        if residue_target in source_names:
            continue
        if child.name not in CLONE_TARGET_RETAINED_STATE_NAMES and child.name not in source_names:
            _remove(child)
    for child in source.iterdir():
        if child.name in CLONE_TARGET_RETAINED_STATE_NAMES:
            continue
        destination = target / child.name
        child_path = f"{root_path}/{child.name}"
        if (
            child.name == _FLOW_STEWARD_STATE_NAME
            and child.is_dir()
            and not child.is_symlink()
            and destination.is_dir()
            and not destination.is_symlink()
        ):
            _merge_durable(
                child,
                destination,
                entries,
                root_path=child_path,
                plan_digest=plan_digest,
            )
            continue
        _replace_entry(
            child,
            destination,
            _relative_entries(entries, child_path),
            plan_digest,
        )
    root_entry = next((entry for entry in entries if entry.path == root_path), None)
    if root_entry is None:
        raise ValueError("Compact Clone durable metadata is incomplete")
    os.chown(target, root_entry.uid, root_entry.gid, follow_symlinks=False)
    target.chmod(root_entry.mode, follow_symlinks=False)


def _validate_durable_publication(
    source: Path,
    target: Path,
    entries: tuple[CloneEntry, ...],
    *,
    root_path: str,
    plan_digest: str,
) -> None:
    if target.is_symlink() or not target.is_dir():
        raise ValueError("Compact Clone durable target is unavailable")
    _require_real_nested_shell(source, target)
    if not _source_content_matches(source, _relative_entries(entries, root_path)):
        raise ValueError("Compact Clone staged durable contents are incomplete")
    _validate_durable_residue_scope(source, target, plan_digest)
    for child in source.iterdir():
        if child.name in CLONE_TARGET_RETAINED_STATE_NAMES:
            continue
        destination = target / child.name
        child_path = f"{root_path}/{child.name}"
        if (
            child.name == _FLOW_STEWARD_STATE_NAME
            and child.is_dir()
            and not child.is_symlink()
            and destination.is_dir()
            and not destination.is_symlink()
        ):
            _validate_durable_publication(
                child,
                destination,
                entries,
                root_path=child_path,
                plan_digest=plan_digest,
            )
            continue
        _validate_replacement_state(
            child,
            destination,
            _relative_entries(entries, child_path),
            plan_digest,
        )


def _durable_matches(
    source: Path,
    target: Path,
    entries: tuple[CloneEntry, ...],
    *,
    root_path: str,
) -> bool:
    if source.is_symlink() or target.is_symlink() or not source.is_dir() or not target.is_dir():
        return False
    try:
        _require_real_nested_shell(source, target)
    except ValueError:
        return False
    source_names = {
        child.name
        for child in source.iterdir()
        if child.name not in CLONE_TARGET_RETAINED_STATE_NAMES
    }
    target_names = {
        child.name
        for child in target.iterdir()
        if child.name not in CLONE_TARGET_RETAINED_STATE_NAMES
    }
    if source_names != target_names or not _entry_metadata_matches(target, entries, root_path):
        return False
    for name in source_names:
        source_child = source / name
        target_child = target / name
        if _replacement_residue_exists(target_child):
            return False
        child_path = f"{root_path}/{name}"
        if (
            name == _FLOW_STEWARD_STATE_NAME
            and source_child.is_dir()
            and not source_child.is_symlink()
        ):
            if not _durable_matches(
                source_child,
                target_child,
                entries,
                root_path=child_path,
            ):
                return False
        elif _content_snapshot(source_child) != _content_snapshot(
            target_child
        ) or not volume_metadata_matches(
            target_child,
            _relative_entries(entries, child_path),
        ):
            return False
    return True


def _replacement_residue_exists(target: Path) -> bool:
    return any(target.parent.glob(f".{target.name}.clone-new-*")) or any(
        target.parent.glob(f".{target.name}.clone-previous-*")
    )


def _validate_durable_residue_scope(
    source: Path,
    target: Path,
    plan_digest: str,
) -> None:
    for child in target.iterdir():
        if (
            _looks_like_replacement_residue(child.name)
            and _residue_target_name(child.name, plan_digest) is None
        ):
            raise RuntimeError("Compact Clone durable replacement has foreign plan residue")
    source_shell = source / _FLOW_STEWARD_STATE_NAME
    target_shell = target / _FLOW_STEWARD_STATE_NAME
    if (
        source_shell.is_dir()
        and not source_shell.is_symlink()
        and target_shell.is_dir()
        and not target_shell.is_symlink()
    ):
        _validate_durable_residue_scope(source_shell, target_shell, plan_digest)


def _looks_like_replacement_residue(name: str) -> bool:
    return name.startswith(".") and any(
        marker in name for marker in (".clone-new-", ".clone-previous-")
    )


def _residue_target_name(name: str, plan_digest: str) -> str | None:
    suffix = plan_digest[:16]
    for marker in (".clone-new-", ".clone-previous-"):
        if name.startswith(".") and marker in name:
            target_name, observed_suffix = name[1:].rsplit(marker, 1)
            if target_name and observed_suffix == suffix:
                return target_name
    return None


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _require_real_nested_shell(source: Path, target: Path) -> None:
    for shell in (
        source / _FLOW_STEWARD_STATE_NAME,
        target / _FLOW_STEWARD_STATE_NAME,
    ):
        if (shell.exists() or shell.is_symlink()) and (shell.is_symlink() or not shell.is_dir()):
            raise ValueError("Compact Clone nested shell must be a real directory")


def _entry_metadata_matches(
    path: Path,
    entries: tuple[CloneEntry, ...],
    entry_path: str,
) -> bool:
    entry = next((item for item in entries if item.path == entry_path), None)
    if entry is None:
        return False
    metadata = path.lstat()
    return (
        stat.S_ISDIR(metadata.st_mode)
        and (metadata.st_uid, metadata.st_gid) == (entry.uid, entry.gid)
        and stat.S_IMODE(metadata.st_mode) == entry.mode
    )


def _entries_for(entries: tuple[CloneEntry, ...], root: str) -> tuple[CloneEntry, ...]:
    prefix = f"{root}/"
    return tuple(entry for entry in entries if entry.path == root or entry.path.startswith(prefix))


def _relative_entries(
    entries: tuple[CloneEntry, ...],
    root_path: str,
) -> tuple[CloneEntry, ...]:
    selected = _entries_for(entries, root_path)
    root_name = Path(root_path).name
    prefix = f"{root_path}/"
    return tuple(
        entry.model_copy(
            update={
                "path": root_name
                if entry.path == root_path
                else f"{root_name}/{entry.path.removeprefix(prefix)}"
            }
        )
        for entry in selected
    )


def _source_content_matches(root: Path, entries: tuple[CloneEntry, ...]) -> bool:
    roots = tuple(entry for entry in entries if "/" not in entry.path)
    if (
        len(roots) != 1
        or roots[0].entry_type != "directory"
        or root.is_symlink()
        or not root.is_dir()
    ):
        return False
    archive_root = roots[0].path
    expected: list[tuple[str, str, str]] = []
    for entry in entries:
        if entry.path == archive_root:
            continue
        relative = entry.path.removeprefix(f"{archive_root}/")
        if entry.entry_type == "directory":
            value = ""
        elif entry.entry_type == "file":
            value = str(entry.sha256 or "")
        else:
            value = str(entry.link_target or "")
        expected.append((relative, entry.entry_type, value))
    return tuple(sorted(expected)) == _content_snapshot(root)


def _copy(source: Path, destination: Path) -> None:
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    elif source.is_file():
        shutil.copy2(source, destination, follow_symlinks=False)
    else:
        raise ValueError("Compact Clone source contains an unsupported entry")


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _content_snapshot(root: Path) -> tuple[tuple[str, str, str], ...]:
    values: list[tuple[str, str, str]] = []
    if root.is_symlink():
        values.append((".", "symlink", os.readlink(root)))
    elif root.is_file():
        values.append((".", "file", _sha256(root)))
    elif root.is_dir():
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                values.append((relative, "symlink", os.readlink(path)))
            elif path.is_dir():
                values.append((relative, "directory", ""))
            elif path.is_file():
                values.append((relative, "file", _sha256(path)))
            else:
                values.append((relative, "unsupported", ""))
    return tuple(values)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
