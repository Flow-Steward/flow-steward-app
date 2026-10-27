"""Atomic filesystem publication helpers for extension artifacts."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path
from typing import Any

_AT_FDCWD = -100
_DARWIN_RENAME_EXCL = 0x00000004
_LINUX_RENAME_NOREPLACE = 1


def rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename ``source`` while refusing to replace any destination."""
    if os.name == "nt":
        os.rename(source, destination)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    rename_function: Any
    rename_args: tuple[object, ...]

    if sys.platform == "linux":
        try:
            rename_function = libc.renameat2
        except AttributeError as exc:
            raise OSError(errno.ENOSYS, "renameat2 is unavailable") from exc
        rename_function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_args = (
            _AT_FDCWD,
            source_bytes,
            _AT_FDCWD,
            destination_bytes,
            _LINUX_RENAME_NOREPLACE,
        )
    elif sys.platform == "darwin":
        try:
            rename_function = libc.renamex_np
        except AttributeError as exc:
            raise OSError(errno.ENOSYS, "renamex_np is unavailable") from exc
        rename_function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename_args = (source_bytes, destination_bytes, _DARWIN_RENAME_EXCL)
    else:
        raise OSError(
            errno.ENOTSUP,
            f"atomic no-replace rename is unsupported on {sys.platform}",
            os.fspath(destination),
        )

    rename_function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if rename_function(*rename_args) == 0:
        return
    error_number = ctypes.get_errno() or errno.EIO
    raise OSError(error_number, os.strerror(error_number), os.fspath(destination))
