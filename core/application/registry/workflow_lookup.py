"""Canonical lookup for platform and extension-projected workflow rows."""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID


def get_registry_artifact_row(
    db_store: Any,
    *,
    artifact_kind: str,
    id_key: str,
    artifact_id: str,
    platform_getter: str,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict[str, Any] | None:
    """Resolve one platform-owned or active extension-projected registry row."""
    normalized = str(artifact_id or "").strip()
    if not normalized or db_store is None:
        return None

    getter = getattr(db_store, platform_getter, None)
    if callable(getter):
        row = getter(normalized)
        if isinstance(row, dict):
            if artifact_kind == "skill" and _is_skill_projection_anchor(row):
                if str(row.get("account_id") or "").strip() != str(account_id or "").strip():
                    return None
                from core.application.registry.projected_artifact_rows import (
                    projected_artifact_rows,
                )

                return next(
                    (
                        dict(projected)
                        for projected in projected_artifact_rows(
                            db_store, "skill", "skill_id", account_id=account_id
                        )
                        if str(projected.get("skill_id") or "").strip() == normalized
                        and str(projected.get("source_install_id") or "").strip()
                        == str(row.get("source_install_id") or "").strip()
                    ),
                    None,
                )
            return dict(row)

    from core.application.registry.projected_artifact_rows import projected_artifact_rows

    return next(
        (
            dict(row)
            for row in projected_artifact_rows(
                db_store, artifact_kind, id_key, account_id=account_id
            )
            if isinstance(row, dict) and str(row.get(id_key) or "").strip() == normalized
        ),
        None,
    )


def is_projection_anchor(row: Any) -> bool:
    """Whether a platform workflow row is an installed extension's projection anchor.

    The anchor carries metadata only; the definition is the install's projection.
    """
    if not isinstance(row, dict):
        return False
    workflow_id = str(row.get("workflow_id") or "").strip()
    return str(row.get("projection_mode") or "").strip().lower() == "derived" or (
        workflow_id.startswith("extension.")
        and bool(str(row.get("source_install_id") or "").strip())
    )


def _is_skill_projection_anchor(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and str(row.get("skill_id") or "").strip().startswith("extension.")
        and bool(str(row.get("source_install_id") or "").strip())
    )


def get_workflow_row(
    db_store: Any,
    workflow_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict[str, Any] | None:
    """Resolve a workflow from either the platform table or active projections."""
    normalized = str(workflow_id or "").strip()
    getter = getattr(db_store, "get_workflow", None)
    platform_row = getter(normalized) if normalized and callable(getter) else None
    anchor = is_projection_anchor(platform_row)
    if isinstance(platform_row, dict) and not anchor:
        return dict(platform_row)
    if isinstance(platform_row, dict) and anchor:
        from core.application.registry.projected_artifact_rows import projected_artifact_rows

        projected = next(
            (
                dict(row)
                for row in projected_artifact_rows(
                    db_store,
                    "workflow",
                    "workflow_id",
                    account_id=account_id,
                )
                if isinstance(row, dict) and str(row.get("workflow_id") or "").strip() == normalized
            ),
            None,
        )
        if projected is not None:
            # Execution pinning uses this persisted anchor revision while the
            # active projection supplies its definition. Publisher metadata is
            # not that revision, and a missing anchor identity stays unknown.
            projected["revision"] = platform_row.get("revision")
            return projected
        return None
    return get_registry_artifact_row(
        db_store,
        artifact_kind="workflow",
        id_key="workflow_id",
        artifact_id=normalized,
        platform_getter="get_workflow",
        account_id=account_id,
    )


__all__ = ["get_registry_artifact_row", "get_workflow_row"]
