"""Manifest-declared cross-process serialization for extension actions."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from typing import Any

_LOCK_NAMESPACE = "flowsteward.extension-operation.v1"


def _serialization_key(
    extension: Mapping[str, Any],
    *,
    action_id: str,
    input_payload: Mapping[str, Any],
    runtime_context: Mapping[str, Any],
) -> str | None:
    actions = extension.get("action_definitions")
    actions = actions if isinstance(actions, list) else []
    matches = [
        row
        for row in actions
        if isinstance(row, Mapping) and str(row.get("action_id") or "").strip() == action_id
    ]
    if len(matches) != 1:
        return ""
    contract = matches[0].get("contract")
    serialization = contract.get("serialization") if isinstance(contract, Mapping) else None
    if serialization is None:
        return ""
    if not isinstance(serialization, Mapping) or not set(serialization) <= {
        "key_fields",
        "resource",
    }:
        return None
    key_fields = serialization.get("key_fields", [])
    if not isinstance(key_fields, list) or len(key_fields) > 8:
        return None
    values: list[str] = []
    for field in key_fields:
        if not isinstance(field, str) or not field or len(field) > 128:
            return None
        value = input_payload.get(field)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        normalized = str(value).strip()
        if not normalized or len(normalized) > 512 or any(ord(char) < 32 for char in normalized):
            return None
        values.append(f"{field}={normalized}")
    extension_id = str(
        extension.get("extension_key") or extension.get("extension_id") or ""
    ).strip()
    install_id = str(runtime_context.get("extension_install_id") or "").strip()
    resource = str(serialization.get("resource") or action_id).strip()
    if (
        not extension_id
        or not install_id
        or len(install_id) > 128
        or not resource
        or len(resource) > 128
    ):
        return None
    scope_values = [
        f"{field}={value}"
        for field in (
            "tenant_id",
            "account_id",
            "project_id",
            "connection_id",
            "oauth_configuration_id",
            "oauth_provider_id",
        )
        if (value := str(runtime_context.get(field) or "").strip())
    ]
    return ":".join((_LOCK_NAMESPACE, extension_id, install_id, resource, *scope_values, *values))


@contextlib.contextmanager
def extension_operation_lock_status(
    *,
    connection: Any,
    extension: Mapping[str, Any],
    action_id: str,
    input_payload: Mapping[str, Any],
    runtime_context: Mapping[str, Any],
) -> Iterator[str]:
    """Yield acquired, busy, or unavailable for the declared serialization gate."""
    lock_name = _serialization_key(
        extension,
        action_id=action_id,
        input_payload=input_payload,
        runtime_context=runtime_context,
    )
    if lock_name is None:
        yield "unavailable"
        return
    if not lock_name:
        yield "acquired"
        return
    if connection is None:
        yield "unavailable"
        return

    from core.infrastructure.db.db_pool import PooledConnectionProxy, pooled_connection

    if isinstance(connection, PooledConnectionProxy):
        try:
            connection_context = pooled_connection(connection._pool, autocommit=True)
            lock_connection = connection_context.__enter__()
        except Exception:
            yield "unavailable"
            return
        try:
            with _database_operation_lock(lock_connection, lock_name=lock_name) as status:
                yield status
        finally:
            connection_context.__exit__(None, None, None)
        return
    with _database_operation_lock(connection, lock_name=lock_name) as status:
        yield status


@contextlib.contextmanager
def extension_operation_lock(
    *,
    connection: Any,
    extension: Mapping[str, Any],
    action_id: str,
    input_payload: Mapping[str, Any],
    runtime_context: Mapping[str, Any],
) -> Iterator[bool]:
    with extension_operation_lock_status(
        connection=connection,
        extension=extension,
        action_id=action_id,
        input_payload=input_payload,
        runtime_context=runtime_context,
    ) as status:
        yield status == "acquired"


@contextlib.contextmanager
def _database_operation_lock(connection: Any, *, lock_name: str) -> Iterator[str]:
    acquired = False
    acquisition_complete = False
    try:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                    (lock_name,),
                )
                row = cursor.fetchone()
        except Exception:
            with contextlib.suppress(Exception):
                connection.close()
            yield "unavailable"
            return
        value = row[0] if isinstance(row, (tuple, list)) and row else row
        acquired = value is True
        acquisition_complete = True
        yield "acquired" if acquired else "busy"
    finally:
        if acquired:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                        (lock_name,),
                    )
                    row = cursor.fetchone()
                value = row[0] if isinstance(row, (tuple, list)) and row else row
                if value is not True:
                    raise RuntimeError("Extension operation advisory lock release failed")
            except BaseException:
                with contextlib.suppress(BaseException):
                    connection.close()
                raise
        elif not acquisition_complete:
            with contextlib.suppress(BaseException):
                connection.close()


__all__ = ["extension_operation_lock", "extension_operation_lock_status"]
