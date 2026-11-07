"""The two Microsoft 365 agent icons the AI agents page packs (ADR 0063).

Microsoft wants a 192x192 colour icon and a 32x32 outline icon that is white on a
transparent background. Both are rendered once from the canonical brand mark
(``docs/agent-skills/flow-steward-operator/assets/icon.svg``) and shipped as web UI
assets; these checks keep a replaced file within those rules.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

BRAND = Path(__file__).resolve().parents[2] / "assets/frontend/src/design-system/assets/brand"
COLOR = BRAND / "microsoft-365-color-192.png"
OUTLINE = BRAND / "microsoft-365-outline-32.png"


def _paeth(left: int, up: int, corner: int) -> int:
    estimate = left + up - corner
    distances = (abs(estimate - left), abs(estimate - up), abs(estimate - corner))
    return (left, up, corner)[distances.index(min(distances))]


def _decode_rgba(path: Path) -> tuple[int, int, list[bytes]]:
    """Width, height and unfiltered RGBA rows of a non-interlaced 8-bit RGBA PNG."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    position, chunks = 8, {}
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8].decode("ascii")
        chunks[kind] = chunks.get(kind, b"") + data[position + 8 : position + 8 + length]
        position += 12 + length
    width, height, depth, color_type, _c, _f, interlace = struct.unpack(">IIBBBBB", chunks["IHDR"])
    assert (depth, color_type, interlace) == (8, 6, 0), path.name
    raw = zlib.decompress(chunks["IDAT"])
    stride, rows, previous = width * 4, [], bytes(width * 4)
    for index in range(height):
        start = index * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1 : start + 1 + stride])
        for x in range(stride):
            left = line[x - 4] if x >= 4 else 0
            up = previous[x]
            corner = previous[x - 4] if x >= 4 else 0
            line[x] = (
                line[x] + (0, left, up, (left + up) // 2, _paeth(left, up, corner))[kind]
            ) & 0xFF
        rows.append(bytes(line))
        previous = bytes(line)
    return width, height, rows


def test_the_colour_icon_is_192_square_and_opaque() -> None:
    width, height, rows = _decode_rgba(COLOR)
    assert (width, height) == (192, 192)
    assert {row[x + 3] for row in rows for x in range(0, len(row), 4)} == {255}


def test_the_outline_icon_is_32_square_white_on_transparent() -> None:
    width, height, rows = _decode_rgba(OUTLINE)
    assert (width, height) == (32, 32)
    pixels = [row[x : x + 4] for row in rows for x in range(0, len(row), 4)]
    marked = [pixel for pixel in pixels if pixel[3]]
    assert all(pixel[:3] == b"\xff\xff\xff" for pixel in marked)
    assert pixels[0][3] == 0
    assert 0.05 * len(pixels) < len(marked) < 0.6 * len(pixels)


@pytest.mark.parametrize("path", [COLOR, OUTLINE], ids=lambda path: path.name)
def test_the_icons_stay_small(path: Path) -> None:
    assert path.stat().st_size < 16_384
