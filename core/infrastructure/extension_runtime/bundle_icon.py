"""An extension bundle's logo: ``icon.png`` next to ``extension.yaml``.

The file name is the contract; the manifest has no icon field. A developer
changes the logo by replacing this one file. It follows the catalog icon rules
(``core/infrastructure/static_catalog/icons.py``) because the same file is what
the extension catalog later publishes as the entry's icon: a PNG, square,
32-512 px on a side, at most 100 KB, 128x128 recommended.

``extensions validate``, ``extensions bundle`` and the archive installer refuse
a bundle without a valid one; ``extension_icon_issues`` is the check they share.
"""

from __future__ import annotations

from pathlib import Path

from core.infrastructure.static_catalog.icons import icon_problem
from core.infrastructure.static_catalog.settings import (
    MAX_CATALOG_ICON_BYTES,
    MAX_CATALOG_ICON_PX,
    MIN_CATALOG_ICON_PX,
)

from .extension_runtime_validation import _issue

EXTENSION_ICON_FILENAME = "icon.png"

EXTENSION_ICON_HINT = (
    f"Put the extension's logo next to extension.yaml as {EXTENSION_ICON_FILENAME}: "
    f"a square PNG, {MIN_CATALOG_ICON_PX}-{MAX_CATALOG_ICON_PX} px on a side "
    f"(128x128 recommended), at most {MAX_CATALOG_ICON_BYTES // 1024} KB. "
    "To change the logo, replace that file."
)


def _icon_issue(path: Path, *, code: str, message: str) -> dict:
    return _issue(
        severity="error",
        code=code,
        message=message,
        file_path=str(path),
        field_path=EXTENSION_ICON_FILENAME,
        hint=EXTENSION_ICON_HINT,
    )


def extension_icon_issues(bundle_root: str | Path) -> list[dict]:
    """The errors that keep ``bundle_root/icon.png`` from being the bundle's logo."""
    path = Path(bundle_root) / EXTENSION_ICON_FILENAME
    if path.is_symlink():
        return [
            _icon_issue(
                path,
                code="extension_icon_invalid",
                message=f"{EXTENSION_ICON_FILENAME} must be a regular file, not a link",
            )
        ]
    if not path.exists():
        return [
            _icon_issue(
                path,
                code="extension_icon_missing",
                message=(
                    f"{EXTENSION_ICON_FILENAME} is missing: every extension ships its logo "
                    "as a square PNG next to extension.yaml"
                ),
            )
        ]
    if not path.is_file():
        return [
            _icon_issue(
                path,
                code="extension_icon_invalid",
                message=f"{EXTENSION_ICON_FILENAME} must be a regular file",
            )
        ]
    size = path.stat().st_size
    if size > MAX_CATALOG_ICON_BYTES:
        problem = f"is {size} bytes; the limit is {MAX_CATALOG_ICON_BYTES} bytes"
    else:
        problem = icon_problem(path.read_bytes())
    if problem:
        return [
            _icon_issue(
                path,
                code="extension_icon_invalid",
                message=f"{EXTENSION_ICON_FILENAME} {problem}",
            )
        ]
    return []


def read_extension_icon(bundle_root: str | Path) -> bytes | None:
    """The bundle's logo when it is a valid icon, otherwise None."""
    if extension_icon_issues(bundle_root):
        return None
    return (Path(bundle_root) / EXTENSION_ICON_FILENAME).read_bytes()


__all__ = [
    "EXTENSION_ICON_FILENAME",
    "EXTENSION_ICON_HINT",
    "extension_icon_issues",
    "read_extension_icon",
]
