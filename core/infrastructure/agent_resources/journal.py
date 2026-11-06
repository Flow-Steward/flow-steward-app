"""Atomic per-registry journal storage with a cross-process cleanup lock."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from core.application.tooling.resource_cleanup.models import Ledger, validate_owner


def no_symlinks(path: Path) -> None:
    """Reject aliases rather than resolve an ownership boundary through them."""
    for current in (path, *path.parents):
        if current.is_symlink():
            raise ValueError("symlink in resource or registry path")


def read_json(path: Path) -> dict:
    no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ValueError("journal must be an owned regular file")
        if metadata.st_size > 2_000_000:
            raise ValueError("journal exceeds size limit")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def write_json(path: Path, value: dict) -> None:
    no_symlinks(path)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ResourceJournal:
    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(root))

    @contextmanager
    def locked(self) -> Iterator[None]:
        no_symlinks(self.root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o022:
            raise ValueError("registry must be owned and not writable by other users")
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def path(self, owner: str) -> Path:
        return self.root / f"{validate_owner(owner)}.json"

    def workspace(self, owner: str, token: str) -> Path:
        temporary = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path("/tmp")  # nosec B108 -- deterministic root; every managed ancestor is validated private and owned.
        namespace = hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        return (
            temporary
            / f"flow-steward-agent-resources-{os.getuid()}"
            / namespace
            / f"{validate_owner(owner)}-{token}"
        )

    def load(self, owner: str) -> Ledger:
        ledger = Ledger.from_dict(read_json(self.path(owner)))
        if ledger.owner != owner or ledger.workspace != str(self.workspace(owner, ledger.token)):
            raise ValueError("ledger owner or workspace does not match registry")
        return ledger

    def save(self, ledger: Ledger) -> None:
        write_json(self.path(ledger.owner), ledger.to_dict())

    def peers(self) -> list[Ledger]:
        # Receipts and scratch paths are in subdirectories, never mixed with ledgers.
        return [self.load(p.stem) for p in sorted(self.root.glob("*.json"))]

    def receipt(self, owner: str, payload: dict) -> None:
        root = self.root / "receipts"
        no_symlinks(root)
        root.mkdir(exist_ok=True, mode=0o700)
        write_json(root / f"{validate_owner(owner)}.json", payload)
