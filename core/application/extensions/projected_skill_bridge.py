"""Declarative identity anchors for install-owned projected skills."""

from __future__ import annotations

from typing import Any

from core.application.extensions.projected_dataset_bridge import projection_bridge_id


def _payload(row: Any) -> dict[str, Any]:
    if hasattr(row, "model_dump"):
        value = row.model_dump(mode="json")
    elif isinstance(row, dict):
        value = dict(row)
    else:
        return {}
    projection = value.get("projection")
    value["projection"] = dict(projection) if isinstance(projection, dict) else {}
    return value


def materialize_projected_skill_anchors(
    store: Any,
    *,
    account_id: str,
    install_id: str,
    release_id: str,
    projections: list[Any],
) -> set[str]:
    """Persist metadata and declared I/O only for active skills in this release."""
    desired: set[str] = set()
    for raw in projections:
        row = _payload(raw)
        snapshot = row["projection"]
        if (
            str(row.get("artifact_kind") or "").strip() != "skill"
            or str(row.get("account_id") or "").strip() != account_id
            or str(row.get("install_id") or "").strip() != install_id
            or str(row.get("release_id") or "").strip() != release_id
            or not bool(row.get("is_active", True))
        ):
            continue
        local_id = str(snapshot.get("skill_id") or row.get("artifact_id") or "").strip()
        if not local_id:
            continue
        skill_id = projection_bridge_id(install_id, local_id)
        desired.add(skill_id)
        if not callable(getattr(store, "ensure_projected_skill_anchor", None)):
            continue
        store.ensure_projected_skill_anchor(
            {
                **snapshot,
                "skill_id": skill_id,
                "source_install_id": install_id,
                "source_release_id": release_id,
            },
            account_id=account_id,
        )
    if callable(getattr(store, "delete_projected_skill_anchors", None)):
        store.delete_projected_skill_anchors(
            account_id=account_id,
            install_id=install_id,
            keep_skill_ids=desired,
        )
    if callable(getattr(store, "refresh_projected_skill_dependents", None)):
        store.refresh_projected_skill_dependents(desired, account_id=account_id)
    return desired


def remove_projected_skill_anchors(store: Any, *, account_id: str, install_id: str) -> list[str]:
    """Remove unused identity anchors after a release is disabled or uninstalled."""
    remove = getattr(store, "delete_projected_skill_anchors", None)
    if not callable(remove):
        return []
    return remove(account_id=account_id, install_id=install_id)
