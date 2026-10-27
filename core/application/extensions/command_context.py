"""Scoped persistence context shared by composed extension command services."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable, Iterator
from typing import Any

from core.application.management.errors import ServiceUnavailableError

from .service_support import (
    _get_default_extension_install_store,
    _get_default_extension_runtime_store,
)

_MESSAGE_EXTENSION_LIFECYCLE_PERSISTENCE_MUST_USE_ONE_DATABASE_CONNECTION = (
    "Extension lifecycle persistence must use one database connection"
)

_CONTEXT_UNSET = object()


class ExtensionCommandContext:
    """Own logger/store resolution and per-operation lifecycle pinning."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        get_install_store: Callable[[], Any] | None = None,
        get_runtime_store: Callable[[], Any] | None = None,
    ) -> None:
        self._get_logger = get_logger
        self._get_install_store = get_install_store
        self._get_runtime_store = get_runtime_store
        self._logger_context: contextvars.ContextVar[Any] = contextvars.ContextVar(
            f"extension_command_logger_{id(self)}", default=_CONTEXT_UNSET
        )
        self._store_context: contextvars.ContextVar[Any] = contextvars.ContextVar(
            f"extension_command_stores_{id(self)}", default=_CONTEXT_UNSET
        )
        self._connection_context: contextvars.ContextVar[Any] = contextvars.ContextVar(
            f"extension_command_connection_{id(self)}", default=_CONTEXT_UNSET
        )

    def logger(self) -> Any:
        scoped = self._logger_context.get()
        if scoped is not _CONTEXT_UNSET:
            return scoped
        try:
            return self._get_logger()
        except Exception:
            return None

    @staticmethod
    def logger_connection(logger: Any) -> Any:
        if logger is None:
            return None
        try:
            attributes = vars(logger)
        except TypeError:
            return getattr(logger, "conn", None)
        if "conn" in attributes:
            return attributes["conn"]
        return getattr(logger, "conn", None) if "conn" in vars(type(logger)) else None

    def persistence_connection(self) -> Any:
        scoped = self._connection_context.get()
        if scoped is not _CONTEXT_UNSET:
            return scoped
        return self.logger_connection(self.logger())

    def install_store(self) -> Any:
        scoped = self._store_context.get()
        if scoped is not _CONTEXT_UNSET:
            return scoped[0]
        if self._get_install_store is not None:
            return self._get_install_store()
        return _get_default_extension_install_store(get_logger=self.logger)

    def runtime_store(self) -> Any:
        scoped = self._store_context.get()
        if scoped is not _CONTEXT_UNSET:
            return scoped[1]
        if self._get_runtime_store is not None:
            return self._get_runtime_store()
        return _get_default_extension_runtime_store(get_logger=self.logger)

    @staticmethod
    def store_connection(store: Any) -> Any:
        if store is None:
            return None
        try:
            attributes = vars(store)
        except TypeError:
            return getattr(store, "_conn", None)
        if "_conn" in attributes:
            return attributes["_conn"]
        return getattr(store, "_conn", None) if "_conn" in vars(type(store)) else None

    def validated_connection(self, logger: Any, stores: tuple[Any, Any]) -> Any:
        logger_connection = self.logger_connection(logger)
        install_connection = self.store_connection(stores[0])
        runtime_connection = self.store_connection(stores[1])
        if (install_connection is None) != (runtime_connection is None):
            raise ServiceUnavailableError(
                _MESSAGE_EXTENSION_LIFECYCLE_PERSISTENCE_MUST_USE_ONE_DATABASE_CONNECTION
            )
        if logger_connection is not None and install_connection is None:
            raise ServiceUnavailableError(
                _MESSAGE_EXTENSION_LIFECYCLE_PERSISTENCE_MUST_USE_ONE_DATABASE_CONNECTION
            )
        connections = [
            connection
            for connection in (logger_connection, install_connection, runtime_connection)
            if connection is not None
        ]
        if not connections:
            return None
        connection = connections[0]
        if any(candidate is not connection for candidate in connections[1:]):
            raise ServiceUnavailableError(
                _MESSAGE_EXTENSION_LIFECYCLE_PERSISTENCE_MUST_USE_ONE_DATABASE_CONNECTION
            )
        return connection

    @contextlib.contextmanager
    def pin_logger(self, logger: Any) -> Iterator[None]:
        token = self._logger_context.set(logger)
        try:
            yield
        finally:
            self._logger_context.reset(token)

    @contextlib.contextmanager
    def pin(
        self,
        *,
        logger: Any,
        stores: tuple[Any, Any],
        connection: Any,
    ) -> Iterator[None]:
        logger_token = self._logger_context.set(logger)
        store_token = self._store_context.set(stores)
        connection_token = self._connection_context.set(connection)
        try:
            yield
        finally:
            self._connection_context.reset(connection_token)
            self._store_context.reset(store_token)
            self._logger_context.reset(logger_token)


__all__ = ["ExtensionCommandContext"]
