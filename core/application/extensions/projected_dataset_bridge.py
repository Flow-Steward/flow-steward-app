"""Host-owned execution bridges for projected datasets and saved queries."""

from __future__ import annotations

import re
from typing import Any

_BRIDGE_AUTHOR_PREFIX = "extension-install:"
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def projection_bridge_id(install_id: str, artifact_id: str) -> str:
    """Return the deterministic canonical handle for one install-owned artifact."""
    normalized_install = _SAFE_ID.sub("-", str(install_id or "").strip()).strip("-")
    normalized_artifact = _SAFE_ID.sub("-", str(artifact_id or "").strip()).strip("-")
    if not normalized_install or not normalized_artifact:
        raise ValueError("install_id and artifact_id are required")
    return f"extension.{normalized_install}.{normalized_artifact}"


def projection_bridge_metadata(
    *, install_id: str, release_id: str, artifact_kind: str, artifact_id: str
) -> dict[str, Any]:
    return {
        "immutable": True,
        "install_id": str(install_id or "").strip(),
        "release_id": str(release_id or "").strip(),
        "artifact_kind": str(artifact_kind or "").strip(),
        "artifact_id": str(artifact_id or "").strip(),
    }


def projection_bridge_owner(payload: dict[str, Any] | None) -> str:
    row = dict(payload or {})
    metadata = row.get("metadata_jsonb") or row.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    bridge = metadata.get("projection_bridge")
    if not isinstance(bridge, dict):
        validation = row.get("validation_result_jsonb") or row.get("validation_result")
        validation = dict(validation) if isinstance(validation, dict) else {}
        bridge = validation.get("projection_bridge")
    return str((bridge or {}).get("install_id") or "").strip()


def assert_projection_bridge_mutable(existing: dict[str, Any] | None, *, author: str) -> None:
    owner = projection_bridge_owner(existing)
    if owner and str(author or "").strip() != f"{_BRIDGE_AUTHOR_PREFIX}{owner}":
        raise ValueError("Install-owned projected dataset/query is immutable")


def _projection_payload(row: Any) -> dict[str, Any]:
    if hasattr(row, "model_dump"):
        payload = row.model_dump(mode="json")
    elif isinstance(row, dict):
        payload = dict(row)
    else:
        return {}
    snapshot = payload.get("projection")
    payload["projection"] = dict(snapshot) if isinstance(snapshot, dict) else {}
    return payload


def _assert_owned_or_absent(existing: dict[str, Any] | None, *, install_id: str) -> None:
    if existing and projection_bridge_owner(existing) != install_id:
        raise ValueError("Projection bridge row is not owned by install")


