"""Measure installed audit-runtime content before application sources are copied."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
from collections.abc import Iterable, Sequence
from pathlib import Path

DEFAULT_ROOTS = tuple(
    Path(value) for value in ("/usr", "/lib", "/lib64", "/etc", "/opt", "/ms-playwright")
)
DEFAULT_OUTPUT = Path("/opt/flow-steward/backend-runtime.sha256")
_CACHES = {".cache", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
_TEMPORARY_IDENTITIES = {
    Path("/etc") / name for name in ("hostname", "hosts", "resolv.conf", "machine-id")
}


def _absolute(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"runtime path must be absolute and normalized: {path}")
    return path


def _entries(
    path: Path, output_directory: Path, *, allow_missing: bool = False
) -> Iterable[tuple[Path, os.stat_result | None]]:
    if (
        path == output_directory
        or output_directory in path.parents
        or _CACHES.intersection(path.parts)
        or path in _TEMPORARY_IDENTITIES
        or path.suffix in {".pyc", ".pyo"}
    ):
        return
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not allow_missing:
            raise
        yield path, None
        return
    yield path, metadata
    if stat.S_ISDIR(metadata.st_mode):
        # scandir failures propagate; symlink directories are never traversed.
        with os.scandir(path) as children:
            names = sorted(entry.name for entry in children)
        for name in names:
            yield from _entries(path / name, output_directory)


def runtime_digest(roots: Iterable[Path] | None = None, *, output: Path = DEFAULT_OUTPUT) -> str:
    """Hash sorted paths, executable bits, symlink targets and streamed file content."""
    selected = sorted(
        {_absolute(Path(root)) for root in (DEFAULT_ROOTS if roots is None else roots)}
    )
    if not selected:
        raise ValueError("runtime receipt requires at least one root")
    output_directory = _absolute(output).parent
    entries = {
        path: metadata
        for root in selected
        for path, metadata in _entries(root, output_directory, allow_missing=roots is None)
    }
    if not entries:
        raise ValueError("runtime receipt has no measured inputs")
    digest = hashlib.sha256(b"flow-steward-runtime-receipt-v1\0")
    for path, metadata in sorted(entries.items()):
        mode = metadata.st_mode if metadata is not None else 0
        if metadata is None:
            kind, content = b"missing", b""
        elif stat.S_ISLNK(mode):
            kind, content = b"link", os.fsencode(os.readlink(path))
        elif stat.S_ISREG(mode):
            with path.open("rb") as stream:
                content = hashlib.file_digest(stream, "sha256").digest()
            kind = b"file"
        elif stat.S_ISDIR(mode):
            kind, content = b"directory", b""
        else:
            raise ValueError(f"unsupported runtime file type: {path}")
        for value in (
            os.fsencode(path.relative_to("/")),
            kind,
            str(mode & 0o111).encode("ascii"),
            content,
        ):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    """Write a content receipt, leaving no stale success receipt after failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        output = _absolute(args.output)
        output.unlink(missing_ok=True)
        identity = runtime_digest(args.root, output=output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(identity + "\n", encoding="ascii")
    except (OSError, ValueError) as error:
        parser.exit(2, f"runtime receipt failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
