"""The MCP Apps viewer's own strings equal the app catalog's.

The viewer runs in a host's sandboxed frame and cannot load the catalog, so it
carries its strings in viewer-labels.json; this keeps them from drifting.
"""

from __future__ import annotations

import json
from pathlib import Path

from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parents[3]
LABELS = ROOT / "assets/frontend/src/report-viewer/viewer-labels.json"


def _catalog(locale: str) -> dict[str, str]:
    with (ROOT / "locales" / locale / "LC_MESSAGES" / "ui.po").open("rb") as stream:
        return {str(message.id): str(message.string) for message in read_po(stream) if message.id}


def test_viewer_labels_equal_the_catalog_for_every_locale() -> None:
    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    keys = labels["keys"]
    for locale, texts in labels["locales"].items():
        catalog = _catalog(locale)
        assert set(texts) == set(keys), locale
        for name, key in keys.items():
            assert texts[name] == catalog[key], (locale, name, key)
