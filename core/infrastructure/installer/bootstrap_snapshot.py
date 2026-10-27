"""Owner-only durable recovery material for the bounded bootstrap publication."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

from core.infrastructure.installer.state_store import _atomic_write_bytes, _atomic_write_text


def require_safe_path(path: Path, *, allow_leaf_symlink: bool = False) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink() and not (parent == path and allow_leaf_symlink):
            raise ValueError("Bootstrap artifact has an unsafe symlink parent")
    if path.exists() and not path.is_file() and not (allow_leaf_symlink and path.is_symlink()):
        raise ValueError("Bootstrap artifact is not a regular file")
    ancestor = path.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if not os.access(ancestor, os.W_OK) or (
        path.exists() and not path.is_symlink() and not os.access(path, os.W_OK)
    ):
        raise ValueError("Bootstrap artifact is not writable")


class BootstrapSnapshot:
    def __init__(self, path: Path, paths: tuple[Path, ...]) -> None:
        self.path = path
        self.paths = tuple(dict.fromkeys(paths))

    def capture(self, metadata: dict[str, object]) -> None:
        if self.path.exists() or self.path.is_symlink():
            raise ValueError("Bootstrap recovery material already exists")
        require_safe_path(self.path / "manifest.json")
        directories: dict[str, int | None] = {}
        rows = []
        for path in self.paths:
            require_safe_path(path, allow_leaf_symlink=True)
            for parent in path.parents:
                if str(parent) not in directories:
                    directories[str(parent)] = (
                        stat.S_IMODE(parent.stat().st_mode) if parent.exists() else None
                    )
            row: dict[str, object] = {"path": str(path), "kind": "absent"}
            if path.is_symlink():
                row.update(kind="symlink", target=os.readlink(path))
            elif path.exists():
                row.update(
                    kind="file",
                    mode=stat.S_IMODE(path.stat().st_mode),
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            rows.append(row)
        self.path.mkdir(mode=0o700, parents=True)
        self.path.chmod(0o700)
        for index, row in enumerate(rows):
            if row["kind"] == "file":
                _atomic_write_bytes(
                    self.path / f"{index}.bin", self.paths[index].read_bytes(), mode=0o600
                )
        _atomic_write_text(
            self.path / "manifest.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "files": rows,
                    "directories": directories,
                    "metadata": metadata,
                },
                sort_keys=True,
            ),
            mode=0o600,
        )
        self._sync()
        self._sync(parent=True)

    def restore(self) -> dict[str, object]:
        manifest = json.loads((self.path / "manifest.json").read_text())
        rows = manifest["files"]
        if [row["path"] for row in rows] != [str(path) for path in self.paths]:
            raise ValueError("Bootstrap recovery destinations changed")
        for index, (path, row) in enumerate(zip(self.paths, rows, strict=True)):
            require_safe_path(path, allow_leaf_symlink=True)
            if row["kind"] == "file":
                payload = (self.path / f"{index}.bin").read_bytes()
                if hashlib.sha256(payload).hexdigest() != row["sha256"]:
                    raise ValueError("Bootstrap recovery bytes changed")
                _atomic_write_bytes(path, payload, mode=row["mode"])
            elif row["kind"] == "symlink":
                path.unlink(missing_ok=True)
                path.symlink_to(row["target"])
            else:
                path.unlink(missing_ok=True)
        for directory, mode in sorted(
            manifest["directories"].items(), key=lambda item: len(Path(item[0]).parts), reverse=True
        ):
            path = Path(directory)
            if mode is None:
                if path.exists():
                    path.rmdir()
            elif path.exists() and stat.S_IMODE(path.stat().st_mode) != mode:
                path.chmod(mode)
        return manifest["metadata"]

    def finish(self) -> None:
        shutil.rmtree(self.path)
        self._sync(parent=True)

    def _sync(self, *, parent: bool = False) -> None:
        descriptor = os.open(self.path.parent if parent else self.path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
