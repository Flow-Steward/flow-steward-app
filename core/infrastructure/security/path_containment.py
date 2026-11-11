"""Paths that must stay inside one fixed directory.

A path built from a value the caller does not control (a request parameter, a
manifest reference, a command argument) is joined to its root here, normalised,
and refused unless it lies strictly inside that root, before anything touches
the file system. The check is the plain normalise-then-prefix shape, so the
reader and the static analysis both see it next to the path it guards.
"""

from __future__ import annotations

import os
from pathlib import Path


class PathEscapeError(ValueError):
    """A path resolved outside the directory it must stay in."""


def contained_path(
    root: str | os.PathLike[str],
    *parts: str | os.PathLike[str],
    follow_symlinks: bool = True,
) -> Path:
    """``root`` joined with ``parts``, refused unless it lies strictly inside ``root``.

    With ``follow_symlinks`` (the default) both sides are real paths, so a link
    inside the root that leads out of it is refused too. Without it, both are
    only made absolute and normalised: for a folder that is itself a managed
    symlink, where the caller needs the link and not its target.

    An absolute part replaces the root in the join and is then refused unless
    it is inside the root anyway. The root itself is never returned.
    """
    if follow_symlinks:
        base = os.path.realpath(root)
        candidate = os.path.realpath(os.path.join(base, *parts))
    else:
        base = os.path.abspath(root)
        candidate = os.path.abspath(os.path.join(base, *parts))
    prefix = base if base.endswith(os.sep) else base + os.sep
    if not candidate.startswith(prefix):
        raise PathEscapeError("Path escapes its root directory")
    return Path(candidate)


__all__ = ["PathEscapeError", "contained_path"]
