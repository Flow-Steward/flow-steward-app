"""Application services for system monitoring payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.application.lifecycle.runtime_installation_context import (
    resolve_projected_runtime_installation_context,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.monitoring.queries import (
    ChannelProjection,
    ComponentsProjection,
    RuntimeStatusQueryService,
)
from core.application.monitoring.repository import RuntimeStatusRepository
from core.infrastructure.config.connection_strings import resolve_postgres_dsn
from core.infrastructure.config.monitoring_runtime import load_monitoring_runtime_config
from core.infrastructure.monitoring.system_storage import (
    load_memory_storage_usage,
    load_monitoring_counts,
)
from core.infrastructure.runtime_env import runtime_env

ORPHAN_STORAGE_CLEANUP_CONFIRM = "DELETE_ORPHAN_STORAGE"


def query_runtime_components(_bl: Any) -> ComponentsProjection | None:
    """Use independent pooled storage, never discovery or a logger transaction."""
    try:
        values = runtime_env.copy()
        context = resolve_projected_runtime_installation_context(values)
        config = load_monitoring_runtime_config(values)
    except Exception:
        return None
    try:
        dsn = resolve_postgres_dsn(values)
        if not dsn:
            raise ValueError("Monitoring database is not configured")
        repository = RuntimeStatusRepository(dsn, config=config)
        return RuntimeStatusQueryService(repository=repository, config=config).get_components(
            context, now=datetime.now(UTC)
        )
    except Exception:
        return ComponentsProjection(
            installation_id=context.installation_id,
            deployment_profile=context.deployment_profile,
            available=False,
            channel=ChannelProjection(state="unavailable"),
        )


def build_monitoring_meta(
    bl,
    *,
    app_version: str,
    env_flag,
    get_ready_queue_depth,
    local_auth_enabled,
    external_auth_enabled,
) -> dict[str, Any]:
    from core.application.jobs.queue_delivery_queries import (
        build_queue_delivery_summary,
        load_queue_delivery_attention_jobs,
        load_queue_delivery_counts,
    )

    meta: dict[str, Any] = {
        "version": app_version,
        "watchdog_enabled": env_flag("FS_WATCHDOG_ENABLED", True),
        "watchdog_auto_apply": env_flag("FS_WATCHDOG_AUTO_APPLY", True),
        "local_auth_enabled": local_auth_enabled(),
        "external_auth_enabled": external_auth_enabled(),
    }
    try:
        meta["ready_queue_depth"] = int(get_ready_queue_depth())
    except Exception as exc:
        meta["ready_queue_depth_error"] = str(exc)

    try:
        meta.update(load_monitoring_counts(bl))
        queue_delivery_counts = load_queue_delivery_counts(bl)
        meta["queue_delivery"] = build_queue_delivery_summary(
            queue_delivery_counts,
            attention_jobs=load_queue_delivery_attention_jobs(bl),
        )
    except Exception as exc:
        meta["query_error"] = str(exc)
    return meta


def build_monitoring_summary_payload(
    *,
    readiness: dict[str, Any],
    meta: dict[str, Any],
    components: ComponentsProjection | None = None,
) -> dict[str, Any]:
    checks = readiness.get("checks") or {}
    failed_checks = [
        name for name, payload in checks.items() if not bool((payload or {}).get("ok"))
    ]
    ready = bool(readiness.get("ready"))
    payload: dict[str, Any] = {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "checks": checks,
        "meta": meta,
        "summary": {
            "checks_total": len(checks),
            "checks_ok": len(checks) - len(failed_checks),
            "checks_failed": len(failed_checks),
            "failed_checks": failed_checks,
        },
        "time": datetime.now(UTC).isoformat(),
    }
    if components is not None:
        payload["components"] = components.model_dump(mode="json")
    return payload


def _project_scope_state(project_row: dict[str, Any] | None) -> str:
    if not isinstance(project_row, dict):
        return "missing"
    return "trashed" if project_row.get("deleted_at") else "active"


def _normalize_storage_scope(value: object | None) -> str:
    return str(value or "").strip()


def _enrich_memory_storage_rows(memory_rows: dict[str, dict[str, Any]]) -> None:
    for row in memory_rows.values():
        project_state = str(row.get("project_state") or "").strip()
        cleanup_supported = project_state == "missing"
        row["storage_scope"] = str(row.get("project_id") or "").strip()
        row["cleanup_supported"] = cleanup_supported
        row["cleanup_confirm"] = ORPHAN_STORAGE_CLEANUP_CONFIRM if cleanup_supported else ""
        if cleanup_supported:
            row["resolution_hint"] = (
                "No project row exists for this memory project id. Clean this orphaned scope."
            )
        elif project_state == "trashed":
            row["resolution_hint"] = (
                "Project is in trash. Storage is retained for restore and will be removed "
                "when the project is hard-deleted."
            )
        else:
            row["resolution_hint"] = "Memory scope resolved by stable project id."


def build_storage_monitoring_payload(bl) -> dict[str, Any]:
    from core.infrastructure.config.memory_config import (
        load_memory_runtime_config,
        redacted_memory_backend_matrix,
    )
    from core.infrastructure.db.workflow_knowledge_store import PostgresAgeWorkflowKnowledgeStore

    config = load_memory_runtime_config()
    backend_matrix = redacted_memory_backend_matrix(config)

    if not getattr(bl, "conn", None):
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "backend_matrix": backend_matrix,
            "summary": {},
            "memory": {"available": False, "error": "Database not configured"},
            "vector": {"available": False, "backend": config.vector_backend},
        }

    memory_storage = load_memory_storage_usage(bl)
    project_rows_by_id: dict[str, dict[str, Any]] = memory_storage["project_rows_by_id"]
    memory_rows: dict[str, dict[str, Any]] = memory_storage["memory_rows"]
    orphan_memory_rows: list[dict[str, Any]] = memory_storage["orphan_memory_rows"]
    has_memory_embedding_column = bool(memory_storage["embedding_column_available"])
    _enrich_memory_storage_rows(memory_rows)

    memory_payload_rows = list(memory_rows.values())
    memory_payload = {
        "available": True,
        "embedding_column_available": has_memory_embedding_column,
        "rows": memory_payload_rows,
        "orphan_rows": orphan_memory_rows,
        "totals": {
            "projects": len(memory_rows),
            "orphan_projects": len(orphan_memory_rows),
            "records": sum(int(row.get("records") or 0) for row in memory_rows.values()),
            "embedded_records": sum(
                int(row.get("embedded_records") or 0) for row in memory_rows.values()
            ),
            "entities": sum(int(row.get("entities") or 0) for row in memory_rows.values()),
            "edges": sum(int(row.get("edges") or 0) for row in memory_rows.values()),
        },
    }

    vector_backend = str(config.vector_backend or "null").strip().lower() or "null"
    vector_rows = [
        {
            "project_id": row.get("project_id"),
            "project_name": row.get("project_name"),
            "project_state": row.get("project_state"),
            "storage_scope": row.get("storage_scope"),
            "cleanup_supported": row.get("cleanup_supported"),
            "cleanup_confirm": row.get("cleanup_confirm"),
            "resolution_hint": row.get("resolution_hint"),
            "records": row.get("embedded_records", 0),
        }
        for row in memory_payload_rows
        if int(row.get("embedded_records") or 0) > 0
    ]
    orphan_vector_rows = [
        {
            "project_id": row.get("project_id"),
            "project_name": row.get("project_name"),
            "project_state": row.get("project_state"),
            "storage_scope": row.get("storage_scope"),
            "cleanup_supported": row.get("cleanup_supported"),
            "cleanup_confirm": row.get("cleanup_confirm"),
            "resolution_hint": row.get("resolution_hint"),
            "records": row.get("embedded_records", 0),
        }
        for row in orphan_memory_rows
        if int(row.get("embedded_records") or 0) > 0
    ]
    vector_payload = {
        "configured": vector_backend not in {"", "null", "none"},
        "available": vector_backend not in {"", "null", "none"} and has_memory_embedding_column,
        "backend": vector_backend,
        "count_mode": (
            "exact_embedded_rows"
            if vector_backend == "postgres_pgvector"
            else "estimated_from_embeddings"
        ),
        "collection": "",
        "url": "",
        "rows": vector_rows,
        "orphan_rows": orphan_vector_rows,
    }
    vector_payload["totals"] = {
        "projects": len(vector_rows),
        "orphan_projects": len(orphan_vector_rows),
        "records": sum(int(row.get("records") or 0) for row in vector_rows),
        "orphan_records": sum(int(row.get("records") or 0) for row in orphan_vector_rows),
    }
    workflow_knowledge_payload: dict[str, Any]
    try:
        workflow_knowledge_status = PostgresAgeWorkflowKnowledgeStore().runtime_status(
            ensure_indexes=False
        )
        workflow_knowledge_payload = {
            "available": bool(workflow_knowledge_status.get("ok")),
            "graph_name": workflow_knowledge_status.get("graph_name") or "",
            "graph": workflow_knowledge_status.get("graph") or {},
            "vector": workflow_knowledge_status.get("vector") or {},
        }
    except Exception as exc:
        workflow_knowledge_payload = {
            "available": False,
            "graph_name": "",
            "graph": {"available": False, "error": str(exc)},
            "vector": {"available": False, "error": str(exc)},
        }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "backend_matrix": backend_matrix,
        "summary": {
            "active_projects": len(
                [
                    row
                    for row in project_rows_by_id.values()
                    if _project_scope_state(row) == "active"
                ]
            ),
            "trashed_projects": len(
                [
                    row
                    for row in project_rows_by_id.values()
                    if _project_scope_state(row) == "trashed"
                ]
            ),
            "orphan_memory_projects": len(orphan_memory_rows),
            "orphan_vector_projects": len(orphan_vector_rows),
        },
        "memory": memory_payload,
        "vector": vector_payload,
        "workflow_knowledge": workflow_knowledge_payload,
    }


def _db_project_row_by_id(bl, project_id: str) -> dict[str, Any] | None:
    normalized_project_id = _normalize_storage_scope(project_id)
    if not normalized_project_id:
        return None
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, name, deleted_at
            FROM projects
            WHERE id::text = %s
            LIMIT 1
            """,
            (normalized_project_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return {
            "id": str(row.get("id") or "").strip(),
            "name": str(row.get("name") or "").strip(),
            "deleted_at": row.get("deleted_at"),
        }
    return {
        "id": str(row[0] or "").strip(),
        "name": str(row[1] or "").strip(),
        "deleted_at": row[2],
    }


def _memory_scope_counts(bl, *, project_id: str) -> dict[str, int]:
    with bl.conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM memory_records WHERE project_id::text = %s",
            (project_id,),
        )
        records = int((cur.fetchone() or [0])[0] or 0)
        cur.execute(
            "SELECT COUNT(*) FROM memory_entities WHERE project_id::text = %s",
            (project_id,),
        )
        entities = int((cur.fetchone() or [0])[0] or 0)
        cur.execute(
            "SELECT COUNT(*) FROM memory_edges WHERE project_id::text = %s",
            (project_id,),
        )
        edges = int((cur.fetchone() or [0])[0] or 0)
    return {"records": records, "entities": entities, "edges": edges}


