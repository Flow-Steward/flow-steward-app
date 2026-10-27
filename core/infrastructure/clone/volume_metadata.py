"""Apply authenticated Clone metadata inside one isolated volume helper."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from core.application.clone.contracts import CloneDataClass, CloneEntry

_TARGET = Path("/target")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--capture":
        root = Path(sys.argv[2])
        if root != Path("/source"):
            raise ValueError("Clone volume capture root is not allowlisted")
        entries = inventory_volume_metadata(
            root,
            archive_root=sys.argv[3],
            data_class=CloneDataClass(sys.argv[4]),
        )
        json.dump([entry.model_dump(mode="json") for entry in entries], sys.stdout)
        return 0
    raw = json.load(sys.stdin)
    if not isinstance(raw, list):
        raise ValueError("Clone volume metadata must be a list")
    entries = tuple(CloneEntry.model_validate(item) for item in raw)
    if len(sys.argv) == 3 and sys.argv[1] == "--verify":
        root = Path(sys.argv[2])
        if root not in (Path("/source"), Path("/target")):
            raise ValueError("Clone volume verification root is not allowlisted")
        return 0 if volume_metadata_matches(root, entries) else 1
    if len(sys.argv) != 1:
        raise ValueError("Clone volume metadata arguments are invalid")
    apply_volume_metadata(_TARGET, entries)
    return 0


def inventory_volume_metadata(
    root: Path,
    *,
    archive_root: str,
    data_class: CloneDataClass,
    ignored_names: frozenset[str] = frozenset(),
) -> tuple[CloneEntry, ...]:
    if "/" in archive_root or archive_root in {"", ".", ".."}:
        raise ValueError("Clone volume archive root is invalid")
    metadata = root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Clone volume root must be a real directory")
    entries: list[CloneEntry] = []

    def visit(path: Path, relative: str) -> None:
        observed = path.lstat()
        mode = stat.S_IMODE(observed.st_mode)
        entry_path = archive_root if relative == "." else f"{archive_root}/{relative}"
        if stat.S_ISLNK(observed.st_mode):
            entries.append(
                CloneEntry(
                    data_class=data_class,
                    path=entry_path,
                    entry_type="symlink",
                    size=0,
                    mode=mode,
                    uid=observed.st_uid,
                    gid=observed.st_gid,
                    link_target=os.readlink(path),
                )
            )
        elif stat.S_ISDIR(observed.st_mode):
            entries.append(
                CloneEntry(
                    data_class=data_class,
                    path=entry_path,
                    entry_type="directory",
                    size=0,
                    mode=mode,
                    uid=observed.st_uid,
                    gid=observed.st_gid,
                )
            )
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                if child.name in ignored_names:
                    continue
                child_relative = child.name if relative == "." else f"{relative}/{child.name}"
                visit(child, child_relative)
        elif stat.S_ISREG(observed.st_mode):
            digest = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            entries.append(
                CloneEntry(
                    data_class=data_class,
                    path=entry_path,
                    entry_type="file",
                    size=observed.st_size,
                    mode=mode,
                    uid=observed.st_uid,
                    gid=observed.st_gid,
                    sha256=digest.hexdigest(),
                )
            )
        else:
            raise ValueError("Clone volume contains an unsupported entry")

    visit(root, ".")
    return tuple(entries)


def apply_volume_metadata(root: Path, entries: tuple[CloneEntry, ...]) -> None:
    paths = _confined_paths(root, entries)
    for entry in sorted(entries, key=lambda item: item.path.count("/"), reverse=True):
        path = paths[entry.path]
        metadata = path.lstat()
        expected_type = {
            "file": stat.S_ISREG,
            "directory": stat.S_ISDIR,
            "symlink": stat.S_ISLNK,
        }[entry.entry_type]
        if not expected_type(metadata.st_mode):
            raise ValueError("Clone volume entry type does not match authenticated metadata")
        os.chown(path, entry.uid, entry.gid, follow_symlinks=False)
        if entry.entry_type != "symlink":
            path.chmod(entry.mode, follow_symlinks=False)


def volume_metadata_matches(root: Path, entries: tuple[CloneEntry, ...]) -> bool:
    try:
        paths = _confined_paths(root, entries)
    except (OSError, ValueError):
        return False
    for entry in entries:
        metadata = paths[entry.path].lstat()
        expected_type = {
            "file": stat.S_ISREG,
            "directory": stat.S_ISDIR,
            "symlink": stat.S_ISLNK,
        }[entry.entry_type]
        if not expected_type(metadata.st_mode):
            return False
        if (metadata.st_uid, metadata.st_gid) != (entry.uid, entry.gid):
            return False
        if entry.entry_type != "symlink" and stat.S_IMODE(metadata.st_mode) != entry.mode:
            return False
    return True


def _confined_paths(root: Path, entries: tuple[CloneEntry, ...]) -> dict[str, Path]:
    roots = tuple(entry.path for entry in entries if "/" not in entry.path)
    if len(roots) != 1:
        raise ValueError("Clone volume metadata must have exactly one root entry")
    archive_root = roots[0]
    expected = {
        "." if entry.path == archive_root else entry.path.removeprefix(f"{archive_root}/")
        for entry in entries
    }
    observed = {".": root}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        observed[relative] = path
    if set(observed) != expected:
        raise ValueError("Clone volume contents do not match authenticated metadata")
    return {
        entry.path: observed[
            "." if entry.path == archive_root else entry.path.removeprefix(f"{archive_root}/")
        ]
        for entry in entries
    }


if __name__ == "__main__":
    raise SystemExit(main())
