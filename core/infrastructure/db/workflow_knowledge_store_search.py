# mypy: ignore-errors
"""Search, neighbor, and subgraph read methods for the workflow knowledge store."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.application.workflows.knowledge_projection import (
    WorkflowKnowledgeDocument,
    WorkflowKnowledgeEdge,
    WorkflowKnowledgeHit,
    WorkflowKnowledgeNeighbor,
    WorkflowKnowledgeNode,
    WorkflowKnowledgeProjection,
)
from core.infrastructure.db import psycopg_runtime

from .workflow_knowledge_store_common import (
    _ENTITY_KIND_SCORE_MULTIPLIERS,
    _scope_value,
    _tenant_value,
)


class WorkflowKnowledgeSearchMixin:
    def _search_text(
        self,
        *,
        query: str,
        account_id: str,
        tenant_id: str,
        project_scope: str,
        entity_kinds: Sequence[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return []
        pattern = f"%{normalized_query.lower()}%"
        kinds = [str(kind or "").strip() for kind in entity_kinds if str(kind or "").strip()]
        params: list[Any] = [
            normalized_query,
            pattern,
            pattern,
            account_id,
            tenant_id,
            project_scope,
        ]
        kind_clause = psycopg_runtime.sql.SQL("")
        if kinds:
            kind_clause = psycopg_runtime.sql.SQL(" AND entity_kind = ANY(%s)")
            params.append(kinds)
        params.extend([normalized_query, pattern, pattern, max(1, int(limit or 1))])
        with (
            self._connect() as conn,
            conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur,
        ):
            cur.execute(
                psycopg_runtime.sql.SQL(
                    """
                    SELECT
                        entity_id,
                        entity_kind,
                        title,
                        LEFT(content_text, 320) AS snippet,
                        metadata_jsonb,
                        GREATEST(
                            (
                                ts_rank_cd(
                                    to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content_text, '')),
                                    plainto_tsquery('simple', %s)
                                )
                                + CASE WHEN lower(content_text) LIKE %s THEN 1.0 ELSE 0.0 END
                                + CASE WHEN lower(title) LIKE %s THEN 0.75 ELSE 0.0 END
                            ) * 100,
                            0.0
                        ) AS text_relevance,
                        0.0 AS vector_relevance
                    FROM public.platform_workflow_knowledge_documents
                    WHERE account_id = %s
                      AND tenant_id = %s
                      AND project_scope = %s
                    """
                )
                + kind_clause
                + psycopg_runtime.sql.SQL(
                    """
                      AND (
                            to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content_text, ''))
                                @@ plainto_tsquery('simple', %s)
                         OR lower(content_text) LIKE %s
                         OR lower(title) LIKE %s
                      )
                    ORDER BY text_relevance DESC, entity_kind, entity_id
                    LIMIT %s
                    """
                ),
                params,
            )
            return [dict(row) for row in cur.fetchall()]

    def _search_vector(
        self,
        *,
        query_embedding: Sequence[float],
        account_id: str,
        tenant_id: str,
        project_scope: str,
        entity_kinds: Sequence[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        kinds = [str(kind or "").strip() for kind in entity_kinds if str(kind or "").strip()]
        params: list[Any] = [list(query_embedding), account_id, tenant_id, project_scope]
        kind_clause = psycopg_runtime.sql.SQL("")
        if kinds:
            kind_clause = psycopg_runtime.sql.SQL(" AND entity_kind = ANY(%s)")
            params.append(kinds)
        params.extend([list(query_embedding), max(1, int(limit or 1))])
        with (
            self._connect() as conn,
            conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur,
        ):
            cur.execute(
                psycopg_runtime.sql.SQL(
                    """
                    SELECT
                        entity_id,
                        entity_kind,
                        title,
                        LEFT(content_text, 320) AS snippet,
                        metadata_jsonb,
                        0.0 AS text_relevance,
                        GREATEST(0.0, 1.0 - (embedding <=> %s::vector)) * 100 AS vector_relevance
                    FROM public.platform_workflow_knowledge_documents
                    WHERE account_id = %s
                      AND tenant_id = %s
                      AND project_scope = %s
                    """
                )
                + kind_clause
                + psycopg_runtime.sql.SQL(
                    """
                      AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector, entity_kind, entity_id
                    LIMIT %s
                    """
                ),
                params,
            )
            return [dict(row) for row in cur.fetchall()]

    def search(
        self,
        *,
        query: str,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        entity_kinds: Sequence[str] = (),
        limit: int = 8,
    ) -> list[WorkflowKnowledgeHit]:
        normalized_query = str(query or "").strip()
        normalized_account_id = str(account_id or "").strip()
        if not normalized_query or not normalized_account_id:
            return []
        normalized_tenant_id = _tenant_value(tenant_id)
        project_scope = _scope_value(project_id)
        self.enforce_runtime(ensure_indexes=False)
        text_rows = self._search_text(
            query=normalized_query,
            account_id=normalized_account_id,
            tenant_id=normalized_tenant_id,
            project_scope=project_scope,
            entity_kinds=entity_kinds,
            limit=max(8, max(1, int(limit or 1)) * 4),
        )
        vector_rows: list[dict[str, Any]] = []
        if self._document_vector_column_available() and (
            query_embedding := self._embed_query(normalized_query, account_id=normalized_account_id)
        ):
            vector_rows = self._search_vector(
                query_embedding=query_embedding,
                account_id=normalized_account_id,
                tenant_id=normalized_tenant_id,
                project_scope=project_scope,
                entity_kinds=entity_kinds,
                limit=max(8, max(1, int(limit or 1)) * 4),
            )

        merged: dict[str, dict[str, Any]] = {}

        def _payload(row: dict[str, Any]) -> dict[str, Any]:
            return {
                "entity_id": str(row.get("entity_id") or ""),
                "entity_kind": str(row.get("entity_kind") or ""),
                "title": str(row.get("title") or ""),
                "snippet": str(row.get("snippet") or ""),
                "metadata": dict(row.get("metadata_jsonb") or {}),
                "text_relevance": max(float(row.get("text_relevance") or 0.0), 0.0),
                "vector_relevance": max(float(row.get("vector_relevance") or 0.0), 0.0),
            }

        for row in text_rows:
            payload = _payload(row)
            merged[payload["entity_id"]] = payload
        for row in vector_rows:
            payload = _payload(row)
            existing = merged.get(payload["entity_id"])
            if existing is None:
                merged[payload["entity_id"]] = payload
                continue
            existing["text_relevance"] = max(
                existing["text_relevance"],
                payload["text_relevance"],
            )
            existing["vector_relevance"] = max(
                existing["vector_relevance"],
                payload["vector_relevance"],
            )

        results: list[WorkflowKnowledgeHit] = []
        for payload in merged.values():
            entity_kind = str(payload.get("entity_kind") or "")
            base_score = float(payload.pop("text_relevance") or 0.0) + (
                float(payload.pop("vector_relevance") or 0.0) * 0.85
            )
            score = base_score * float(_ENTITY_KIND_SCORE_MULTIPLIERS.get(entity_kind, 1.0))
            results.append(
                WorkflowKnowledgeHit(
                    entity_id=str(payload.get("entity_id") or ""),
                    entity_kind=entity_kind,
                    score=round(max(score, 0.0), 3),
                    title=str(payload.get("title") or ""),
                    snippet=str(payload.get("snippet") or ""),
                    metadata=dict(payload.get("metadata") or {}),
                )
            )
        results.sort(
            key=lambda item: (
                -float(item.score or 0.0),
                item.entity_kind,
                item.entity_id,
            )
        )
        return results[: max(1, int(limit or 1))]

    def neighbors(
        self,
        *,
        entity_id: str,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        edge_kinds: Sequence[str] = (),
        depth: int = 1,
        limit: int = 20,
    ) -> list[WorkflowKnowledgeNeighbor]:
        normalized_entity_id = str(entity_id or "").strip()
        normalized_account_id = str(account_id or "").strip()
        if not normalized_entity_id or not normalized_account_id:
            return []
        normalized_tenant_id = _tenant_value(tenant_id)
        project_scope = _scope_value(project_id)
        pairs = self._age_reachable_ids(
            entity_ids=[normalized_entity_id],
            account_id=normalized_account_id,
            tenant_id=normalized_tenant_id,
            project_scope=project_scope,
            edge_kinds=edge_kinds,
            depth=max(1, int(depth or 1)),
            limit=max(1, int(limit or 1)) + 1,
        )
        if not pairs:
            return []
        direct_edge_map: dict[str, str] = {}
        with (
            self._connect() as conn,
            conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur,
        ):
            cur.execute(
                """
                SELECT source_id, target_id, edge_kind
                  FROM public.platform_workflow_knowledge_edges
                 WHERE account_id = %s
                   AND tenant_id = %s
                   AND project_scope = %s
                   AND (
                        (source_id = %s AND target_id = ANY(%s))
                     OR (target_id = %s AND source_id = ANY(%s))
                   )
                """,
                (
                    normalized_account_id,
                    normalized_tenant_id,
                    project_scope,
                    normalized_entity_id,
                    [entity_id for entity_id, _depth in pairs],
                    normalized_entity_id,
                    [entity_id for entity_id, _depth in pairs],
                ),
            )
            for row in cur.fetchall():
                row_dict = dict(row)
                left = str(row_dict.get("source_id") or "").strip()
                right = str(row_dict.get("target_id") or "").strip()
                kind = str(row_dict.get("edge_kind") or "related_to").strip() or "related_to"
                if left == normalized_entity_id:
                    direct_edge_map[right] = kind
                elif right == normalized_entity_id:
                    direct_edge_map[left] = kind
        neighbors: list[WorkflowKnowledgeNeighbor] = []
        for target_id, distance in pairs:
            if target_id == normalized_entity_id:
                continue
            neighbors.append(
                WorkflowKnowledgeNeighbor(
                    source_id=normalized_entity_id,
                    edge_kind=direct_edge_map.get(target_id, "related_to"),
                    target_id=target_id,
                    depth=max(1, int(distance or 1)),
                )
            )
        neighbors.sort(key=lambda item: (item.depth, item.target_id))
        return neighbors[: max(1, int(limit or 1))]

    def fetch_subgraph(
        self,
        *,
        entity_ids: Sequence[str],
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        edge_kinds: Sequence[str] = (),
        depth: int = 2,
        limit: int = 100,
    ) -> WorkflowKnowledgeProjection:
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            return WorkflowKnowledgeProjection()
        normalized_tenant_id = _tenant_value(tenant_id)
        project_scope = _scope_value(project_id)
        reachable_pairs = self._age_reachable_ids(
            entity_ids=entity_ids,
            account_id=normalized_account_id,
            tenant_id=normalized_tenant_id,
            project_scope=project_scope,
            edge_kinds=edge_kinds,
            depth=max(1, int(depth or 1)),
            limit=max(1, int(limit or 1)),
        )
        reachable_ids = [entity_id for entity_id, _distance in reachable_pairs if entity_id]
        if not reachable_ids:
            return WorkflowKnowledgeProjection()
        edge_kind_filter = [
            str(kind or "").strip() for kind in edge_kinds if str(kind or "").strip()
        ]
        with (
            self._connect() as conn,
            conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur,
        ):
            cur.execute(
                """
                SELECT entity_id, entity_kind, label, summary, content_text, metadata_jsonb
                  FROM public.platform_workflow_knowledge_nodes
                 WHERE account_id = %s
                   AND tenant_id = %s
                   AND project_scope = %s
                   AND entity_id = ANY(%s)
                """,
                (normalized_account_id, normalized_tenant_id, project_scope, reachable_ids),
            )
            nodes = [
                WorkflowKnowledgeNode(
                    id=str(row.get("entity_id") or ""),
                    kind=str(row.get("entity_kind") or ""),
                    label=str(row.get("label") or ""),
                    summary=str(row.get("summary") or ""),
                    text=str(row.get("content_text") or ""),
                    metadata=dict(row.get("metadata_jsonb") or {}),
                )
                for row in cur.fetchall()
            ]
            edge_sql = """
                SELECT edge_id, edge_kind, source_id, target_id, metadata_jsonb
                  FROM public.platform_workflow_knowledge_edges
                 WHERE account_id = %s
                   AND tenant_id = %s
                   AND project_scope = %s
                   AND source_id = ANY(%s)
                   AND target_id = ANY(%s)
            """
            edge_params: list[Any] = [
                normalized_account_id,
                normalized_tenant_id,
                project_scope,
                reachable_ids,
                reachable_ids,
            ]
            if edge_kind_filter:
                edge_sql += " AND edge_kind = ANY(%s)"
                edge_params.append(edge_kind_filter)
            cur.execute(edge_sql, edge_params)
            edges = [
                WorkflowKnowledgeEdge(
                    id=str(row.get("edge_id") or ""),
                    kind=str(row.get("edge_kind") or ""),
                    source_id=str(row.get("source_id") or ""),
                    target_id=str(row.get("target_id") or ""),
                    metadata=dict(row.get("metadata_jsonb") or {}),
                )
                for row in cur.fetchall()
            ]
            cur.execute(
                """
                SELECT doc_id, entity_id, entity_kind, title, content_text, metadata_jsonb
                  FROM public.platform_workflow_knowledge_documents
                 WHERE account_id = %s
                   AND tenant_id = %s
                   AND project_scope = %s
                   AND entity_id = ANY(%s)
                """,
                (normalized_account_id, normalized_tenant_id, project_scope, reachable_ids),
            )
            documents = [
                WorkflowKnowledgeDocument(
                    id=str(row.get("doc_id") or ""),
                    entity_id=str(row.get("entity_id") or ""),
                    entity_kind=str(row.get("entity_kind") or ""),
                    title=str(row.get("title") or ""),
                    text=str(row.get("content_text") or ""),
                    metadata=dict(row.get("metadata_jsonb") or {}),
                )
                for row in cur.fetchall()
            ]
        return WorkflowKnowledgeProjection(nodes=nodes, edges=edges, documents=documents)
