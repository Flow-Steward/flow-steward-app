"""Human extension names from the exact release descriptor and its dictionaries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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


def installed_extension_names(
    connection: Any, *, account_id: str, extension_ids: Iterable[str], locale: str = ""
) -> dict[str, str]:
    """The names people know *extension_ids* by, from the releases the account's installs pin.

    An extension the account cannot see, or one whose release declares no name,
    is left out: the caller keeps showing whatever it had.
    """
    from core.application.extensions.install import DBExtensionInstallStore
    from core.application.extensions.runtime_store_db import DBExtensionRuntimeStore

    wanted = {str(value or "").strip() for value in extension_ids} - {""}
    if connection is None or not wanted:
        return {}
    releases = DBExtensionRuntimeStore(connection)
    names: dict[str, str] = {}
    records = DBExtensionInstallStore(connection).list_for_account(
        account_id, include_global=True, include_inactive_owned=True
    )
    for record in records:
        extension_id = str(getattr(record, "extension_id", "") or "").strip()
        release_id = str(getattr(record, "release_id", "") or "").strip()
        if extension_id not in wanted or extension_id in names or not release_id:
            continue
        release = releases.get_release(release_id)
        name = localized_extension_display_name(getattr(release, "descriptor", None), locale=locale)
        if name:
            names[extension_id] = name
    return names
