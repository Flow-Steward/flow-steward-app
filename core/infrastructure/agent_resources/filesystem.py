"""Deletion restricted to newly created, marked task scratch directories."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from core.application.tooling.resource_cleanup.models import Ledger, Resource
from core.infrastructure.agent_resources.journal import no_symlinks, read_json, write_json

MARKER = ".flow-steward-resource-owner.json"


def capture(path: Path) -> dict[str, str]:
    no_symlinks(path)
    if not path.is_dir():
        raise ValueError("resource is not a directory")
    st = path.stat()
    return {"device": str(st.st_dev), "inode": str(st.st_ino)}


def create(ledger: Ledger, name: str) -> tuple[str, dict[str, str]]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name) or name in {".", ".."}:
        raise ValueError("directory name must be a single safe component")
    root = Path(ledger.workspace)
    no_symlinks(root)
    for directory in (root.parent.parent, root.parent, root):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = directory.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise ValueError("workspace must be private and owned")
    if (root / MARKER).exists():
        if read_json(root / MARKER) != {"owner": ledger.owner, "token": ledger.token}:
            raise ValueError("workspace marker changed")
    else:
        if any(root.iterdir()):
            raise ValueError("workspace contains unregistered files")
        write_json(root / MARKER, {"owner": ledger.owner, "token": ledger.token})
    path = root / name
    path.mkdir(mode=0o700)
    write_json(path / MARKER, {"owner": ledger.owner, "token": ledger.token})
    return str(path), capture(path)


def validate(ledger: Ledger, resource: Resource) -> None:
    path = Path(resource.identity)
    root = Path(ledger.workspace)
    if not path.is_absolute() or path.parent != root:
        raise ValueError("directory is outside its exclusive task workspace")
    if capture(path) != resource.proof:
        raise ValueError("directory identity changed")
    if read_json(path / MARKER) != {"owner": ledger.owner, "token": ledger.token}:
        raise ValueError("directory owner marker changed")
    for parent, directories, files in os.walk(path, followlinks=False):
        if ".git" in directories or ".git" in files:
            raise ValueError(
                "Git checkout/worktree requires verified archival outside automatic cleanup"
            )
        if MARKER in files and Path(parent) != path:
            raise ValueError("nested independently owned directory")


def remove(ledger: Ledger, resource: Resource) -> None:
    validate(ledger, resource)
    path = Path(resource.identity)
    # fd-relative rmtree does not traverse symlink replacements on supported hosts.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError("host lacks symlink-safe directory removal")
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        st = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        if {"device": str(st.st_dev), "inode": str(st.st_ino)} != resource.proof:
            raise ValueError("directory identity changed before deletion")
        shutil.rmtree(path.name, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def finalize_workspace(ledger: Ledger) -> None:
    root = Path(ledger.workspace)
    no_symlinks(root)
    if not root.exists():
        return
    if read_json(root / MARKER) != {"owner": ledger.owner, "token": ledger.token}:
        raise ValueError("workspace owner marker changed")
    if {p.name for p in root.iterdir()} != {MARKER}:
        raise ValueError("workspace has unregistered residue; retained for review")
    (root / MARKER).unlink()
    root.rmdir()
