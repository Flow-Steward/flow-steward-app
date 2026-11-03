"""Bind saved-query language to the active Dataset connector before dispatch."""

from __future__ import annotations

from typing import Any

from core.application.datasets.saved_query_errors import invalid_saved_query
from core.application.extensions.projected_dataset_bridge import projection_bridge_id
from core.application.registry.data_connection_binding import resolve_connection_dataset_connector


def _text(value: Any) -> str:
    return str(value or "").strip()


def _projected_connector(
    payload: dict[str, Any],
    dataset: dict[str, Any],
    metadata: dict[str, Any],
    rows: list[dict[str, Any]],
    connector_id: str,
) -> dict[str, Any] | None:
    """Resolve an extension-local ID only inside its immutable bridge owner."""
    bridge = metadata.get("projection_bridge")
    if not isinstance(bridge, dict):
        return None
    install_id = _text(bridge.get("install_id"))
    artifact_id = _text(bridge.get("artifact_id"))
    account_id = _text(dataset.get("account_id") or metadata.get("account_id"))
    if (
        bridge.get("immutable") is not True
        or bridge.get("artifact_kind") != "dataset"
        or not install_id
        or not artifact_id
        or not _text(bridge.get("release_id"))
        or not account_id
        or any(
            _text(value) and _text(value) != account_id
            for value in (
                dataset.get("account_id"),
                metadata.get("account_id"),
                payload.get("account_id"),
            )
        )
    ):
        return None
    try:
        expected_dataset_id = projection_bridge_id(install_id, artifact_id)
    except ValueError:
        return None
    if _text(dataset.get("dataset_id")) != expected_dataset_id:
        return None
    matches = [
        row
        for row in rows
        if _text(row.get("source_install_id")) == install_id
        and _text(row.get("account_id")) == account_id
        and _text(row.get("local_connector_id"))
        and _text(row.get("connector_id"))
        == f"extension.{install_id}.{_text(row.get('local_connector_id'))}"
        and connector_id in {_text(row.get("local_connector_id")), _text(row.get("connector_id"))}
    ]
    return matches[0] if len(matches) == 1 else None


def validate_saved_query_connector_binding(
    payload: dict[str, Any],
    *,
    dataset: dict[str, Any],
    db: Any,
    connection: dict[str, Any] | None = None,
) -> None:
    """Check the active catalog when available; absent catalogs retain dataset validation.

    A present catalog is authoritative, including an empty catalog or a missing
    connector. Callers without a catalog still validate persisted dataset language
    through the shared saved-query contract.
    """
    list_connectors = getattr(db, "list_dataset_connectors", None)
    if not callable(list_connectors):
        return
    rows = [row for row in list_connectors() or [] if isinstance(row, dict)]
    catalog = {str(row.get("connector_id") or ""): row for row in rows}
    if connection is not None:
        connector, issue = resolve_connection_dataset_connector(connection, catalog)
        if issue:
            raise invalid_saved_query("query_language", "Dataset connector binding is unavailable")
    else:
        metadata = dataset.get("metadata_jsonb") or dataset.get("metadata") or {}
        metadata = metadata if isinstance(metadata, dict) else {}
        reference = metadata.get("dataset_connector") or {}
        reference = reference if isinstance(reference, dict) else {}
        connector_id = str(dataset.get("connector_id") or reference.get("connector_id") or "")
        if "projection_bridge" in metadata:
            connector = _projected_connector(payload, dataset, metadata, rows, connector_id)
        else:
            matches = [row for row in rows if str(row.get("connector_id") or "") == connector_id]
            connector = matches[0] if len(matches) == 1 else None
    if not connector:
        raise invalid_saved_query("query_language", "Dataset connector binding is unavailable")
    language = str(payload.get("query_language") or "sql").strip().lower()
    connector_language = str(connector.get("query_language") or "").strip().lower()
    if not connector_language or language != connector_language:
        raise invalid_saved_query(
            "query_language", "Query language must match the active connector"
        )
