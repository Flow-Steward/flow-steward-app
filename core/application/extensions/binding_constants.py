"""Constants shared by extension binding lifecycle components."""

from __future__ import annotations

import re

MAX_EXTENSION_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES = 150 * 1024 * 1024
_MANAGED_RELEASE_INSTALL_METADATA_FILENAME = ".fs-extension-install.yaml"
_EXTENSION_STORE_UNSET = object()
_MANAGED_EXTENSION_STAGE_NAME_RE = re.compile(
    r"^\.[a-z0-9_]+\.(?:install|update|migration)-staged-[0-9a-f]{8}$"
)


def archive_too_large_message() -> str:
    return f"Extension archive is too large — maximum {MAX_EXTENSION_ARCHIVE_BYTES} bytes"


__all__ = [
    "MAX_EXTENSION_ARCHIVE_BYTES",
    "MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES",
    "_EXTENSION_STORE_UNSET",
    "_MANAGED_EXTENSION_STAGE_NAME_RE",
    "_MANAGED_RELEASE_INSTALL_METADATA_FILENAME",
    "archive_too_large_message",
]
