"""Resolve a composite action in one install: which workflow implements it (ADR 0065).

The pairing itself is a property of an immutable release
(``composite_actions.release_composite_actions``); this module reads the install
and its release for an account. Save-time validation, the builder catalog and
the Invoke runtime all resolve through here, so the three never disagree about
which workflow an action stands for.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.composite_actions import (
    CompositeAction,
    release_composite_actions,
    required_dependency_ids,
)

_RELEASE_CACHE_SIZE = 64
_release_cache: OrderedDict[tuple[str, str], tuple[dict[str, CompositeAction], frozenset[str]]] = (
    OrderedDict()
)
_release_lock = threading.Lock()


@dataclass(frozen=True)
class InstalledComposites:
    """The composite actions one install offers an account."""

    install_id: str
    extension_id: str
    release_id: str
    active: bool
    actions: Mapping[str, CompositeAction] = field(default_factory=dict)
    required_dependencies: frozenset[str] = frozenset()
    #: The name a person knows the extension by, as its release declares it.
    extension_name: str = ""

    def action(self, action_id: str) -> CompositeAction | None:
        return self.actions.get(str(action_id or "").strip())


def _text(value: Any) -> str:
    return str(value or "").strip()


def clear_composite_release_cache() -> None:
    """Forget every cached release pairing (tests and release maintenance)."""
    with _release_lock:
        _release_cache.clear()


def _release_composites(release: Any) -> tuple[dict[str, CompositeAction], frozenset[str]]:
    """The pairing of one immutable release, parsed once per release."""
    key = (
        _text(getattr(release, "release_id", "")),
        _text(getattr(release, "descriptor_sha256", "")),
    )
    cacheable = all(key)
    if cacheable:
        with _release_lock:
            cached = _release_cache.get(key)
            if cached is not None:
                _release_cache.move_to_end(key)
                return cached
    descriptor = dict(getattr(release, "descriptor", {}) or {})
    parsed = (release_composite_actions(descriptor), frozenset(required_dependency_ids(descriptor)))
    if cacheable:
        with _release_lock:
            _release_cache[key] = parsed
            _release_cache.move_to_end(key)
            while len(_release_cache) > _RELEASE_CACHE_SIZE:
                _release_cache.popitem(last=False)
    return parsed


def installed_composites(
    connection: Any, *, install_id: str, account_id: str = ""
) -> InstalledComposites | None:
    """The composite actions of *install_id* as *account_id* may use it, or ``None``.

    ``None`` means the account cannot see such an install. An inactive install
    is returned with ``active=False`` so callers can say why it cannot run.
    """
    from core.application.extensions.display_labels import localized_extension_display_name
    from core.application.extensions.install import DBExtensionInstallStore
    from core.application.extensions.runtime_store_db import DBExtensionRuntimeStore

    wanted = _text(install_id)
    if connection is None or not wanted:
        return None
    records = DBExtensionInstallStore(connection).list_for_account(
        _text(account_id) or DEFAULT_ACCOUNT_ID,
        include_global=True,
        include_inactive_owned=True,
    )
    install = next(
        (row for row in records if _text(getattr(row, "install_id", "")) == wanted), None
    )
    if install is None:
        return None
    release_id = _text(getattr(install, "release_id", ""))
    extension_id = _text(getattr(install, "extension_id", ""))
    release = DBExtensionRuntimeStore(connection).get_release(release_id) if release_id else None
    if release is None or _text(getattr(release, "extension_id", "")) != extension_id:
        return None
    actions, dependencies = _release_composites(release)
    state = getattr(install, "state", "active")
    return InstalledComposites(
        install_id=wanted,
        extension_id=extension_id,
        release_id=release_id,
        active=_text(getattr(state, "value", state)).lower() == "active",
        actions=actions,
        required_dependencies=dependencies,
        extension_name=localized_extension_display_name(getattr(release, "descriptor", None)),
    )


def installed_composites_for_store(
    db_store: Any, *, install_id: str, account_id: str = ""
) -> InstalledComposites | None:
    """:func:`installed_composites` through a registry store's own transaction."""
    connection = getattr(db_store, "conn", None)
    if connection is not None:
        return installed_composites(connection, install_id=install_id, account_id=account_id)
    transaction = getattr(db_store, "_transaction", None)
    if not callable(transaction):
        return None
    with transaction() as cur:
        return installed_composites(cur.connection, install_id=install_id, account_id=account_id)


__all__ = [
    "InstalledComposites",
    "clear_composite_release_cache",
    "installed_composites",
    "installed_composites_for_store",
]
