"""Bring the trees a Scalable installation created into the shared model (item 5, step 3).

ADR 0029, decision B: on a switch to Compact, artifacts and the extensions stay in
Scalable's host directories and are mounted where Compact keeps them. Compact's roles
reach them through fixed groups -- 10100 artifacts, 10103 extension writers -- so the
trees first get that group and the model's modes.
Nothing is copied, moved or re-owned: only the group and the mode of an entry change.

Run by the profile switch alone (step 4), as root in a container of the target image,
before the runtime starts on the trees. It is safe to kill at any point: every entry is
either in the model or not yet, and the tree root's own group and mode -- set last --
record that the walk finished.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from core.infrastructure.extension_runtime.tree_modes import (
    EXTENSION_DIRECTORY_MODE,
    EXTENSION_EXECUTABLE_MODE,
    EXTENSION_FILE_MODE,
)
from core.infrastructure.runtime.compact.volume import CompactVolumeLayout


@dataclass(frozen=True, slots=True)
class TreeModel:
    gid: int
    directory_mode: int
    file_mode: int
    executable_mode: int


def tree_models(layout: CompactVolumeLayout | None = None) -> dict[str, TreeModel]:
    """The group and modes of each shared tree, as the Compact data volume has them."""
    selected = layout or CompactVolumeLayout()
    return {
        "artifacts": TreeModel(selected.artifact_gid, 0o2770, 0o660, 0o770),
        "extensions": TreeModel(
            selected.extension_gid,
            EXTENSION_DIRECTORY_MODE,
            EXTENSION_FILE_MODE,
            EXTENSION_EXECUTABLE_MODE,
        ),
    }


# ADR 0029: a host group with one of the fixed gids is ours when it is named after it --
# the runbook's optional `groupadd -g 10100 flow-steward-artifacts` for host access.
OWN_GROUP_NAMES = {
    "artifacts": "flow-steward-artifacts",
    "extensions": "flow-steward-extensions",
}


class ForeignGroupError(ValueError):
    """A host group already holds a gid the shared trees would be given."""


def refuse_foreign_groups(
    group_file_text: str, trees: Sequence[str], models: Mapping[str, TreeModel]
) -> None:
    """Refuse, before anything changes, a host group that is not ours on a model gid.

    Giving a tree gid 10100 when the host already has a group with that gid gives that
    group's members the tree -- silently. Read from the host's own group file (the
    container's is not the host's); a group known only to a network directory service
    is not seen here, which the runbook says.
    """
    wanted = {models[tree].gid: tree for tree in trees}
    for line in group_file_text.splitlines():
        fields = line.split(":")
        if len(fields) < 3 or not fields[2].strip().isdigit():
            continue
        name, gid = fields[0], int(fields[2])
        tree = wanted.get(gid)
        if tree is not None and name != OWN_GROUP_NAMES[tree]:
            raise ForeignGroupError(
                f"host group {name} already uses gid {gid}, which the {tree} tree would be "
                f"given: its members would read it. Remove or renumber that group, or name "
                f"it {OWN_GROUP_NAMES[tree]} if it is meant for access to the {tree}."
            )


@dataclass
class TreeReport:
    tree: str
    root: str
    gid: int
    directories: int = 0
    files: int = 0
    private: int = 0
    links: int = 0
    other: int = 0
    already: bool = False
    pending: int = 0  # inspect only: entries not in the model yet

    def summary(self) -> str:
        if self.already:
            return f"{self.root} was already in the {self.tree} model (group {self.gid})."
        left = [
            f"{count} {what}"
            for count, what in (
                (self.private, "private entries"),
                (self.links, "symbolic links"),
                (self.other, "special files"),
            )
            if count
        ]
        return (
            f"Normalized {self.directories} directories and {self.files} files under "
            f"{self.root} to group {self.gid}"
            + (f"; left alone: {', '.join(left)}." if left else ".")
        )


def _is_private(mode: int) -> bool:
    return mode & 0o077 == 0


def inspect_tree(root: Path, tree: str, model: TreeModel) -> TreeReport:
    """What normalize_tree would change, changing nothing (`flowsteward doctor`).

    Every entry is looked at -- the root's own group and mode record that a walk once
    finished, not that nothing was added since -- and none is changed.
    """
    report = TreeReport(tree=tree, root=str(root), gid=model.gid)
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError(f"{tree} root is not a real directory: {root}") from exc
    try:
        metadata = os.fstat(root_fd)
        if not _grants_the_model(metadata, model.directory_mode, model):
            report.pending += 1
        for _path, directories, files, walk_fd in os.fwalk(
            ".", dir_fd=root_fd, follow_symlinks=False
        ):
            directories[:] = [
                name
                for name in directories
                if _normalize_entry(name, walk_fd, model, report, change=False)
            ]
            for name in files:
                _normalize_entry(name, walk_fd, model, report, change=False)
    finally:
        os.close(root_fd)
    return report


def normalize_tree(root: Path, tree: str, model: TreeModel, *, full: bool = False) -> TreeReport:
    """Give one tree the model's group and modes; owners, links and private entries stay.

    Walks by descriptor and never follows a symbolic link, so nothing outside the root
    is reached; the root itself must be a real directory. A private entry -- one that
    grants nothing to group or others, such as 0600 or 0700 -- is left as it is, and so
    is everything under a private directory. `full` walks a tree whose root already
    records a finished walk too: doctor repairs what was added after it.
    """
    report = TreeReport(tree=tree, root=str(root), gid=model.gid)
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError(f"{tree} root is not a real directory: {root}") from exc
    try:
        metadata = os.fstat(root_fd)
        if not full and (metadata.st_gid, stat.S_IMODE(metadata.st_mode)) == (
            model.gid,
            model.directory_mode,
        ):
            report.already = True
            return report
        for _path, directories, files, walk_fd in os.fwalk(
            ".", dir_fd=root_fd, follow_symlinks=False
        ):
            # In place: what is private, or a link, is not walked into.
            directories[:] = [
                name for name in directories if _normalize_entry(name, walk_fd, model, report)
            ]
            for name in files:
                _normalize_entry(name, walk_fd, model, report)
        # Last: the record that the walk finished.
        os.fchown(root_fd, -1, model.gid)
        os.fchmod(root_fd, model.directory_mode)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return report


def _grants_the_model(metadata: os.stat_result, target: int, model: TreeModel) -> bool:
    """Whether an entry gives the roles what the model gives them: its group, the group
    rights (and setgid, for a directory). Bits for others are not access here -- the
    tree roots are closed to others -- and an entry root owns is the runtime's own
    control file, not data a role writes (the doctor task: what `doctor` reports must
    be what breaks, or the runtime's own 0664 files are a finding for ever)."""
    if metadata.st_uid == 0:
        return True
    wanted = target & 0o2070
    return metadata.st_gid == model.gid and stat.S_IMODE(metadata.st_mode) & 0o2070 == wanted


def _normalize_entry(
    name: str, walk_fd: int, model: TreeModel, report: TreeReport, *, change: bool = True
) -> bool:
    """Normalize one entry (or, not `change`, count it); whether it is a directory to walk."""
    metadata = os.stat(name, dir_fd=walk_fd, follow_symlinks=False)
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        report.links += 1
        return False
    directory = stat.S_ISDIR(metadata.st_mode)
    if not directory and not stat.S_ISREG(metadata.st_mode):
        report.other += 1
        return False
    if _is_private(mode):
        report.private += 1
        return False
    if directory:
        target = model.directory_mode
    else:
        target = model.executable_mode if mode & 0o111 else model.file_mode
    if not change:
        if not _grants_the_model(metadata, target, model):
            report.pending += 1
        return directory
    if (metadata.st_gid, mode) != (model.gid, target):
        # chown first: it clears the setgid bit chmod then sets.
        os.chown(name, -1, model.gid, dir_fd=walk_fd, follow_symlinks=False)
        os.chmod(name, target, dir_fd=walk_fd, follow_symlinks=False)
        if directory:
            report.directories += 1
        else:
            report.files += 1
    return directory


def normalize_trees(
    roots: Mapping[str, Path],
    *,
    group_file_text: str,
    layout: CompactVolumeLayout | None = None,
    full: bool = False,
) -> list[TreeReport]:
    """Check every tree's gid against the host first, then normalize each."""
    models = tree_models(layout)
    unknown = sorted(set(roots) - set(models))
    if unknown:
        raise ValueError("unknown shared trees: " + ", ".join(unknown))
    refuse_foreign_groups(group_file_text, tuple(roots), models)
    return [
        normalize_tree(Path(root), tree, models[tree], full=full) for tree, root in roots.items()
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host-group-file", type=Path)
    parser.add_argument("--inspect", action="store_true", help="count, change nothing")
    parser.add_argument("--full", action="store_true", help="walk trees already marked done")
    parser.add_argument("trees", nargs="+", help="tree=path, for artifacts, extensions")
    args = parser.parse_args(argv)
    pairs = (item.split("=", 1) for item in args.trees)
    roots = {tree: Path(path) for tree, path in pairs}
    try:
        if args.inspect:
            models = tree_models()
            reports = [inspect_tree(path, tree, models[tree]) for tree, path in roots.items()]
        else:
            if args.host_group_file is None:
                parser.error("--host-group-file is required to normalize")
            reports = normalize_trees(
                roots,
                group_file_text=args.host_group_file.read_text(encoding="utf-8"),
                full=args.full,
            )
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(
        json.dumps(
            {"trees": [asdict(report) | {"summary": report.summary()} for report in reports]}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
