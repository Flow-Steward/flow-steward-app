"""Runtime entrypoint for refreshing workflow discovery knowledge out of band."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.auth.config import DEFAULT_TENANT_ID
from core.application.workflows.workflow_knowledge_sync import (
    sync_workflow_knowledge_catalog_if_stale,
)


def _bool_payload(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def run_workflow_knowledge_sync_job(
    payload: Mapping[str, Any] | None = None,
    *,
    db: Any = None,
    store: Any = None,
) -> dict[str, Any]:
    """Refresh the scoped workflow-knowledge index when its catalog digest is stale.

    This function is intentionally safe to call from a control-plane/scheduled job:
    it performs the digest check first and skips writes for fresh scopes, keeping
    live workflow authoring requests out of the sync path.
    """

    job_payload = dict(payload or {})
    account_id = str(job_payload.get("account_id") or "").strip()
    if not account_id:
        raise ValueError("account_id is required for workflow knowledge sync jobs")
    # Default to the canonical tenant so sync writes land in the same scope live
    # discovery reads from.
    tenant_id = str(job_payload.get("tenant_id") or "").strip() or DEFAULT_TENANT_ID
    project_id = str(job_payload.get("project_id") or "").strip() or None

    if db is None:
        from core.application.registry.registry_manager import RegistryManager

        db = RegistryManager.instance().db_store
    if db is None:
        raise RuntimeError("Registry DB backend not configured")
    if store is None:
        from core.infrastructure.db.workflow_knowledge_store import (
            PostgresAgeWorkflowKnowledgeStore,
        )

        store = PostgresAgeWorkflowKnowledgeStore(dsn=getattr(db, "dsn", None))

    return sync_workflow_knowledge_catalog_if_stale(
        store=store,
        db=db,
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
        dry_run=_bool_payload(job_payload.get("dry_run"), False),
        include_capability_catalog=_bool_payload(
            job_payload.get("include_capability_catalog"),
            True,
        ),
        ensure_indexes=_bool_payload(job_payload.get("ensure_indexes"), True),
    )
