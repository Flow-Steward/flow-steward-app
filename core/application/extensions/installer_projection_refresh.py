"""Refresh local extension consumers after a remote installer commits changes."""

from collections.abc import Callable
from threading import RLock
from typing import Any

from core.infrastructure.extension_runtime.browser_permissions import (
    refresh_browser_permissions_from_stores,
)


def refresh_installer_projections(
    *, get_install_store: Callable[[], Any], get_runtime_store: Callable[[], Any]
) -> None:
    from core.application.registry.registry_manager import RegistryManager

    refresh_browser_permissions_from_stores(
        get_install_store=get_install_store, get_runtime_store=get_runtime_store
    )
    RegistryManager.instance().reload_all()


class InstallerProjectionSync:
    """Each serving process observes committed install changes before serving HTTP."""

    def __init__(
        self, *, get_install_store: Callable[[], Any], refresh: Callable[[], None]
    ) -> None:
        self.get_install_store = get_install_store
        self.refresh = refresh
        self._signature: tuple[tuple[str, str, str], ...] | None = None
        self._lock = RLock()

    def __call__(self) -> None:
        with self._lock:
            installs = self.get_install_store().list_active_installs()
            signature = tuple(
                sorted(
                    (str(item.install_id), str(item.release_id), str(item.state))
                    for item in installs
                )
            )
            if signature != self._signature:
                self.refresh()
                self._signature = signature
