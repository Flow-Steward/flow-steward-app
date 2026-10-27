from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from core.application.management.errors import (
    ServiceConflictError,
    ServiceUnavailableError,
    ServiceValidationError,
)

from .binding_constants import (
    _EXTENSION_STORE_UNSET,
    MAX_EXTENSION_ARCHIVE_BYTES,
    MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES,
    archive_too_large_message,
)
from .install import DBExtensionInstallStore, ExtensionInstallStore
from .lifecycle_ports import (
    ExtensionCommandServicePort,
    ExtensionQueryServicePort,
)
from .runtime_store import DBExtensionRuntimeStore, ExtensionRuntimeStore

_MAX_EXTENSION_ARCHIVE_BYTES = MAX_EXTENSION_ARCHIVE_BYTES
_MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES = MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES
_archive_too_large_message = archive_too_large_message
_ARCHIVE_MANAGED_TRANSACTION_REQUIRED = (
    "Extension archive lifecycle requires a managed transaction "
    "to publish runtime files after commit"
)


ExtensionInstallStoreType: TypeAlias = ExtensionInstallStore | DBExtensionInstallStore
ExtensionRuntimeStoreType: TypeAlias = ExtensionRuntimeStore | DBExtensionRuntimeStore
ExtensionInstallStoreProvider: TypeAlias = Callable[[], ExtensionInstallStoreType]
ExtensionRuntimeStoreProvider: TypeAlias = Callable[[], ExtensionRuntimeStoreType]


class ExtensionQueryServiceFactory(Protocol):
    def __call__(
        self,
        *,
        bl: Any,
        install_store: ExtensionInstallStoreType,
        runtime_store: ExtensionRuntimeStoreType,
    ) -> ExtensionQueryServicePort: ...


