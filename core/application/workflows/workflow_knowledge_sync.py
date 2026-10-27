from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.workflows.knowledge_projection import build_workflow_catalog_projection

from .discovery.capability_discovery import WorkflowCapabilityDiscovery


def catalog_sync_digest(
    db: Any,
    *,
    account_id: str,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> str:
    discovery = WorkflowCapabilityDiscovery(db)
    scope: dict[str, str] = {"account_id": str(account_id or "").strip()}
    normalized_tenant_id = str(tenant_id or "").strip()
    if normalized_tenant_id:
        scope["tenant_id"] = normalized_tenant_id
    normalized_project_id = str(project_id or "").strip()
    if normalized_project_id:
        scope["project_id"] = normalized_project_id
    return discovery._catalog_sync_digest(scope=scope, project_id=normalized_project_id)


def projection_counts(projection: Any) -> dict[str, int]:
    return {
        "documents": len(list(getattr(projection, "documents", None) or [])),
        "nodes": len(list(getattr(projection, "nodes", None) or [])),
        "edges": len(list(getattr(projection, "edges", None) or [])),
    }


def workflow_knowledge_scope_status(
    *,
    store: Any,
    db: Any,
    account_id: str,
    tenant_id: str | None = None,
    project_id: str | None = None,
    ensure_indexes: bool = False,
) -> dict[str, Any]:
    digest = catalog_sync_digest(
        db,
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    status = store.scope_status(
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
        expected_digest=digest,
        ensure_indexes=ensure_indexes,
    )
    return {
        "catalog_digest": digest,
        "status": status,
    }


def sync_workflow_knowledge_catalog(
    *,
    store: Any,
    db: Any,
    account_id: str,
    tenant_id: str | None = None,
    project_id: str | None = None,
    dry_run: bool = False,
    include_capability_catalog: bool = True,
    ensure_indexes: bool = True,
) -> dict[str, Any]:
    digest = catalog_sync_digest(
        db,
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    before = store.scope_status(
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
        expected_digest=digest,
        ensure_indexes=ensure_indexes,
    )
    projection = build_workflow_catalog_projection(
        db,
        include_capability_catalog=include_capability_catalog,
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    return _sync_prepared_workflow_knowledge_projection(
        store=store,
        projection=projection,
        digest=digest,
        before=before,
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
        dry_run=dry_run,
    )


def _sync_prepared_workflow_knowledge_projection(
    *,
    store: Any,
    projection: Any,
    digest: str,
    before: Mapping[str, Any],
    account_id: str,
    tenant_id: str | None,
    project_id: str | None,
    dry_run: bool,
    prepared_write: Any | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "account_id": str(account_id or "").strip(),
        "tenant_id": str(tenant_id or "").strip(),
        "project_id": str(project_id or "").strip(),
        "catalog_digest": digest,
        "projection_counts": projection_counts(projection),
        "before": before,
        "after": before,
    }
    if dry_run:
        return result
    if prepared_write is None:
        store.upsert_projection(
            projection=projection,
            account_id=account_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
    else:
        store.apply_prepared_projection(prepared=prepared_write)
    store.set_scope_sync_digest(
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
        digest=digest,
    )
    result["after"] = store.scope_status(
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
        expected_digest=digest,
        ensure_indexes=False,
    )
    return result


def sync_workflow_knowledge_catalog_if_stale(
    *,
    store: Any,
    db: Any,
    account_id: str,
    tenant_id: str | None = None,
    project_id: str | None = None,
    dry_run: bool = False,
    include_capability_catalog: bool = True,
    ensure_indexes: bool = True,
) -> dict[str, Any]:
    digest = catalog_sync_digest(
        db,
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    status = store.scope_status(
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
        expected_digest=digest,
        ensure_indexes=ensure_indexes,
    )
    scope = dict(status.get("scope") or {})
    if scope.get("freshness") == "fresh":
        return {
            "dry_run": bool(dry_run),
            "skipped": True,
            "skip_reason": "workflow_knowledge_scope_fresh",
            "account_id": str(account_id or "").strip(),
            "tenant_id": str(tenant_id or "").strip(),
            "project_id": str(project_id or "").strip(),
            "catalog_digest": digest,
            "before": status,
            "after": status,
        }
    with store.scope_sync_lock(
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
    ):
        digest = catalog_sync_digest(
            db,
            account_id=account_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        status = store.scope_status(
            account_id=account_id,
            tenant_id=tenant_id,
            project_id=project_id,
            expected_digest=digest,
            ensure_indexes=ensure_indexes,
        )
        scope = dict(status.get("scope") or {})
        if scope.get("freshness") == "fresh":
            return {
                "dry_run": bool(dry_run),
                "skipped": True,
                "skip_reason": "workflow_knowledge_scope_fresh",
                "account_id": str(account_id or "").strip(),
                "tenant_id": str(tenant_id or "").strip(),
                "project_id": str(project_id or "").strip(),
                "catalog_digest": digest,
                "before": status,
                "after": status,
            }
        projection = build_workflow_catalog_projection(
            db,
            include_capability_catalog=include_capability_catalog,
            account_id=account_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if dry_run:
            result = _sync_prepared_workflow_knowledge_projection(
                store=store,
                projection=projection,
                digest=digest,
                before=status,
                account_id=account_id,
                tenant_id=tenant_id,
                project_id=project_id,
                dry_run=True,
            )
        else:
            prepared_write = store.prepare_projection_write(
                projection=projection,
                account_id=account_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            with store.scope_sync_transaction():
                status = store.scope_status(
                    account_id=account_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    expected_digest=digest,
                    ensure_indexes=False,
                )
                scope = dict(status.get("scope") or {})
                if scope.get("freshness") == "fresh":
                    return {
                        "dry_run": False,
                        "skipped": True,
                        "skip_reason": "workflow_knowledge_scope_fresh",
                        "account_id": str(account_id or "").strip(),
                        "tenant_id": str(tenant_id or "").strip(),
                        "project_id": str(project_id or "").strip(),
                        "catalog_digest": digest,
                        "before": status,
                        "after": status,
                    }
                result = _sync_prepared_workflow_knowledge_projection(
                    store=store,
                    projection=projection,
                    digest=digest,
                    before=status,
                    account_id=account_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    dry_run=False,
                    prepared_write=prepared_write,
                )
        result["skipped"] = False
        result["trigger"] = "workflow_knowledge_scope_not_fresh"
        return result


def summarize_scope_status(payload: Mapping[str, Any]) -> str:
    status = dict(payload.get("status") or payload.get("after") or {})
    scope = dict(status.get("scope") or {})
    counts = dict(scope.get("counts") or {})
    return (
        f"workflow knowledge scope {scope.get('account_id') or '-'}"
        f"/{scope.get('tenant_id') or '-'}"
        f"/{scope.get('project_scope') or 'global'}: "
        f"{scope.get('freshness') or 'unknown'} "
        f"(docs={counts.get('documents', 0)}, nodes={counts.get('nodes', 0)}, "
        f"edges={counts.get('edges', 0)})"
    )
