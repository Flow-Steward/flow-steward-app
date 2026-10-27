"""An installed-extension descriptor for the platform's grounding tests.

Capability discovery, the scoped catalog and the builder's integration grounding
all read an installed extension's normalized contract. Testing them against a
shipped bundle makes the platform's suite assert that bundle's operations, and it
breaks when the bundle changes them or moves to its own repository.

The shapes here are the contract, not any extension's: a source operation with an
object-schema input, a write operation, a delete operation, artifact outputs, and
declared external effects.
"""

from __future__ import annotations

from typing import Any

EXTENSION_ID = "acme.record-source"
CONNECTOR_ID = f"extension.{EXTENSION_ID}"
CONNECTION_TYPE_ID = "acme_record_source"

OPERATION_KINDS = {
    "search_records": "source",
    "get_attachment": "source",
    "update_record": "action",
    "delete_record": "action",
}
EXTERNAL_EFFECTS = {
    "update_record": "record_update",
    "delete_record": "record_delete",
}
GROUNDING_EFFECTS = {
    "search_records": "read",
    "get_attachment": "read",
    "update_record": "write",
    "delete_record": "delete",
}
FILTER_PROPERTIES: dict[str, Any] = {
    "owner": {"type": "string"},
    "label": {"type": "string"},
    "unread": {"type": "boolean"},
    "has_attachments": {"type": "boolean"},
    "record_id_gte": {"type": "integer", "minimum": 1},
}


def _operation(operation_id: str, kind: str) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "operation_id": operation_id,
        "operation_kind": kind,
        "display_name": operation_id.replace("_", " ").title(),
        "description": f"Synthetic {operation_id} operation.",
        "connection_type_ids": [CONNECTION_TYPE_ID],
        "inputs": [
            {"name": "connection_ref", "value_type": "string", "required": True},
        ],
        "outputs": [],
    }
    if operation_id == "search_records":
        operation["inputs"].append(
            {
                "name": "filters",
                "value_type": "object",
                "required": False,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": dict(FILTER_PROPERTIES),
                },
            }
        )
        operation["outputs"] = [{"name": "records", "value_type": "array"}]
    if operation_id == "get_attachment":
        operation["outputs"] = [
            {"name": "attachment_artifact_handle", "value_type": "artifact_handle"},
            {"name": "attachment_metadata", "value_type": "object"},
        ]
    return operation


def installed_release_descriptor() -> dict[str, Any]:
    """Return a descriptor shaped like one the platform stores after an install."""
    operation_manifest = [
        _operation(operation_id, kind) for operation_id, kind in OPERATION_KINDS.items()
    ]
    connection_types = [
        {
            "connection_type_id": CONNECTION_TYPE_ID,
            "display_name": "Acme record source",
            "description": "Synthetic connection type for platform tests.",
            "config_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["acme_host"],
                "properties": {"acme_host": {"type": "string"}},
            },
            "secret_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["password"],
                "properties": {"password": {"type": "string"}},
            },
            "capabilities": dict.fromkeys(OPERATION_KINDS, True),
        }
    ]
    artifact_policies = [
        {"operation_id": "get_attachment", "direction": "output", "artifact_kind": "file"}
    ]
    external_effects = [
        {
            "operation_id": operation_id,
            "effect_kind": effect_kind,
            "idempotency": "required",
            "test_mode_behavior": "suppress",
            "observability": {"target_kind": "record", "target_label": "record"},
        }
        for operation_id, effect_kind in EXTERNAL_EFFECTS.items()
    ]
    manifest = {
        "manifest_version": 2,
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_id": EXTENSION_ID,
        "kind": "tool_provider",
        "version": "1.0.0",
        "display_name": "Acme Record Source",
        "description": "Synthetic bundle for platform tests.",
        "features": ["tool"],
        "required_scopes": ["extension:invoke"],
        "entrypoint": {"command": ["python3", "main.py"]},
        "external_effects": external_effects,
    }
    action_definitions = [
        {
            "action_id": operation_id,
            "title": operation_id.replace("_", " ").title(),
            "handler": {"mode": "extension"},
            "workflow_visible": True,
            "mutates_platform": False,
            "parameters": [{"name": "connection_ref", "type": "connection_ref", "required": True}],
        }
        for operation_id in OPERATION_KINDS
    ]
    return {
        **manifest,
        "operation_manifest": operation_manifest,
        "connection_types": connection_types,
        "artifact_policies": artifact_policies,
        "action_definitions": action_definitions,
        "extension_contract_v2_normalized": {
            "operation_manifest": operation_manifest,
            "connection_types": connection_types,
            "artifact_policies": artifact_policies,
        },
    }
