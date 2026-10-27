"""The modes of an installed extension tree, on either profile (ADR 0029, decision B).

Compact and Scalable share one extension tree. Group 10103 is the writers' group --
the extension installer of either profile, and nothing else -- so the other profile's
installer can replace what this one wrote; every other process reads through the
"other" bits (AC17-R59: only the installer writes). Directories carry the setgid bit so
a new entry keeps the tree's group whoever creates it.

One definition for every writer: a freshly installed release, the bundled initial
extensions, their dependency sites, and the one-time normalization of a tree installed
before these modes. A published release is compared with a retried install of the same
archive by a fingerprint that includes the modes, so writers that disagreed would
refuse the retry.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable

EXTENSION_DIRECTORY_MODE = 0o2775
EXTENSION_FILE_MODE = 0o664
EXTENSION_EXECUTABLE_MODE = 0o775


def extension_file_mode(executable: bool) -> int:
    return EXTENSION_EXECUTABLE_MODE if executable else EXTENSION_FILE_MODE


# Docker Desktop's macOS file sharing refuses the setgid bit (EPERM) on a bind-mounted
# extension tree, though plain modes, owners and groups pass. Such a tree is reached
# from one host account, so no other writer needs the inherited group; refusing the
# whole install over it made every archive install fail on Scalable on a Mac.
EXTENSION_DIRECTORY_MODE_WITHOUT_SETGID = EXTENSION_DIRECTORY_MODE & ~stat.S_ISGID


def apply_extension_directory_mode(set_mode: Callable[[int], None]) -> int:
    """Give a directory the tree's mode, without setgid where the filesystem refuses it.

    ``set_mode`` applies one mode to the directory (a chmod, an fchmod, a chmod at a
    descriptor). Returns the mode applied.
    """
    try:
        set_mode(EXTENSION_DIRECTORY_MODE)
    except PermissionError as exc:
        if exc.errno != errno.EPERM:
            raise
        set_mode(EXTENSION_DIRECTORY_MODE_WITHOUT_SETGID)
        return EXTENSION_DIRECTORY_MODE_WITHOUT_SETGID
    return EXTENSION_DIRECTORY_MODE


# AC17-R163: the modes the releases before decision B gave an extension tree. The
# bundled extensions and their dependency sites: 02750 (0750 at the top) directories,
# 0640 files, 0755 executables, readable by 10103 only. An installed release: 0755
# directories, 0644 and 0755 files. Only these are remapped: a mode a writer chose on
# purpose (a 0600 cleanup record, a 0700 staging directory) is not one of them.
_PREVIOUS_DIRECTORY_MODES = frozenset({0o2750, 0o750, 0o755})
_PREVIOUS_FILE_MODES = {
    0o640: EXTENSION_FILE_MODE,
    0o644: EXTENSION_FILE_MODE,
    0o755: EXTENSION_EXECUTABLE_MODE,
}


def normalize_previous_tree(directory_fd: int) -> int:
    """Move what an earlier release wrote below `directory_fd` to these modes.

    Walks by descriptor and never follows a symbolic link, so nothing outside the tree
    is reached, and a link is left untouched. Idempotent: a run killed half-way leaves
    entries either remapped or still in a previous mode, and the next run finishes.
    Owners and groups are not changed. Returns how many entries changed.
    """
    changed = 0
    for _path, directories, files, walk_fd in os.fwalk(
        ".", dir_fd=directory_fd, follow_symlinks=False
    ):
        for name in (*directories, *files):
            metadata = os.stat(name, dir_fd=walk_fd, follow_symlinks=False)
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode) and mode in _PREVIOUS_DIRECTORY_MODES:
                apply_extension_directory_mode(
                    lambda target, name=name, walk_fd=walk_fd: os.chmod(
                        name, target, dir_fd=walk_fd, follow_symlinks=False
                    )
                )
            elif stat.S_ISREG(metadata.st_mode) and mode in _PREVIOUS_FILE_MODES:
                os.chmod(name, _PREVIOUS_FILE_MODES[mode], dir_fd=walk_fd, follow_symlinks=False)
            else:
                continue
            changed += 1
    return changed


def normalize_previous_release(release_fd: int) -> int:
    """`normalize_previous_tree`, and the release's own directory after it."""
    changed = normalize_previous_tree(release_fd)
    if stat.S_IMODE(os.fstat(release_fd).st_mode) in _PREVIOUS_DIRECTORY_MODES:
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 02775 is intentional: ADR 0029 decision B (every process reads, only the installer group writes), AC17-R59.
        apply_extension_directory_mode(lambda target: os.fchmod(release_fd, target))
        changed += 1
    return changed
