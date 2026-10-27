# mypy: ignore-errors
"""Scope digest and status methods for the workflow knowledge store."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .workflow_knowledge_store_common import (
    _SCOPE_SYNC_DIGEST_DOC_ID,
    _SCOPE_SYNC_DIGEST_ENTITY_KIND,
    _json_dumps,
    _scope_value,
    _tenant_value,
)


class WorkflowKnowledgeSyncMixin:
    def get_scope_sync_digest(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> str:
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            return ""
        normalized_tenant_id = _tenant_value(tenant_id)
        project_scope = _scope_value(project_id)
        self.enforce_runtime(ensure_indexes=False)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT content_text
                  FROM public.platform_workflow_knowledge_documents
                 WHERE account_id = %s
                   AND tenant_id = %s
                   AND project_scope = %s
                   AND doc_id = %s
                   AND entity_kind = %s
                 LIMIT 1
                """,
                (
                    normalized_account_id,
                    normalized_tenant_id,
                    project_scope,
                    _SCOPE_SYNC_DIGEST_DOC_ID,
                    _SCOPE_SYNC_DIGEST_ENTITY_KIND,
                ),
            )
            row = cur.fetchone()
        return str(row[0] or "").strip() if row else ""

    def scope_counts(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            return {
                "documents": 0,
                "sync_state_documents": 0,
                "nodes": 0,
                "edges": 0,
                "latest_indexed_at": "",
            }
        normalized_tenant_id = _tenant_value(tenant_id)
        project_scope = _scope_value(project_id)
        self.enforce_runtime(ensure_indexes=False)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH
                  docs AS (
                    SELECT
                      COUNT(*) FILTER (
                        WHERE NOT (
                          doc_id = %s
                          AND entity_kind = %s
                        )
                      ) AS document_count,
                      COUNT(*) FILTER (
                        WHERE doc_id = %s
                          AND entity_kind = %s
                      ) AS sync_state_document_count,
                      MAX(indexed_at) FILTER (
                        WHERE NOT (
                          doc_id = %s
                          AND entity_kind = %s
                        )
                      ) AS latest_document_indexed_at
                    FROM public.platform_workflow_knowledge_documents
                    WHERE account_id = %s
                      AND tenant_id = %s
                      AND project_scope = %s
                  ),
                  nodes AS (
                    SELECT COUNT(*) AS node_count, MAX(indexed_at) AS latest_node_indexed_at
                    FROM public.platform_workflow_knowledge_nodes
                    WHERE account_id = %s
                      AND tenant_id = %s
                      AND project_scope = %s
                  ),
                  edges AS (
                    SELECT COUNT(*) AS edge_count, MAX(indexed_at) AS latest_edge_indexed_at
                    FROM public.platform_workflow_knowledge_edges
                    WHERE account_id = %s
                      AND tenant_id = %s
                      AND project_scope = %s
                  )
                SELECT
                  docs.document_count,
                  docs.sync_state_document_count,
                  nodes.node_count,
                  edges.edge_count,
                  (
                    SELECT MAX(value)
                    FROM (
                      VALUES
                        (docs.latest_document_indexed_at),
                        (nodes.latest_node_indexed_at),
                        (edges.latest_edge_indexed_at)
                    ) AS latest(value)
                  ) AS latest_indexed_at
                FROM docs, nodes, edges
                """,
                (
                    _SCOPE_SYNC_DIGEST_DOC_ID,
                    _SCOPE_SYNC_DIGEST_ENTITY_KIND,
                    _SCOPE_SYNC_DIGEST_DOC_ID,
                    _SCOPE_SYNC_DIGEST_ENTITY_KIND,
                    _SCOPE_SYNC_DIGEST_DOC_ID,
                    _SCOPE_SYNC_DIGEST_ENTITY_KIND,
                    normalized_account_id,
                    normalized_tenant_id,
                    project_scope,
                    normalized_account_id,
                    normalized_tenant_id,
                    project_scope,
                    normalized_account_id,
                    normalized_tenant_id,
                    project_scope,
                ),
            )
            row = cur.fetchone()
        if not row:
            return {
                "documents": 0,
                "sync_state_documents": 0,
                "nodes": 0,
                "edges": 0,
                "latest_indexed_at": "",
            }
        latest_indexed_at = row[4]
        return {
            "documents": int(row[0] or 0),
            "sync_state_documents": int(row[1] or 0),
            "nodes": int(row[2] or 0),
            "edges": int(row[3] or 0),
            "latest_indexed_at": (
                latest_indexed_at.isoformat()
                if hasattr(latest_indexed_at, "isoformat")
                else str(latest_indexed_at or "")
            ),
        }

    def scope_status(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        expected_digest: str = "",
        ensure_indexes: bool = False,
    ) -> dict[str, Any]:
        runtime = self.runtime_status(ensure_indexes=ensure_indexes)
        normalized_account_id = str(account_id or "").strip()
        normalized_tenant_id = _tenant_value(tenant_id)
        project_scope = _scope_value(project_id)
        if not bool(runtime.get("ok")):
            return {
                **runtime,
                "scope": {
                    "account_id": normalized_account_id,
                    "tenant_id": normalized_tenant_id,
                    "project_scope": project_scope,
                    "counts": {
                        "documents": 0,
                        "sync_state_documents": 0,
                        "nodes": 0,
                        "edges": 0,
                        "latest_indexed_at": "",
                    },
                    "catalog_digest": str(expected_digest or "").strip(),
                    "stored_digest": "",
                    "digest_fresh": False,
                    "empty": True,
                    "freshness": "unavailable",
                    "ready": False,
                },
            }
        counts = self.scope_counts(
            account_id=normalized_account_id,
            tenant_id=normalized_tenant_id,
            project_id=project_scope,
        )
        stored_digest = self.get_scope_sync_digest(
            account_id=normalized_account_id,
            tenant_id=normalized_tenant_id,
            project_id=project_scope,
        )
        catalog_digest = str(expected_digest or "").strip()
        empty = not any(int(counts.get(key) or 0) > 0 for key in ("documents", "nodes", "edges"))
        digest_fresh = bool(catalog_digest and stored_digest and stored_digest == catalog_digest)
        if empty:
            freshness = "empty"
        elif not catalog_digest:
            freshness = "unknown"
        elif digest_fresh:
            freshness = "fresh"
        else:
            freshness = "stale"
        ready = bool(runtime.get("ok")) and not empty and (not catalog_digest or digest_fresh)
        return {
            **runtime,
            "scope": {
                "account_id": normalized_account_id,
                "tenant_id": normalized_tenant_id,
                "project_scope": project_scope,
                "counts": counts,
                "catalog_digest": catalog_digest,
                "stored_digest": stored_digest,
                "digest_fresh": digest_fresh,
                "empty": empty,
                "freshness": freshness,
                "ready": ready,
            },
        }

    def set_scope_sync_digest(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        digest: str,
    ) -> None:
        normalized_account_id = str(account_id or "").strip()
        normalized_digest = str(digest or "").strip()
        if not normalized_account_id or not normalized_digest:
            return
        normalized_tenant_id = _tenant_value(tenant_id)
        project_scope = _scope_value(project_id)
        self.enforce_runtime(ensure_indexes=False)
        indexed_at = datetime.now(UTC).isoformat()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.platform_workflow_knowledge_documents (
                    account_id,
                    tenant_id,
                    project_scope,
                    doc_id,
                    entity_id,
                    entity_kind,
                    title,
                    content_text,
                    metadata_jsonb,
                    embedding_jsonb,
                    indexed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id, tenant_id, project_scope, doc_id)
                DO UPDATE SET
                    entity_id = EXCLUDED.entity_id,
                    entity_kind = EXCLUDED.entity_kind,
                    title = EXCLUDED.title,
                    content_text = EXCLUDED.content_text,
                    metadata_jsonb = EXCLUDED.metadata_jsonb,
                    embedding_jsonb = EXCLUDED.embedding_jsonb,
                    indexed_at = EXCLUDED.indexed_at
                """,
                (
                    normalized_account_id,
                    normalized_tenant_id,
                    project_scope,
                    _SCOPE_SYNC_DIGEST_DOC_ID,
                    _SCOPE_SYNC_DIGEST_DOC_ID,
                    _SCOPE_SYNC_DIGEST_ENTITY_KIND,
                    "Catalog sync digest",
                    normalized_digest,
                    _json_dumps(
                        {
                            "digest": normalized_digest,
                            "kind": "catalog_sync_digest",
                            "tenant_id": normalized_tenant_id,
                        }
                    ),
                    None,
                    indexed_at,
                ),
            )
