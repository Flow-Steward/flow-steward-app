from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import sentinel

import pytest

from core.application.extensions.command_core import (
    ExtensionCommandCore as ExtensionCommandCoreMixin,
)
from core.application.extensions.command_install import (
    ExtensionCommandInstall as ExtensionCommandInstallMixin,
)
from core.application.extensions.command_lifecycle import (
    ExtensionCommandLifecycle as ExtensionCommandLifecycleMixin,
)
from core.application.extensions.command_service import ExtensionCommandService
from core.application.management.errors import ServiceConflictError, ServiceUnavailableError
from core.infrastructure.extension_runtime.lifecycle_lock import extension_lifecycle_lock
from tests.unit.test_extension_api import _make_extension_api_client

_LIFECYCLE_MUTATORS = (
    ("import_descriptor_file", ExtensionCommandCoreMixin, (sentinel.input,), {}),
    ("import_descriptor_url", ExtensionCommandCoreMixin, (sentinel.input,), {}),
    ("install_release", ExtensionCommandInstallMixin, (sentinel.input,), {}),
    (
        "run_install_handshake",
        ExtensionCommandInstallMixin,
        (),
        {"install_id": "pi_fixture"},
    ),
    ("uninstall_extension", ExtensionCommandLifecycleMixin, ("acme.fixture",), {}),
    ("enable_extension", ExtensionCommandLifecycleMixin, ("acme.fixture",), {}),
    ("disable_extension", ExtensionCommandLifecycleMixin, ("acme.fixture",), {}),
)


@pytest.mark.parametrize(
    ("method_name", "owner", "args", "kwargs"),
    _LIFECYCLE_MUTATORS,
    ids=[row[0] for row in _LIFECYCLE_MUTATORS],
)
def test_raw_command_lifecycle_mutators_hold_global_gate_before_delegating(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    owner: type,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    connection = object()
    events: list[tuple[str, object]] = []

    @contextmanager
    def _tracked_lock(*, connection: object) -> Iterator[bool]:
        events.append(("lock_enter", connection))
        try:
            yield True
        finally:
            events.append(("lock_exit", connection))

    def _delegate(_self: object, *_args: object, **_kwargs: object) -> object:
        events.append(("delegate", method_name))
        return sentinel.result

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.lifecycle_lock.extension_lifecycle_lock",
        _tracked_lock,
    )
    monkeypatch.setattr(owner, method_name, _delegate)
    service = ExtensionCommandService(get_logger=lambda: SimpleNamespace(conn=connection))

    assert getattr(service, method_name)(*args, **kwargs) is sentinel.result
    assert events == [
        ("lock_enter", connection),
        ("delegate", method_name),
        ("lock_exit", connection),
    ]


def test_raw_command_lifecycle_mutation_refuses_an_unavailable_global_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def _unavailable_lock(*, connection: object) -> Iterator[bool]:
        del connection
        yield False

    def _must_not_delegate(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("mutation reached its persistence implementation")

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.lifecycle_lock.extension_lifecycle_lock",
        _unavailable_lock,
    )
    monkeypatch.setattr(
        ExtensionCommandInstallMixin,
        "install_release",
        _must_not_delegate,
    )
    service = ExtensionCommandService(get_logger=lambda: SimpleNamespace(conn=object()))

    with pytest.raises(ServiceConflictError, match="lifecycle operation"):
        service.install_release(sentinel.input)


def test_raw_command_lifecycle_gate_pins_a_recovered_logger_for_the_complete_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = object()
    recovered_logger = SimpleNamespace(conn=connection)
    logger_calls = 0
    locked_connections: list[object] = []

    def _get_logger() -> object | None:
        nonlocal logger_calls
        logger_calls += 1
        return None if logger_calls == 1 else recovered_logger

    @contextmanager
    def _tracked_lock(*, connection: object) -> Iterator[bool]:
        locked_connections.append(connection)
        yield True

    def _delegate(service: object, *_args: object, **_kwargs: object) -> object:
        assert service._logger() is recovered_logger
        assert service._logger() is recovered_logger
        return sentinel.result

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.lifecycle_lock.extension_lifecycle_lock",
        _tracked_lock,
    )
    monkeypatch.setattr(ExtensionCommandInstallMixin, "install_release", _delegate)
    service = ExtensionCommandService(get_logger=_get_logger)

    assert service.install_release(sentinel.input) is sentinel.result
    assert locked_connections == [connection]
    assert logger_calls == 2


