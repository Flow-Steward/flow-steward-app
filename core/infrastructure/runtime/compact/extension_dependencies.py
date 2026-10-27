"""Materialize shipped extension dependencies into the mounted Compact volume.

AC17-R120. Scalable runs `core.infrastructure.extension_runtime.bundled_dependencies`
as its own `extension-dependencies` service, and every long-running consumer declares
`condition: service_completed_successfully` on it, so no application or worker starts
until each shipped bundle's pinned wheel closure sits in its own local site. The
Compact overlay never had that step -- not removed, never written -- so the same
bundles arrive on the volume with no `.fs-python`, and enabling one answers HTTP 400
`dependency_unavailable` with its requirements listed as missing.

So the step exists here too, in the one process that owns the volume before anything
else runs. It is fail-closed for the same reason it is fail-closed there: a runtime
that came up with half its extensions unusable is worse than one that refused to come
up, because the first is discovered by a user and the second by the operator starting
it.

The ownership pass is the part Compact needs and Scalable does not. There the service
runs as the same host user as the application, so what pip writes is already readable
by what reads it. Here the bundles are `fs-installer:fs-extensions` with `2750`
directories, and this runs as PID 1, so pip's output would land root-owned inside an
installer-owned tree: readable today by accident of the default mode, unwritable by
the installer process that later re-materializes it, and looser than everything around
it. The site is therefore given the same ownership and modes the bundle extractor
already applies to the bundle itself.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.infrastructure.extension_runtime.bundled_dependencies import (
    materialize_bundled_extension_dependencies,
)
from core.infrastructure.extension_runtime.python_dependencies import PYTHON_DEPS_ROOT_DIRNAME
from core.infrastructure.extension_runtime.tree_modes import (
    EXTENSION_DIRECTORY_MODE,
    EXTENSION_EXECUTABLE_MODE,
    EXTENSION_FILE_MODE,
)
from core.infrastructure.runtime.compact.volume import CompactVolumeLayout

_DIRECTORY_MODE = EXTENSION_DIRECTORY_MODE
_EXECUTABLE_MODE = EXTENSION_EXECUTABLE_MODE
_FILE_MODE = EXTENSION_FILE_MODE


def materialize_compact_extension_dependencies(
    extensions_root: Path,
    layout: CompactVolumeLayout,
) -> tuple[dict[str, Any], ...]:
    """Install every shipped bundle's pinned wheels and hand the site to the extensions group."""
    root = Path(extensions_root)
    if not root.is_dir():
        return ()
    evidence = tuple(materialize_bundled_extension_dependencies(root))
    for bundle in sorted(path for path in root.iterdir() if path.is_dir()):
        _adopt_dependency_site(bundle / PYTHON_DEPS_ROOT_DIRNAME, layout=layout)
    return evidence


def _adopt_dependency_site(site_root: Path, *, layout: CompactVolumeLayout) -> None:
    if not site_root.is_dir() or site_root.is_symlink():
        return
    _secure(site_root, layout=layout, mode=_DIRECTORY_MODE)
    for current, directories, files in os.walk(site_root, followlinks=False):
        base = Path(current)
        for name in directories:
            _secure(base / name, layout=layout, mode=_DIRECTORY_MODE)
        for name in files:
            path = base / name
            if path.is_symlink():
                # Ownership still moves; the mode belongs to the target, and chmod on a
                # symlink is not supported on Linux, so asking would raise rather than
                # protect anything.
                _chown(path, layout=layout)
                continue
            executable = bool(path.stat().st_mode & 0o111)
            _secure(path, layout=layout, mode=_EXECUTABLE_MODE if executable else _FILE_MODE)


def _secure(path: Path, *, layout: CompactVolumeLayout, mode: int) -> None:
    _chown(path, layout=layout)
    os.chmod(path, mode, follow_symlinks=False)


def _chown(path: Path, *, layout: CompactVolumeLayout) -> None:
    metadata = path.lstat()
    owner = (layout.installer_uid, layout.extension_gid)
    if (metadata.st_uid, metadata.st_gid) != owner:
        os.chown(path, *owner, follow_symlinks=False)


__all__ = ["materialize_compact_extension_dependencies"]