def remove_projected_dataset_bridges(
    store: Any,
    *,
    install_id: str,
    keep_dataset_ids: set[str] | None = None,
    keep_saved_query_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    """Remove obsolete canonical rows owned by one extension install."""
    keep_datasets = set(keep_dataset_ids or set())
    keep_queries = set(keep_saved_query_ids or set())
    removed = {"datasets": [], "saved_queries": []}

    query_rows = store.list_dataset_query_templates(include_trashed=True)
    for row in query_rows:
        query_id = str((row or {}).get("query_template_id") or "").strip()
        if query_id and query_id not in keep_queries and projection_bridge_owner(row) == install_id:
            store.purge_dataset_query_template(query_id)
            removed["saved_queries"].append(query_id)

    dataset_rows = store.list_datasets(include_trashed=True)
    for row in dataset_rows:
        dataset_id = str((row or {}).get("dataset_id") or "").strip()
        if (
            dataset_id
            and dataset_id not in keep_datasets
            and projection_bridge_owner(row) == install_id
        ):
            store.purge_dataset(dataset_id)
            removed["datasets"].append(dataset_id)

    removed["datasets"].sort()
    removed["saved_queries"].sort()
    return removed


def materialize_projected_dataset_bridges(
    store: Any,
    *,
    account_id: str,
    install_id: str,
    release_id: str,
    projections: list[Any],
) -> dict[str, list[str]]:
    """Materialize collision-safe canonical handles without touching user-owned IDs."""
    rows = [_projection_payload(row) for row in projections]
    datasets = [row for row in rows if row.get("artifact_kind") == "dataset"]
    saved_queries = [row for row in rows if row.get("artifact_kind") == "saved_query"]
    author = f"{_BRIDGE_AUTHOR_PREFIX}{install_id}"
    created = {"datasets": [], "saved_queries": []}
    desired_dataset_ids = {
        projection_bridge_id(
            install_id,
            str(row["projection"].get("dataset_id") or row.get("artifact_id") or "").strip(),
        )
        for row in datasets
    }
    desired_query_ids = {
        projection_bridge_id(
            install_id,
            str(row["projection"].get("query_template_id") or row.get("artifact_id") or "").strip(),
        )
        for row in saved_queries
    }
    cleanup_methods = (
        "list_datasets",
        "list_dataset_query_templates",
        "purge_dataset",
        "purge_dataset_query_template",
    )
    if all(callable(getattr(store, name, None)) for name in cleanup_methods):
        remove_projected_dataset_bridges(
            store,
            install_id=install_id,
            keep_dataset_ids=desired_dataset_ids,
            keep_saved_query_ids=desired_query_ids,
        )

    for row in datasets:
        snapshot = row["projection"]
        local_id = str(snapshot.get("dataset_id") or row.get("artifact_id") or "").strip()
        bridge_id = projection_bridge_id(install_id, local_id)
        existing = store.get_dataset(bridge_id, include_trashed=True)
        _assert_owned_or_absent(existing, install_id=install_id)
        metadata = snapshot.get("metadata_jsonb") or snapshot.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        resource_scope = snapshot.get("resource_scope_jsonb") or snapshot.get("resource_scope")
        if isinstance(resource_scope, dict):
            metadata["resource_scope_jsonb"] = dict(resource_scope)
        metadata["account_id"] = account_id
        metadata["projection_bridge"] = projection_bridge_metadata(
            install_id=install_id,
            release_id=release_id,
            artifact_kind="dataset",
            artifact_id=local_id,
        )
        store.save_dataset(
            {
                **snapshot,
                "dataset_id": bridge_id,
                "dataset_key": bridge_id,
                "account_id": account_id,
                "workspace_id": None,
                "availability_mode": "specific_projects_only",
                "metadata_jsonb": metadata,
            },
            author=author,
        )
        created["datasets"].append(bridge_id)

    for row in saved_queries:
        snapshot = row["projection"]
        local_id = str(snapshot.get("query_template_id") or row.get("artifact_id") or "").strip()
        local_dataset_id = str(snapshot.get("dataset_id") or "").strip()
        bridge_id = projection_bridge_id(install_id, local_id)
        existing = store.get_dataset_query_template(bridge_id, include_trashed=True)
        _assert_owned_or_absent(existing, install_id=install_id)
        validation = snapshot.get("validation_result_jsonb") or snapshot.get("validation_result")
        validation = dict(validation) if isinstance(validation, dict) else {}
        validation["projection_bridge"] = projection_bridge_metadata(
            install_id=install_id,
            release_id=release_id,
            artifact_kind="saved_query",
            artifact_id=local_id,
        )
        store.save_dataset_query_template(
            {
                **snapshot,
                "query_template_id": bridge_id,
                "dataset_id": projection_bridge_id(install_id, local_dataset_id),
                "account_id": account_id,
                "validation_result_jsonb": validation,
                "created_by": author,
            },
            author=author,
        )
        created["saved_queries"].append(bridge_id)

    return created


__all__ = [
    "assert_projection_bridge_mutable",
    "materialize_projected_dataset_bridges",
    "projection_bridge_id",
    "projection_bridge_metadata",
    "projection_bridge_owner",
    "remove_projected_dataset_bridges",
]
