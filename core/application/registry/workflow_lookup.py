"""Canonical lookup for platform and extension-projected workflow rows."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from core.application.registry.workflow_account import workflow_account_scope, workflow_owner


def get_registry_artifact_row(
    db_store: Any,
    *,
    artifact_kind: str,
    id_key: str,
    artifact_id: str,
    platform_getter: str,
    account_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve one platform-owned or active extension-projected registry row."""
    normalized = str(artifact_id or "").strip()
    if not normalized or db_store is None:
        return None

    getter = getattr(db_store, platform_getter, None)
    if callable(getter):
        with _account_binding(account_id if artifact_kind == "workflow" else None):
            row = getter(normalized)
        if isinstance(row, dict):
            if artifact_kind == "skill" and _is_skill_projection_anchor(row):
                if str(row.get("account_id") or "").strip() != workflow_owner(account_id):
                    return None
                from core.application.registry.projected_artifact_rows import (
                    projected_artifact_rows,
                )

                return next(
                    (
                        dict(projected)
                        for projected in projected_artifact_rows(
                            db_store, "skill", "skill_id", account_id=workflow_owner(account_id)
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
                db_store, artifact_kind, id_key, account_id=workflow_owner(account_id)
            )
            if isinstance(row, dict) and str(row.get(id_key) or "").strip() == normalized
        ),
        None,
    )


def _account_binding(account_id: str | None):
    """Bind an explicit account around a store call; keep the current binding otherwise.

    Store getters read the bound account when they are given none, so a lookup for
    another account binds it here instead of passing a keyword every getter (and
    every stand-in store) would have to accept (ADR 0020, C4b).
    """
    if str(account_id or "").strip():
        return workflow_account_scope(account_id)
    return nullcontext()


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
    account_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a workflow of *account_id* from the platform table or active projections.

    Workflow identity is account-qualified (ADR 0020, C4b): the platform row is the
    one *account_id* owns, never another account's workflow with the same id.
    """
    normalized = str(workflow_id or "").strip()
    getter = getattr(db_store, "get_workflow", None)
    with _account_binding(account_id):
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
                    account_id=workflow_owner(account_id),
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
