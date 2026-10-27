from __future__ import annotations

import json
import logging
from typing import Any

from core.infrastructure.config.memory_config import (
    MemoryRuntimeConfig,
    load_memory_runtime_config,
    redacted_memory_backend_matrix,
    required_postgres_extensions,
    validate_memory_runtime_config,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_EMBEDDING_DIMENSION = 1536


def _expected_embedding_dimension() -> int:
    # Runtime memory embeddings currently come from text-embedding-3-small via
    # _embed_query(), so external vector stores must match that fixed dimension.
    return DEFAULT_MEMORY_EMBEDDING_DIMENSION


def _postgres_extension_errors(
    bl: Any,
    config: MemoryRuntimeConfig,
    *,
    require_connection: bool = True,
) -> list[str]:
    required = required_postgres_extensions(config)
    needs_pgvector_column = config.vector_backend == "postgres_pgvector"
    if not required and not needs_pgvector_column:
        return []
    if bl is None or getattr(bl, "conn", None) is None:
        if not require_connection:
            return []
        return ["Postgres memory backend validation requires a live database connection."]
    errors: list[str] = []
    try:
        with bl.conn.cursor() as cur:
            if required:
                cur.execute(
                    """
                    SELECT extname
                    FROM pg_extension
                    WHERE extname = ANY(%s)
                    """,
                    [list(required)],
                )
                installed = {str(row[0]) for row in cur.fetchall()}
                missing = sorted(set(required) - installed)
                if missing:
                    errors.append(f"Missing required Postgres extension(s): {', '.join(missing)}.")
            if needs_pgvector_column:
                cur.execute(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'memory_records'
                      AND column_name = 'embedding'
                    """
                )
                if cur.fetchone() is None:
                    errors.append(
                        "Missing memory_records.embedding pgvector column. "
                        "Apply the canonical database schema or run the Postgres bootstrap."
                    )
    except Exception as exc:
        return [f"Postgres extension validation failed: {exc}"]
    return errors


def _env_flag(name: str, default: bool) -> bool:
    raw = str(_runtime_env.get(name, "1" if default else "0") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _workflow_knowledge_runtime_check(
    *,
    ensure_indexes: bool,
    account_id: str = "",
    tenant_id: str = "",
    project_id: str | None = None,
    catalog_digest: str = "",
) -> tuple[list[str], dict[str, Any]]:
    from core.infrastructure.db.workflow_knowledge_store import PostgresAgeWorkflowKnowledgeStore

    store = PostgresAgeWorkflowKnowledgeStore()
    normalized_account_id = str(account_id or "").strip()
    if normalized_account_id:
        status = store.scope_status(
            account_id=normalized_account_id,
            tenant_id=str(tenant_id or "").strip(),
            project_id=project_id,
            expected_digest=str(catalog_digest or "").strip(),
            ensure_indexes=ensure_indexes,
        )
    else:
        status = store.runtime_status(ensure_indexes=ensure_indexes)
    errors: list[str] = []
    schema_status = dict(status.get("schema") or {})
    if schema_status and not bool(schema_status.get("available")):
        missing_relations = ", ".join(schema_status.get("missing_relations") or [])
        missing_indexes = ", ".join(schema_status.get("missing_indexes") or [])
        details = []
        if missing_relations:
            details.append(f"missing relations: {missing_relations}")
        if missing_indexes:
            details.append(f"missing indexes: {missing_indexes}")
        errors.append(
            "Canonical workflow knowledge schema is incomplete"
            + (f" ({'; '.join(details)})" if details else ".")
        )
    if not bool(status.get("graph", {}).get("available")):
        graph_error = str(status.get("graph", {}).get("error") or "").strip()
        errors.append(
            "Workflow knowledge graph backend unavailable"
            + (f": {graph_error}" if graph_error else ".")
        )
    if not bool(status.get("vector", {}).get("available")):
        vector_error = str(status.get("vector", {}).get("error") or "").strip()
        errors.append(
            "Workflow knowledge vector backend unavailable"
            + (f": {vector_error}" if vector_error else ".")
        )
    scope_status = dict(status.get("scope") or {})
    if scope_status:
        if bool(scope_status.get("empty")):
            errors.append(
                "Workflow knowledge index is empty for the requested account/project scope."
            )
        elif scope_status.get("freshness") == "stale":
            errors.append(
                "Workflow knowledge index is stale for the requested account/project scope."
            )
    return errors, status


def validate_memory_preflight(
    *,
    bl: Any = None,
    config: MemoryRuntimeConfig | None = None,
    check_connectivity: bool = True,
    check_workflow_knowledge: bool = False,
    workflow_knowledge_account_id: str = "",
    workflow_knowledge_tenant_id: str = "",
    workflow_knowledge_project_id: str | None = None,
    workflow_knowledge_catalog_digest: str = "",
) -> dict[str, Any]:
    resolved = config or load_memory_runtime_config()
    errors = validate_memory_runtime_config(resolved)
    warnings: list[str] = []
    matrix = redacted_memory_backend_matrix(resolved)
    workflow_knowledge: dict[str, Any] = {
        "checked": False,
        # Discovery and startup preflight share the same fallback policy.
        "required": not _env_flag("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", True),
        "ok": True,
        "schema": {},
        "graph": {},
        "vector": {},
        "scope": {},
        "error": "",
    }

    errors.extend(_postgres_extension_errors(bl, resolved, require_connection=check_connectivity))
    if check_workflow_knowledge:
        workflow_knowledge["checked"] = True
        try:
            wk_errors, wk_status = _workflow_knowledge_runtime_check(
                ensure_indexes=check_connectivity,
                account_id=workflow_knowledge_account_id,
                tenant_id=workflow_knowledge_tenant_id,
                project_id=workflow_knowledge_project_id,
                catalog_digest=workflow_knowledge_catalog_digest,
            )
            workflow_knowledge.update(
                {
                    "ok": not wk_errors,
                    "schema": dict(wk_status.get("schema") or {}),
                    "graph": dict(wk_status.get("graph") or {}),
                    "vector": dict(wk_status.get("vector") or {}),
                    "scope": dict(wk_status.get("scope") or {}),
                    "error": "; ".join(wk_errors),
                }
            )
            if wk_errors:
                if workflow_knowledge["required"]:
                    errors.extend(wk_errors)
                else:
                    warnings.extend(wk_errors)
        except Exception as exc:
            workflow_knowledge.update({"ok": False, "error": str(exc)})
            message = f"Workflow knowledge runtime validation failed: {exc}"
            if workflow_knowledge["required"]:
                errors.append(message)
            else:
                warnings.append(message)
    ok = not errors
    return {
        "ok": ok,
        "matrix": matrix,
        "errors": errors,
        "warnings": warnings,
        "workflow_knowledge": workflow_knowledge,
    }


def enforce_memory_preflight(*, bl: Any = None) -> dict[str, Any]:
    result = validate_memory_preflight(bl=bl, check_workflow_knowledge=True)
    logger.info("Memory backend matrix: %s", json.dumps(result["matrix"], sort_keys=True))
    if not result["ok"]:
        raise RuntimeError("Memory backend preflight failed: " + " ".join(result["errors"]))
    return result
