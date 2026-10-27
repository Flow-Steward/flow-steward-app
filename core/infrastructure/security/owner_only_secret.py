"""Strict bounded reader for deployment-local owner-only secret inputs."""

from __future__ import annotations

import os
import stat
from pathlib import Path

OWNER_ONLY_REGULAR_FILE_REQUIRED = "Secret input must be an owner-only regular file"


def read_owner_only_secret(
    path: Path,
    *,
    maximum_bytes: int = 4096,
    optional: bool = False,
) -> bytes | None:
    """Read a regular 0600 file beneath a real owner-only 0700 directory."""
    path = path.absolute()
    if optional and not path.exists() and not path.is_symlink():
        return None
    try:
        parent = path.parent
        parent_stat = parent.lstat()
        file_stat = path.lstat()
        if parent.is_symlink() or not stat.S_ISDIR(parent_stat.st_mode):
            raise ValueError("Secret input directory must be an owner-only directory")
        if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(OWNER_ONLY_REGULAR_FILE_REQUIRED)
        if os.name != "nt":
            owner = os.getuid()
            if parent_stat.st_uid != owner or stat.S_IMODE(parent_stat.st_mode) != 0o700:
                raise ValueError("Secret input directory must be owner-only mode 0700")
            if file_stat.st_uid != owner or stat.S_IMODE(file_stat.st_mode) != 0o600:
                raise ValueError("Secret input file must be owner-only mode 0600")
        if file_stat.st_size > maximum_bytes:
            raise ValueError(f"Secret input exceeds the maximum size of {maximum_bytes} bytes")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened_stat = os.fstat(descriptor)
            if (opened_stat.st_dev, opened_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino):
                raise ValueError("Secret input changed while it was being validated")
            value = os.read(descriptor, maximum_bytes + 1)
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        if optional:
            return None
        raise ValueError(OWNER_ONLY_REGULAR_FILE_REQUIRED) from None
    except OSError as exc:
        raise ValueError(OWNER_ONLY_REGULAR_FILE_REQUIRED) from exc
    if len(value) > maximum_bytes:
        raise ValueError(f"Secret input exceeds the maximum size of {maximum_bytes} bytes")
    if value.endswith(b"\r\n"):
        return value[:-2]
    if value.endswith((b"\n", b"\r")):
        return value[:-1]
    return value


__all__ = ["read_owner_only_secret"]
