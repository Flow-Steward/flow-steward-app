"""The scaffold's default logo counts as no logo, wherever Core reports one.

A bundle that still ships the grey package glyph has no logo of its own. Core
reports none, so the UI draws the themed glyph it uses for a bundle without an
icon, instead of a fixed grey picture that looks like a different icon.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.application.extensions import installed_icon
from core.application.extensions.default_icon import (
    DEFAULT_EXTENSION_ICON_PATH,
    DEFAULT_EXTENSION_ICON_SHA256,
    is_default_extension_icon,
)

_WAVE = Path(installed_icon.__file__).resolve().parent / "icons" / "flow-steward-icon.png"


def test_the_digest_constant_matches_the_default_icon_file() -> None:
    assert hashlib.sha256(DEFAULT_EXTENSION_ICON_PATH.read_bytes()).hexdigest() == (
        DEFAULT_EXTENSION_ICON_SHA256
    )


def test_the_default_icon_is_recognised_by_bytes_and_by_digest() -> None:
    assert is_default_extension_icon(data=DEFAULT_EXTENSION_ICON_PATH.read_bytes())
    assert is_default_extension_icon(sha256=DEFAULT_EXTENSION_ICON_SHA256.upper())


def test_an_authors_logo_is_not_the_default() -> None:
    assert not is_default_extension_icon(data=_WAVE.read_bytes())
    assert not is_default_extension_icon(sha256="")


def test_an_installed_bundle_with_the_default_icon_reports_no_logo(tmp_path, monkeypatch) -> None:
    bundle = tmp_path / "acme_sample"
    bundle.mkdir()
    monkeypatch.setattr(installed_icon, "installed_extension_bundle_dir", lambda _id: bundle)

    (bundle / "icon.png").write_bytes(DEFAULT_EXTENSION_ICON_PATH.read_bytes())
    assert installed_icon.installed_extension_icon_url("acme.sample") == ""

    (bundle / "icon.png").write_bytes(_WAVE.read_bytes())
    assert installed_icon.installed_extension_icon_url("acme.sample").startswith(
        "/api/extensions/acme.sample/icon?v="
    )


def test_the_flow_steward_logo_has_a_transparent_background() -> None:
    """The wave is drawn on transparency: no opaque black box behind it."""
    data = _WAVE.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    # Colour type 6 (RGBA) in the IHDR chunk: the logo carries an alpha channel.
    assert data[25] == 6
