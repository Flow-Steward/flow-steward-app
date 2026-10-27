"""Human extension names from the exact release descriptor and its dictionaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.extensions.extension_locales import resolve_locale_text


def descriptor_display_name(descriptor: Any) -> str:
    """Use the installed-list declaration precedence, without inventing an ID label."""
    source = descriptor if isinstance(descriptor, Mapping) else {}
    identity = source.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    for value in (identity.get("display_name"), source.get("display_name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def localized_extension_display_name(descriptor: Any, *, locale: str = "") -> str:
    """The requested dictionary, English, then this release's declared name."""
    source = descriptor if isinstance(descriptor, Mapping) else {}
    bundles = source.get("locale_bundles")
    return resolve_locale_text(
        bundles if isinstance(bundles, Mapping) else None,
        locale,
        "ext.display_name",
        descriptor_display_name(source),
    )
