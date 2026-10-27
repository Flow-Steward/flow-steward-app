"""Publish authenticated host trees without replacing their bind-mount roots."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from core.application.clone.contracts import CloneEntry
from core.infrastructure.clone.compact_volume_publication import (
    _content_snapshot,
    _looks_like_replacement_residue,
    _relative_entries,
    _remove,
    _replace_entry,
    _replacement_residue_exists,
    _residue_target_name,
    _source_content_matches,
    _validate_replacement_state,
)
from core.infrastructure.clone.volume_metadata import volume_metadata_matches

_TARGET = Path("/target")
_SOURCE = Path("/capture/source")
_ROOT_CLASSES = frozenset({"artifacts", "extensions"})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-digest", required=True)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    raw = json.load(sys.stdin)
    if not isinstance(raw, list):
        raise ValueError("Host Clone metadata must be a list")
    entries = tuple(CloneEntry.model_validate(item) for item in raw)
    owner = (args.uid, args.gid)
    if args.verify:
        return 0 if publication_matches(_SOURCE, _TARGET, entries, owner=owner) else 1
    if args.validate_only:
        validate_publication(_SOURCE, _TARGET, entries, plan_digest=args.plan_digest, owner=owner)
    else:
        publish(_SOURCE, _TARGET, entries, plan_digest=args.plan_digest, owner=owner)
    return 0


def _host_entries(
    entries: tuple[CloneEntry, ...], owner: tuple[int, int]
) -> tuple[CloneEntry, ...]:
    if any(value < 0 or value > 2**31 - 1 for value in owner):
        raise ValueError("Host Clone target identity is invalid")
    roots = tuple(entry for entry in entries if "/" not in entry.path)
    if len(roots) != 1 or roots[0].path not in _ROOT_CLASSES or roots[0].entry_type != "directory":
        raise ValueError("Host Clone publication requires one allowlisted filesystem root")
    root = roots[0]
    expected_class = {"artifacts": "local_artifacts", "extensions": "extensions"}[root.path]
    if any(
        entry.data_class.value != expected_class
        or not (entry.path == root.path or entry.path.startswith(f"{root.path}/"))
        for entry in entries
    ):
        raise ValueError("Host Clone metadata crosses filesystem ownership classes")
    return tuple(entry.model_copy(update={"uid": owner[0], "gid": owner[1]}) for entry in entries)


def validate_publication(
    source: Path,
    target: Path,
    entries: tuple[CloneEntry, ...],
    *,
    plan_digest: str,
    owner: tuple[int, int],
) -> None:
    """Validate every child before deleting any target-only data."""
    if len(plan_digest) != 64 or any(c not in "0123456789abcdef" for c in plan_digest):
        raise ValueError("Host Clone publication plan digest is invalid")
    normalized = _host_entries(entries, owner)
    if source.is_symlink() or not source.is_dir() or target.is_symlink() or not target.is_dir():
        raise ValueError("Host Clone publication requires real directory roots")
    if not _source_content_matches(source, normalized):
        raise ValueError("Host Clone staged contents do not match authenticated metadata")
    source_names = {child.name for child in source.iterdir()}
    if any(_looks_like_replacement_residue(name) for name in source_names):
        raise ValueError("Host Clone source uses a reserved publication residue name")
    for child in target.iterdir():
        if _looks_like_replacement_residue(child.name):
            if _residue_target_name(child.name, plan_digest) not in source_names:
                raise RuntimeError("Host Clone publication has foreign plan residue")
            if child.is_symlink():
                raise RuntimeError("Host Clone publication residue must not be a symlink")
    # The root entry need not be first in a serialized manifest.
    root = next(entry.path for entry in normalized if "/" not in entry.path)
    for child in source.iterdir():
        if child.is_symlink() and _replacement_residue_exists(target / child.name):
            raise RuntimeError("Host Clone symlink publication has unexpected residue")
        _validate_replacement_state(
            child,
            target / child.name,
            _relative_entries(normalized, f"{root}/{child.name}"),
            plan_digest,
        )


def publish(
    source: Path,
    target: Path,
    entries: tuple[CloneEntry, ...],
    *,
    plan_digest: str,
    owner: tuple[int, int],
) -> None:
    """Replace children resumably while preserving the exact mounted root inode."""
    validate_publication(source, target, entries, plan_digest=plan_digest, owner=owner)
    normalized = _host_entries(entries, owner)
    root_entry = next(entry for entry in normalized if "/" not in entry.path)
    source_names = {child.name for child in source.iterdir()}
    for child in tuple(target.iterdir()):
        if child.name not in source_names and not _looks_like_replacement_residue(child.name):
            _remove(child)
    for child in source.iterdir():
        destination = target / child.name
        # Never create symlink residue: unlinking or recreating this exact child
        # can be retried, while untrusted residue remains rejected above.
        if child.is_symlink():
            _remove(destination)
            destination.symlink_to(os.readlink(child))
            os.chown(destination, owner[0], owner[1], follow_symlinks=False)
            continue
        if destination.is_symlink():
            destination.unlink()
        _replace_entry(
            child,
            destination,
            _relative_entries(normalized, f"{root_entry.path}/{child.name}"),
            plan_digest,
        )
    os.chown(target, owner[0], owner[1], follow_symlinks=False)
    target.chmod(root_entry.mode, follow_symlinks=False)


def publication_matches(
    source: Path,
    target: Path,
    entries: tuple[CloneEntry, ...],
    *,
    owner: tuple[int, int],
) -> bool:
    """Verify complete contents, host ownership, modes, and absence of child residue."""
    normalized = _host_entries(entries, owner)
    if source.is_symlink() or target.is_symlink() or not source.is_dir() or not target.is_dir():
        return False
    if not _source_content_matches(source, normalized):
        return False
    if any(_replacement_residue_exists(target / child.name) for child in source.iterdir()):
        return False
    return _content_snapshot(source) == _content_snapshot(target) and volume_metadata_matches(
        target, normalized
    )


if __name__ == "__main__":
    raise SystemExit(main())
