"""Catalog icons: one square PNG, the index object that names it, and its cache.

The MCP server catalog and the extension catalog publish the same icon object,
and an extension bundle's own ``icon.png`` is the file such an object points
at, so the rules are written once, here. The catalog generators apply the same
ones before they publish an index:

* PNG only, recognised by its signature. No SVG: an SVG is a document that can
  carry script.
* Square, ``MIN_CATALOG_ICON_PX``-``MAX_CATALOG_ICON_PX`` (32-512) px on a side,
  read from the IHDR chunk. 128x128 is the recommended size.
* At most ``MAX_CATALOG_ICON_BYTES`` (100 KB).

An index names an icon as ``{"url", "sha256", "bytes", "mime_type", "size"}``.
Core fetches the file once through the bounded catalog transport, refuses it
unless its size and SHA-256 match the index and it is a PNG, and keeps the
verified bytes in a small process-local cache keyed by digest, so the browser
only ever loads an icon from the product's own origin.
"""

from __future__ import annotations

import re
import struct
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from core.infrastructure.static_catalog.settings import (
    MAX_CATALOG_ICON_BYTES,
    MAX_CATALOG_ICON_PX,
    MIN_CATALOG_ICON_PX,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ICON_MIME_TYPE = "image/png"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIZE = re.compile(r"^(\d{1,4})x(\d{1,4})$")
_DEFAULT_CACHE_ENTRIES = 256


class IconError(ValueError):
    """An icon file breaks the catalog icon rules."""


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height of a PNG, read from its IHDR chunk."""
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise IconError("is not a PNG file")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def is_png(data: bytes) -> bool:
    return data.startswith(PNG_SIGNATURE)


def _side_problem(width: int, height: int) -> str:
    if width != height or not MIN_CATALOG_ICON_PX <= width <= MAX_CATALOG_ICON_PX:
        return (
            f"must be square, {MIN_CATALOG_ICON_PX}-{MAX_CATALOG_ICON_PX} px on a side "
            f"(is {width}x{height})"
        )
    return ""


def icon_problem(data: bytes) -> str:
    """Why ``data`` is not a valid catalog icon, or '' when it is one."""
    if not data:
        return "is empty"
    if len(data) > MAX_CATALOG_ICON_BYTES:
        return f"is {len(data)} bytes; the limit is {MAX_CATALOG_ICON_BYTES} bytes"
    try:
        width, height = png_size(data)
    except IconError as exc:
        return str(exc)
    return _side_problem(width, height)


@dataclass(frozen=True, slots=True)
class CatalogIcon:
    """The icon object an index publishes for one entry."""

    url: str
    sha256: str
    bytes: int
    size: str


def parse_catalog_icon(raw: Any, where: str) -> CatalogIcon:
    """Validate one index icon object, or raise ``ValueError`` naming the field."""
    if not isinstance(raw, dict):
        raise ValueError(f"{where} is required and must be an object")
    url = str(raw.get("url") or "").strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"{where}.url must be an absolute https URL")
    digest = str(raw.get("sha256") or "").strip().lower()
    if not _SHA256.match(digest):
        raise ValueError(f"{where}.sha256 must be a SHA-256 hex digest")
    size_bytes = raw.get("bytes")
    if (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or not 0 < size_bytes <= MAX_CATALOG_ICON_BYTES
    ):
        raise ValueError(f"{where}.bytes must be 1..{MAX_CATALOG_ICON_BYTES}")
    if str(raw.get("mime_type") or "").strip() != ICON_MIME_TYPE:
        raise ValueError(f"{where}.mime_type must be {ICON_MIME_TYPE}")
    size = str(raw.get("size") or "").strip()
    match = _SIZE.match(size)
    if match is None or _side_problem(int(match.group(1)), int(match.group(2))):
        raise ValueError(
            f"{where}.size must be WxH of a square icon, "
            f"{MIN_CATALOG_ICON_PX}-{MAX_CATALOG_ICON_PX} px on a side"
        )
    return CatalogIcon(url=url, sha256=digest, bytes=size_bytes, size=size)


class CatalogIconCache:
    """Verified icon bytes by digest: fetched once, bounded, never persisted."""

    def __init__(self, max_entries: int = _DEFAULT_CACHE_ENTRIES) -> None:
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._icons: OrderedDict[str, bytes] = OrderedDict()

    def clear(self) -> None:
        with self._lock:
            self._icons.clear()

    def fetch(
        self,
        icon: CatalogIcon,
        *,
        fetch: Callable[..., bytes],
        guard: Any,
        purpose: str,
    ) -> bytes:
        """Return the icon's bytes, fetching and verifying them on first use.

        ``fetch`` is the bounded catalog artifact fetch, which refuses a body
        whose size or SHA-256 differs from the index (``CatalogFetchError``).
        A body that verifies but is not a PNG raises ``IconError``.
        """
        with self._lock:
            cached = self._icons.get(icon.sha256)
            if cached is not None:
                self._icons.move_to_end(icon.sha256)
                return cached
        data = fetch(
            icon.url,
            guard=guard,
            expected_bytes=icon.bytes,
            expected_sha256=icon.sha256,
            max_bytes=MAX_CATALOG_ICON_BYTES,
            purpose=purpose,
        )
        if not is_png(data):
            raise IconError("is not a PNG file")
        with self._lock:
            self._icons[icon.sha256] = data
            while len(self._icons) > self._max_entries:
                self._icons.popitem(last=False)
        return data


__all__ = [
    "ICON_MIME_TYPE",
    "PNG_SIGNATURE",
    "CatalogIcon",
    "CatalogIconCache",
    "IconError",
    "icon_problem",
    "is_png",
    "parse_catalog_icon",
    "png_size",
]
