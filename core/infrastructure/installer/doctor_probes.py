"""Read-only facts `./flow-steward doctor` needs that no other adapter has.

Plain doctor takes no lock and writes nothing (the doctor task, condition 1 and 3), so
these look without touching: a flock is found in /proc/locks, never by trying to take
it -- even a momentary try would make an operation starting that instant fail -- and
modes are read with lstat, never through a link.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

_PROC_LOCKS = Path("/proc/locks")


def flock_held(path: Path, *, proc_locks: Path = _PROC_LOCKS) -> bool | None:
    """Whether some process holds a flock on `path`; None when this host cannot tell.

    /proc/locks names each lock by device and inode ("fd:01:1234"), in hex for the
    device. Absent file: nobody can hold it.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    try:
        lines = proc_locks.read_text(encoding="ascii", errors="replace").splitlines()
    except OSError:
        return None
    wanted = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    for line in lines:
        fields = line.split()
        if "FLOCK" not in fields or "->" in fields:  # "->" marks a waiter, not a holder
            continue
        for field in fields:
            parts = field.split(":")
            if len(parts) != 3:
                continue
            try:
                identity = (int(parts[0], 16), int(parts[1], 16), int(parts[2]))
            except ValueError:
                continue
            if identity == wanted:
                return True
    return False


@dataclass(frozen=True, slots=True)
class WideEntry:
    """A secret or state entry that others can read or write."""

    path: Path
    mode: int
    is_dir: bool
    owned: bool  # by this process's user, so it can be narrowed without privileges

    @property
    def wanted_mode(self) -> int:
        return stat.S_IMODE(self.mode) & 0o700

    def words(self) -> str:
        return f"{self.path} is {stat.S_IMODE(self.mode):04o}, wanted {self.wanted_mode:04o}"


def wide_entries(
    root: Path, *, exempt: frozenset[str] = frozenset(), include_root: bool = True
) -> list[WideEntry]:
    """Every entry under `root` with group or other bits; links are neither followed nor
    reported (a link's own mode means nothing). `exempt` names top-level entries that
    hold nothing secret."""
    found: list[WideEntry] = []
    uid = os.getuid()

    def visit(path: Path, *, top: bool) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            return
        is_dir = stat.S_ISDIR(info.st_mode)
        if (not top or include_root) and stat.S_IMODE(info.st_mode) & 0o077:
            found.append(WideEntry(path, info.st_mode, is_dir, info.st_uid == uid))
        if not is_dir:
            return
        try:
            children = sorted(path.iterdir())
        except PermissionError:
            return
        for child in children:
            if top and child.name in exempt:
                continue
            visit(child, top=False)

    visit(root, top=True)
    return found


def narrow(entry: WideEntry) -> None:
    """Take group and other bits away, through the entry itself: a link put in its place
    since it was looked at is refused (O_NOFOLLOW), never followed."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(entry.path, flags)
    try:
        os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) & 0o700)
    finally:
        os.close(fd)
