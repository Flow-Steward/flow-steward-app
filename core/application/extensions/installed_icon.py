"""The logo of an installed extension, read from its bundle's ``icon.png``.

An installed extension lives in its canonical folder under the extensions root
(for an archive install, a link to the immutable release directory), so its
logo is that folder's ``icon.png``. Projections say whether there is one and
give the URL Core serves it from; the URL carries a token of the file's
identity, so a new release's logo is never hidden behind a cached old one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

from core.application.extensions.default_icon import is_default_extension_icon
from core.application.extensions.identity import extension_folder_name
from core.infrastructure.extension_runtime.bundle_icon import (
    EXTENSION_ICON_FILENAME,
    read_extension_icon,
)
from core.infrastructure.static_catalog.settings import MAX_CATALOG_ICON_BYTES


def installed_extension_bundle_dir(extension_id: str) -> Path | None:
    from core.infrastructure.extension_runtime.extension_runtime import default_extensions_root

    try:
        return Path(default_extensions_root()) / extension_folder_name(
            str(extension_id or "").strip()
        )
    except (ValueError, OSError):
        return None


def installed_extension_icon_url(extension_id: str, *, version: str = "") -> str:
    """``/api/extensions/{id}/icon?v=…`` when the bundle has a logo, otherwise ''."""
    bundle_dir = installed_extension_bundle_dir(extension_id)
    if bundle_dir is None:
        return ""
    path = bundle_dir / EXTENSION_ICON_FILENAME
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        stat = path.stat()
    except OSError:
        return ""
    if not 0 < stat.st_size <= MAX_CATALOG_ICON_BYTES:
        return ""
    try:
        if is_default_extension_icon(data=path.read_bytes()):
            # The scaffold's grey glyph is no logo of its own: the UI draws
            # the themed glyph, as for a bundle without an icon.
            return ""
    except OSError:
        return ""
    token = hashlib.sha256(f"{version}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:16]
    return f"/api/extensions/{quote(str(extension_id).strip(), safe='')}/icon?v={token}"


def installed_extension_icon(extension_id: str) -> bytes | None:
    """The installed bundle's logo when it is a valid icon, otherwise None."""
    bundle_dir = installed_extension_bundle_dir(extension_id)
    if bundle_dir is None:
        return None
    try:
        return read_extension_icon(bundle_dir)
    except OSError:
        return None


__all__ = [
    "installed_extension_bundle_dir",
    "installed_extension_icon",
    "installed_extension_icon_url",
]