class ExtensionLifecycleContext:
    def __init__(
        self,
        *,
        get_extension_install_store: ExtensionInstallStoreProvider | None = None,
        get_extension_runtime_store: ExtensionRuntimeStoreProvider | None = None,
        extension_query_service_factory: ExtensionQueryServiceFactory | None = None,
    ) -> None:
        self._get_extension_install_store = get_extension_install_store
        self._get_extension_runtime_store = get_extension_runtime_store
        self._extension_query_service_factory = extension_query_service_factory

    def lifecycle_transaction(self, bl: Any) -> contextlib.AbstractContextManager[Any]:
        return self._extension_lifecycle_transaction(bl)

    def archive_lifecycle_transaction(self, bl: Any) -> contextlib.AbstractContextManager[Any]:
        return self._extension_archive_lifecycle_transaction(bl)

    def archive_transaction_is_caller_owned(self, bl: Any) -> bool:
        return self._extension_archive_transaction_is_caller_owned(bl)

    def register_archive_after_commit(self, bl: Any, callback: Callable[[], None]) -> None:
        self._register_extension_archive_after_commit(bl, callback)

    def publish_committed_managed_extension_authority(
        self,
        *,
        bl: Any,
        extension_id: str,
        install_store: Any,
        runtime_store: Any,
        after_recovery: Callable[[], None] | None = None,
    ) -> None:
        self._publish_committed_managed_extension_authority(
            bl=bl,
            extension_id=extension_id,
            install_store=install_store,
            runtime_store=runtime_store,
            after_recovery=after_recovery,
        )

    def recover_managed_release_publications(self, *, bl: Any) -> None:
        self._recover_managed_release_publications(bl=bl)

    def install_store(self, bl: Any) -> ExtensionInstallStoreType:
        return self._extension_install_store(bl)

    def runtime_store(self, bl: Any) -> ExtensionRuntimeStoreType:
        return self._extension_runtime_store(bl)

    def assert_store_authority(self, *, bl: Any, store: Any, store_name: str) -> None:
        self._assert_extension_store_authority(bl=bl, store=store, store_name=store_name)

    def lifecycle_stores(
        self, bl: Any
    ) -> tuple[ExtensionInstallStoreType, ExtensionRuntimeStoreType]:
        return self._extension_lifecycle_stores(bl)

    def command_service(
        self,
        bl: Any,
        *,
        install_store: Any | None = None,
        runtime_store: Any | None = None,
    ) -> ExtensionCommandServicePort:
        if install_store is not None and runtime_store is not None:
            service = self._extension_command_service_with_stores(
                bl,
                install_store=install_store,
                runtime_store=runtime_store,
            )
        else:
            service = self._extension_command_service(
                bl,
                install_store=install_store,
                runtime_store=runtime_store,
            )
        return service

    def query_service(
        self,
        bl: Any,
        *,
        install_store: Any | None = None,
        runtime_store: Any | None = None,
    ) -> ExtensionQueryServicePort:
        return self._extension_query_service(
            bl,
            install_store=install_store,
            runtime_store=runtime_store,
        )

    def update_lock(
        self,
        *,
        bl: Any,
        extension_id: str,
        validate_stores: bool = False,
        install_store: Any = _EXTENSION_STORE_UNSET,
        runtime_store: Any = _EXTENSION_STORE_UNSET,
    ) -> contextlib.AbstractContextManager[None]:
        return self._extension_update_lock(
            bl=bl,
            extension_id=extension_id,
            validate_stores=validate_stores,
            install_store=install_store,
            runtime_store=runtime_store,
        )

    @contextlib.contextmanager
    def _extension_lifecycle_transaction(self, bl: Any) -> Iterator[Any]:
        from core.infrastructure.db.db_pool import (
            PooledConnectionProxy,
            transaction_connection,
        )

        conn = getattr(bl, "conn", None)
        transaction = getattr(conn, "transaction", None) if conn is not None else None
        if callable(transaction) and hasattr(conn, "pgconn"):
            with transaction():
                yield conn
            return
        if isinstance(conn, PooledConnectionProxy):
            with transaction_connection(bl) as transaction_conn:
                yield transaction_conn
            return
        yield conn

    @contextlib.contextmanager
    def _extension_archive_lifecycle_transaction(self, bl: Any) -> Iterator[Any]:
        from core.infrastructure.db.db_pool import (
            active_transaction_connection,
            transaction_after_commit_supported,
        )

        conn = getattr(bl, "conn", None)
        active_connection = active_transaction_connection(conn) if conn is not None else None
        if conn is not None and transaction_after_commit_supported(bl):
            yield active_connection or conn
            return
        if active_connection is not None:
            yield active_connection
            return
        with self._extension_lifecycle_transaction(bl) as transaction_conn:
            yield transaction_conn

    @staticmethod
    def _extension_archive_transaction_is_caller_owned(bl: Any) -> bool:
        from core.infrastructure.db.db_pool import (
            PooledConnectionProxy,
            active_transaction_connection,
            transaction_after_commit_supported,
            transaction_connection_is_verifiably_idle,
        )

        conn = getattr(bl, "conn", None)
        if conn is None:
            return False
        if transaction_after_commit_supported(bl):
            return True
        if active_transaction_connection(conn) is not None:
            raise ServiceConflictError(_ARCHIVE_MANAGED_TRANSACTION_REQUIRED)
        if isinstance(conn, PooledConnectionProxy):
            return False
        if not transaction_connection_is_verifiably_idle(conn):
            raise ServiceConflictError(_ARCHIVE_MANAGED_TRANSACTION_REQUIRED)
        native_transaction = getattr(conn, "transaction", None)
        native_transaction_supported = (
            getattr(conn, "autocommit", None) is True
            and callable(native_transaction)
            and hasattr(conn, "pgconn")
        )
        if native_transaction_supported:
            return False
        raise ServiceConflictError(_ARCHIVE_MANAGED_TRANSACTION_REQUIRED)

    @staticmethod
    def _register_extension_archive_after_commit(bl: Any, callback: Callable[[], None]) -> None:
        from core.infrastructure.db.db_pool import register_transaction_after_commit

        if not register_transaction_after_commit(bl, callback):
            raise ServiceConflictError(
                "Extension archive lifecycle could not register post-commit publication"
            )

    def _publish_committed_managed_extension_authority(
        self,
        *,
        bl: Any,
        extension_id: str,
        install_store: Any,
        runtime_store: Any,
        after_recovery: Callable[[], None] | None = None,
    ) -> None:
        """Publish a fresh committed DB view while holding the lifecycle lock."""
        with self._extension_update_lock(
            bl=bl,
            extension_id=extension_id,
            validate_stores=True,
            install_store=install_store,
            runtime_store=runtime_store,
        ):
            self._recover_managed_release_publications(bl=bl)
            if after_recovery is not None:
                after_recovery()

    @staticmethod
    def _recover_managed_release_publications(*, bl: Any) -> None:
        """Finish a post-commit symlink publication left by a process crash."""
        if not hasattr(bl, "list_extensions"):
            return
        conn = getattr(bl, "conn", None)
        if conn is not None:
            from core.infrastructure.db.db_pool import (
                active_transaction_connection,
                transaction_after_commit_supported,
            )

            if (
                transaction_after_commit_supported(bl)
                or active_transaction_connection(conn) is not None
            ):
                return
        from core.infrastructure.extension_runtime.extension_runtime import default_extensions_root
        from core.infrastructure.extension_runtime.publication_recovery import (
            recover_managed_release_publications,
        )

        recover_managed_release_publications(
            extensions_root=Path(default_extensions_root()),
            extensions=bl.list_extensions(enabled_only=False) or [],
        )

    def _extension_install_store(self, bl: Any) -> ExtensionInstallStoreType:
        if self._get_extension_install_store is not None:
            return self._get_extension_install_store()
        conn = getattr(bl, "conn", None) if bl is not None else None
        if conn is not None:
            from core.application.extensions.install import DBExtensionInstallStore

            return DBExtensionInstallStore(conn)
        from core.application.extensions.install import ExtensionInstallStore

        return ExtensionInstallStore()

    def _extension_runtime_store(self, bl: Any) -> ExtensionRuntimeStoreType:
        if self._get_extension_runtime_store is not None:
            return self._get_extension_runtime_store()
        conn = getattr(bl, "conn", None) if bl is not None else None
        if conn is not None:
            from core.application.extensions.runtime_store import DBExtensionRuntimeStore

            return DBExtensionRuntimeStore(conn)
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        return ExtensionRuntimeStore()

    @staticmethod
    def _assert_extension_store_authority(*, bl: Any, store: Any, store_name: str) -> None:
        if store is None:
            raise ServiceUnavailableError(f"Extension lifecycle {store_name} is unavailable")
        try:
            logger_authority = vars(bl).get("conn")
        except TypeError:
            logger_authority = getattr(bl, "conn", None)
        store_state = vars(store) if hasattr(store, "__dict__") else {}
        has_database_authority = "_conn" in store_state
        store_authority = store_state.get("_conn")
        if has_database_authority and store_authority is None:
            matches = False
        elif logger_authority is None:
            matches = not has_database_authority
        else:
            matches = has_database_authority and store_authority is logger_authority
        if not matches:
            raise ServiceUnavailableError(
                f"Extension lifecycle {store_name} authority does not match the locked logger"
            )

    def _extension_lifecycle_stores(
        self, bl: Any
    ) -> tuple[ExtensionInstallStoreType, ExtensionRuntimeStoreType]:
        install_store = self._extension_install_store(bl)
        runtime_store = self._extension_runtime_store(bl)
        self._assert_extension_store_authority(
            bl=bl,
            store=install_store,
            store_name="install store",
        )
        self._assert_extension_store_authority(
            bl=bl,
            store=runtime_store,
            store_name="runtime store",
        )
        return install_store, runtime_store

    def _extension_command_service(
        self,
        bl: Any,
        *,
        install_store: Any | None = None,
        runtime_store: Any | None = None,
    ) -> ExtensionCommandServicePort:
        from core.application.extensions.command_service import ExtensionCommandService

        return ExtensionCommandService(
            get_logger=lambda: bl,
            get_install_store=lambda: (
                install_store if install_store is not None else self._extension_install_store(bl)
            ),
            get_runtime_store=lambda: (
                runtime_store if runtime_store is not None else self._extension_runtime_store(bl)
            ),
        )

    def _extension_command_service_with_stores(
        self,
        bl: Any,
        *,
        install_store: Any,
        runtime_store: Any,
    ) -> ExtensionCommandServicePort:
        return self._extension_command_service(
            bl,
            install_store=install_store,
            runtime_store=runtime_store,
        )

    def _extension_query_service(
        self,
        bl: Any,
        *,
        install_store: Any | None = None,
        runtime_store: Any | None = None,
    ) -> ExtensionQueryServicePort:
        if self._extension_query_service_factory is None:
            raise ServiceUnavailableError("Extension query service is not configured")
        return self._extension_query_service_factory(
            bl=bl,
            install_store=(
                install_store if install_store is not None else self._extension_install_store(bl)
            ),
            runtime_store=(
                runtime_store if runtime_store is not None else self._extension_runtime_store(bl)
            ),
        )

    @contextlib.contextmanager
    def _extension_update_lock(
        self,
        *,
        bl: Any,
        extension_id: str,
        validate_stores: bool = False,
        install_store: Any = _EXTENSION_STORE_UNSET,
        runtime_store: Any = _EXTENSION_STORE_UNSET,
    ) -> Iterator[None]:
        from core.infrastructure.extension_runtime.lifecycle_lock import (
            extension_lifecycle_lock,
        )

        normalized_id = str(extension_id or "").strip()
        if not normalized_id:
            raise ServiceValidationError("extension_id is required for update")
        if validate_stores:
            install_store_provided = install_store is not _EXTENSION_STORE_UNSET
            runtime_store_provided = runtime_store is not _EXTENSION_STORE_UNSET
            if install_store_provided != runtime_store_provided:
                raise ServiceUnavailableError(
                    "Extension lifecycle stores must be validated as one pinned pair"
                )
            if not install_store_provided:
                self._extension_lifecycle_stores(bl)
            else:
                self._assert_extension_store_authority(
                    bl=bl,
                    store=install_store,
                    store_name="install store",
                )
                self._assert_extension_store_authority(
                    bl=bl,
                    store=runtime_store,
                    store_name="runtime store",
                )
        with extension_lifecycle_lock(connection=getattr(bl, "conn", None)) as acquired:
            if not acquired:
                raise ServiceConflictError(
                    "Another extension lifecycle operation is already in progress"
                )
            yield


__all__ = ["ExtensionLifecycleContext"]
