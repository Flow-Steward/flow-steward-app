"""Content-addressed cache for static backend test module selections."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

_PRUNED = {
    "assets",
    "logs",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def _modules(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in (root / "tests/unit").rglob("*.py")
        if (path.name.startswith("test_") or path.name.endswith("_test.py"))
        and "stress" not in path.relative_to(root / "tests/unit").parts
    }


def _source_key(root: Path, modules: set[str]) -> str:
    listed = subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "*.py",
        ),
        capture_output=True,
        check=False,
    )
    if listed.returncode == 0:
        paths = {os.fsdecode(path) for path in listed.stdout.split(b"\0") if path}
    else:
        paths = set()
        for directory, folders, files in os.walk(root):
            folders[:] = sorted(folder for folder in folders if folder not in _PRUNED)
            paths.update(
                (Path(directory) / name).relative_to(root).as_posix()
                for name in files
                if name.endswith(".py")
            )
    digest = hashlib.sha256(b"backend-partition-cache-v1\0")
    for name in sorted(paths | modules):
        path = root / name
        target = os.fsencode(os.readlink(path)) if path.is_symlink() else b""
        for value in (os.fsencode(name), target, path.read_bytes()):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def _cache_directory(root: Path) -> Path:
    shared = Path.home() / ".cache/flow-steward-quality"
    directory = (
        shared / "backend-partitions"
        if shared.is_dir() and os.access(shared, os.W_OK)
        else root / "logs/quality/backend-partitions"
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _encoded(partition: dict) -> bytes:
    return json.dumps(partition, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _valid(partition: object, modules: set[str]) -> bool:
    if not isinstance(partition, dict):
        return False
    independent = partition.get("independent")
    contracts = partition.get("frontend_contract")
    if not isinstance(independent, list) or not isinstance(contracts, list):
        return False
    values = independent + contracts
    return (
        all(isinstance(value, str) for value in values)
        and len(values) == len(set(values))
        and set(values) == modules
    )


def cached_partition(root: Path, compute: Callable[[], dict]) -> dict:
    """Reuse only an intact selection covering the current unit modules once."""
    try:
        modules = _modules(root)
        key = _source_key(root, modules)
        directory = _cache_directory(root)
    except (OSError, subprocess.SubprocessError):
        return compute()
    entry = directory / f"{key}.json"
    try:
        record = json.loads(entry.read_text(encoding="utf-8"))
        if (
            isinstance(record, dict)
            and set(record) == {"schema", "key", "checksum", "partition"}
            and record["schema"] == 1
            and record["key"] == key
            and _valid(record["partition"], modules)
            and hashlib.sha256(_encoded(record["partition"])).hexdigest() == record["checksum"]
        ):
            return record["partition"]
    except (OSError, ValueError, TypeError):
        pass
    result = compute()
    temporary: Path | None = None
    try:
        # JSON normalization accepts tuple selections from a fresh computation.
        partition = json.loads(_encoded(result))
        if not _valid(partition, modules):
            return result
        record = {
            "schema": 1,
            "key": key,
            "partition": partition,
            "checksum": hashlib.sha256(_encoded(partition)).hexdigest(),
        }
        descriptor, filename = tempfile.mkstemp(prefix=f".{key}.", suffix=".tmp", dir=directory)
        temporary = Path(filename)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, separators=(",", ":"))
        os.replace(temporary, entry)
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()
    return result