def test_raw_command_lifecycle_gate_derives_connection_and_pins_injected_database_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions.install import DBExtensionInstallStore
    from core.application.extensions.runtime_store import DBExtensionRuntimeStore

    connection = object()
    install_store = DBExtensionInstallStore(connection)
    runtime_store = DBExtensionRuntimeStore(connection)
    logger_calls = 0
    store_calls = {"install": 0, "runtime": 0}
    locked_connections: list[object | None] = []

    def _get_logger() -> None:
        nonlocal logger_calls
        logger_calls += 1

    def _get_install_store() -> object:
        store_calls["install"] += 1
        return install_store

    def _get_runtime_store() -> object:
        store_calls["runtime"] += 1
        return runtime_store

    @contextmanager
    def _tracked_lock(*, connection: object | None) -> Iterator[bool]:
        locked_connections.append(connection)
        yield True

    def _delegate(service: object, *_args: object, **_kwargs: object) -> object:
        assert service._install_store() is install_store
        assert service._install_store() is install_store
        assert service._runtime_store() is runtime_store
        assert service._runtime_store() is runtime_store
        return sentinel.result

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.lifecycle_lock.extension_lifecycle_lock",
        _tracked_lock,
    )
    monkeypatch.setattr(ExtensionCommandInstallMixin, "install_release", _delegate)
    service = ExtensionCommandService(
        get_logger=_get_logger,
        get_install_store=_get_install_store,
        get_runtime_store=_get_runtime_store,
    )

    assert service.install_release(sentinel.input) is sentinel.result
    assert locked_connections == [connection]
    assert logger_calls == 2
    assert store_calls == {"install": 1, "runtime": 1}


def test_raw_command_lifecycle_gate_derives_connection_from_cached_default_database_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions import service_support as extension_services_common
    from core.application.extensions.install import DBExtensionInstallStore
    from core.application.extensions.runtime_store import DBExtensionRuntimeStore

    connection = object()
    install_store = DBExtensionInstallStore(connection)
    runtime_store = DBExtensionRuntimeStore(connection)
    locked_connections: list[object | None] = []

    @contextmanager
    def _tracked_lock(*, connection: object | None) -> Iterator[bool]:
        locked_connections.append(connection)
        yield True

    def _delegate(service: object, *_args: object, **_kwargs: object) -> object:
        assert service._install_store() is install_store
        assert service._runtime_store() is runtime_store
        return sentinel.result

    monkeypatch.setattr(
        extension_services_common,
        "_default_extension_install_store",
        install_store,
    )
    monkeypatch.setattr(
        extension_services_common,
        "_default_extension_runtime_store",
        runtime_store,
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.lifecycle_lock.extension_lifecycle_lock",
        _tracked_lock,
    )
    monkeypatch.setattr(ExtensionCommandInstallMixin, "install_release", _delegate)
    service = ExtensionCommandService(get_logger=lambda: None)

    assert service.install_release(sentinel.input) is sentinel.result
    assert locked_connections == [connection]


def test_default_database_stores_refresh_to_the_current_logger_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions import service_support as extension_services_common
    from core.application.extensions.install import DBExtensionInstallStore
    from core.application.extensions.runtime_store import DBExtensionRuntimeStore

    stale_connection = object()
    current_connection = object()
    locked_connections: list[object | None] = []

    @contextmanager
    def _tracked_lock(*, connection: object | None) -> Iterator[bool]:
        locked_connections.append(connection)
        yield True

    def _delegate(service: object, *_args: object, **_kwargs: object) -> object:
        assert service._install_store()._conn is current_connection
        assert service._runtime_store()._conn is current_connection
        return sentinel.result

    monkeypatch.setattr(
        extension_services_common,
        "_default_extension_install_store",
        DBExtensionInstallStore(stale_connection),
    )
    monkeypatch.setattr(
        extension_services_common,
        "_default_extension_runtime_store",
        DBExtensionRuntimeStore(stale_connection),
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.lifecycle_lock.extension_lifecycle_lock",
        _tracked_lock,
    )
    monkeypatch.setattr(ExtensionCommandInstallMixin, "install_release", _delegate)
    service = ExtensionCommandService(get_logger=lambda: SimpleNamespace(conn=current_connection))

    assert service.install_release(sentinel.input) is sentinel.result
    assert locked_connections == [current_connection]


def test_raw_command_lifecycle_gate_preserves_explicit_in_memory_store_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions.install import ExtensionInstallStore
    from core.application.extensions.runtime_store import ExtensionRuntimeStore

    install_store = ExtensionInstallStore()
    runtime_store = ExtensionRuntimeStore()
    store_calls = {"install": 0, "runtime": 0}
    locked_connections: list[object | None] = []

    def _get_install_store() -> object:
        store_calls["install"] += 1
        return install_store

    def _get_runtime_store() -> object:
        store_calls["runtime"] += 1
        return runtime_store

    @contextmanager
    def _tracked_lock(*, connection: object | None) -> Iterator[bool]:
        locked_connections.append(connection)
        yield True

    def _delegate(service: object, *_args: object, **_kwargs: object) -> object:
        assert service._install_store() is install_store
        assert service._install_store() is install_store
        assert service._runtime_store() is runtime_store
        assert service._runtime_store() is runtime_store
        return sentinel.result

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.lifecycle_lock.extension_lifecycle_lock",
        _tracked_lock,
    )
    monkeypatch.setattr(ExtensionCommandInstallMixin, "install_release", _delegate)
    service = ExtensionCommandService(
        get_logger=lambda: None,
        get_install_store=_get_install_store,
        get_runtime_store=_get_runtime_store,
    )

    assert service.install_release(sentinel.input) is sentinel.result
    assert locked_connections == [None]
    assert store_calls == {"install": 1, "runtime": 1}


