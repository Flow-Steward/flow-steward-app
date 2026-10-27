"""Initialize only the installed Clone namespace, never its state ancestors."""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack, suppress
from pathlib import Path


def initialize_clone_namespace(workspace_root: Path) -> None:
    """Narrow owned real Clone prefixes through no-follow directory descriptors."""
    root = Path(workspace_root)
    if root.name == "workspace" and root.parent.name == "clone":
        anchor, names = root.parent.parent, ("clone", "workspace")
    elif root.name == "clone":
        anchor, names = root.parent, ("clone",)
    else:
        raise ValueError("Clone workspace is outside the canonical private namespace")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        parent = os.open(anchor, flags)
        stack.callback(os.close, parent)
        anchor_metadata = os.fstat(parent)
        if not stat.S_ISDIR(anchor_metadata.st_mode) or anchor_metadata.st_uid != os.getuid():
            raise RuntimeError("Clone state anchor must be owned by the current user")
        descriptors: list[int] = []
        for name in names:
            with suppress(FileExistsError):
                os.mkdir(name, mode=0o700, dir_fd=parent)
            try:
                descriptor = os.open(name, flags, dir_fd=parent)
            except OSError as exc:
                raise RuntimeError("Clone namespace must contain only real directories") from exc
            stack.callback(os.close, descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise RuntimeError("Clone namespace must be owned by the current user")
            descriptors.append(descriptor)
            parent = descriptor
        # Restore leaves use mkdir(parents=True); explicitly own their shared
        # prefixes before runtime umask 0002 can create them group-readable.
        for name in ("prepared", "restore-evidence"):
            with suppress(FileExistsError):
                os.mkdir(name, mode=0o700, dir_fd=parent)
            try:
                descriptor = os.open(name, flags, dir_fd=parent)
            except OSError as exc:
                raise RuntimeError("Clone namespace must contain only real directories") from exc
            stack.callback(os.close, descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise RuntimeError("Clone namespace must be owned by the current user")
            descriptors.append(descriptor)
        # Validate every prefix before repairing any pre-existing prefix mode.
        for descriptor in descriptors:
            # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- validated owned O_DIRECTORY descriptor needs owner-only traversal.
            os.fchmod(descriptor, 0o700)