def _delete_memory_project_scope(bl, *, project_id: str) -> dict[str, int]:
    deleted: dict[str, int] = {}
    with bl.conn.cursor() as cur:
        cur.execute(
            "DELETE FROM memory_edges WHERE project_id::text = %s",
            (project_id,),
        )
        deleted["edges"] = int(cur.rowcount or 0)
        cur.execute(
            """
            DELETE FROM memory_record_entities
            WHERE record_id IN (
                SELECT id FROM memory_records WHERE project_id::text = %s
            )
            OR entity_id IN (
                SELECT id FROM memory_entities WHERE project_id::text = %s
            )
            """,
            (project_id, project_id),
        )
        deleted["record_entities"] = int(cur.rowcount or 0)
        cur.execute(
            "DELETE FROM memory_records WHERE project_id::text = %s",
            (project_id,),
        )
        deleted["records"] = int(cur.rowcount or 0)
        cur.execute(
            """
            DELETE FROM memory_entity_aliases
            WHERE entity_id IN (
                SELECT id FROM memory_entities WHERE project_id::text = %s
            )
            """,
            (project_id,),
        )
        deleted["entity_aliases"] = int(cur.rowcount or 0)
        cur.execute(
            "DELETE FROM memory_entities WHERE project_id::text = %s",
            (project_id,),
        )
        deleted["entities"] = int(cur.rowcount or 0)
    commit = getattr(bl.conn, "commit", None)
    if callable(commit):
        commit()
    return deleted