def test_store_derived_pooled_connection_owns_install_release_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions.install import DBExtensionInstallStore
    from core.application.extensions.runtime_store import DBExtensionRuntimeStore
    from core.infrastructure.db.db_pool import (
        PooledConnectionProxy,
        active_transaction_connection,
    )

    class _Connection:
        autocommit = True
        closed = 0

        def get_transaction_status(self) -> int:
            return 0

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    class _Pool:
        def __init__(self) -> None:
            self.connections: list[_Connection] = []

        def getconn(self) -> _Connection:
            connection = _Connection()
            self.connections.append(connection)
            return connection

        def putconn(self, _connection: _Connection, *, close: bool = False) -> None:
            assert close is False

    pool = _Pool()
    proxy = PooledConnectionProxy(pool)
    install_store = DBExtensionInstallStore(proxy)
    runtime_store = DBExtensionRuntimeStore(proxy)

    @contextmanager
    def _tracked_lock(*, connection: object) -> Iterator[bool]:
        assert connection is proxy
        yield True

    def _delegate(service: object, *_args: object, **_kwargs: object) -> object:
        assert service._install_store() is install_store
        assert service._runtime_store() is runtime_store
        assert active_transaction_connection(proxy) is pool.connections[0]
        return sentinel.result

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.lifecycle_lock.extension_lifecycle_lock",
        _tracked_lock,
    )
    monkeypatch.setattr(ExtensionCommandInstallMixin, "_install_release", _delegate)
    service = ExtensionCommandService(
        get_logger=lambda: None,
        get_install_store=lambda: install_store,
        get_runtime_store=lambda: runtime_store,
    )

    assert service.install_release(sentinel.input) is sentinel.result
    assert len(pool.connections) == 1


@pytest.mark.parametrize(
    "wiring",
    (
        "logger_vs_stores",
        "logger_vs_memory",
        "store_vs_store",
        "database_vs_memory",
    ),
)
def test_raw_command_lifecycle_gate_rejects_non_atomic_store_connections(
    monkeypatch: pytest.MonkeyPatch,
    wiring: str,
) -> None:
    from core.application.extensions.install import (
        DBExtensionInstallStore,
        ExtensionInstallStore,
    )
    from core.application.extensions.runtime_store import (
        DBExtensionRuntimeStore,
        ExtensionRuntimeStore,
    )

    connection_a = object()
    connection_b = object()
    logger = None
    install_store: object = DBExtensionInstallStore(connection_a)
    runtime_store: object = DBExtensionRuntimeStore(connection_a)
    if wiring == "logger_vs_stores":
        logger = SimpleNamespace(conn=connection_b)
    elif wiring == "logger_vs_memory":
        logger = SimpleNamespace(conn=connection_b)
        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
    elif wiring == "store_vs_store":
        runtime_store = DBExtensionRuntimeStore(connection_b)
    else:
        install_store = ExtensionInstallStore()
        runtime_store = DBExtensionRuntimeStore(connection_b)

    def _must_not_lock(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("mismatched persistence stores reached the lifecycle lock")

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.lifecycle_lock.extension_lifecycle_lock",
        _must_not_lock,
    )
    service = ExtensionCommandService(
        get_logger=lambda: logger,
        get_install_store=lambda: install_store,
        get_runtime_store=lambda: runtime_store,
    )

    with pytest.raises(ServiceUnavailableError, match="one database connection"):
        service.install_release(sentinel.input)


def test_install_release_reuses_an_active_direct_database_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions.install import DBExtensionInstallStore
    from core.application.extensions.runtime_store import DBExtensionRuntimeStore

    class _ActiveConnection:
        closed = 0

        @property
        def autocommit(self) -> bool:
            return True

        @autocommit.setter
        def autocommit(self, _value: bool) -> None:
            raise AssertionError("active transaction autocommit must not be changed")

        def get_transaction_status(self) -> int:
            return 2

    connection = _ActiveConnection()
    install_store = DBExtensionInstallStore(connection)
    runtime_store = DBExtensionRuntimeStore(connection)

    @contextmanager
    def _tracked_lock(*, connection: object) -> Iterator[bool]:
        assert connection is not None
        yield True

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.lifecycle_lock.extension_lifecycle_lock",
        _tracked_lock,
    )
    monkeypatch.setattr(
        ExtensionCommandInstallMixin,
        "_install_release",
        lambda *_args, **_kwargs: sentinel.result,
    )
    service = ExtensionCommandService(
        get_logger=lambda: SimpleNamespace(conn=connection),
        get_install_store=lambda: install_store,
        get_runtime_store=lambda: runtime_store,
    )

    assert service.install_release(sentinel.input) is sentinel.result


def test_raw_extension_api_mutation_contends_with_global_lifecycle_owner() -> None:
    client, _store, _runtime_store, _logger = _make_extension_api_client()

    with extension_lifecycle_lock(connection=None) as acquired:
        assert acquired is True
        response = client.post("/api/extensions/acme.fixture/enable")

    assert response.status_code == 409
    assert "lifecycle operation" in response.json()["error"]
