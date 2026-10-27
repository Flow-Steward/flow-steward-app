"""A default extension store one test caches never reaches the next test.

``service_support`` caches the default install and runtime store per process.
A service built with a logger that has a connection caches a DB-backed store
on that connection; a later service built without a logger then gets that
store back. When the connection was a test double, the later test reads rows
from it and fails depending on which test ran before it. The unit conftest
resets both caches around every test.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.application.extensions import service_support
from core.application.extensions.install import DBExtensionInstallStore, ExtensionInstallStore
from core.application.extensions.runtime_store import (
    DBExtensionRuntimeStore,
    ExtensionRuntimeStore,
)

# The second test proves what the first one left behind, so both run on one
# worker, in this order.
pytestmark = pytest.mark.xdist_group("default_extension_store_isolation")


def test_a_service_given_a_connection_caches_db_backed_default_stores() -> None:
    logger = MagicMock()

    install_store = service_support.get_default_extension_install_store(get_logger=lambda: logger)
    runtime_store = service_support.get_default_extension_runtime_store(get_logger=lambda: logger)

    assert isinstance(install_store, DBExtensionInstallStore)
    assert isinstance(runtime_store, DBExtensionRuntimeStore)
    assert service_support.get_default_extension_install_store() is install_store


def test_the_next_test_starts_from_in_memory_default_stores() -> None:
    assert type(service_support.get_default_extension_install_store()) is ExtensionInstallStore
    assert type(service_support.get_default_extension_runtime_store()) is ExtensionRuntimeStore
