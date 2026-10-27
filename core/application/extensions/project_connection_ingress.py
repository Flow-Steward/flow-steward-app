"""`project_connection_ingress_patch`: declared fields, and nothing else, into `ingress_config`.

Inbound settings such as a polling mode and interval are read by the extension
runtime from the connection's `ingress_config`. Routing them through the
configuration patch wrote them into `connection_config`, where no consumer
looked. This operation writes the ingress partition and carries every other
piece of connection state across untouched.

Runtime-owned ingress fields (webhook status, update cursor, discovered chats)
are not reachable here: only manifest-declared fields are accepted, and an
undeclared key in the payload is ignored.
"""

from __future__ import annotations

import copy
from typing import Any

from core.application.extensions.project_connection_state import (
    carried_connection_fields,
    nested_get,
    nested_set,
)

CONFIG_FIELDS = "fields"


class IngressPatchRejected(ValueError):
    """The patch was refused before any write. `code` is the operation's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _declared_fields(config: dict[str, Any]) -> list[str]:
    raw = config.get(CONFIG_FIELDS)
    rows = list(raw) if isinstance(raw, (list, tuple)) else []
    fields: list[str] = []
    for row in rows:
        field = _text(row)
        if field and field not in fields:
            fields.append(field)
    return fields


def patch_project_connection_ingress(
    store: Any,
    *,
    project_id: str,
    extension_id: str,
    config: dict[str, Any],
    input_payload: dict[str, Any],
    extension_install_id: str = "",
    connection_type: str = "",
    account_id: str = "",
    ingress_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge the supplied declared fields into `ingress_config`.

    Returns the declared fields as stored after the write, the fields that were
    written, and the connection identity.
    """
    normalized_project_id = _text(project_id)
    normalized_extension_id = _text(extension_id)
    if not normalized_project_id:
        raise IngressPatchRejected("project_context_required", "A project is required.")
    if not normalized_extension_id:
        raise IngressPatchRejected("extension_context_required", "An extension is required.")

    declared = _declared_fields(_as_dict(config))
    if not declared:
        raise IngressPatchRejected(
            "ingress_fields_undeclared", "The manifest declares no ingress fields to patch."
        )
    payload = _as_dict(input_payload)

    existing = _as_dict(
        store.get_project_connection(
            project_id=normalized_project_id,
            extension_id=normalized_extension_id,
            extension_install_id=_text(extension_install_id),
            enabled_only=False,
        )
    )
    merged = copy.deepcopy(_as_dict(existing.get("ingress_config")))
    written: list[str] = []
    for field in declared:
        if field not in payload:
            continue
        nested_set(merged, field, payload.get(field))
        written.append(field)

    schema_errors: list[str] = []
    if ingress_schema:
        from core.application.extensions.json_schema_subset import validate_json_schema_value

        validate_json_schema_value(
            schema=ingress_schema,
            value=merged,
            path="connection_ingress",
            errors=schema_errors,
        )
    if schema_errors:
        raise IngressPatchRejected("invalid_payload", "; ".join(schema_errors[:8]))

    row = existing
    if written:
        carried = carried_connection_fields(
            existing,
            extension_install_id=extension_install_id,
            account_id=account_id,
        )
        carried["connection_type"] = connection_type or carried["connection_type"]
        carried["ingress_config"] = merged
        row = _as_dict(
            store.upsert_project_connection(
                project_id=normalized_project_id,
                extension_id=normalized_extension_id,
                **carried,
            )
        )

    return {
        "values": {field: nested_get(merged, field) for field in declared},
        "written_fields": written,
        "connection_id": _text(row.get("connection_id")),
    }


__all__ = ["CONFIG_FIELDS", "IngressPatchRejected", "patch_project_connection_ingress"]
