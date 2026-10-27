"""The scaffold's default logo, and how to tell it from an author's own.

``flow-steward extensions create`` writes this grey package glyph as the bundle's
required ``icon.png``. A bundle that still ships it has no logo of its own, so
Core reports no logo and the UI draws the same package glyph in the theme's
colour, exactly as it does for an extension installed without any icon. A fixed
grey picture next to themed glyphs looked like a different, brighter icon.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

DEFAULT_EXTENSION_ICON_PATH = Path(__file__).resolve().parent / "icons" / "default-icon.png"


#: SHA-256 of ``icons/default-icon.png``. A constant, not read from the file at
#: run time: the worker and control images carry this module but not the PNG.
#: tests/unit/test_extension_default_icon.py keeps the two in step.
DEFAULT_EXTENSION_ICON_SHA256 = (
    "f3aa3c7b2856a53c358d0dfc2acae8e3fe1e53954a12ebb6639a1fe07eac7e2a"  # pragma: allowlist secret
)


def default_extension_icon_sha256() -> str:
    """The SHA-256 of the scaffold's default logo."""
    return DEFAULT_EXTENSION_ICON_SHA256


def is_default_extension_icon(*, data: bytes | None = None, sha256: str = "") -> bool:
    """Whether a logo, by its bytes or its digest, is the scaffold's default."""
    digest = hashlib.sha256(data).hexdigest() if data is not None else sha256.strip().lower()
    return bool(digest) and digest == default_extension_icon_sha256()


__all__ = [
    "DEFAULT_EXTENSION_ICON_PATH",
    "DEFAULT_EXTENSION_ICON_SHA256",
    "default_extension_icon_sha256",
    "is_default_extension_icon",
]
