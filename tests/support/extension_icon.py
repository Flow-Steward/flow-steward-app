"""A valid ``icon.png`` for the extension bundles tests build.

Every bundle ships its logo next to ``extension.yaml``; validation, packaging
and install refuse one without it. Tests that build a bundle write this one,
the Flow Steward wave mark Core keeps for first-party extensions.
"""

from __future__ import annotations

from pathlib import Path

EXTENSION_ICON_FILENAME = "icon.png"
EXTENSION_ICON_PNG = (
    Path(__file__).resolve().parents[2]
    / "core"
    / "application"
    / "extensions"
    / "icons"
    / "flow-steward-icon.png"
).read_bytes()


def write_extension_icon(bundle_dir: Path) -> Path:
    """Write the logo into ``bundle_dir`` and return its path."""
    path = Path(bundle_dir) / EXTENSION_ICON_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(EXTENSION_ICON_PNG)
    return path


def png_bytes(width: int, height: int, *, padding: int = 0) -> bytes:
    """A minimal PNG header of the given size: enough for the IHDR checks."""
    import struct
    import zlib

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
        + b"\x00" * padding
    )
