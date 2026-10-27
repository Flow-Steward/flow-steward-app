"""Deterministic worktree snapshots that do not invoke Git content filters."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO, Protocol

_CHUNK_SIZE = 1024 * 1024


class _HashWriter(Protocol):
    def update(self, value: bytes, /) -> None: ...


def _update_field(digest: _HashWriter, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _update_file(digest: _HashWriter, stream: BinaryIO, size: int) -> None:
    digest.update(size.to_bytes(8, "big"))
    while chunk := stream.read(_CHUNK_SIZE):
        digest.update(chunk)


def repository_snapshot(repository_root: Path | str) -> str:
    """Hash tracked and non-ignored untracked worktree state without Git filters."""

    root = Path(repository_root).resolve()
    listed = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
    ).split(b"\0")
    root_bytes = os.fsencode(root)
    digest = hashlib.sha256()
    digest.update(b"flow-steward-repository-snapshot-v1\0")

    for relative_path in sorted(path for path in listed if path):
        _update_field(digest, relative_path)
        absolute_path = os.path.join(root_bytes, relative_path)
        try:
            metadata = os.lstat(absolute_path)
        except FileNotFoundError:
            digest.update(b"missing\0")
            continue

        digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(b"symlink\0")
            _update_field(digest, os.readlink(absolute_path))
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"file\0")
            with open(absolute_path, "rb") as stream:
                _update_file(digest, stream, metadata.st_size)
        else:
            digest.update(b"other\0")
            digest.update(stat.S_IFMT(metadata.st_mode).to_bytes(4, "big"))

    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    args = parser.parse_args(argv)
    print(repository_snapshot(args.repository_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
