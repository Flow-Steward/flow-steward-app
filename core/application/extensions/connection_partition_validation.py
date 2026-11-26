"""Validate extension-owned project-connection partitions before persistence."""

from __future__ import annotations

from typing import Any

from core.application.extensions.json_schema_subset import validate_json_schema_value
from core.application.extensions.oauth_contract import contract_section


class ConnectionPartitionSchemaRejected(ValueError):
    """The declared connection type cannot be resolved safely."""


class ConnectionPartitionsRejected(ValueError):
    """The final connection partitions do not satisfy the extension contract."""

    code = "invalid_payload"


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def connection_partition_schema(
    extension: dict[str, Any],
    *,
    connection_type: str,
    partition: str,
) -> dict[str, Any]:
    """Return the schema published by the selected connection type."""

    schema_key = {
        "config": "config_schema",
        "ingress": "ingress_schema",
        "secret": "secret_schema",
    }.get(partition, "")
    if not schema_key:
        raise ConnectionPartitionSchemaRejected("unknown connection partition")
    normalized_type = str(connection_type or "").strip()
    descriptors = [extension, _as_dict(extension.get("compiled_manifest"))]
    for descriptor in descriptors:
        rows = contract_section(descriptor, "connection_types")
        if not rows:
            continue
        selected = next(
            (
                row
                for row in rows
                if normalized_type
                and normalized_type == str(row.get("connection_type_id") or "").strip()
            ),
            rows[0] if len(rows) == 1 else None,
        )
        if selected is None:
            raise ConnectionPartitionSchemaRejected(
                "connection type is required and must match the extension contract"
            )
        schema = _as_dict(_as_dict(selected).get(schema_key))
        if schema:
            return schema
    return {}


#: Top-level keys Core itself keeps in a connection's partitions. They are the
#: platform's, never the extension's, so no extension schema judges them: an
#: OAuth connection's ``oauth`` lifecycle block in its config, and the
#: background poll status Core records in its ingress config.
CORE_OWNED_PARTITION_KEYS: dict[str, frozenset[str]] = {
    "config": frozenset({"oauth"}),
    "ingress": frozenset({"background_poll_status"}),
}


def extension_owned_partition(partition: str, value: Any) -> Any:
    """*value* without the keys Core owns in that partition."""
    owned = CORE_OWNED_PARTITION_KEYS.get(str(partition or "").strip(), frozenset())
    if not owned or not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key not in owned}


def validate_connection_partition(
    extension: dict[str, Any],
    *,
    connection_type: str,
    partition: str,
    value: Any,
) -> list[str]:
    errors: list[str] = []
    value = extension_owned_partition(partition, value)
    try:
        schema = connection_partition_schema(
            extension,
            connection_type=connection_type,
            partition=partition,
        )
    except ConnectionPartitionSchemaRejected as exc:
        return [str(exc)]
    if schema:
        validate_json_schema_value(
            schema=schema,
            value=value,
            path=f"connection_{partition}",
            errors=errors,
        )
    return errors


def require_valid_connection_partitions(
    extension: dict[str, Any],
    *,
    connection_type: str,
    connection_config: Any,
    ingress_config: Any,
) -> None:
    """Validate the final persisted config and ingress snapshots."""

    normalized_type = str(connection_type or "").strip()
    declared_type = False
    for descriptor in (extension, _as_dict(extension.get("compiled_manifest"))):
        for row in contract_section(descriptor, "connection_types"):
            if normalized_type == str(row.get("connection_type_id") or "").strip():
                declared_type = True
                break
        if declared_type:
            break
    if not normalized_type or not declared_type:
        raise ConnectionPartitionsRejected("connection type is not declared by the contract")

    errors: list[str] = []
    for partition, value in (
        ("config", connection_config),
        ("ingress", ingress_config),
    ):
        errors.extend(
            validate_connection_partition(
                extension,
                connection_type=connection_type,
                partition=partition,
                value=value,
            )
        )
    if errors:
        raise ConnectionPartitionsRejected("connection partitions do not match the contract")


__all__ = [
    "CORE_OWNED_PARTITION_KEYS",
    "ConnectionPartitionSchemaRejected",
    "ConnectionPartitionsRejected",
    "connection_partition_schema",
    "extension_owned_partition",
    "require_valid_connection_partitions",
    "validate_connection_partition",
]