def _cleanup_memory_or_vector_orphan_storage_scope(
    bl,
    *,
    backend: str,
    storage_scope: str,
) -> dict[str, Any]:
    project_id = storage_scope
    if not project_id:
        raise ServiceValidationError("project_id is required for memory storage cleanup")
    project_row = _db_project_row_by_id(bl, project_id)
    if project_row is not None:
        project_state = _project_scope_state(project_row)
        raise ServiceConflictError(
            "Memory storage scope still belongs to a project",
            payload={
                "error": "Memory storage scope still belongs to a project",
                "project_id": project_id,
                "project_name": project_row.get("name") or "",
                "project_state": project_state,
            },
        )
    counts = _memory_scope_counts(bl, project_id=project_id)
    if not any(counts.values()):
        raise ServiceNotFoundError(f"Memory storage scope '{storage_scope}' was not found")
    deleted = _delete_memory_project_scope(bl, project_id=project_id)
    return {
        "status": "deleted",
        "backend": backend,
        "scope": storage_scope,
        "project_id": project_id,
        "detected": counts,
        "deleted": deleted,
        "message": "Orphaned memory storage scope deleted.",
    }


def cleanup_orphan_storage_scope(
    bl,
    *,
    backend: str,
    scope: str,
    confirm: str,
) -> dict[str, Any]:
    if confirm != ORPHAN_STORAGE_CLEANUP_CONFIRM:
        raise ServiceValidationError("cleanup confirmation token is required")
    if not getattr(bl, "conn", None):
        raise ServiceUnavailableError("Database not configured")

    normalized_backend = _normalize_storage_scope(backend).lower()
    storage_scope = _normalize_storage_scope(scope)
    if not storage_scope:
        raise ServiceValidationError("storage scope is required")

    if normalized_backend in {"memory", "vector"}:
        return _cleanup_memory_or_vector_orphan_storage_scope(
            bl,
            backend=normalized_backend,
            storage_scope=storage_scope,
        )
    raise ServiceValidationError(f"Unsupported storage backend '{backend}'")


__all__ = [
    "ORPHAN_STORAGE_CLEANUP_CONFIRM",
    "build_monitoring_meta",
    "build_monitoring_summary_payload",
    "build_storage_monitoring_payload",
    "cleanup_orphan_storage_scope",
]
